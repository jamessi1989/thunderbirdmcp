"""Install user-scoped MCP entries and terminal commands without changing sessions.

The default invocation only previews changes. ``--apply`` writes private backups
before atomically updating individual files; unrelated client settings survive.
"""

from __future__ import annotations

import argparse
from collections.abc import MutableMapping
import copy
import datetime as dt
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import sys
import tempfile
from typing import Any

import tomlkit

CLIENTS = ("claude", "codex", "opencode", "agy", "gemini")
SERVER_NAME = "thunderbird"


def _json_clean(raw: str) -> str:
    """Blank JSONC comments/trailing commas while retaining character offsets."""
    chars = list(raw)
    i = 0
    quoted = False
    while i < len(chars):
        char = chars[i]
        if quoted:
            if char == "\\":
                i += 2
                continue
            if char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif raw[i:i + 2] == "//":
            end = raw.find("\n", i)
            end = len(raw) if end < 0 else end
            chars[i:end] = " " * (end - i)
            i = end
            continue
        elif raw[i:i + 2] == "/*":
            end = raw.find("*/", i + 2)
            if end < 0:
                raise ValueError("Unterminated JSONC comment")
            end += 2
            chars[i:end] = ["\n" if c == "\n" else " " for c in raw[i:end]]
            i = end
            continue
        i += 1
    quoted = False
    i = 0
    while i < len(chars):
        if quoted:
            if chars[i] == "\\":
                i += 2
                continue
            if chars[i] == '"':
                quoted = False
        elif chars[i] == '"':
            quoted = True
        elif chars[i] == ",":
            nxt = i + 1
            while nxt < len(chars) and chars[nxt].isspace():
                nxt += 1
            if nxt < len(chars) and chars[nxt] in "}]":
                chars[i] = " "
        i += 1
    return "".join(chars)


def _json_load(raw: str) -> dict:
    value = json.loads(_json_clean(raw))
    if not isinstance(value, dict):
        raise ValueError("Client configuration must be a JSON object")
    return value


def _json_set(raw: str, keys: tuple[str, ...], value: Any) -> str:
    """Replace one nested value, preserving every unrelated byte and comment."""
    clean = _json_clean(raw)
    decoder = json.JSONDecoder()

    def skip(pos: int) -> int:
        while pos < len(clean) and clean[pos].isspace():
            pos += 1
        return pos

    def edit(start: int, remaining: tuple[str, ...]) -> str:
        if clean[start] != "{":
            raise ValueError("MCP configuration section must be an object")
        pos = skip(start + 1)
        has_members = clean[pos] != "}"
        while clean[pos] != "}":
            key, key_end = decoder.raw_decode(clean, pos)
            value_start = skip(skip(key_end) + 1)  # skip the colon
            _, value_end = decoder.raw_decode(clean, value_start)
            if key == remaining[0]:
                if len(remaining) > 1:
                    return edit(value_start, remaining[1:])
                replacement = json.dumps(value, ensure_ascii=False, indent=2)
                return raw[:value_start] + replacement + raw[value_end:]
            pos = skip(value_end)
            if clean[pos] == ",":
                pos = skip(pos + 1)
        nested = value
        for key in reversed(remaining[1:]):
            nested = {key: nested}
        insertion = "\n  " + json.dumps(remaining[0]) + ": "
        insertion += json.dumps(nested, ensure_ascii=False, indent=2)
        insertion += "," if has_members else "\n"
        return raw[:start + 1] + insertion + raw[start + 1:]

    result = edit(skip(0), keys)
    _json_load(result)
    return result


def _launcher(python: Path, module: str, project: Path) -> str:
    # Import from the project without cd: callers' attachment paths stay relative
    # to their terminal. Avoid uv startup and dependence on a client's PATH.
    program = (
        "import runpy,sys; "
        f"sys.path.insert(0, {str(project)!r}); "
        "from thunderbird_mcp.runtime import configure_session_environment; "
        "configure_session_environment(); "
        f"runpy.run_module({module!r}, run_name='__main__')"
    )
    return "#!/bin/sh\n# Managed by thunderbird-mcp-setup.\nexec " + shlex.quote(str(python)) + " -c " + shlex.quote(program) + ' "$@"\n'


