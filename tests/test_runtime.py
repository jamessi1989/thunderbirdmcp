from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from thunderbird_mcp import runtime


class SessionEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def bus(self):
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.bind(str(self.root / "bus"))
        self.addCleanup(connection.close)
        return self.root / "bus"

    def test_inherited_bus_and_runtime_are_unchanged(self):
        env = {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/existing/session", "XDG_RUNTIME_DIR": "/existing/runtime"}
        with patch.object(runtime, "_session_bus") as probe:
            self.assertFalse(runtime.configure_session_environment(env))
        probe.assert_not_called()
        self.assertEqual(env, {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/existing/session", "XDG_RUNTIME_DIR": "/existing/runtime"})

    def test_uses_owned_socket_in_existing_runtime(self):
        self.bus()
        env = {"XDG_RUNTIME_DIR": str(self.root)}
        self.assertTrue(runtime.configure_session_environment(env))
        self.assertEqual(env["DBUS_SESSION_BUS_ADDRESS"], "unix:path=" + str(self.root / "bus"))
        self.assertEqual(env["XDG_RUNTIME_DIR"], str(self.root))

    def test_fills_missing_runtime_with_current_uid_fallback(self):
        fallback = Path("/run/user") / str(os.getuid())
        with patch.object(runtime, "_session_bus", return_value=fallback / "bus") as probe:
            env = {"HOME": "/preserved"}
            self.assertTrue(runtime.configure_session_environment(env))
        probe.assert_called_once_with(fallback, os.getuid())
        self.assertEqual(env["XDG_RUNTIME_DIR"], str(fallback))
        self.assertEqual(env["HOME"], "/preserved")

    def test_invalid_runtime_can_fall_back_without_overriding_inherited_runtime(self):
        fallback = Path("/run/user") / str(os.getuid())
        with patch.object(runtime, "_session_bus", side_effect=[None, fallback / "bus"]):
            env = {"XDG_RUNTIME_DIR": "/missing"}
            self.assertTrue(runtime.configure_session_environment(env))
        self.assertEqual(env["XDG_RUNTIME_DIR"], "/missing")
        self.assertEqual(env["DBUS_SESSION_BUS_ADDRESS"], "unix:path=" + str(fallback / "bus"))

    def test_absent_bus_does_not_invent_a_session(self):
        with patch.object(runtime, "_session_bus", return_value=None):
            env = {}
            self.assertFalse(runtime.configure_session_environment(env))
        self.assertEqual(env, {})

    def test_platform_without_getuid_does_not_invent_a_session(self):
        with patch.object(runtime.os, "getuid", None):
            env = {}
            self.assertFalse(runtime.configure_session_environment(env))
        self.assertEqual(env, {})

    def test_console_entrypoints_restore_session_bus_before_mail_operations(self):
        from thunderbird_mcp import cli, server

        self.bus()
        expected = "unix:path=" + str(self.root / "bus")

        def check_bus(*args, **kwargs):
            self.assertEqual(os.environ.get("DBUS_SESSION_BUS_ADDRESS"), expected)
            return {"ok": True}

        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.root)}, clear=True):
            with patch.object(cli, "dispatch", side_effect=check_bus), redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["status"]), 0)

        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.root)}, clear=True):
            with patch.object(server._index, "default_db_path", return_value=self.root / "missing.sqlite"):
                with patch.object(server.mcp, "run", side_effect=check_bus):
                    server.main()

    def test_regular_file_and_missing_file_are_rejected(self):
        self.assertIsNone(runtime._session_bus(self.root, os.getuid()))
        (self.root / "bus").write_text("not a socket")
        self.assertIsNone(runtime._session_bus(self.root, os.getuid()))

    def test_other_owner_and_writable_directory_are_rejected(self):
        self.bus()
        self.assertIsNone(runtime._session_bus(self.root, os.getuid() + 1))
        self.root.chmod(0o777)
        self.assertIsNone(runtime._session_bus(self.root, os.getuid()))

    def test_socket_and_directory_symlinks_are_rejected(self):
        bus = self.bus()
        alias = self.root / "alias"
        alias.mkdir()
        (alias / "bus").symlink_to(bus)
        self.assertIsNone(runtime._session_bus(alias, os.getuid()))
        link = self.root / "runtime-link"
        link.symlink_to(self.root, target_is_directory=True)
        self.assertIsNone(runtime._session_bus(link, os.getuid()))

    def test_relative_runtime_is_rejected(self):
        self.assertIsNone(runtime._session_bus(Path("relative/path"), os.getuid()))


if __name__ == "__main__":
    unittest.main()
