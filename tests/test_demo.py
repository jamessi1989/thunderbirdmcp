"""The public demonstration must exercise real CLI behavior without user data."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

spec = importlib.util.spec_from_file_location("mail_demo", Path(__file__).resolve().parents[1] / "scripts" / "demo.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)


class DemoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        poisoned = {"TB_PROFILE": "/must-not-read-personal-profile", "TB_MAIL_INDEX_DB": "/must-not-read-personal.sqlite",
                    "TB_OUTBOX_DB": "/must-not-write-personal.sqlite", "TB_ACCOUNT": "personal_account",
                    "TB_SMTP_PASSWORD": "private-value-must-not-leak", "TB_FROM": "private@example.test"}
        with patch.dict(os.environ, poisoned), patch.object(demo, "tbmail", wraps=demo.tbmail) as calls:
            cls.result = demo.run()
            cls.calls = calls.call_args_list

    def test_actual_cli_search_reference_thread_and_unsent_reply(self):
        result = self.result
        self.assertTrue(result["verified"])
        self.assertEqual(result["search"], {"count": 1, "match_source": "attachment"})
        self.assertNotIn("INV-2049", result["message"]["body"] + result["message"]["subject"])
        self.assertIn("INV-2049", result["attachment"]["text"])
        self.assertEqual(result["thread"], {"count": 2, "match_method": "message_headers"})
        self.assertEqual(result["draft"]["status"], "prepared")
        self.assertTrue(result["draft"]["preview"])
        self.assertEqual(result["draft"]["to"], ["billing@example.test"])
        self.assertEqual(result["draft"]["in_reply_to"], demo.INVOICE_ID)
        self.assertEqual(len(self.calls), result["cli_processes"])
        self.assertEqual([call.args[2] for call in self.calls],
                         ["reindex", "search", "read", "thread", "attachment-text", "reply", "send"])
        self.assertEqual(self.calls[2].args[3], result["reference"])
        self.assertEqual(self.calls[3].args[3], result["reference"])
        self.assertNotIn("--confirm", self.calls[-1].args)

    def test_user_environment_is_ignored_and_temporary_mail_removed(self):
        for call in self.calls:
            root, env = call.args[:2]
            self.assertFalse(root.exists())
            self.assertEqual(env["HOME"], str(root / "home"))
            self.assertEqual(env["TB_PROFILE"], str(root / "profile"))
            self.assertEqual(env["TB_OUTBOX_DB"], str(root / "outbox.sqlite"))
            self.assertEqual(env["PYTHON_KEYRING_BACKEND"], "keyring.backends.fail.Keyring")
            self.assertEqual(env["HF_HUB_OFFLINE"], "1")
            self.assertNotIn("TB_SMTP_PASSWORD", env)
            self.assertNotIn("TB_ACCOUNT", env)
        encoded = json.dumps(self.result)
        self.assertNotIn("private", encoded)
        self.assertNotIn("/must-not", encoded)

    def test_guard_blocks_network_and_disables_system_keyring(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            env = demo.environment(root)
            code = '''import socket, keyring
try:
    socket.create_connection(("127.0.0.1", 9))
except RuntimeError as error:
    assert "Network access is disabled" in str(error)
else:
    raise AssertionError("Network guard was not loaded")
assert type(keyring.get_keyring()).__module__ == "keyring.backends.fail"
'''
            check = subprocess.run([sys.executable, "-c", code], cwd=root, env=env,
                                   capture_output=True, text=True, timeout=15)
            self.assertEqual(check.returncode, 0, check.stderr)

    def test_send_confirmation_and_live_commands_are_rejected(self):
        with patch.object(demo.subprocess, "run") as process:
            for args in (("send", "draft", "--confirm"), ("search", "--live"), ("reindex", "--embeddings")):
                with self.subTest(args=args), self.assertRaises(ValueError):
                    demo.tbmail(Path("unused"), {}, *args)
            process.assert_not_called()

    def test_svg_matches_actual_verified_output_and_escapes_markup(self):
        expected = demo.svg(demo.transcript(self.result))
        path = demo.PROJECT / "docs" / "demo.svg"
        self.assertEqual(path.read_text(encoding="utf-8"), expected)
        ET.fromstring(expected)
        escaped = demo.svg(['<script>bad()</script>', 'control\x1btext'])
        self.assertIn("&lt;script&gt;", escaped)
        self.assertNotIn("<script>", escaped)
        self.assertNotIn("\x1b", escaped)


if __name__ == "__main__":
    unittest.main()
