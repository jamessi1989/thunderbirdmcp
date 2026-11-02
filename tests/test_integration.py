"""Client installation tests use isolated homes and never inspect personal config."""

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import tomlkit

from thunderbird_mcp import setup


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.project = self.root / "project with spaces"
        package = self.project / "thunderbird_mcp"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("")
        (package / "server.py").write_text("pass\n")
        (package / "runtime.py").write_text("def configure_session_environment(): pass\n")
        (package / "cli.py").write_text("import os,sys,json\nprint(json.dumps({'cwd':os.getcwd(),'args':sys.argv[1:]}))\n")

    def write(self, name, text):
        path = self.home / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def plan(self, clients=None):
        return setup.build_plan(home=self.home, project=self.project, python=Path(sys.executable), clients=clients or ["claude", "codex", "opencode", "agy"])

    def test_install_preserves_unrelated_config_private_backups_and_is_idempotent(self):
        claude = self.write(".claude.json", '{"userID":"private-user", "mcpServers":{"other":{"command":"keep-me"}, "thunderbird":{"command":"old", "env":{"CUSTOM":"preserve"}}}}\n')
        codex = self.write(".codex/config.toml", '# keep this comment\nmodel = "existing"\n\n[mcp_servers.other]\ncommand = "keep-me"\n')
        opencode = self.write(".config/opencode/opencode.json", '{"provider":{"example":{"options":{"apiKey":"private-key"}}}}\n')
        old = {p: p.read_bytes() for p in [claude, codex, opencode]}
        plan = self.plan()
        self.assertNotIn("private-key", json.dumps(plan["report"]))
        self.assertFalse((self.home / ".local/bin/tbmail").exists())
        report = setup.apply_plan(plan)
        self.assertEqual(len(report["backups"]), 3)
        for backup in map(Path, report["backups"]):
            source = Path(str(backup).split(".thunderbird-backup-")[0])
            self.assertEqual(backup.read_bytes(), old[source])
            self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        data = json.loads(claude.read_text())
        self.assertEqual(data["userID"], "private-user")
        self.assertEqual(data["mcpServers"]["other"]["command"], "keep-me")
        self.assertEqual(data["mcpServers"]["thunderbird"]["env"], {"CUSTOM": "preserve"})
        self.assertIn("# keep this comment", codex.read_text())
        self.assertEqual(tomlkit.parse(codex.read_text())["model"], "existing")
        self.assertEqual(json.loads(opencode.read_text())["provider"]["example"]["options"]["apiKey"], "private-key")
        self.assertEqual(self.plan()["report"]["change_count"], 0)
        self.assertEqual(setup.apply_plan(self.plan())["backups"], [])

    def test_jsonc_comments_and_trailing_commas_survive(self):
        text = '''{
  // keep provider comment
  "provider": {"url": "https://example.test/*literal*/",},
  "mcp": {
    "other": {"command": ["other"],}, // keep MCP comment
    "thunderbird": {"command": ["old"], "enabled": false,},
  },
}
'''
        path = self.write(".config/opencode/opencode.jsonc", text)
        setup.apply_plan(self.plan(["opencode"]))
        after = path.read_text()
        self.assertIn('// keep provider comment', after)
        self.assertIn('"other": {"command": ["other"],}, // keep MCP comment', after)
        self.assertIn('"provider": {"url": "https://example.test/*literal*/",},', after)
        self.assertTrue(setup._json_load(after)["mcp"]["thunderbird"]["enabled"])
        self.assertEqual(self.plan(["opencode"])["report"]["change_count"], 0)

    def test_codex_existing_table_merged_without_destroying_other_tables(self):
        raw = '''# prefix
[mcp_servers.thunderbird]
command = "old" # command comment
env_vars = ["CUSTOM"]
disabled_tools = ["move_emails"]
[mcp_servers.thunderbird.env]
CUSTOM = "value"
[mcp_servers.other]
command = "keep"
'''
        updated, _ = setup._client_update("codex", raw, "/new/bin")
        parsed = tomlkit.parse(updated)
        self.assertEqual(parsed["mcp_servers"]["thunderbird"]["env_vars"], ["CUSTOM"])
        self.assertEqual(parsed["mcp_servers"]["thunderbird"]["disabled_tools"], ["move_emails"])
        self.assertEqual(parsed["mcp_servers"]["thunderbird"]["env"]["CUSTOM"], "value")
        self.assertEqual(parsed["mcp_servers"]["other"]["command"], "keep")
        self.assertIn("# command comment", updated)

    def test_stale_config_refuses_before_any_changes(self):
        path = self.write(".claude.json", '{}\n')
        plan = self.plan(["claude"])
        path.write_text('{"changed":true}\n')
        with self.assertRaisesRegex(RuntimeError, "changed since planning"):
            setup.apply_plan(plan)
        self.assertFalse((self.home / ".local/bin/tbmail").exists())
        self.assertEqual(json.loads(path.read_text()), {"changed": True})

    def test_malformed_config_is_never_replaced(self):
        path = self.write(".claude.json", '{invalid')
        with self.assertRaises(ValueError):
            self.plan(["claude"])
        self.assertEqual(path.read_text(), '{invalid')

    def test_symlink_is_not_replaced(self):
        target = self.write("other.json", '{}')
        (self.home / ".claude.json").symlink_to(target)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.plan(["claude"])

    def test_launcher_keeps_callers_directory_and_literal_arguments(self):
        setup.apply_plan(self.plan(["claude"]))
        working = self.root / "another project"
        working.mkdir()
        run = subprocess.run([str(self.home / ".local/bin/tbmail"), "--body-file", "text with spaces.txt", "$(literal)`argument`"], cwd=working, capture_output=True, text=True, check=True)
        data = json.loads(run.stdout)
        self.assertEqual(data["cwd"], str(working.resolve()))
        self.assertEqual(data["args"], ["--body-file", "text with spaces.txt", "$(literal)`argument`"])

    def test_project_overrides_are_reported_without_changes(self):
        raw = json.dumps({"projects": {"/example": {"mcpServers": {"thunderbird": {"command": "keep"}}, "disabledMcpServers": ["thunderbird"]}}})
        updated, notes = setup._client_update("claude", raw, "/new/bin")
        self.assertEqual(json.loads(updated)["projects"], json.loads(raw)["projects"])
        self.assertEqual(len(notes), 2)

    def test_auto_detection_only_selects_installed_clients(self):
        with patch("thunderbird_mcp.setup.shutil.which", side_effect=lambda c: "/bin/" + c if c in ["claude", "agy"] else None):
            plan = setup.build_plan(home=self.home, project=self.project, python=Path(sys.executable))
        self.assertEqual([x["name"] for x in plan["report"]["clients"]], ["claude", "agy"])

    def test_installed_package_uses_running_interpreter_without_checkout_venv(self):
        with patch.object(setup, "__file__", str(self.project / "thunderbird_mcp/setup.py")):
            plan = setup.build_plan(home=self.home, clients=["claude"])
        self.assertEqual(plan["report"]["change_count"], 5)
        launcher = next(change for change in plan["changes"] if change["path"].endswith("/tbmail"))
        self.assertIn(str(Path(sys.executable).absolute()).encode(), launcher["after"])
        self.assertNotIn(str(self.project / ".venv/bin/python").encode(), launcher["after"])

    def test_explicit_home_ignores_external_client_directory_overrides(self):
        outside = self.root / "unselected config"
        with patch.dict(os.environ, {"CODEX_HOME": str(outside), "CLAUDE_CONFIG_DIR": str(outside), "XDG_CONFIG_HOME": str(outside)}):
            plan = self.plan(["claude", "codex", "opencode"])
            setup.apply_plan(plan)
        self.assertFalse(outside.exists())
        self.assertTrue(all(Path(change["path"]).is_relative_to(self.home) for change in plan["changes"]))

    def test_uv_tool_entrypoint_symlinks_are_preserved(self):
        environment = self.root / "tool environment/bin"
        environment.mkdir(parents=True)
        python = environment / "python"
        python.symlink_to(sys.executable)
        commands = {"thunderbird-mcp": "server", "tbmail": "cli", "thunderbird-mail": "cli", "thunderbird-mcp-setup": "setup"}
        for name, module in commands.items():
            target = environment / name
            target.write_text(f"#!/usr/bin/env python3\nfrom thunderbird_mcp.{module} import main\n", encoding="utf-8")
            target.chmod(0o755)
            link = self.home / ".local/bin" / name
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(target)
        plan = setup.build_plan(home=self.home, project=self.project, python=python, clients=["claude"])
        self.assertEqual(plan["report"]["change_count"], 1)
        setup.apply_plan(plan)
        self.assertTrue(all((self.home / ".local/bin" / name).is_symlink() for name in commands))
        self.assertEqual(sum(bool(item.get("preserved_entrypoint")) for item in plan["report"]["files"]), 4)

    def test_unrelated_command_symlinks_remain_rejected(self):
        target = self.write("unrelated-command", "#!/bin/sh\nexit 0\n")
        target.chmod(0o755)
        link = self.home / ".local/bin/tbmail"
        link.parent.mkdir(parents=True)
        link.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.plan(["claude"])
        self.assertEqual(link.resolve(), target)


if __name__ == "__main__":
    unittest.main()
