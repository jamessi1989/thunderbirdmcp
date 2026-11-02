"""Exercise a release wheel on native Windows with synthetic mail and clients.

Run in Windows CI or a Windows terminal. Package installation may use the
network; application checks allow only loopback sockets needed by asyncio and
use a null keyring. Real credentials, Thunderbird and AI clients are not used.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile

from test_install import CheckFailed, PROBE, decoded, isolated_environment, make_fixture, require, run_command


WINDOWS_GUARD = '''import ipaddress, socket, sys
def _loopback(value):
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return value == 'localhost'
def _local_only(event, args):
    if event in {'socket.getaddrinfo', 'socket.gethostbyname', 'socket.gethostbyaddr', 'socket.getnameinfo'}:
        host = args[0][0] if event == 'socket.getnameinfo' else args[0]
        if not _loopback(host):
            raise RuntimeError('External network disabled during Windows install verification')
    if event in {'socket.connect', 'socket.bind', 'socket.sendto', 'socket.sendmsg'} and args:
        if getattr(args[0], 'family', None) in (socket.AF_INET, socket.AF_INET6):
            address = args[-1]
            if not isinstance(address, tuple) or not _loopback(address[0]):
                raise RuntimeError('External network disabled during Windows install verification')
sys.addaudithook(_local_only)
'''


def verify(root: Path, *, uv: str, wheel: Path, constraints: Path | None) -> dict:
    env = isolated_environment(root)
    home = Path(env["HOME"])
    system = Path(os.environ["SystemRoot"])
    env.update(SystemRoot=str(system), WINDIR=str(system), COMSPEC=str(system / "System32/cmd.exe"),
               PATH=str(system / "System32"), PATHEXT=".COM;.EXE;.BAT;.CMD",
               APPDATA=str(home / "AppData/Roaming"), LOCALAPPDATA=str(home / "AppData/Local"))
    working = root / "unrelated working directory"
    working.mkdir()
    environment = root / "fresh environment"
    run_command("create Windows venv", [uv, "venv", "--python", sys.executable, str(environment)], env=env, cwd=working)
    python = environment / "Scripts/python.exe"
    install = [uv, "pip", "install", "--python", str(python), str(wheel)]
    if constraints:
        install.extend(["--constraint", str(constraints)])
    run_command("install wheel", install, env=env, cwd=working)
    guard = root / "network guard"
    guard.mkdir()
    (guard / "sitecustomize.py").write_text(WINDOWS_GUARD, encoding="utf-8")
    env["PYTHONPATH"] = str(guard)
    def run(label, args):
        return run_command(label, [str(python), *args], env=env, cwd=working)
    info = decoded("installed paths", run("installed paths", ["-c",
        "import importlib.metadata,json,sys,fastembed,onnxruntime; from thunderbird_mcp import index; "
        "from keyring.backends.Windows import WinVaultKeyring; "
        "print(json.dumps({'version':importlib.metadata.version('thunderbird-mcp'),"
        "'module':index.__file__,'database':str(index.default_db_path()),'keyring_backend':WinVaultKeyring.__name__}))"]))
    require(Path(info["module"]).resolve().is_relative_to(environment.resolve()), "Imports escaped the installed wheel")
    expected_db = Path(env["LOCALAPPDATA"]) / "thunderbird-mcp/mail-index.sqlite"
    require(Path(info["database"]) == expected_db, "Wrong native Windows data directory")
    # Verify the audit guard without blocking Windows asyncio's loopback pair.
    run("network guard", ["-c", "import socket\ntry: socket.getaddrinfo('example.test',443)\nexcept RuntimeError: pass\nelse: raise AssertionError('DNS guard missing')\na,b=socket.socketpair(); a.close(); b.close()"])
    options = ["-m", "thunderbird_mcp.setup", "--home", str(home), "--clients", "codex", "claude", "opencode"]
    preview = decoded("setup preview", run("setup preview", options))
    require(preview["change_count"] == 15 and not preview["applied"], "Expected twelve launcher files and three client entries")
    require(all(not Path(item["path"]).exists() for item in preview["files"]), "Preview wrote files")
    entrypoints = {name: hashlib.sha256((environment / "Scripts" / name).read_bytes()).hexdigest()
                   for name in ("tbmail.exe", "thunderbird-mcp.exe")}
    decoded("setup apply", run("setup apply", [*options, "--apply"]))
    repeated = decoded("setup repeat", run("setup repeat", [*options, "--apply"]))
    require(repeated["change_count"] == 0, "Windows setup was not idempotent")
    require(entrypoints == {name: hashlib.sha256((environment / "Scripts" / name).read_bytes()).hexdigest() for name in entrypoints},
            "Setup replaced installed Windows console executables")
    original_profile = make_fixture(home)
    native_root = Path(env["APPDATA"]) / "Thunderbird"
    native_root.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(original_profile.parent.parent), str(native_root))
    profile = native_root / "Profiles" / original_profile.name
    def profile_hashes():
        return {str(path.relative_to(profile)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in profile.rglob("*") if path.is_file()}
    before = profile_hashes()
    launcher = home / "AppData/Local/thunderbird-mcp/bin/tbmail.cmd"
    # Native cmd.exe, including its /c quote grammar, is part of this smoke test.
    def terminal(label, arguments):
        command = f'"{env["COMSPEC"]}" /d /s /c ""{launcher}" {arguments}"'
        result = subprocess.run(command, env=env, cwd=working, capture_output=True, text=True, encoding="utf-8", timeout=90)
        require(result.returncode == 0, f"{label}: {(result.stderr or result.stdout)[-2000:]}")
        return decoded(label, result)
    terminal("index synthetic Windows profile", "reindex --json")
    require(expected_db.is_file(), "Index was not created under LOCALAPPDATA")
    found = terminal("Windows CLI search", 'search "installed beacon" --keyword --json')
    require(found["count"] == 1, "Windows CLI search failed")
    (working / "relative body.txt").write_text("Synthetic working directory check", encoding="utf-8")
    (working / "relative attachment.txt").write_text("Synthetic attachment", encoding="utf-8")
    draft = terminal("Windows draft", 'draft --to recipient@example.test --subject "Windows fixture" --body-file "relative body.txt" --attach "relative attachment.txt" --json')
    require(draft["status"] == "prepared" and len(draft["attachments"]) == 1, "Relative Windows draft inputs failed")
    config = json.loads((home / ".claude.json").read_text())["mcpServers"]["thunderbird"]
    require(Path(config["command"]) == python, "MCP client must execute the selected Python directly")
    probe = working / "mcp_probe.py"
    probe.write_text(PROBE.replace("command=sys.argv[1], env=", "command=sys.argv[1], args=sys.argv[2:], env="), encoding="utf-8")
    mcp = decoded("Windows MCP protocol", run("Windows MCP protocol", [str(probe), config["command"], *config["args"]]))
    require(before == profile_hashes(), "Windows workflows changed the synthetic source profile")
    return {"ok": True, "platform": sys.platform, "python": platform.python_version(), "version": info["version"],
            "native_launcher": True, "profile_discovery": True, "keyring_backend_import": info["keyring_backend"],
            "client_config": ["codex", "claude", "opencode"], "mcp": mcp,
            "live_provider_or_desktop_verified": False, "sent": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--constraints", type=Path)
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        print(json.dumps({"ok": False, "error": "This runner requires native Windows; use scripts/test_install.py on Linux/macOS."}))
        return 1
    uv = shutil.which("uv")
    try:
        require(uv is not None, "Install uv before running the Windows check")
        with tempfile.TemporaryDirectory(prefix="thunderbird-windows-") as temporary:
            report = verify(Path(temporary), uv=uv, wheel=args.wheel.resolve(),
                            constraints=args.constraints.resolve() if args.constraints else None)
    except (CheckFailed, OSError) as exc:
        report = {"ok": False, "error": str(exc)}
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
