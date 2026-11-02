"""Exercise real model tool calls against synthetic mail in installed clients.

This opt-in check uses the clients' configured models/accounts and may consume
model credits. It exposes only three read-only tools over a temporary mailbox;
the real mailbox, outgoing tools, and persistent client settings are untouched.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import mailbox
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from email.message import EmailMessage

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from thunderbird_mcp import index, messages  # noqa: E402

TOOLS = ("search_email", "get_email", "get_email_reference")
PROMPT = (
    "Run this read-only mail integration test using the thunderbird MCP tools. "
    "1. Call search_email with query 'integration beacon' and semantic=false. "
    "2. Call get_email on its result and read the verification code in the body. "
    "3. Call get_email_reference on that message. "
    "Finally reply MAIL_SMOKE_OK, the verification code, and the exact mail reference URI. "
    "Use only these three MCP tools. Do not use shell or filesystem tools."
)


def fixture(root: Path) -> tuple[Path, str, str]:
    profile = root / "profile"
    account = profile / "ImapMail" / "example.test"
    account.mkdir(parents=True)
    path = account / "INBOX"
    code = "BEACON-" + uuid.uuid4().hex[:12]
    mid = "<" + uuid.uuid4().hex + "@example.test>"
    message = EmailMessage()
    message["From"] = "sender@example.test"
    message["To"] = "recipient@example.test"
    message["Subject"] = "Integration beacon"
    message["Message-ID"] = mid
    message["Date"] = "Mon, 02 Nov 2026 09:00:00 +0000"
    message.set_content("Synthetic test message. Verification code: " + code)
    box = mailbox.mbox(path)
    box.add(message)
    box.close()
    path.with_suffix(".msf").touch()
    database = root / "mail.sqlite"
    index.reindex(db_path=database, profile=profile)
    server = root / "server.py"
    environment = {"TB_PROFILE": str(profile), "TB_MAIL_INDEX_DB": str(database),
                   "TB_OUTBOX_DB": str(root / "outbox.sqlite")}
    server.write_text(
        "import asyncio, os, sys\n"
        f"sys.path.insert(0, {str(PROJECT)!r})\n"
        f"os.environ.update({environment!r})\n"
        "from thunderbird_mcp.server import mcp\n"
        "for tool in asyncio.run(mcp.list_tools()):\n"
        f"    if tool.name not in {TOOLS!r}: mcp.remove_tool(tool.name)\n"
        "mcp.run()\n"
    )
    return server, code, messages.reference_for("mid:" + mid)


def command(client: str, root: Path, server: Path, opencode_model: str | None):
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    python = str(PROJECT / ".venv/bin/python")
    if not Path(python).is_file():
        python = sys.executable
    spec = {"command": python, "args": [str(server)]}
    if client == "codex":
        args = ["codex", "exec", "--ephemeral", "--json", "--color", "never",
                "--skip-git-repo-check", "--sandbox", "read-only", "-C", str(root),
                "-c", "approval_policy=\"never\"",
                "-c", "mcp_servers.thunderbird.command=" + json.dumps(python),
                "-c", "mcp_servers.thunderbird.args=" + json.dumps([str(server)]),
                "-c", "mcp_servers.thunderbird.enabled=true",
                "-c", "mcp_servers.thunderbird.enabled_tools=" + json.dumps(TOOLS)]
        # Disable unrelated servers for this invocation only.
        import tomlkit
        config = Path(env.get("CODEX_HOME", str(Path.home() / ".codex"))) / "config.toml"
        if config.is_file():
            for name in tomlkit.parse(config.read_text()).get("mcp_servers", {}):
                if name != "thunderbird":
                    args += ["-c", f"mcp_servers.{json.dumps(name)}.enabled=false"]
        return args + [PROMPT], env
    if client == "claude":
        config = root / "claude-mcp.json"
        config.write_text(json.dumps({"mcpServers": {"thunderbird": spec}}))
        allowed = ",".join("mcp__thunderbird__" + tool for tool in TOOLS)
        return ["claude", "--print", "--no-session-persistence",
                "--settings", '{"disableAllHooks":true}', "--disable-slash-commands",
                "--output-format", "stream-json", "--verbose", "--tools", "",
                "--strict-mcp-config", "--mcp-config", str(config),
                "--allowedTools", allowed, "--permission-mode", "dontAsk",
                "--max-budget-usd", "1", PROMPT], env
    config = {"mcp": {"thunderbird": {"type": "local",
               "command": [python, str(server)], "enabled": True}},
              "permission": {"*": "deny", "thunderbird_*": "allow"}, "share": "disabled"}
    # Merge overrides into the normal config, preserving the user's provider.
    user_config = Path(env.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "opencode"
    from thunderbird_mcp.setup import _json_load
    for name in ("opencode.json", "opencode.jsonc"):
        path = user_config / name
        if path.is_file():
            for name in _json_load(path.read_text()).get("mcp", {}):
                if name != "thunderbird":
                    config["mcp"][name] = {"enabled": False}
    # Test the selected OpenCode model without optional Terminalman SSH tunnels.
    env.pop("OPENCODE_CONFIG", None)
    env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config)
    args = ["opencode", "run", "--pure", "--format", "json", "--dir", str(root),
            "--title", "Synthetic Thunderbird MCP test"]
    if opencode_model:
        args += ["--model", opencode_model]
    return args + [PROMPT], env


def _tool_calls(events: list[dict]) -> set[str]:
    calls = set()
    claude_calls = {}
    for event in events:
        item = event.get("item", {})
        if event.get("type") == "item.completed" and item.get("type") == "mcp_tool_call":
            if not item.get("error") and item.get("status") == "completed":
                calls.add(item.get("tool", ""))
        if event.get("type") == "tool_use":
            part = event.get("part", {})
            if part.get("state", {}).get("status") == "completed":
                calls.add(part.get("tool", ""))
        if event.get("type") == "assistant":
            for block in event.get("message", {}).get("content", []):
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    claude_calls[block.get("id")] = block.get("name", "")
        if event.get("type") == "user":
            for block in event.get("message", {}).get("content", []):
                if isinstance(block, dict) and block.get("type") == "tool_result" and not block.get("is_error"):
                    name = claude_calls.get(block.get("tool_use_id"))
                    if name:
                        calls.add(name)
    return {name.removeprefix("mcp__thunderbird__").removeprefix("thunderbird_") for name in calls}


def run(client: str, timeout: int, opencode_model: str | None, report_dir: Path | None) -> dict:
    if not shutil.which(client):
        return {"client": client, "ok": False, "error": "Client executable is not installed"}
    with tempfile.TemporaryDirectory(prefix="mail-client-test-") as temporary:
        root = Path(temporary)
        server, code, reference = fixture(root)
        args, env = command(client, root, server, opencode_model)
        started = time.monotonic()
        process = subprocess.Popen(args, cwd=root, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, start_new_session=True)
        timed_out = False
        try:
            output, errors = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGTERM)
            try:
                output, errors = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                output, errors = process.communicate()
        events = []
        for line in output.splitlines():
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict):
                events.append(item)
        calls = _tool_calls(events)
        final_text = "\n".join(json.dumps(e, ensure_ascii=False) for e in events if
            e.get("type") in ("text", "result") or
            e.get("item", {}).get("type") == "agent_message")
        ok = (not timed_out and process.returncode == 0 and set(TOOLS) <= calls
              and code in final_text and reference in final_text and "MAIL_SMOKE_OK" in final_text)
        result = {"client": client, "ok": ok, "exit_code": process.returncode,
                  "timed_out": timed_out, "seconds": round(time.monotonic() - started, 1),
                  "tools_called": sorted(calls), "verification_code_returned": code in final_text,
                  "reference_returned": reference in final_text}
        if report_dir:
            report_dir.mkdir(parents=True, exist_ok=True)
            for suffix, content in (("jsonl", output), ("stderr.log", errors)):
                path = report_dir / f"{client}.{suffix}"
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as stream:
                    stream.write(content)
            result["logs"] = str(report_dir / f"{client}.jsonl")
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clients", nargs="+", choices=("codex", "claude", "opencode"),
                        default=["codex", "claude", "opencode"])
    parser.add_argument("--opencode-model", help="Exact provider/model to test; default is the user's configured model")
    parser.add_argument("--timeout", type=int, default=120, help="Seconds allowed per client")
    parser.add_argument("--report-dir", type=Path, help="Optional private raw logs; keep outside Git")
    args = parser.parse_args()
    if args.timeout < 1:
        parser.error("--timeout must be positive")
    clients = list(dict.fromkeys(args.clients))
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(clients)) as pool:
        futures = [pool.submit(run, client, args.timeout, args.opencode_model, args.report_dir) for client in clients]
        results = []
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            results.append(result)
            print(json.dumps(result), flush=True)
    return 0 if all(result["ok"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