def _read(path: Path) -> bytes | None:
    if path.is_symlink():
        raise ValueError(f"Refusing to replace symlink: {path}")
    return path.read_bytes() if path.exists() else None


def _client_path(client: str, home: Path, use_env: bool) -> Path:
    env = os.environ if use_env else {}
    if client == "claude":
        override = env.get("CLAUDE_CONFIG_DIR")
        return Path(override) / ".claude.json" if override else home / ".claude.json"
    if client == "codex":
        return Path(env.get("CODEX_HOME", home / ".codex")) / "config.toml"
    if client == "opencode":
        root = Path(env.get("XDG_CONFIG_HOME", home / ".config")) / "opencode"
        jsonc = root / "opencode.jsonc"
        return jsonc if jsonc.exists() else root / "opencode.json"
    if client == "agy":
        return home / ".gemini/config/mcp_config.json"
    return home / ".gemini/settings.json"


def _client_update(client: str, raw: str, command: str) -> tuple[str, list[str]]:
    notes: list[str] = []
    if client == "codex":
        doc = tomlkit.parse(raw)
        servers = doc.setdefault("mcp_servers", tomlkit.table())
        if not isinstance(servers, MutableMapping):
            raise ValueError("mcp_servers must be a TOML table")
        entry = servers.setdefault(SERVER_NAME, tomlkit.table())
        if not isinstance(entry, MutableMapping):
            raise ValueError("thunderbird MCP entry must be a TOML table")
        for name in ("url", "bearer_token_env_var", "http_headers", "env_http_headers", "oauth"):
            entry.pop(name, None)
        entry["command"] = command
        entry["args"] = []
        entry["enabled"] = True
        entry["startup_timeout_sec"] = 30
        entry["tool_timeout_sec"] = 180
        return tomlkit.dumps(doc), notes
    doc = _json_load(raw or "{}\n")
    group = "mcp" if client == "opencode" else "mcpServers"
    servers = doc.get(group, {})
    if not isinstance(servers, dict):
        raise ValueError(f"{group} must be an object")
    entry = copy.deepcopy(servers.get(SERVER_NAME, {}))
    if not isinstance(entry, dict):
        raise ValueError("thunderbird MCP entry must be an object")
    for name in ("url", "serverUrl", "httpUrl", "headers", "oauth"):
        entry.pop(name, None)
    if client == "opencode":
        entry.update(type="local", command=[command], enabled=True, timeout=180000)
        entry.pop("args", None)
    else:
        entry.update(command=command, args=[])
        if client == "claude":
            entry["type"] = "stdio"
        else:
            entry.pop("type", None)
        if "disabled" in entry:
            entry["disabled"] = False
    if client == "claude":
        for project, options in doc.get("projects", {}).items():
            if SERVER_NAME in options.get("mcpServers", {}):
                notes.append(f"Claude project entry overrides user configuration: {project}")
            if SERVER_NAME in options.get("disabledMcpServers", []):
                notes.append(f"Thunderbird is disabled in Claude project: {project}")
    if servers.get(SERVER_NAME) == entry:
        return raw, notes
    return _json_set(raw or "{}\n", (group, SERVER_NAME), entry), notes


