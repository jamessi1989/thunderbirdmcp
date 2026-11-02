"""Enforced capabilities for tool dispatch, operations and service contexts.

Profiles control effects, not advisory MCP annotations. Operation implementations
must call ``require`` at the effect boundary, including background workers.
Tool allowlists are checked against the outer user-visible tool: a permitted
reply may invoke the shared draft helper without separately exposing that helper.
"""

from __future__ import annotations

import contextvars
import functools
import inspect
import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote, urlsplit

from mcp.server.fastmcp import FastMCP


CAPABILITIES = frozenset({"read", "draft", "mail-write", "send", "file-write", "index", "configure"})
PROFILES = {
    "read-only": frozenset({"read"}),
    "draft-only": frozenset({"read", "draft"}),
    "full": CAPABILITIES,
}
TOOL_CAPABILITIES = {
    **dict.fromkeys(("mail_status", "list_accounts", "search_email", "search_live_email",
                    "get_email", "get_thread", "get_email_reference", "list_attachments",
                    "get_attachment_text", "list_folders", "imap_list_folders", "get_draft",
                    "list_drafts", "list_identities", "get_operation", "list_operations",
                    "authentication_evidence", "follow_up_candidates", "list_mail_events",
                    "get_schedule", "list_schedules"), "read"),
    **dict.fromkeys(("draft_email", "draft_reply", "draft_forward", "edit_draft",
                    "discard_draft", "save_draft_to_thunderbird", "reconcile_draft"), "draft"),
    **dict.fromkeys(("create_folder", "move_emails", "undo_move", "update_email_flags",
                    "organize_emails", "manage_folder", "cancel_operation"), "mail-write"),
    **dict.fromkeys(("send_email", "send_draft", "schedule_draft", "cancel_schedule"), "send"),
    **dict.fromkeys(("export_email", "export_attachment", "plan_export", "execute_export"), "file-write"),
    "reindex": "index",
    "search_page": "read", "special_folders": "read", "watch_mail": "read",
    "run_scheduled": "send", "recover_export": "file-write",
    "provider_search": "read", "provider_labels": "read", "provider_update_labels": "mail-write",
    "save_draft_to_provider": "draft", "send_draft_via_provider": "send",
    "oauth_setup_guide": "read", "ocr_status": "read",
}


class PolicyError(PermissionError):
    """An operation is outside the configured capabilities or access scope."""


@dataclass(frozen=True)
class Policy:
    profile: str = "full"
    tools: frozenset[str] | None = None
    accounts: frozenset[str] | None = None
    file_roots: tuple[Path, ...] | None = None
    principal: str | None = None

    def __post_init__(self):
        if self.profile not in PROFILES:
            raise ValueError("TB_MCP_PROFILE must be read-only, draft-only, or full")
        if self.tools is not None:
            object.__setattr__(self, "tools", frozenset(self.tools))
        if self.accounts is not None:
            object.__setattr__(self, "accounts", frozenset(self.accounts))
        if self.file_roots is not None:
            object.__setattr__(self, "file_roots", tuple(Path(p).expanduser().resolve() for p in self.file_roots))


_context = contextvars.ContextVar("thunderbird_policy", default=())
_active_tool = contextvars.ContextVar("thunderbird_active_tool", default=None)
_active_account = contextvars.ContextVar("thunderbird_active_account", default=None)


def _csv(value: str | None) -> frozenset[str] | None:
    return None if value is None else frozenset(item.strip() for item in value.split(",") if item.strip())


def from_environment() -> Policy:
    """Read current process restrictions; an empty allowlist permits no tools."""
    roots = os.environ.get("TB_MCP_FILE_ROOTS")
    return Policy(
        profile=os.environ.get("TB_MCP_PROFILE", "full").strip().lower(),
        tools=_csv(os.environ.get("TB_MCP_TOOLS")),
        accounts=_csv(os.environ.get("TB_MCP_ACCOUNTS")),
        file_roots=None if roots is None else tuple(Path(p) for p in roots.split(os.pathsep) if p),
    )


def policies() -> tuple[Policy, ...]:
    return (from_environment(), *_context.get())


