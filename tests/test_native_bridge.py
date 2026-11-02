"""Native companion setup never touches real Thunderbird or personal configs."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import tomlkit

from thunderbird_mcp import setup

_spec = importlib.util.spec_from_file_location("native_setup", Path(__file__).resolve().parents[1] / "scripts/setup_native_bridge.py")
native = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(native)


@unittest.skipUnless(shutil.which("git"), "Native integration verification requires Git")
class NativeBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "client home"
        self.profile = self.root / "synthetic Thunderbird profile"
        self.profile.mkdir()
        (self.profile / "prefs.js").write_text('// synthetic only\n')
        self.checkout = self.root / "native checkout"
        self.checkout.mkdir()
        source = self.checkout / "src/tbmcp"
        source.mkdir(parents=True)
        (source / "__init__.py").write_text('"""Synthetic package."""\n')
        (source / "cli.py").write_text("def main(): return 0\n")
        (self.checkout / ".gitignore").write_text(".venv/\n")
        self.git("init", "-q")
        self.git("add", ".")
        self.git("-c", "user.name=Synthetic Test", "-c", "user.email=test@example.test",
                 "-c", "commit.gpgsign=false", "commit", "-qm", "Synthetic native fixture")
        commit = self.git("rev-parse", "HEAD")
        self.prefix = self.checkout / ".venv"
        self.python = self.prefix / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        self.python.parent.mkdir(parents=True)
        self.python.write_text("synthetic executable")
        self.python.chmod(0o755)
        self.package = self.prefix / "lib/tbmcp"
        shutil.copytree(source, self.package)
        runtime = {"prefix": str(self.prefix), "base_prefix": str(self.root / "base"),
                   "python": [3, 11], "version": native.UPSTREAM_VERSION,
                   "module": str(self.package / "__init__.py")}
        self.runtime = runtime
        patcher = patch.object(native, "UPSTREAM_COMMIT", commit)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(native, "_installed_runtime", return_value=runtime)
        self.runtime_mock = patcher.start()
        self.addCleanup(patcher.stop)

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.checkout), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def write(self, relative, value):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
        return path

    def plan(self, **kwargs):
        return native.build_plan(checkout=self.checkout, profile=self.profile, home=self.home,
                                 clients=list(native.CLIENTS), **kwargs)

    def test_separate_entries_preserve_mail_mcp_secrets_comments_and_profile(self):
        claude = self.write(".claude.json", '{"userID":"private-user","mcpServers":{"thunderbird":{"command":"mail-server"}}}\n')
        codex = self.write(".codex/config.toml", '# keep comment\nmodel="kept"\n[mcp_servers.thunderbird]\ncommand="mail-server"\n')
        opencode = self.write(".config/opencode/opencode.jsonc", '{\n// keep comment\n"provider":{"key":"private-key"},"mcp":{"thunderbird":{"type":"local","command":["mail-server"]},},\n}\n')
        originals = {p: p.read_bytes() for p in (claude, codex, opencode)}
        profile_before = (self.profile / "prefs.js").read_bytes()
        plan = self.plan()
        public = json.dumps(plan["report"])
        self.assertNotIn("private-key", public)
        self.assertNotIn("private-user", public)
        self.assertFalse(plan["report"]["native_runtime_verified"])
        self.assertEqual({p: p.read_bytes() for p in originals}, originals)
        report = setup.apply_plan(plan)
        self.assertEqual(len(report["backups"]), 3)
        claude_doc = json.loads(claude.read_text())
        codex_doc = tomlkit.parse(codex.read_text())
        opencode_doc = setup._json_load(opencode.read_text())
        self.assertEqual(claude_doc["mcpServers"]["thunderbird"]["command"], "mail-server")
        self.assertEqual(codex_doc["mcp_servers"]["thunderbird"]["command"], "mail-server")
        self.assertEqual(opencode_doc["mcp"]["thunderbird"]["command"], ["mail-server"])
        self.assertIn("# keep comment", codex.read_text())
        self.assertIn("// keep comment", opencode.read_text())
        self.assertEqual(opencode_doc["provider"]["key"], "private-key")
        for entry in (claude_doc["mcpServers"][native.SERVER_NAME], codex_doc["mcp_servers"][native.SERVER_NAME]):
            self.assertEqual(entry["command"], str(self.python))
            self.assertIn("--read-only", entry["args"])
            self.assertEqual(entry["env"]["TBMCP_YOLO"], "0")
            self.assertEqual(entry["env"]["TBMCP_TOOLS"], "")
            self.assertIn(str(self.profile), entry["args"])
        self.assertIn("--read-only", opencode_doc["mcp"][native.SERVER_NAME]["command"])
        self.assertEqual(opencode_doc["mcp"][native.SERVER_NAME]["environment"]["TBMCP_READ_ONLY"], "1")
        self.assertEqual((self.profile / "prefs.js").read_bytes(), profile_before)
        self.assertEqual(list(self.profile.iterdir()), [self.profile / "prefs.js"])
        for backup in map(Path, report["backups"]):
            original = Path(str(backup).split(".thunderbird-backup-")[0])
            self.assertEqual(backup.read_bytes(), originals[original])
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        self.assertEqual(self.plan()["report"]["change_count"], 0)

    def test_allow_writes_retains_confirmation_and_excludes_mail_tools(self):
        plan = self.plan(allow_writes=True)
        for change in plan["changes"]:
            raw = change["after"].decode()
            self.assertNotIn("--read-only", raw)
            self.assertIn(native.TOOLSETS, raw)
            self.assertNotIn("--yolo", raw)
        self.assertFalse(plan["report"]["read_only"])
        self.assertNotIn("mail", plan["report"]["toolsets"])

    def test_dirty_or_wrong_checkout_and_changed_installed_code_are_rejected(self):
        self.assertEqual(native.verify_upstream(self.checkout), self.python)
        with patch.object(native, "UPSTREAM_COMMIT", "0" * 40), self.assertRaisesRegex(ValueError, "pinned"):
            native.verify_upstream(self.checkout)
        source = self.checkout / "src/tbmcp/cli.py"
        original = source.read_bytes()
        source.write_text("changed\n")
        with self.assertRaisesRegex(ValueError, "local changes"):
            native.verify_upstream(self.checkout)
        source.write_bytes(original)
        (self.package / "cli.py").write_text("changed installed code\n")
        with self.assertRaisesRegex(ValueError, "differs"):
            native.verify_upstream(self.checkout)

    def test_shared_environment_and_editable_package_are_rejected(self):
        self.runtime_mock.return_value = {**self.runtime, "prefix": sys.prefix}
        with self.assertRaisesRegex(ValueError, "own virtual environment"):
            self.plan()
        self.runtime_mock.return_value = {**self.runtime, "module": str(self.checkout / "src/tbmcp/__init__.py")}
        with self.assertRaisesRegex(ValueError, "editable or external"):
            self.plan()

    def test_apply_refuses_stale_config(self):
        path = self.write(".claude.json", "{}\n")
        plan = self.plan()
        path.write_text('{"concurrent":"edit"}\n')
        with self.assertRaisesRegex(RuntimeError, "changed since planning"):
            setup.apply_plan(plan)
        self.assertFalse((self.home / ".codex/config.toml").exists())

    @unittest.skipUnless(os.name == "posix", "Symlink permission semantics differ on Windows")
    def test_symlinked_client_file_is_not_replaced(self):
        self.home.mkdir()
        target = self.root / "unrelated.json"
        target.write_text("{}\n")
        (self.home / ".claude.json").symlink_to(target)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.plan()
        self.assertEqual(target.read_text(), "{}\n")

    def test_profile_and_client_validation_precede_config_changes(self):
        (self.profile / "prefs.js").unlink()
        with self.assertRaisesRegex(ValueError, "prefs.js"):
            self.plan()
        self.assertFalse(self.home.exists())


if __name__ == "__main__":
    unittest.main()
