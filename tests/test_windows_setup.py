"""Portable config checks plus native Windows launcher execution in Windows CI."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import tomlkit

from thunderbird_mcp import setup


class WindowsSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home 100% ready!"
        self.project = self.root / "source with spaces"
        package = self.project / "thunderbird_mcp"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("")
        (package / "runtime.py").write_text("def configure_session_environment(): pass\n")
        program = "import json,os,sys\nprint(json.dumps({'cwd':os.getcwd(),'args':sys.argv[1:]}))\n"
        (package / "server.py").write_text(program)
        (package / "cli.py").write_text(program)
        self.platform = patch.object(setup.sys, "platform", "win32")
        self.platform.start()
        self.addCleanup(self.platform.stop)
        # Python 3.12's shutil.which uses _winapi when sys.platform is mocked.
        # Installed-client discovery is unrelated to these explicit selections.
        discovery = patch.object(setup.shutil, "which", return_value=None)
        discovery.start()
        self.addCleanup(discovery.stop)

    def plan(self):
        return setup.build_plan(home=self.home, project=self.project, python=Path(sys.executable),
                                clients=["codex", "claude", "opencode"])

    def test_clients_launch_python_directly_and_preserve_paths_and_arguments(self):
        plan = self.plan()
        self.assertEqual(plan["report"]["change_count"], 15)
        report = setup.apply_plan(plan)
        bin_dir = self.home / "AppData/Local/thunderbird-mcp/bin"
        self.assertTrue((bin_dir / "tbmail.cmd").is_file())
        batch = (bin_dir / "tbmail.cmd").read_text()
        self.assertIn("100%% ready!", batch)
        self.assertIn("setlocal DisableDelayedExpansion", batch)
        configs = [
            tomlkit.parse((self.home / ".codex/config.toml").read_text())["mcp_servers"]["thunderbird"],
            json.loads((self.home / ".claude.json").read_text())["mcpServers"]["thunderbird"],
        ]
        working = self.root / "caller directory"
        working.mkdir()
        args = ["--body-file", "relative body.txt", "literal!argument", "$(literal)`argument`"]
        for entry in configs:
            self.assertEqual(entry["command"], str(Path(sys.executable).absolute()))
            self.assertEqual(entry["args"], [str(bin_dir / "thunderbird-mcp.py")])
            run = subprocess.run([entry["command"], *entry["args"], *args], check=True,
                                 cwd=working, capture_output=True, text=True)
            result = json.loads(run.stdout)
            self.assertEqual(Path(result["cwd"]), working)
            self.assertEqual(result["args"], args)
        opencode = json.loads((self.home / ".config/opencode/opencode.json").read_text())["mcp"]["thunderbird"]
        self.assertEqual(opencode["command"], [str(Path(sys.executable).absolute()), str(bin_dir / "thunderbird-mcp.py")])
        self.assertTrue(report["applied"])
        self.assertEqual(self.plan()["report"]["change_count"], 0)

    def test_explicit_home_does_not_use_external_local_appdata(self):
        outside = self.root / "unselected data"
        with patch.dict(os.environ, {"LOCALAPPDATA": str(outside)}):
            plan = self.plan()
            self.assertTrue(all(Path(change["path"]).is_relative_to(self.home) for change in plan["changes"]))
            self.assertEqual(setup._bin_dir(self.home, True), outside / "thunderbird-mcp/bin")

    def test_windows_checkout_uses_scripts_interpreter(self):
        python = self.project / ".venv/Scripts/python.exe"
        python.parent.mkdir(parents=True)
        python.write_bytes(b"synthetic executable")
        python.chmod(0o755)
        plan = setup.build_plan(home=self.home, project=self.project, clients=["claude"])
        config = next(change for change in plan["changes"] if change["path"].endswith(".claude.json"))
        self.assertEqual(json.loads(config["after"])["mcpServers"]["thunderbird"]["command"], str(python))

    @unittest.skipUnless(os.name == "nt", "Requires native cmd.exe; executed by the Windows CI job")
    def test_cmd_launcher_preserves_cwd_and_relative_body_path(self):
        setup.apply_plan(self.plan())
        launcher = self.home / "AppData/Local/thunderbird-mcp/bin/tbmail.cmd"
        working = self.root / "caller directory"
        working.mkdir()
        # Pass the complete Windows command line: list2cmdline implements CRT
        # quoting, which is different from cmd.exe's /s /c outer-quote grammar.
        command = f'cmd.exe /d /s /c ""{launcher}" --body-file "relative body.txt" "literal!argument""'
        run = subprocess.run(command, cwd=working, capture_output=True, text=True, check=True)
        result = json.loads(run.stdout)
        self.assertEqual(Path(result["cwd"]), working)
        self.assertEqual(result["args"], ["--body-file", "relative body.txt", "literal!argument"])


if __name__ == "__main__":
    unittest.main()
