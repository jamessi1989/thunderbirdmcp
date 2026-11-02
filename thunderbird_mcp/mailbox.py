"""Explicit, bounded IMAP mutations using account-bound live references.

Never fall back to sequence numbers or global EXPUNGE. A lost response is an
uncertain result and is not retried. COPYUID receipts provide safe move undo.
"""

from __future__ import annotations

import re
from . import imap, live, policy, operations

MAX_BATCH = 100


def _references(references: list[str]) -> list[live.Locator]:
    if not isinstance(references, list) or not 1 <= len(references) <= MAX_BATCH:
        raise ValueError(f"references must contain 1 to {MAX_BATCH} live references")
    result = [live.parse_reference(value) for value in dict.fromkeys(references)]
    if len({loc.account for loc in result}) != 1:
        raise ValueError("A mutation batch must belong to one account")
    return result


def _begin(kind, locators, dry_run):
    if dry_run:
        return None
    operation = operations.create_operation(kind, account=locators[0].account, total=len(locators))
    operations.claim_operation(operation["operation_id"])
    return operation["operation_id"]


def _result(dry_run, results, operation_id):
    complete = all(r["status"] in {"preview", "completed"} for r in results)
    output = {"dry_run": dry_run, "results": results, "complete": complete}
    if operation_id:
        output["operation_id"] = operation_id
        status = "completed" if complete else "uncertain" if any(r["status"] == "uncertain" for r in results) else "partial" if any(r["status"] == "completed" for r in results) else "failed"
        if operations.cancellation_requested(operation_id):
            if status != "uncertain":
                status = "partial" if any(r["status"] == "completed" for r in results) else "cancelled"
            output["complete"] = False
            output["cancellation_requested"] = True
        operations.finish_operation(operation_id, status=status, result=output)
    return output


def _caps(connection) -> set[str]:
    return set(b" ".join(imap._check(connection.capability(), "CAPABILITY")).decode("ascii").upper().split())


def _select(connection, locator: live.Locator, *, writable: bool) -> list[str]:
    imap._check(connection.select(imap._q(locator.folder), readonly=not writable), "SELECT")
    _, values = connection.response("UIDVALIDITY")
    value = values[0] if values else None
    if not isinstance(value, bytes) or value != str(locator.uidvalidity).encode():
        raise LookupError("UIDVALIDITY changed or missing; run a new live search")
    data = imap._check(connection.uid("FETCH", str(locator.uid), "(UID FLAGS)"), "FETCH flags")
    for row in data:
        raw = row[0] if isinstance(row, tuple) else row
        if not isinstance(raw, bytes):
            continue
        uid = re.search(rb"\bUID (\d+)\b", raw)
        flags = re.search(rb"\bFLAGS \(([^)]*)\)", raw)
        if uid and int(uid[1]) == locator.uid and flags:
            return flags[1].decode("ascii", errors="replace").split()
    raise LookupError("Referenced message no longer exists in this folder")


def _folders(connection) -> list[dict]:
    result = []
    for line in imap._check(connection.list(), "LIST"):
        if not isinstance(line, bytes):
            continue
        match = imap._LIST_RE.match(line.decode("utf-8", errors="replace"))
        if not match:
            continue
        name = match["name"]
        if name.startswith('"') and name.endswith('"'):
            name = re.sub(r"\\(.)", r"\1", name[1:-1])
        flags = match["flags"].split()
        if "\\noselect" not in [f.lower() for f in flags]:
            result.append({"folder": imap.decode_folder(name), "attributes": flags,
                           "delimiter": None if match["delim"] == "NIL" else match["delim"].strip('"')})
    return result


def special_folders(account: str | None = None) -> dict:
    from .outgoing import account_config
    config = account_config(account)
    policy.require("read", tool="special_folders", account=config["account_id"])
    with live._session(config) as connection:
        folders = _folders(connection)
    special = {}
    for item in folders:
        for attribute in item["attributes"]:
            key = attribute.lstrip("\\").lower()
            if key in {"archive", "all", "trash", "drafts", "sent", "junk"}:
                special.setdefault(key, []).append(item["folder"])
    return {"account_id": config["account_id"], "special": special, "folders": folders}


def _keyword(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9$][A-Za-z0-9$_.-]{0,63}", value):
        raise ValueError("Keywords must be 1–64 ASCII letters, digits, $, _, . or -")
    return value


