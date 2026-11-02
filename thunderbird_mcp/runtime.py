"""Restore the current user's session bus when an MCP client filters its env.

Only a local socket address is discovered. Credentials stay in the existing
system keyring; no secrets are copied into launchers or client configuration.
"""

from __future__ import annotations

from collections.abc import MutableMapping
import os
from pathlib import Path
import stat
from urllib.parse import quote


def _session_bus(runtime: Path, uid: int) -> Path | None:
    """Accept a protected user-owned directory and its user-owned UNIX socket."""
    if not runtime.is_absolute():
        return None
    try:
        directory = runtime.lstat()
        bus = runtime / "bus"
        socket = bus.lstat()
    except OSError:
        return None
    if not stat.S_ISDIR(directory.st_mode) or directory.st_uid != uid:
        return None
    if directory.st_mode & 0o022:  # another user must not be able to replace bus
        return None
    if not stat.S_ISSOCK(socket.st_mode) or socket.st_uid != uid:
        return None
    return bus


def configure_session_environment(env: MutableMapping[str, str] | None = None) -> bool:
    """Fill a missing D-Bus address using the current user's existing session.

    Inherited addresses are authoritative. A valid XDG runtime directory is
    preferred, followed by Linux's /run/user/<uid>. Return whether an address was
    discovered; an absent session bus is left for normal keyring diagnostics.
    """
    env = os.environ if env is None else env
    if env.get("DBUS_SESSION_BUS_ADDRESS"):
        return False
    uid = os.getuid()
    candidates = []
    if env.get("XDG_RUNTIME_DIR"):
        candidates.append(Path(env["XDG_RUNTIME_DIR"]))
    candidates.append(Path("/run/user") / str(uid))
    for runtime in dict.fromkeys(candidates):
        bus = _session_bus(runtime, uid)
        if bus is None:
            continue
        # D-Bus addresses use percent escapes, not shell quoting.
        env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=" + quote(str(bus), safe="/_.-")
        if not env.get("XDG_RUNTIME_DIR"):
            env["XDG_RUNTIME_DIR"] = str(runtime)
        return True
    return False
