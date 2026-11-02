"""Bounded, local attachment text extraction. Files are never executed or uploaded."""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import PurePosixPath
import re
import subprocess
import sys
import zipfile
from html.parser import HTMLParser

MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024
MAX_MESSAGE_BYTES = 24 * 1024 * 1024
MAX_EXPANDED_BYTES = 16 * 1024 * 1024
MAX_ZIP_ENTRIES = 2048
MAX_ATTACHMENT_CHARS = 25_000
MAX_MESSAGE_CHARS = 100_000
MAX_ATTACHMENTS = 64
MAX_PDF_PAGES = 50
PDF_TIMEOUT_SECONDS = 10

_TEXT_SUFFIXES = {".txt", ".csv", ".tsv", ".json", ".md", ".log", ".yaml", ".yml", ".ics"}
_OOXML_TYPES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
}


class ExtractionLimit(ValueError):
    """A document exceeded a fixed extraction limit."""


class EncryptedDocument(ValueError):
    """Encrypted attachments require opening explicitly in another application."""


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "head"}:
            self.skip += 1
        if tag in {"p", "br", "div", "li", "tr"}:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "head"} and self.skip:
            self.skip -= 1
        self.parts.append(" ")

    def handle_data(self, value):
        if not self.skip:
            self.parts.append(value)


def _plain(value: str) -> str:
    return " ".join(re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", value).split())


def _decode(payload: bytes, charset: str | None) -> str:
    if payload.startswith((b"\xff\xfe", b"\xfe\xff")):
        return payload.decode("utf-16", "replace")
    try:
        return payload.decode(charset or "utf-8-sig", "replace")
    except LookupError:
        return payload.decode("utf-8", "replace")


def _xml(payload: bytes):
    from defusedxml.ElementTree import fromstring
    return fromstring(payload, forbid_dtd=True, forbid_entities=True, forbid_external=True)


def _office(payload: bytes, suffix: str) -> str:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_ZIP_ENTRIES:
            raise ExtractionLimit("Archive has too many entries")
        if any(entry.flag_bits & 1 for entry in entries):
            raise EncryptedDocument("Encrypted archive")
        if sum(entry.file_size for entry in entries) > MAX_EXPANDED_BYTES:
            raise ExtractionLimit("Archive exceeds expanded byte limit")
        if any(entry.file_size > MAX_EXPANDED_BYTES for entry in entries):
            raise ExtractionLimit("Archive entry exceeds byte limit")
        # Read only document XML. No files, relationships, links, or macros run.
        names = set(archive.namelist())
        if suffix == ".docx":
            selected = sorted(name for name in names if re.fullmatch(
                r"word/(document|header\d+|footer\d+|footnotes|endnotes|comments)\.xml", name))
        elif suffix == ".pptx":
            selected = sorted(name for name in names if re.fullmatch(
                r"ppt/(slides/slide\d+|notesSlides/notesSlide\d+)\.xml", name))
        else:
            selected = sorted(name for name in names if re.fullmatch(
                r"xl/(sharedStrings|worksheets/sheet\d+|comments\d+)\.xml", name))
        if not selected:
            raise ValueError("Document XML missing")
        parts, chars, expanded = [], 0, 0
        for name in selected:
            # ZipFile checks the central-directory length, and a bounded read
            # also protects against inconsistent or malformed declarations.
            with archive.open(name) as member:
                data = member.read(MAX_EXPANDED_BYTES - expanded + 1)
            expanded += len(data)
            if expanded > MAX_EXPANDED_BYTES:
                raise ExtractionLimit("Document exceeds expanded byte limit")
            root = _xml(data)
            # Ignore spreadsheet formulas; retain their cached values and text.
            tags = {"t", "v"} if suffix == ".xlsx" else {"t"}
            for node in root.iter():
                if node.tag.rsplit("}", 1)[-1] in tags and node.text:
                    parts.append(node.text)
                    chars += len(node.text) + 1
                    if chars > MAX_ATTACHMENT_CHARS:
                        return " ".join(parts)
        return " ".join(parts)


def _pdf_document(payload: bytes) -> dict:
    """Run only in the bounded child process below."""
    from pypdf import PdfReader
    from pypdf import filters
    # pypdf also bounds its Flate decompressor; our process limits contain
    # resource use in other PDF encodings and damaged object graphs.
    filters.ZLIB_MAX_OUTPUT_LENGTH = MAX_EXPANDED_BYTES
    reader = PdfReader(io.BytesIO(payload), strict=False)
    if reader.is_encrypted:
        raise EncryptedDocument("Encrypted PDF")
    parts, size = [], 0
    truncated = len(reader.pages) > MAX_PDF_PAGES
    for page in reader.pages[:MAX_PDF_PAGES]:
        contents = page.get_contents()
        if contents is not None and len(contents.get_data()) > MAX_EXPANDED_BYTES:
            raise ExtractionLimit("PDF page exceeds expanded byte limit")
        value = page.extract_text() or ""
        parts.append(value)
        size += len(value)
        if size > MAX_ATTACHMENT_CHARS:
            truncated = True
            break
    return {"text": "\n".join(parts)[:MAX_ATTACHMENT_CHARS + 1], "truncated": truncated}


def _pdf(payload: bytes) -> dict:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "thunderbird_mcp.attachments", "--pdf-worker"],
            input=payload, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=PDF_TIMEOUT_SECONDS, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ExtractionLimit("PDF extraction timed out") from exc
    if result.returncode != 0:
        raise ExtractionLimit("PDF extraction exceeded process resource limits")
    data = json.loads(result.stdout)
    if data.get("status") == "encrypted":
        raise EncryptedDocument("Encrypted PDF")
    if data.get("status") == "too_large":
        raise ExtractionLimit("PDF exceeds extraction limits")
    if data.get("status") != "extracted":
        raise ValueError("PDF could not be parsed")
    return data