def update_email_flags(references: list[str], *, read: bool | None = None,
                       flagged: bool | None = None, add_keywords: list[str] | None = None,
                       remove_keywords: list[str] | None = None,
                       add_labels: list[str] | None = None, remove_labels: list[str] | None = None,
                       dry_run: bool = True) -> dict:
    locators = _references(references)
    for value in (read, flagged):
        if value is not None and not isinstance(value, bool):
            raise ValueError("read and flagged must be booleans")
    add = [_keyword(v) for v in add_keywords or []]
    remove = [_keyword(v) for v in remove_keywords or []]
    for value, flag in ((read, "\\Seen"), (flagged, "\\Flagged")):
        if value is not None:
            (add if value else remove).append(flag)
    labels_add, labels_remove = add_labels or [], remove_labels or []
    for label in [*labels_add, *labels_remove]:
        live._text(label, "Gmail label", limit=256)
        if label.startswith("\\"):
            raise ValueError("Use archive/trash for Gmail system-label changes")
    if set(add) & set(remove) or set(labels_add) & set(labels_remove):
        raise ValueError("The same flag or label cannot be added and removed")
    if not any((add, remove, labels_add, labels_remove)):
        raise ValueError("Specify a read, flagged, keyword or Gmail-label change")
    for locator in locators:
        policy.require("mail-write", tool="update_email_flags", account=locator.account)
    operation_id = _begin("update_email_flags", locators, dry_run)
    results = []
    for locator in locators:
        if operation_id and operations.cancellation_requested(operation_id):
            break
        policy.require("mail-write", tool="update_email_flags", account=locator.account)
        attempted = False
        item = {"reference": locator.reference, "account_id": locator.account}
        try:
            with live._session(live._config(locator)) as connection:
                before = _select(connection, locator, writable=not dry_run)
                if labels_add or labels_remove:
                    if "X-GM-EXT-1" not in _caps(connection):
                        raise ValueError("Gmail labels require the X-GM-EXT-1 server extension")
                if add_keywords or remove_keywords:
                    _, values = connection.response("PERMANENTFLAGS")
                    permanent = b" ".join(v for v in values or [] if isinstance(v, bytes)).decode("ascii")
                    supported = permanent.strip("()").split()
                    if "\\*" not in supported and any(k not in supported for k in [*(add_keywords or []), *(remove_keywords or [])]):
                        raise ValueError("Server does not permit one or more requested keywords")
                item.update(before_flags=before, add_flags=add, remove_flags=remove,
                            add_labels=labels_add, remove_labels=labels_remove)
                if not dry_run:
                    for sign, values in (("+", add), ("-", remove)):
                        if values:
                            attempted = True
                            imap._check(connection.uid("STORE", str(locator.uid), sign + "FLAGS.SILENT", "(" + " ".join(values) + ")"), "STORE flags")
                    for sign, values in (("+", labels_add), ("-", labels_remove)):
                        if values:
                            attempted = True
                            imap._check(connection.uid("STORE", str(locator.uid), sign + "X-GM-LABELS", "(" + " ".join(imap._q(v) for v in values) + ")"), "STORE labels")
                item["status"] = "preview" if dry_run else "completed"
        except (ValueError, LookupError, RuntimeError, OSError) as exc:
            item.update(status="uncertain" if attempted else "failed", error=type(exc).__name__, detail=str(exc)[:300])
        results.append(item)
        if operation_id:
            operations.update_operation(operation_id, completed=len(results), result={"results": results})
    return _result(dry_run, results, operation_id)


def _destination(connection, action: str, destination: str | None) -> str:
    folders = _folders(connection)
    if action in {"archive", "trash"}:
        attribute = "\\Archive" if action == "archive" else "\\Trash"
        matches = [v["folder"] for v in folders if attribute.lower() in [f.lower() for f in v["attributes"]]]
        if action == "archive" and not matches:
            matches = [v["folder"] for v in folders if "\\all" in [f.lower() for f in v["attributes"]]]
        if destination is None:
            if len(matches) != 1:
                raise ValueError("No unique special-use folder; provide destination explicitly")
            destination = matches[0]
    if destination is None:
        raise ValueError("destination is required")
    imap._q(destination)
    if destination not in [v["folder"] for v in folders]:
        raise LookupError("Destination does not exist; create it first")
    return destination


def _copy_receipt(connection, response, locator, destination) -> str | None:
    candidates = [v for v in response[1] if isinstance(v, bytes)]
    _, values = connection.response("COPYUID")
    candidates.extend(v for v in values or [] if isinstance(v, bytes))
    for raw in candidates:
        match = re.search(rb"(?:COPYUID\s+)?(\d+)\s+(\d+)\s+(\d+)(?:\]|$|\s)", raw)
        if match and int(match[2]) == locator.uid:
            return live.Locator(locator.account, locator.fingerprint, destination,
                                live._number(int(match[1]), "UIDVALIDITY"), live._number(int(match[3]), "UID")).reference
    return None