def current() -> Policy:
    """Return the effective policy. Contexts can only narrow process policy."""
    items = policies()
    profile = min((p.profile for p in items), key=lambda p: len(PROFILES[p]))
    tools = accounts = None
    roots = None
    principal = None
    for item in items:
        if item.tools is not None:
            tools = item.tools if tools is None else tools & item.tools
        if item.accounts is not None:
            accounts = item.accounts if accounts is None else accounts & item.accounts
        if item.file_roots is not None:
            if roots is None:
                roots = item.file_roots
            else:
                roots = tuple(dict.fromkeys(
                    child for left in roots for right in item.file_roots
                    for child, parent in ((left, right), (right, left))
                    if child == parent or parent in child.parents))
        if item.principal is not None:
            if principal is not None and principal != item.principal:
                raise PolicyError("A nested context cannot change its authenticated principal")
            principal = item.principal
    return Policy(profile, tools, accounts, roots, principal)


@contextmanager
def context(policy: Policy):
    """Add request/service restrictions without weakening enclosing contexts."""
    token = _context.set((*_context.get(), policy))
    try:
        current()  # Validate principal consistency before any operation begins.
        yield
    finally:
        _context.reset(token)


def require(capability: str, *, tool: str | None = None, account: str | None = None,
            paths: Iterable[str | Path] = ()) -> None:
    """Authorize an operation immediately before accessing data or causing effects.

    With an account scope, callers must supply the *resolved* account ID. A
    missing ID is rejected rather than interpreted as the default/all accounts.
    File roots apply to source attachments as well as export destinations.
    """
    if capability not in CAPABILITIES:
        raise ValueError("Unknown policy capability")
    policy = current()
    _require_tool(policy, capability, _active_tool.get() or tool)
    account = account if account is not None else _active_account.get()
    if policy.accounts is not None and account not in policy.accounts:
        raise PolicyError("A permitted, resolved account is required for this operation")
    if policy.file_roots is not None:
        for raw in paths:
            path = Path(raw).expanduser().resolve()
            if not any(path == root or root in path.parents for root in policy.file_roots):
                raise PolicyError("The file path is outside the configured file roots")


def _require_tool(policy: Policy, capability: str, name: str | None) -> None:
    if capability not in PROFILES[policy.profile]:
        raise PolicyError(f"The {policy.profile} profile does not permit {capability} operations")
    if policy.tools is not None and (name is None or name not in policy.tools):
        raise PolicyError("The tool is not in TB_MCP_TOOLS or the request allowlist")


def allowed(tool: str, capability: str | None = None) -> bool:
    try:
        _require_tool(current(), capability or TOOL_CAPABILITIES.get(tool, "mail-write"), tool)
        return True
    except PolicyError:
        return False


def _argument_scope(arguments: dict) -> tuple[str | None, list[str | Path]]:
    account = arguments.get("account")
    reference = arguments.get("reference")
    if isinstance(reference, str) and reference.startswith(("thunderbird://imap/", "thunderbird://gmail/", "thunderbird://graph/")):
        encoded_account = urlsplit(reference).path.strip("/").split("/", 1)[0]
        reference_account = unquote(encoded_account)
        if account is not None and account != reference_account:
            raise PolicyError("The selected account does not match the message reference")
        account = reference_account
    # Resolve persisted draft ownership from its frozen account snapshot before
    # entering the public call. Read-only SQLite access cannot create an outbox.
    if account is None and arguments.get("draft_id") and current().accounts is not None:
        import json
        import sqlite3
        from . import outgoing
        path = outgoing.outbox_path()
        if path.is_file():
            conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                row = conn.execute("SELECT config FROM drafts WHERE id=?", (arguments["draft_id"],)).fetchone()
                if row:
                    account = json.loads(row[0])["account_id"]
            finally:
                conn.close()
    if account is None and current().accounts is not None and (reference is not None or arguments.get("id") is not None):
        from . import index, messages, outgoing
        try:
            if reference is not None:
                messages._key(reference)
        except ValueError:
            raise PolicyError("The local reference cannot be resolved within the permitted account scope") from None
        conn = index.connect(read_only=True)
        try:
            row = messages._resolve(conn, reference if reference is not None else arguments["id"])
            account = outgoing.infer_account(row["folder"])
        finally:
            conn.close()
    if account is None and arguments.get("references"):
        for value in arguments["references"]:
            if isinstance(value, str) and value.startswith("thunderbird://imap/"):
                selected = unquote(urlsplit(value).path.strip("/").split("/", 1)[0])
                if current().accounts is not None and selected not in current().accounts:
                    raise PolicyError("A reference belongs to another account")
                account = account or selected
    if account is None and arguments.get("scopes"):
        for scope in arguments["scopes"]:
            selected = scope.get("account") if isinstance(scope, dict) else None
            if current().accounts is not None and selected not in current().accounts:
                raise PolicyError("Every search scope must name a permitted account")
            account = account or selected
    if account is None and current().accounts is not None:
        import json
        import sqlite3
        from . import index
        for field, table, override, filename, principal_column in (
            ("operation_id", "operations", "TB_OPERATIONS_DB", "mail-operations.sqlite", "principal"),
            ("schedule_id", "schedules", "TB_AUTOMATION_DB", "mail-automation.sqlite", "principal"),
            ("plan_id", "plans", "TB_EXPORTS_DB", "export-plans.sqlite", "principal"),
        ):
            if not arguments.get(field):
                continue
            if field == "operation_id":
                from . import operations
                path = operations.default_db_path()
            else:
                path = Path(os.environ.get(override, str(index.default_db_path().parent / filename))).expanduser()
            if not path.is_file():
                continue
            conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                row = conn.execute(f"SELECT account,{principal_column} FROM {table} WHERE id=?", (arguments[field],)).fetchone()
                if row and row[1] == current().principal:
                    account = row[0]
            finally:
                conn.close()
    paths = []
    for name in ("directory", "path"):
        if arguments.get(name) is not None:
            paths.append(arguments[name])
    paths.extend(arguments.get("attachments") or [])
    for attachment in arguments.get("inline_attachments") or []:
        if isinstance(attachment, dict) and attachment.get("path"):
            paths.append(attachment["path"])
    return account, paths


