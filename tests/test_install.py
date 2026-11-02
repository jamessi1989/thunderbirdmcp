"""Focused checks for wheel data paths and the isolated smoke-test harness."""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from thunderbird_mcp import index

_script = Path(__file__).resolve().parents[1] / "scripts/test_install.py"
_spec = importlib.util.spec_from_file_location("install_smoke", _script)
install_smoke = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(install_smoke)


class InstalledDataTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.package = self.root / "environment/lib/site-packages/thunderbird_mcp"
        self.package.mkdir(parents=True)
        for context in (patch.dict(os.environ, {}, clear=True), patch.object(Path, "home", return_value=self.home),
                        patch.object(index, "__file__", str(self.package / "index.py"))):
            context.start()
            self.addCleanup(context.stop)

    def test_linux_wheel_uses_user_data_and_honors_absolute_xdg_override(self):
        with patch.object(index.sys, "platform", "linux"):
            self.assertEqual(index.default_db_path(), self.home / ".local/share/thunderbird-mcp/mail-index.sqlite")
            with patch.dict(os.environ, {"XDG_DATA_HOME": str(self.home / "custom data")}):
                self.assertEqual(index.default_db_path(), self.home / "custom data/thunderbird-mcp/mail-index.sqlite")
            with patch.dict(os.environ, {"XDG_DATA_HOME": "relative-data"}):
                self.assertEqual(index.default_db_path(), self.home / ".local/share/thunderbird-mcp/mail-index.sqlite")

    def test_macos_wheel_uses_application_support(self):
        with patch.object(index.sys, "platform", "darwin"):
            self.assertEqual(index.default_db_path(), self.home / "Library/Application Support/thunderbird-mcp/mail-index.sqlite")

    def test_source_checkout_and_explicit_database_paths_remain_unchanged(self):
        (self.package.parent / "pyproject.toml").write_text('[project]\nname="thunderbird-mcp"\n', encoding="utf-8")
        self.assertEqual(index.default_db_path(), self.package.parent / "mail-index.sqlite")
        with patch.dict(os.environ, {"TB_MAIL_INDEX_DB": str(self.home / "explicit.sqlite")}):
            self.assertEqual(index.default_db_path(), self.home / "explicit.sqlite")

    def test_isolated_environment_does_not_copy_credentials_or_personal_paths(self):
        with patch.dict(os.environ, {"TB_IMAP_PASSWORD": "do-not-copy", "CODEX_HOME": "/private/config",
                                     "ANTHROPIC_API_KEY": "do-not-copy", "PYTHONPATH": "/private/source"}):
            env = install_smoke.isolated_environment(self.root / "smoke")
        self.assertNotIn("TB_IMAP_PASSWORD", env)
        self.assertNotIn("CODEX_HOME", env)
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("PYTHONPATH", env)
        self.assertNotIn("do-not-copy", json.dumps(env))
        self.assertTrue(Path(env["HOME"]).is_relative_to(self.root))
        self.assertEqual(env["PYTHON_KEYRING_BACKEND"], "keyring.backends.null.Keyring")

    def test_smoke_does_not_claim_windows_support(self):
        output = io.StringIO()
        with patch.object(install_smoke.sys, "platform", "win32"), contextlib.redirect_stdout(output):
            result = install_smoke.main(["--json"])
        self.assertEqual(result, 1)
        self.assertIn("Windows is not supported", json.loads(output.getvalue())["error"])

    def test_smoke_guard_blocks_dns_tcp_udp_and_allows_local_async_sockets(self):
        guard = self.root / "guard"
        guard.mkdir()
        (guard / "sitecustomize.py").write_text(install_smoke.GUARD, encoding="utf-8")
        env = install_smoke.isolated_environment(self.root / "smoke")
        env["PYTHONPATH"] = str(guard)
        program = """import socket
for action in (lambda: socket.getaddrinfo('example.test', 443),
               lambda: socket.socket(socket.AF_INET, socket.SOCK_STREAM),
               lambda: socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)):
    try:
        action()
    except RuntimeError as error:
        assert 'Network disabled' in str(error)
    else:
        raise AssertionError('Network guard did not block an operation')
first, second = socket.socketpair()
first.close()
second.close()
print('guarded')
"""
        result = subprocess.run([sys.executable, "-c", program], env=env, cwd=self.root, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "guarded")


if __name__ == "__main__":
    unittest.main()
