#!/usr/bin/env python3
"""Preview or register the optional pinned Thunderbird desktop MCP alongside ours.

This configures clients only. It never installs an add-on, starts a broker,
restarts Thunderbird, or writes a Thunderbird profile.
"""

from __future__ import annotations

import argparse
from collections.abc import MutableMapping
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import tomlkit

# Allow running this repository helper from any working directory.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from thunderbird_mcp import setup

UPSTREAM_REPOSITORY = "https://github.com/U-C4N/Thunderbird-MCP"
UPSTREAM_COMMIT = "cddefb8010fa39ac85bf4787c3449dca45de2770"
UPSTREAM_VERSION = "1.4.0"
SERVER_NAME = "thunderbird-native"
TOOLSETS = "contacts,calendar,filters,accounts,settings,admin"
CLIENTS = ("codex", "claude", "opencode")


def _run(args: list[str]) -> str:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=30, check=True)
    except (OSError, subprocess.SubprocessError):
        # Child diagnostics could contain local configuration. Do not echo them.
        raise ValueError("Native bridge verification failed; check the pinned checkout and its separate virtual environment") from None
    return result.stdout.strip()


def _installed_runtime(python: Path) -> dict:
    program = (
        "import importlib.util, importlib.metadata, json, sys; "
        "spec=importlib.util.find_spec('tbmcp'); "
        "print(json.dumps({'prefix':sys.prefix,'base_prefix':sys.base_prefix,"
        "'python':list(sys.version_info[:2]),"
        "'version':importlib.metadata.version('thunderbird-mcp'),"
        "'module':spec.origin if spec else None}))"
    )
    try:
        return json.loads(_run([str(python), "-I", "-c", program]))
    except (ValueError, TypeError):
        raise ValueError("Install the pinned native bridge in its own .venv before configuring clients") from None


def verify_upstream(checkout: Path) -> Path:
    """Require the reviewed source and matching installed code in a separate venv."""
    git = ["git", "--no-replace-objects", "-C", str(checkout)]
    if _run([*git, "rev-parse", "--verify", "HEAD"]) != UPSTREAM_COMMIT:
        raise ValueError(f"Native checkout must be pinned to {UPSTREAM_COMMIT}")
    if _run([*git, "status", "--porcelain", "--untracked-files=all"]):
        raise ValueError("Native checkout has local changes; use a clean pinned checkout")
    python = checkout / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError("Create the native checkout's separate .venv and install that checkout into it")
    installed = _installed_runtime(python)
    prefix = Path(installed["prefix"]).resolve()
    if (prefix == Path(installed["base_prefix"]).resolve()
            or prefix == Path(sys.prefix).resolve()
            or prefix != (checkout / ".venv").resolve()):
        raise ValueError("The native bridge must use its own virtual environment, separate from this mail MCP")
    if installed["python"] < [3, 11] or installed["version"] != UPSTREAM_VERSION or not installed["module"]:
        raise ValueError("Native bridge requires Python 3.11+ and the pinned upstream package version")
    package = Path(installed["module"]).parent.resolve()
    if not package.is_relative_to(prefix):
        raise ValueError("Install the native bridge normally in its .venv; editable or external packages are unsupported")
    source = checkout / "src/tbmcp"
    files = list(source.rglob("*.py"))
    if not files:
        raise ValueError("Native checkout contains no tbmcp source")
    for path in files:
        target = package / path.relative_to(source)
        if not target.is_file() or target.read_bytes() != path.read_bytes():
            raise ValueError("Installed native code differs from the pinned checkout; reinstall it in its separate .venv")
    return python


def _entry(client: str, python: Path, profile: Path, state_dir: Path, allow_writes: bool) -> dict:
    args = ["-I", "-m", "tbmcp", "serve", "--profile", str(profile), "--toolsets", TOOLSETS]
    if not allow_writes:
        args.append("--read-only")
    environment = {
        "TBMCP_READ_ONLY": "0" if allow_writes else "1",
        "TBMCP_YOLO": "0", "TBMCP_UNSAFE_PREFS": "0", "TBMCP_SEND": "draft",
        "TBMCP_TOOLS": "", "TBMCP_NO_AUTOSTART": "0", "TBMCP_STATE_DIR": str(state_dir),
    }
    if client == "opencode":
        return {"type": "local", "command": [str(python), *args], "environment": environment,
                "enabled": True, "timeout": 180000}
    entry = {"command": str(python), "args": args, "env": environment}
    if client == "claude":
        entry["type"] = "stdio"
    else:
        entry.update(enabled=True, startup_timeout_sec=60, tool_timeout_sec=180)
    return entry