def guard(fn, name: str, capability: str):
    """Wrap a public tool without changing its MCP schema or direct-call API."""
    signature = inspect.signature(fn)

    @contextmanager
    def authorize(args, kwargs):
        values = signature.bind(*args, **kwargs)
        values.apply_defaults()
        selected_policy = current()
        if (selected_policy.accounts is not None and len(selected_policy.accounts) == 1
                and "account" in values.arguments and values.arguments["account"] is None
                and not values.arguments.get("reference") and values.arguments.get("id") is None):
            values.arguments["account"] = next(iter(selected_policy.accounts))
        account, paths = _argument_scope(values.arguments)
        if name in {"list_accounts", "oauth_setup_guide", "ocr_status"} and account is None and selected_policy.accounts:
            account = next(iter(selected_policy.accounts))
        # Every public call is checked independently, even nested Python calls.
        _require_tool(current(), capability, name)
        token = _active_tool.set(name)
        account_token = _active_account.set(account)
        try:
            if name in {"export_email", "export_attachment"} and values.arguments.get("destination"):
                paths.append(values.arguments["destination"])
            require(capability, account=account, paths=paths)
            yield values
        finally:
            _active_tool.reset(token)
            _active_account.reset(account_token)

    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def wrapped(*args, **kwargs):
            with authorize(args, kwargs) as values:
                return await fn(*values.args, **values.kwargs)
    else:
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            with authorize(args, kwargs) as values:
                return fn(*values.args, **values.kwargs)
    return wrapped


class PolicyFastMCP(FastMCP):
    """Apply the same checks to discovery, protocol dispatch and direct calls."""

    def __init__(self, *args, **kwargs):
        self._policy_capabilities = {}
        super().__init__(*args, **kwargs)

    def tool(self, name=None, *, capability: str | None = None, **kwargs):
        if callable(name):
            raise TypeError("Use @mcp.tool(), including parentheses")

        def decorate(fn):
            tool_name = name or fn.__name__
            selected = capability or TOOL_CAPABILITIES.get(tool_name, "mail-write")
            if selected not in CAPABILITIES:
                raise ValueError("Unknown policy capability")
            self._policy_capabilities[tool_name] = selected
            wrapped = guard(fn, tool_name, selected)
            super(PolicyFastMCP, self).tool(name=name, **kwargs)(wrapped)
            return wrapped
        return decorate

    async def list_tools(self):
        return [tool for tool in await super().list_tools()
                if allowed(tool.name, self._policy_capabilities.get(tool.name))]

    async def call_tool(self, name, arguments):
        _require_tool(current(), self._policy_capabilities.get(name, "mail-write"), name)
        return await super().call_tool(name, arguments)

    async def read_resource(self, uri):
        reference = str(uri)
        name = "mail_status" if reference == "thunderbird://status" else "get_email"
        _require_tool(current(), "read", name)
        return await super().read_resource(uri)