def build_plan(
    *, project: Path | None = None, home: Path | None = None,
    clients: list[str] | None = None, python: Path | None = None,
) -> dict:
    """Return a serializable plan, including original bytes for change detection.

    ``report`` is safe to print; ``changes`` contains private config contents and
    must never be printed or logged.
    """
    project = (project or Path(__file__).resolve().parent.parent).absolute()
    use_env = home is None
    home = (home or Path.home()).absolute()
    python = (python or project / ".venv/bin/python").absolute()
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError(f"Python environment is missing: {python}. Run uv sync in {project} first.")
    if not (project / "thunderbird_mcp/server.py").is_file():
        raise ValueError(f"Thunderbird MCP source was not found in {project}")
    if clients is None:
        clients = [client for client in CLIENTS if shutil.which(client)]
    unknown = sorted(set(clients) - set(CLIENTS))
    if unknown:
        raise ValueError("Unknown clients: " + ", ".join(unknown))
    bin_dir = home / ".local/bin"
    command = str(bin_dir / "thunderbird-mcp")
    report: dict[str, Any] = {"project": str(project), "clients": [], "files": [], "notes": []}
    changes: list[dict] = []

    def add(path: Path, content: str, mode: int, kind: str) -> None:
        before = _read(path)
        after = content.encode("utf-8")
        changed = before != after or (path.exists() and stat.S_IMODE(path.stat().st_mode) != mode)
        report["files"].append({"path": str(path), "kind": kind, "changed": changed})
        if changed:
            changes.append({"path": str(path), "before": before, "after": after, "mode": mode})

    for name, module in (("thunderbird-mcp", "thunderbird_mcp.server"), ("tbmail", "thunderbird_mcp.cli"), ("thunderbird-mail", "thunderbird_mcp.cli"), ("thunderbird-mcp-setup", "thunderbird_mcp.setup")):
        add(bin_dir / name, _launcher(python, module, project), 0o755, "command")
    for client in dict.fromkeys(clients):
        path = _client_path(client, home, use_env)
        before = _read(path)
        raw = before.decode("utf-8") if before is not None else ("" if client == "codex" else "{}\n")
        result, notes = _client_update(client, raw, command)
        add(path, result, 0o600, "client")
        report["clients"].append({"name": client, "path": str(path), "installed": bool(shutil.which(client))})
        report["notes"].extend(notes)
    if str(bin_dir) not in os.environ.get("PATH", "").split(os.pathsep):
        report["notes"].append(f"Add {bin_dir} to PATH to run tbmail by name; MCP uses an absolute command.")
    report["notes"].append("Existing agent sessions load MCP configuration on restart; use tbmail immediately from their shell tools.")
    report["change_count"] = len(changes)
    return {"report": report, "changes": changes}


def apply_plan(plan: dict) -> dict:
    """Write reviewed changes, refusing stale inputs and retaining private backups."""
    # Preflight all files before any write; each file is checked again at commit.
    for change in plan["changes"]:
        path = Path(change["path"])
        if _read(path) != change["before"]:
            raise RuntimeError(f"Configuration changed since planning: {path}; rerun setup.")
    report = copy.deepcopy(plan["report"])
    report["backups"] = []
    for change in plan["changes"]:
        path = Path(change["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        if _read(path) != change["before"]:
            raise RuntimeError(f"Configuration changed while applying: {path}; rerun setup.")
        if change["before"] is not None:
            stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            backup = path.with_name(path.name + ".thunderbird-backup-" + stamp)
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(change["before"])
            report["backups"].append(str(backup))
        fd, temp = tempfile.mkstemp(prefix=".thunderbird-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(change["after"])
                stream.flush()
                os.fsync(stream.fileno())
                os.fchmod(stream.fileno(), change["mode"])
            if _read(path) != change["before"]:
                raise RuntimeError(f"Configuration changed while applying: {path}; rerun setup.")
            os.replace(temp, path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
    report["applied"] = True
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Install commands and register selected clients (default: preview)")
    parser.add_argument("--clients", nargs="+", choices=(*CLIENTS, "none"), help="Clients to configure; defaults to installed CLIs")
    parser.add_argument("--home", type=Path, help="Alternate user home, useful for preview/testing")
    parser.add_argument("--project", type=Path, help="Source checkout containing .venv")
    parser.add_argument("--python", type=Path, help="Override the checkout's .venv/bin/python")
    args = parser.parse_args(argv)
    if args.clients and "none" in args.clients and len(args.clients) > 1:
        parser.error("--clients none cannot be combined with client names")
    try:
        plan = build_plan(project=args.project, home=args.home, python=args.python, clients=[] if args.clients == ["none"] else args.clients)
        report = apply_plan(plan) if args.apply else {**plan["report"], "applied": False}
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    except (ValueError, RuntimeError, OSError) as error:
        print(f"Setup failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