def organize_emails(references: list[str], action: str, destination: str | None = None,
                    *, dry_run: bool = True, confirm_permanent: bool = False) -> dict:
    if action not in {"move", "copy", "archive", "trash", "restore", "delete"}:
        raise ValueError("action must be move, copy, archive, trash, restore or delete")
    if action == "delete" and not confirm_permanent:
        raise ValueError("Permanent deletion requires confirm_permanent=True; use trash for recoverable removal")
    locators = _references(references)
    for locator in locators:
        policy.require("mail-write", tool="organize_emails", account=locator.account)
    operation_id = _begin("organize_emails", locators, dry_run)
    results = []
    for locator in locators:
        if operation_id and operations.cancellation_requested(operation_id):
            break
        policy.require("mail-write", tool="organize_emails", account=locator.account)
        item = {"reference": locator.reference, "account_id": locator.account, "action": action, "source": locator.folder}
        attempted = False
        try:
            with live._session(live._config(locator)) as connection:
                _select(connection, locator, writable=not dry_run)
                caps = _caps(connection)
                if action == "delete":
                    if "UIDPLUS" not in caps:
                        raise ValueError("Permanent deletion requires UIDPLUS for message-scoped expunge")
                else:
                    dest = _destination(connection, action, destination)
                    item["destination"] = dest
                    if dest == locator.folder:
                        raise ValueError("Source and destination must differ")
                    if action != "copy" and "MOVE" not in caps:
                        raise ValueError("Server lacks atomic MOVE; refusing unsafe copy/delete fallback")
                if not dry_run:
                    attempted = True
                    if action == "delete":
                        imap._check(connection.uid("STORE", str(locator.uid), "+FLAGS.SILENT", "(\\Deleted)"), "STORE Deleted")
                        imap._check(connection.uid("EXPUNGE", str(locator.uid)), "UID EXPUNGE")
                    else:
                        response = connection.uid("COPY" if action == "copy" else "MOVE", str(locator.uid), imap._q(dest))
                        imap._check(response, action.upper())
                        new_reference = _copy_receipt(connection, response, locator, dest)
                        item["destination_reference"] = new_reference
                        if new_reference and action != "copy":
                            item["undo"] = {"references": [new_reference], "action": "restore", "destination": locator.folder}
                        elif not new_reference:
                            item["undo_unavailable"] = "Server supplied no unambiguous COPYUID receipt; search destination before restoring"
                item["status"] = "preview" if dry_run else "completed"
        except (ValueError, LookupError, RuntimeError, OSError) as exc:
            item.update(status="uncertain" if attempted else "failed", error=type(exc).__name__, detail=str(exc)[:300])
        results.append(item)
        if operation_id:
            operations.update_operation(operation_id, completed=len(results), result={"results": results})
    return _result(dry_run, results, operation_id)


def manage_folder(action: str, folder: str, destination: str | None = None,
                  *, account: str | None = None, dry_run: bool = True) -> dict:
    from .outgoing import account_config
    if action not in {"rename", "delete"}:
        raise ValueError("action must be rename or delete; use create_folder to create")
    imap._q(folder)
    if folder.upper() == "INBOX":
        raise ValueError("INBOX cannot be renamed or deleted")
    config = account_config(account)
    policy.require("mail-write", tool="manage_folder", account=config["account_id"])
    with live._session(config) as connection:
        folders = _folders(connection)
        names = [v["folder"] for v in folders]
        if folder not in names:
            raise LookupError("Folder does not exist")
        if any(v["folder"] == folder and any(f.lower() in {"\\sent", "\\drafts", "\\trash", "\\archive", "\\all", "\\junk"} for f in v["attributes"]) for v in folders):
            raise ValueError("Special-use folders cannot be renamed or deleted by this tool")
        if action == "rename":
            imap._q(destination)
            if destination in names:
                raise ValueError("Destination folder already exists")
            if not dry_run:
                imap._check(connection.rename(imap._q(folder), imap._q(destination)), "RENAME")
        else:
            imap._check(connection.select(imap._q(folder), readonly=True), "SELECT")
            matches = imap._check(connection.uid("SEARCH", None, "ALL"), "SEARCH ALL")
            if b" ".join(v for v in matches if isinstance(v, bytes)).strip():
                raise ValueError("Only empty folders can be deleted; move or explicitly delete their messages first")
            delimiter = next(v["delimiter"] for v in folders if v["folder"] == folder)
            if delimiter and any(name.startswith(folder + delimiter) for name in names):
                raise ValueError("Folder has children; remove them explicitly first")
            if not dry_run:
                # CLOSE on a read-only selection cannot expunge mail.
                imap._check(connection.close(), "CLOSE")
                imap._check(connection.delete(imap._q(folder)), "DELETE folder")
    return {"action": action, "folder": folder, "destination": destination,
            "account_id": config["account_id"], "dry_run": dry_run,
            "status": "preview" if dry_run else "completed"}
