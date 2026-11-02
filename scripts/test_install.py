"""Verify a built wheel in a fresh environment without personal mail or clients.

Requires uv. Dependency installation can contact package indexes; after install,
all application checks block Internet sockets and use a null keyring. No model
client or mail server is invoked. The report contains synthetic checks only.

    python scripts/test_install.py --wheel dist/thunderbird_mcp-VERSION-py3-none-any.whl --json

Omit --wheel to build one in a temporary directory. --constraints accepts an
optional requirements-style lock export for the fresh dependency installation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mailbox
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from email.message import EmailMessage

PROJECT = Path(__file__).resolve().parents[1]


class CheckFailed(RuntimeError):
    pass


def isolated_environment(root: Path) -> dict[str, str]:
    """Construct a small environment instead of copying credentials/settings."""
    home = root / "home with spaces"
    temporary = root / "tmp"
    home.mkdir(parents=True, exist_ok=True)
    temporary.mkdir(parents=True, exist_ok=True)
    return {
        "HOME": str(home), "USERPROFILE": str(home),
        "PATH": os.defpath, "TMPDIR": str(temporary), "TEMP": str(temporary), "TMP": str(temporary),
        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
        "UV_CACHE_DIR": str(root / "uv-cache"), "UV_NO_PROGRESS": "1", "NO_COLOR": "1",
        "XDG_CACHE_HOME": str(home / ".cache"), "XDG_RUNTIME_DIR": str(root / "runtime"),
        "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
        "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
    }


def run_command(label: str, args: list[str], *, env: dict[str, str], cwd: Path,
                expected: tuple[int, ...] = (0,), timeout: int = 300) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(args, env=env, cwd=cwd, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CheckFailed(f"{label}: {type(exc).__name__}") from None
    if result.returncode not in expected:
        # The child receives only this script's synthetic environment and data.
        detail = (result.stderr.strip() or result.stdout.strip())[-3000:]
        raise CheckFailed(f"{label}: exit {result.returncode}\n{detail}")
    return result


def decoded(label: str, result: subprocess.CompletedProcess) -> dict:
    try:
        data = json.loads(result.stdout)
    except ValueError:
        raise CheckFailed(f"{label}: expected one JSON result on stdout") from None
    if not isinstance(data, dict):
        raise CheckFailed(f"{label}: expected a JSON object")
    return data


def require(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailed(message)


def make_fixture(home: Path) -> Path:
    """Exercise real profile discovery, including spaces and literal percent."""
    root = home / "Library" / "Thunderbird" if sys.platform == "darwin" else home / ".thunderbird"
    profile = root / "Profiles" / "smoke 100% ready"
    account = profile / "ImapMail" / "imap.example.test"
    account.mkdir(parents=True)
    (root / "profiles.ini").write_text("[Profile0]\nName=Smoke fixture\nIsRelative=1\nPath=Profiles/smoke 100% ready\nDefault=1\n", encoding="utf-8")
    prefs = {
        "mail.accountmanager.accounts": "account1", "mail.accountmanager.defaultaccount": "account1",
        "mail.account.account1.server": "server1", "mail.account.account1.identities": "id1",
        "mail.identity.id1.useremail": "fixture@example.test", "mail.identity.id1.fullName": "Install Fixture",
        "mail.identity.id1.smtpServer": "smtp1", "mail.server.server1.type": "imap",
        "mail.server.server1.hostname": "imap.example.test", "mail.server.server1.userName": "fixture",
        "mail.server.server1.socketType": 3, "mail.server.server1.directory-rel": "[ProfD]ImapMail/imap.example.test",
        "mail.smtpserver.smtp1.hostname": "smtp.example.test", "mail.smtpserver.smtp1.username": "fixture",
        "mail.smtpserver.smtp1.port": 465, "mail.smtpserver.smtp1.try_ssl": 3,
    }
    (profile / "prefs.js").write_text("\n".join(f"user_pref({json.dumps(key)}, {json.dumps(value)});" for key, value in prefs.items()), encoding="utf-8")
    message = EmailMessage()
    message["From"] = "Sender <sender@example.test>"
    message["To"] = "Install Fixture <fixture@example.test>"
    message["Subject"] = "Installed beacon"
    message["Message-ID"] = "<installed-beacon@example.test>"
    message["Date"] = "Mon, 02 Nov 2026 09:00:00 +0000"
    message.set_content("Synthetic verification phrase: fresh-wheel-ready.")
    message.add_attachment("Synthetic attachment contains the word zephyrglass.\n", subtype="plain", filename="release.txt")
    path = account / "INBOX"
    box = mailbox.mbox(path)
    try:
        box.add(message)
        box.flush()
    finally:
        box.close()
    path.with_suffix(".msf").touch()
    return profile


GUARD = '''import socket, sys
def _local_only(event, args):
    denied = event in {"socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr", "socket.getnameinfo"}
    if event == "socket.__new__" and len(args) > 1:
        denied = args[1] in (socket.AF_INET, socket.AF_INET6)
    elif event in {"socket.connect", "socket.sendto", "socket.sendmsg", "socket.bind"} and args:
        denied = getattr(args[0], "family", None) in (socket.AF_INET, socket.AF_INET6)
    if denied:
        raise RuntimeError("Network disabled during synthetic install verification")
sys.addaudithook(_local_only)
'''


PROBE = '''import asyncio, json, os, sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    parameters = StdioServerParameters(command=sys.argv[1], env=dict(os.environ), cwd=os.getcwd())
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            result = await session.initialize()
            assert result.serverInfo.name == "thunderbird"
            names = {tool.name for tool in (await session.list_tools()).tools}
            assert {"search_email", "get_email", "get_email_reference", "draft_reply", "get_draft"} <= names
            async def call(name, arguments):
                response = await session.call_tool(name, arguments)
                assert not response.isError, name
                return response.structuredContent or json.loads(response.content[0].text)
            found = await call("search_email", {"query": "installed beacon", "semantic": False})
            assert found["count"] == 1
            reference = found["results"][0]["reference"]
            message = await call("get_email", {"reference": reference})
            assert "fresh-wheel-ready" in message["body"]
            citation = await call("get_email_reference", {"reference": reference})
            assert citation["reference"] == reference
            resource = await session.read_resource(reference)
            assert json.loads(resource.contents[0].text)["message_id"] == "<installed-beacon@example.test>"
            attachments = await call("search_email", {"query": "zephyrglass", "semantic": False})
            assert attachments["results"][0]["reference"] == reference
            draft = await call("draft_reply", {"reference": reference, "body": "Synthetic reply prepared only."})
            saved = await call("get_draft", {"draft_id": draft["draft_id"]})
            assert saved["status"] == "prepared" and saved["account_id"] == "account1"
            print(json.dumps({"handshake": True, "search": True, "read": True, "reference": True,
                              "resource": True, "attachment_search": True, "draft": True,
                              "tool_count": len(names), "sent": False}))
asyncio.run(main())
'''


def verify(root: Path, *, uv: str, wheel: Path | None, constraints: Path | None = None) -> dict:
    env = isolated_environment(root)
    home = Path(env["HOME"])
    working = root / "unrelated working directory"
    working.mkdir()
    if wheel is None:
        output = root / "wheel"
        run_command("build wheel", [uv, "build", "--wheel", "--out-dir", str(output), str(PROJECT)], env=env, cwd=working)
        candidates = list(output.glob("*.whl"))
        require(len(candidates) == 1, "Expected one built wheel")
        wheel = candidates[0]
    require(wheel.is_file() and wheel.suffix == ".whl", "Supply an existing wheel with --wheel")
    environment = root / "fresh environment"
    run_command("create fresh venv", [uv, "venv", "--python", sys.executable, str(environment)], env=env, cwd=working)
    python = environment / "bin/python"
    install = [uv, "pip", "install", "--python", str(python), str(wheel)]
    if constraints:
        install += ["-c", str(constraints)]
    run_command("install wheel", install, env=env, cwd=working, timeout=900)
    env["PATH"] = str(environment / "bin") + os.pathsep + os.defpath
    guard = root / "network guard"
    guard.mkdir()
    (guard / "sitecustomize.py").write_text(GUARD, encoding="utf-8")
    env["PYTHONPATH"] = str(guard)
    # Explicit --home must ignore inherited client configuration directories.
    outside = root / "unselected client configuration"
    outside.mkdir()
    sentinel = outside / "untouched.txt"
    sentinel.write_text("synthetic sentinel", encoding="utf-8")
    env.update(CODEX_HOME=str(outside), CLAUDE_CONFIG_DIR=str(outside), XDG_CONFIG_HOME=str(outside))
    info = decoded("installed imports", run_command("installed imports", [str(python), "-c",
        "import importlib.metadata,json,sys; from thunderbird_mcp import index; "
        "print(json.dumps({'version':importlib.metadata.version('thunderbird-mcp'),'module':index.__file__,"
        "'prefix':sys.prefix,'base_prefix':sys.base_prefix,'database':str(index.default_db_path())}))"], env=env, cwd=working))
    require(Path(info["module"]).resolve().is_relative_to(environment.resolve()), "The installed package leaked imports from outside the fresh venv")
    require(info["prefix"] != info["base_prefix"], "Checks did not run in an isolated virtual environment")
    database = Path(info["database"])
    require(database.is_relative_to(home), "Installed-wheel data must live under the isolated user home")
    require(not database.is_relative_to(environment), "Mail data must not live in site-packages")
    cli = environment / "bin/tbmail"
    missing = decoded("missing profile", run_command("missing profile", [str(cli), "status", "--json"], env=env, cwd=working, expected=(1,)))
    require("TB_PROFILE" in missing.get("profile", {}).get("error", ""), "Missing-profile status must explain TB_PROFILE")
    require(not database.exists(), "Status created an index for a nonexistent profile")
    setup = environment / "bin/thunderbird-mcp-setup"
    options = ["--home", str(home), "--clients", "codex", "claude", "opencode"]
    dry = decoded("setup preview", run_command("setup preview", [str(setup), *options], env=env, cwd=working))
    require(not dry["applied"] and dry["change_count"] == 7, "Fresh setup preview should contain four launchers and three clients")
    require(all(Path(item["path"]).is_relative_to(home) for item in dry["files"]), "Setup escaped its explicit --home")
    require(all(not Path(item["path"]).exists() for item in dry["files"]), "Setup preview wrote planned files")
    applied = decoded("setup apply", run_command("setup apply", [str(setup), *options, "--apply"], env=env, cwd=working))
    require(applied["applied"] and applied["change_count"] == 7, "Setup did not apply all expected changes")
    repeated = decoded("setup idempotence", run_command("setup idempotence", [str(setup), *options, "--apply"], env=env, cwd=working))
    require(repeated["change_count"] == 0 and not repeated["backups"], "Second setup application was not a no-op")
    for launcher in ("tbmail", "thunderbird-mail", "thunderbird-mcp-setup"):
        run_command("launcher " + launcher, [str(home / ".local/bin" / launcher), "--help"], env=env, cwd=working)
    profile = make_fixture(home)
    profile_before = {str(path.relative_to(profile)): hashlib.sha256(path.read_bytes()).hexdigest()
                      for path in profile.rglob("*") if path.is_file()}
    launcher = home / ".local/bin/tbmail"
    run_command("index synthetic profile", [str(launcher), "reindex", "--json"], env=env, cwd=working)
    require(database.is_file(), "Index was not created at its expected user-data path")
    require(database.stat().st_mode & 0o777 == 0o600, "Installed-wheel index permissions must be private")
    found = decoded("terminal search", run_command("terminal search", [str(launcher), "search", "installed beacon", "--keyword", "--json"], env=env, cwd=working))
    require(found["count"] == 1, "Installed CLI could not search the synthetic mailbox")
    (working / "relative body.txt").write_text("The launcher preserves the caller's working directory.", encoding="utf-8")
    (working / "relative attachment.txt").write_text("Synthetic attachment from another directory.", encoding="utf-8")
    draft = decoded("relative draft body", run_command("relative draft body", [str(launcher), "draft", "--to", "recipient@example.test",
        "--subject", "Fresh install draft", "--body-file", "relative body.txt", "--attach", "relative attachment.txt", "--json"], env=env, cwd=working))
    require(draft["status"] == "prepared" and "working directory" in draft["body"], "Launcher changed its caller's working directory")
    require(len(draft["attachments"]) == 1 and draft["attachments"][0]["filename"] == "relative attachment.txt", "Launcher could not resolve a relative attachment")
    probe = working / "mcp_probe.py"
    probe.write_text(PROBE, encoding="utf-8")
    mcp = decoded("stdio MCP workflow", run_command("stdio MCP workflow", [str(python), str(probe), str(home / ".local/bin/thunderbird-mcp")], env=env, cwd=working))
    profile_after = {str(path.relative_to(profile)): hashlib.sha256(path.read_bytes()).hexdigest()
                     for path in profile.rglob("*") if path.is_file()}
    require(profile_before == profile_after, "Application checks modified the synthetic Thunderbird source profile")
    require(list(outside.iterdir()) == [sentinel] and sentinel.read_text() == "synthetic sentinel", "Setup modified an unselected client configuration directory")
    require(all((Path(item["path"]).stat().st_mode & 0o777) == (0o755 if item["kind"] == "command" else 0o600)
                for item in applied["files"]), "Setup installed incorrect command/config permissions")
    # Exercise the release guide's actual uv-tool installation path as well.
    tool_env = isolated_environment(root / "uv-tool-install")
    tool_home = Path(tool_env["HOME"])
    tool_bin = tool_home / ".local/bin"
    tool_env.update(UV_CACHE_DIR=env["UV_CACHE_DIR"], UV_TOOL_DIR=str(root / "uv tools"),
                    UV_TOOL_BIN_DIR=str(tool_bin), PYTHONPATH=str(guard))
    tool_command = [uv, "tool", "install", "--python", sys.executable, str(wheel)]
    if constraints:
        tool_command += ["-c", str(constraints)]
    run_command("uv tool install", tool_command, env=tool_env, cwd=working, timeout=900)
    tool_links = {name: (tool_bin / name).resolve(strict=True) for name in
                  ("thunderbird-mcp", "tbmail", "thunderbird-mail", "thunderbird-mcp-setup")}
    require(all((tool_bin / name).is_symlink() for name in tool_links), "uv tool install did not create expected entrypoint links")
    tool_setup = [str(tool_bin / "thunderbird-mcp-setup"), "--home", str(tool_home), "--clients", "codex", "claude", "opencode", "--apply"]
    tool_applied = decoded("uv tool setup", run_command("uv tool setup", tool_setup, env=tool_env, cwd=working))
    require(tool_applied["change_count"] == 3, "uv-tool setup should write only three client configurations")
    require(sum(bool(item.get("preserved_entrypoint")) for item in tool_applied["files"]) == 4, "Setup did not preserve uv-tool entrypoints")
    require(all((tool_bin / name).is_symlink() and (tool_bin / name).resolve(strict=True) == target for name, target in tool_links.items()),
            "Setup replaced an entrypoint managed by uv tool")
    tool_repeat = decoded("uv tool setup idempotence", run_command("uv tool setup idempotence", tool_setup, env=tool_env, cwd=working))
    require(tool_repeat["change_count"] == 0, "uv-tool setup was not idempotent")
    make_fixture(tool_home)
    run_command("uv tool index", [str(tool_bin / "tbmail"), "reindex", "--json"], env=tool_env, cwd=working)
    decoded("uv tool MCP workflow", run_command("uv tool MCP workflow", [str(python), str(probe), str(tool_bin / "thunderbird-mcp")], env=tool_env, cwd=working))
    return {"ok": True, "platform": platform.system(), "python": platform.python_version(),
            "version": info["version"], "wheel": wheel.name, "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
            "fresh_venv": True, "isolated_home": True, "installed_imports": True, "unrelated_cwd": True,
            "missing_profile_guidance": True, "user_data_directory": True,
            "setup": {"preview": True, "apply": True, "idempotent": True, "clients": ["codex", "claude", "opencode"]},
            "launchers": True, "terminal_search": True, "relative_attachment_body_paths": True,
            "profile_unchanged": True, "unselected_configs_unchanged": True,
            "application_network_blocked": True, "network_guard": "python-socket-audit-hook", "keyring": "null", "models_invoked": False, "mcp": mcp,
            "uv_tool_install": {"installed": True, "entrypoint_links_preserved": True, "setup_idempotent": True, "mcp": True}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, help="Install this already-built wheel instead of building another")
    parser.add_argument("--constraints", type=Path, help="Optional requirements lock export for uv pip install")
    parser.add_argument("--json", action="store_true", help="Print a machine-readable report")
    args = parser.parse_args(argv)
    start = time.monotonic()
    if sys.platform not in ("linux", "darwin"):
        report = {"ok": False, "error": "This smoke runner verifies Linux/macOS POSIX launchers; Windows is not supported."}
    elif not shutil.which("uv"):
        report = {"ok": False, "error": "Install uv before running the clean-install check."}
    else:
        try:
            with tempfile.TemporaryDirectory(prefix="thunderbird-install-") as temporary:
                report = verify(Path(temporary), uv=shutil.which("uv"),
                                wheel=args.wheel.expanduser().resolve() if args.wheel else None,
                                constraints=args.constraints.expanduser().resolve() if args.constraints else None)
        except (CheckFailed, OSError) as exc:
            report = {"ok": False, "error": str(exc)}
    report["elapsed_seconds"] = round(time.monotonic() - start, 2)
    if args.json:
        print(json.dumps(report, indent=2))
    elif report["ok"]:
        print(f"Clean install passed: {report['version']} on {report['platform']} / Python {report['python']}; fresh venv, setup, CLI, and MCP verified.")
    else:
        print("Clean install failed: " + report["error"], file=sys.stderr)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