def extract_text(payload: bytes, filename: str = "", content_type: str = "", *,
                 charset: str | None = None) -> dict:
    """Return bounded text and a redacted, machine-readable extraction status."""
    result = {"text": "", "status": "unsupported", "truncated": False}
    if len(payload) > MAX_ATTACHMENT_BYTES:
        return {**result, "status": "too_large", "reason": "attachment_byte_limit"}
    suffix = PurePosixPath(filename.lower()).suffix
    ctype = content_type.lower().split(";", 1)[0]
    try:
        truncated = False
        if suffix == ".pdf" or ctype == "application/pdf":
            pdf = _pdf(payload)
            value, truncated = pdf["text"], pdf["truncated"]
            if not value.strip():
                from . import ocr
                if ocr.enabled():
                    return ocr.extract_text(payload, pdf=True)
        elif suffix in {".docx", ".xlsx", ".pptx"} or ctype in _OOXML_TYPES:
            value = _office(payload, _OOXML_TYPES.get(ctype, suffix))
        elif suffix in {".html", ".htm"} or ctype == "text/html":
            parser = _HTMLText()
            parser.feed(_decode(payload, charset))
            value = " ".join(parser.parts)
        elif suffix == ".xml" or ctype in {"text/xml", "application/xml"} or ctype.endswith("+xml"):
            value = " ".join(_xml(payload).itertext())
        elif suffix in _TEXT_SUFFIXES or ctype.startswith("text/") or ctype in {"application/json", "application/ld+json"}:
            value = _decode(payload, charset)
        elif suffix in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".webp"} or ctype in {
                "image/png", "image/jpeg", "image/tiff", "image/bmp", "image/gif", "image/webp"}:
            from . import ocr
            return ocr.extract_text(payload)
        else:
            return {**result, "reason": "format_not_supported"}
        value = _plain(value)
        return {"text": value[:MAX_ATTACHMENT_CHARS], "status": "extracted" if value else "empty",
                "truncated": truncated or len(value) > MAX_ATTACHMENT_CHARS,
                **({"reason": "no_extractable_text_or_scanned_document"} if not value else {})}
    except EncryptedDocument:
        return {**result, "status": "encrypted", "reason": "password_required"}
    except ExtractionLimit:
        return {**result, "status": "too_large", "reason": "extraction_resource_limit"}
    except Exception:
        # Parser errors can contain document text or local paths. Keep them
        # private while retaining a useful status for the caller.
        return {**result, "status": "error", "reason": "document_could_not_be_parsed"}


