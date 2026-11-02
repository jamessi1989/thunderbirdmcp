#!/usr/bin/env python3
"""Run an offline, synthetic-mail demonstration with real tbmail subprocesses.

No mail account, model download, or credentials are required. All mailbox and
draft files live in a temporary directory removed when the demonstration exits.
"""

from __future__ import annotations

import argparse
import html
import io
import json
import mailbox
import os
from pathlib import Path
import subprocess
import sys
import tempfile

PROJECT = Path(__file__).resolve().parents[1]
INVOICE_ID = "<invoice@demo.example.test>"
REPLY_BODY = "Thanks, I checked the invoice. Please confirm the payment date."

# Inherited by each CLI process and its PDF extraction child. This is a demo
# guard, not an OS sandbox; it rejects Python socket networking if a local-only
# demonstration step accidentally starts using a network-dependent API.
NETWORK_GUARD = '''import sys
def _demo_audit(event, arguments):
    if event in {"socket.connect", "socket.getaddrinfo", "socket.gethostbyname", "socket.sendto"}:
        raise RuntimeError("Network access is disabled in the synthetic mail demo")
sys.addaudithook(_demo_audit)
'''


def environment(root: Path) -> dict[str, str]:
    """Construct a fresh environment; never inherit TB_*, credentials or HOME."""
    home = root / "home"
    guard = root / "guard"
    for path in (home, guard, root / "tmp", root / "cache", root / "config", root / "data"):
        path.mkdir(parents=True, exist_ok=True)
    (guard / "sitecustomize.py").write_text(NETWORK_GUARD, encoding="utf-8")
    env = {
        "PATH": os.pathsep.join((str(Path(sys.executable).parent), os.defpath)),
        "HOME": str(home), "USERPROFILE": str(home),
        "XDG_CACHE_HOME": str(root / "cache"), "XDG_CONFIG_HOME": str(root / "config"),
        "XDG_DATA_HOME": str(root / "data"),
        "TMPDIR": str(root / "tmp"), "TMP": str(root / "tmp"), "TEMP": str(root / "tmp"),
        "PYTHONPATH": os.pathsep.join((str(guard), str(PROJECT))),
        "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
        "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring",
        "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_HOME": str(root / "cache" / "huggingface"),
        "TB_PROFILE": str(root / "profile"), "TB_MAIL_INDEX_DB": str(root / "index.sqlite"),
        "TB_OUTBOX_DB": str(root / "outbox.sqlite"),
    }
    # Python on Windows needs its OS installation directory. No user settings
    # or credential-bearing environment variables cross the demo boundary.
    for key in ("SystemRoot", "WINDIR"):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def invoice_pdf() -> bytes:
    """Create a small selectable-text PDF; the invoice code appears only here."""
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=200)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({
        NameObject("/F1"): writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 20 150 Td (Invoice INV-2049 / Aurora rollout / Total EUR 1240.00) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def write_mailbox(root: Path) -> None:
    from email.message import EmailMessage

    profile = root / "profile"
    account = profile / "ImapMail" / "mail.demo.example.test"
    account.mkdir(parents=True)
    prefs = {
        "mail.accountmanager.accounts": "account1", "mail.accountmanager.defaultaccount": "account1",
        "mail.account.account1.server": "server1", "mail.account.account1.identities": "id1",
        "mail.server.server1.type": "imap", "mail.server.server1.hostname": "mail.demo.example.test",
        "mail.server.server1.userName": "builder@example.test", "mail.server.server1.port": 993,
        "mail.server.server1.socketType": 3,
        "mail.identity.id1.useremail": "builder@example.test", "mail.identity.id1.fullName": "Demo Builder",
        "mail.identity.id1.smtpServer": "smtp1", "mail.smtpserver.smtp1.hostname": "mail.demo.example.test",
        "mail.smtpserver.smtp1.username": "builder@example.test", "mail.smtpserver.smtp1.port": 465,
        "mail.smtpserver.smtp1.try_ssl": 3,
    }
    (profile / "prefs.js").write_text("".join(
        f"user_pref({json.dumps(key)}, {json.dumps(value)});\n" for key, value in prefs.items()), encoding="utf-8")

    def message(mid, subject, body, *, sender="Billing <billing@example.test>", minute=0):
        item = EmailMessage()
        item["Message-ID"], item["From"], item["To"] = mid, sender, "Demo Builder <builder@example.test>"
        item["Subject"] = subject
        item["Date"] = f"Mon, 02 Nov 2026 09:{minute:02d}:00 +0000"
        item.set_content(body)
        return item

    invoice = message(INVOICE_ID, "Invoice for the Aurora rollout", "The invoice is attached. Please confirm the payment date.")
    invoice.add_attachment(invoice_pdf(), maintype="application", subtype="pdf", filename="invoice.pdf")
    reply = message("<reply@demo.example.test>", "Re: Invoice for the Aurora rollout",
                    "Thanks. I will check the totals before confirming.", sender="Demo Builder <builder@example.test>", minute=5)
    reply.replace_header("To", "Billing <billing@example.test>")
    reply["In-Reply-To"], reply["References"] = INVOICE_ID, INVOICE_ID
    unrelated = message("<bulletin@demo.example.test>", "Routine maintenance", "The nightly checks passed.", minute=10)
    box = mailbox.mbox(account / "INBOX")
    try:
        for item in (invoice, reply, unrelated):
            box.add(item)
        box.flush()
    finally:
        box.close()
    (account / "INBOX.msf").touch()


def tbmail(root: Path, env: dict[str, str], *arguments: str) -> dict:
    if not arguments or arguments[0] not in {"reindex", "search", "read", "thread", "attachment-text", "reply", "send"}:
        raise ValueError("The demo only permits its local mail commands")
    if any(flag in arguments for flag in ("--confirm", "--live", "--network", "--embeddings")):
        raise ValueError("Delivery and network operations are disabled in the demo")
    command = [sys.executable, "-m", "thunderbird_mcp.cli", "--json", *arguments]
    completed = subprocess.run(command, env=env, cwd=root, capture_output=True, text=True,
                               encoding="utf-8", timeout=60, check=False)
    if completed.returncode:
        # Do not embed process environment or arbitrary parser stderr in assets.
        raise RuntimeError(f"Demo command {arguments[0]!r} failed (exit {completed.returncode})")
    result = json.loads(completed.stdout)
    if result.get("error"):
        raise RuntimeError(f"Demo command {arguments[0]!r} returned an error")
    return result


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError("Demo verification failed: " + message)


def run() -> dict:
    with tempfile.TemporaryDirectory(prefix="thunderbird-demo-") as temporary:
        root = Path(temporary).resolve()
        env = environment(root)
        write_mailbox(root)
        tbmail(root, env, "reindex")
        found = tbmail(root, env, "search", "--keyword", "INV-2049")
        require(found.get("count") == 1, "the invoice query must match exactly one message")
        hit = found["results"][0]
        require(hit.get("match_source") == "attachment", "the code must be found inside the PDF")
        require(found.get("diagnostics", {}).get("semantic") == "disabled", "the demo must not load a model")
        reference = hit["reference"]
        require(reference.startswith("thunderbird://message/"), "search must return a durable local URI")

        # Every call starts a new CLI process, so no in-memory message state can
        # carry the reference, thread, or draft across these steps.
        original = tbmail(root, env, "read", reference)
        thread = tbmail(root, env, "thread", reference)
        document = tbmail(root, env, "attachment-text", reference, "1")
        draft = tbmail(root, env, "reply", reference, "--body", REPLY_BODY)
        preview = tbmail(root, env, "send", draft["draft_id"])
        require(original.get("reference") == reference and original.get("message_id") == INVOICE_ID,
                "the reference must resolve in a different process")
        require(thread.get("count") == 2 and thread.get("match_method") == "message_headers",
                "the conversation must follow real reply headers")
        require(document.get("status") == "extracted" and "INV-2049" in document.get("text", ""),
                "the generated PDF must contain extractable invoice text")
        require(draft.get("status") == preview.get("status") == "prepared" and preview.get("preview") is True,
                "the saved draft must remain an unsent preview")
        require(preview.get("in_reply_to") == INVOICE_ID and INVOICE_ID in preview.get("references", []),
                "the reply must retain original threading")
        require(preview.get("to") == ["billing@example.test"] and preview.get("body") == REPLY_BODY,
                "the preview must retain the intended recipient and exact body")
        require("delivery" not in preview, "a preview must not contain delivery activity")

        # Select actual result fields; omit random draft IDs, timestamps, host
        # paths and PDF hashes so documentation assets reproduce byte-for-byte.
        result = {
            "verified": True, "synthetic_messages": 3, "cli_processes": 7,
            "reference": reference, "search": {"count": found["count"], "match_source": hit["match_source"]},
            "message": {key: original[key] for key in ("from", "subject", "body", "message_id")},
            "thread": {"count": thread["count"], "match_method": thread["match_method"]},
            "attachment": {"filename": document["attachment"]["filename"], "status": document["status"], "text": document["text"]},
            "draft": {key: preview[key] for key in ("status", "preview", "to", "subject", "body", "in_reply_to", "references")},
        }
    return result


def transcript(result: dict) -> list[str]:
    """A compact projection of verified CLI JSON, not fabricated agent output."""
    return [
        "THUNDERBIRD MCP / OFFLINE MAIL DEMO", "Synthetic mailbox. Actual tbmail processes. No model downloads.", "",
        "$ tbmail search --keyword INV-2049 --json",
        f"{result['search']['count']} match / source: {result['search']['match_source']} / {result['attachment']['filename']}",
        result["message"]["subject"],
        'REF="' + result["reference"] + '"', "",
        "$ tbmail read \"$REF\" --json    # a new terminal process",
        "From: " + result["message"]["from"], result["message"]["body"].strip(), "",
        "$ tbmail thread \"$REF\" --json",
        f"{result['thread']['count']} messages / linked by Message-ID and reply headers", "",
        "$ tbmail attachment-text \"$REF\" 1 --json",
        result["attachment"]["filename"] + " / " + result["attachment"]["status"], result["attachment"]["text"], "",
        'REPLY="' + result["draft"]["body"] + '"',
        "$ tbmail reply \"$REF\" --body \"$REPLY\" --json",
        'DRAFT="<draft-id>" / status: ' + result["draft"]["status"],
        "To: " + ", ".join(result["draft"]["to"]), "Subject: " + result["draft"]["subject"], "",
        "$ tbmail send \"$DRAFT\" --json    # preview; no --confirm",
        "preview: " + str(result["draft"]["preview"]).lower() + " / status: " + result["draft"]["status"],
        result["draft"]["body"], "",
        "Verified: PDF search -> durable reference -> thread -> unsent reply.",
        "Selected JSON fields shown; random draft ID normalized. Temporary mail removed.",
    ]


def svg(lines: list[str]) -> str:
    width, line_height, padding = 1040, 24, 28
    height = 70 + len(lines) * line_height + padding
    elements = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title description">',
        '<title id="title">Thunderbird MCP: search a PDF and prepare an unsent reply</title>',
        '<desc id="description">Verified synthetic demo using separate tbmail processes. This is a compact view of actual JSON results with the random draft identifier normalized.</desc>',
        f'<rect width="{width}" height="{height}" rx="14" fill="#0c1220"/>',
        f'<path d="M14 0 H{width - 14} Q{width} 0 {width} 14 V48 H0 V14 Q0 0 14 0" fill="#182235"/>',
        '<circle cx="26" cy="24" r="6" fill="#fb7185"/><circle cx="46" cy="24" r="6" fill="#fbbf24"/><circle cx="66" cy="24" r="6" fill="#4ade80"/>',
        '<text x="98" y="30" fill="#cbd5e1" font-family="ui-monospace, Menlo, Consolas, monospace" font-size="14">python scripts/demo.py</text>',
        '<g font-family="ui-monospace, Menlo, Consolas, monospace" font-size="14">',
    ]
    for number, line in enumerate(lines):
        clean = "".join(char for char in line if ord(char) >= 32 and not 127 <= ord(char) <= 159)
        color = "#67e8f9" if clean.startswith("$") else "#86efac" if clean.startswith(("Verified:", "THUNDERBIRD", "preview:")) else "#dbe4f0"
        if number == len(lines) - 1:
            color = "#94a3b8"
        elements.append(f'<text x="{padding}" y="{80 + number * line_height}" fill="{color}">{html.escape(clean)}</text>')
    elements.extend(("</g>", "</svg>", ""))
    return "\n".join(elements)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Print deterministic verified result fields")
    parser.add_argument("--svg", type=Path, help="Write a deterministic terminal preview to this .svg file")
    args = parser.parse_args(argv)
    if args.svg and args.svg.suffix.lower() != ".svg":
        parser.error("The preview destination must end in .svg")
    try:
        result = run()
        lines = transcript(result)
        if args.svg:
            args.svg.parent.mkdir(parents=True, exist_ok=True)
            args.svg.write_text(svg(lines), encoding="utf-8")
        print(json.dumps(result, indent=2) if args.json else "\n".join(lines))
        return 0
    except (ImportError, OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(f"Demo failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