def _update(client: str, raw: str, entry: dict) -> str:
    if client == "codex":
        doc = tomlkit.parse(raw)
        servers = doc.setdefault("mcp_servers", tomlkit.table())
        if not isinstance(servers, MutableMapping):
            raise ValueError("mcp_servers must be a TOML table")
        if servers.get(SERVER_NAME) == entry:
            return raw
        servers[SERVER_NAME] = entry
        return tomlkit.dumps(doc)
    group = "mcp" if client == "opencode" else "mcpServers"
    doc = setup._json_load(raw or "{}\n")
    servers = doc.get(group, {})
    if not isinstance(servers, dict):
        raise ValueError(f"{group} must be an object")
    if servers.get(SERVER_NAME) == entry:
        return raw
    return setup._json_set(raw or "{}\n", (group, SERVER_NAME), entry)


def build_plan(*, checkout: Path, profile: Path, clients: list[str], home: Path | None = None,
               allow_writes: bool = False) -> dict:
    checkout = checkout.expanduser().resolve()
    profile = profile.expanduser().absolute()
    if not profile.is_dir() or not (profile / "prefs.js").is_file():
        raise ValueError("Select an existing Thunderbird profile directory containing prefs.js")
    if not clients or set(clients) - set(CLIENTS):
        raise ValueError("Select at least one supported client: codex, claude or opencode")
    python = verify_upstream(checkout)
    use_env = home is None
    home = (home or Path.home()).expanduser().absolute()
    state_dir = home / ".thunderbird-native-mcp"
    report = {"server": SERVER_NAME, "upstream": UPSTREAM_REPOSITORY, "commit": UPSTREAM_COMMIT,
              "native_version": UPSTREAM_VERSION, "read_only": not allow_writes,
              "profile": str(profile), "toolsets": TOOLSETS.split(","), "files": [],
              "native_runtime_verified": False,
              "notes": ["Client configuration only; install the pinned add-on separately and verify tb_status.",
                        "The native server has its own policy and profile-wide access. Mail MCP account/tool restrictions do not carry over."]}
    changes = []
    for client in dict.fromkeys(clients):
        path = setup._client_path(client, home, use_env)
        before = setup._read(path)
        raw = before.decode("utf-8") if before is not None else ("" if client == "codex" else "{}\n")
        after = _update(client, raw, _entry(client, python, profile, state_dir, allow_writes)).encode()
        changed = before != after or (os.name == "posix" and path.exists() and stat.S_IMODE(path.stat().st_mode) != 0o600)
        report["files"].append({"client": client, "path": str(path), "changed": changed})
        if changed:
            changes.append({"path": str(path), "before": before, "after": after, "mode": 0o600})
    report["change_count"] = len(changes)
    # As in setup.py, only report may be printed: changes contains private configs.
    return {"report": report, "changes": changes}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", type=Path, required=True, help="Clean checkout of the pinned native bridge, with its own .venv")
    parser.add_argument("--profile", type=Path, required=True, help="Explicit Thunderbird profile directory")
    parser.add_argument("--clients", nargs="+", choices=CLIENTS, required=True)
    parser.add_argument("--home", type=Path, help="Alternate client-config home for isolated testing")
    parser.add_argument("--allow-writes", action="store_true", help="Include native write tools, retaining upstream confirmation gates")
    parser.add_argument("--apply", action="store_true", help="Apply the client changes with private backups; default is preview")
    args = parser.parse_args(argv)
    try:
        plan = build_plan(checkout=args.upstream, profile=args.profile, clients=args.clients,
                          home=args.home, allow_writes=args.allow_writes)
        report = setup.apply_plan(plan) if args.apply else plan["report"]
    except (ValueError, RuntimeError, OSError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