def _filename(value: str | None, number: int) -> str:
    value = (value or f"attachment-{number}").replace("\\", "/").rsplit("/", 1)[-1]
    return _plain(value)[:255] or f"attachment-{number}"


def extract_message(msg) -> list[dict]:
    """Extract up to 64 MIME attachments, with shared byte and text budgets."""
    result, used_bytes, used_chars = [], 0, 0
    for part in msg.walk():
        if part.is_multipart() or not (part.get_content_disposition() == "attachment" or part.get_filename()):
            continue
        number = len(result) + 1
        if number > MAX_ATTACHMENTS:
            result.append({"id": number, "filename": "", "content_type": "", "size": None,
                           "text": "", "status": "too_many", "truncated": True,
                           "reason": "message_attachment_limit"})
            break
        filename = _filename(part.get_filename(), number)
        meta = {"id": number, "filename": filename, "content_type": part.get_content_type(),
                "size": None, "text": "", "status": "too_large", "truncated": False}
        raw = part.get_payload()
        # Check before base64 decoding. The email parser already holds encoded
        # content; a second unbounded decoded allocation is unnecessary.
        if not isinstance(raw, str) or len(raw) > MAX_ATTACHMENT_BYTES * 2:
            result.append({**meta, "reason": "attachment_byte_limit"})
            continue
        if used_bytes >= MAX_MESSAGE_BYTES or used_chars >= MAX_MESSAGE_CHARS:
            result.append({**meta, "reason": "message_extraction_limit"})
            continue
        payload = part.get_payload(decode=True) or b""
        meta["size"] = len(payload)
        if len(payload) > MAX_ATTACHMENT_BYTES or used_bytes + len(payload) > MAX_MESSAGE_BYTES:
            result.append({**meta, "reason": "attachment_or_message_byte_limit"})
            continue
        used_bytes += len(payload)
        meta["sha256"] = hashlib.sha256(payload).hexdigest()
        extracted = extract_text(payload, filename, meta["content_type"], charset=part.get_content_charset())
        separator = 1 if used_chars and extracted["text"] else 0
        remaining = max(0, MAX_MESSAGE_CHARS - used_chars - separator)
        if len(extracted["text"]) > remaining:
            extracted.update(text=extracted["text"][:remaining], truncated=True, reason="message_text_limit")
        used_chars += len(extracted["text"]) + (separator if extracted["text"] else 0)
        extracted["text_chars"] = len(extracted["text"])
        result.append({**meta, **extracted})
    return result


def summarise_message(msg) -> dict:
    entries = extract_message(msg)
    return {
        "attachment_text": "\n".join(entry["text"] for entry in entries if entry["text"]),
        "attachment_names": "\n".join(entry["filename"] for entry in entries if entry["filename"]),
        "attachments_json": json.dumps([{k: v for k, v in entry.items() if k != "text"} for entry in entries], ensure_ascii=False),
        "attachments_indexed": 1,
    }


def _worker() -> None:
    if os.name == "posix":
        import resource
        # Darwin's shared-library virtual mappings can already exceed this
        # Linux-oriented cap before parsing begins. Keep portable CPU/wall and
        # document limits there; use the address-space cap where it is reliable.
        if sys.platform.startswith("linux"):
            resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_CPU, (8, 8))
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    try:
        payload = sys.stdin.buffer.read(MAX_ATTACHMENT_BYTES + 1)
        if len(payload) > MAX_ATTACHMENT_BYTES:
            raise ExtractionLimit("PDF too large")
        result = {"status": "extracted", **_pdf_document(payload)}
    except EncryptedDocument:
        result = {"status": "encrypted"}
    except (ExtractionLimit, MemoryError):
        result = {"status": "too_large"}
    except Exception:
        result = {"status": "error"}
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__" and sys.argv[1:] == ["--pdf-worker"]:
    _worker()
