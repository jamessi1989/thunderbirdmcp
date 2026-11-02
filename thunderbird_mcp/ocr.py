"""Opt-in local OCR with explicit installed engines, languages and fixed bounds.

No engine, model or language data is downloaded. Tesseract and (for PDFs)
Poppler must already be installed. Image decoding uses the optional Pillow extra.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time


MAX_INPUT_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 12_000_000
MAX_EDGE = 3000
MAX_CHARS = 25_000
MEMORY_BYTES = 768 * 1024 * 1024
_LANGUAGE = re.compile(r"[A-Za-z0-9_]{1,32}(?:/[A-Za-z0-9_]{1,32})?\Z")


def enabled() -> bool:
    return os.environ.get("TB_OCR", "0").lower() in {"1", "true", "yes"}


def _executable(name, variable):
    selected = os.environ.get(variable)
    if selected:
        path = Path(selected).expanduser()
        if not path.is_absolute() or not path.is_file():
            raise ValueError("OCR executable overrides must name an installed absolute file")
        return str(path)
    return shutil.which(name)


def _configuration() -> dict:
    languages = os.environ.get("TB_OCR_LANGUAGES", "eng").split("+")
    if not 1 <= len(languages) <= 8 or any(not _LANGUAGE.fullmatch(language) for language in languages):
        raise ValueError("TB_OCR_LANGUAGES must contain 1 to 8 installed language codes joined by +")
    try:
        pages, timeout = int(os.environ.get("TB_OCR_MAX_PAGES", "3")), int(os.environ.get("TB_OCR_TIMEOUT", "20"))
    except ValueError:
        raise ValueError("OCR page/time limits must be integers") from None
    if not 1 <= pages <= 5 or not 1 <= timeout <= 30:
        raise ValueError("OCR allows 1 to 5 pages and 1 to 30 seconds")
    return {"languages": list(dict.fromkeys(languages)), "pages": pages, "timeout": timeout,
            "tesseract": _executable("tesseract", "TB_TESSERACT_PATH"),
            "pdftoppm": _executable("pdftoppm", "TB_PDFTOPPM_PATH"),
            "pdfinfo": _executable("pdfinfo", "TB_PDFINFO_PATH")}


def _languages(executable: str) -> list[str]:
    result = subprocess.run([executable, "--list-langs"], stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, timeout=3, check=False)
    if result.returncode or len(result.stdout) > 64 * 1024:
        raise ValueError("Installed OCR languages could not be enumerated")
    return sorted({line.strip() for line in result.stdout.decode("utf-8", "replace").splitlines()
                   if _LANGUAGE.fullmatch(line.strip())})[:256]


def status() -> dict:
    """Report local availability and limits without exposing executable paths."""
    settings = _configuration()
    languages = []
    diagnostic = None
    if settings["tesseract"]:
        try:
            languages = _languages(settings["tesseract"])
        except Exception:
            diagnostic = "language_probe_failed"
    return {"enabled": enabled(), "engine": "tesseract", "engine_available": bool(settings["tesseract"]),
            "pdf_renderer_available": bool(settings["pdftoppm"] and settings["pdfinfo"]),
            "requested_languages": settings["languages"], "available_languages": languages,
            "missing_languages": sorted(set(settings["languages"]) - set(languages)), "diagnostic": diagnostic,
            "limits": {"input_bytes": MAX_INPUT_BYTES, "image_pixels": MAX_PIXELS, "rendered_edge_pixels": MAX_EDGE,
                       "pages": settings["pages"], "seconds": settings["timeout"], "text_characters": MAX_CHARS,
                       "linux_memory_bytes": MEMORY_BYTES, "linux_cpu_seconds": 25},
            "downloads": False}


def _empty(status, reason):
    return {"text": "", "status": status, "truncated": False, "reason": reason, "method": "ocr"}


def extract_text(payload: bytes, *, pdf: bool = False) -> dict:
    if not enabled():
        return _empty("unsupported", "ocr_disabled")
    if len(payload) > MAX_INPUT_BYTES:
        return _empty("too_large", "ocr_input_byte_limit")
    try:
        settings = _configuration()
    except ValueError:
        return _empty("error", "ocr_configuration")
    if not settings["tesseract"]:
        return _empty("unavailable", "ocr_engine_not_installed")
    if pdf and not (settings["pdftoppm"] and settings["pdfinfo"]):
        return _empty("unavailable", "ocr_pdf_renderer_not_installed")
    try:
        installed = _languages(settings["tesseract"])
        if set(settings["languages"]) - set(installed):
            return _empty("unavailable", "ocr_requested_language_not_installed")
        command = [sys.executable, "-m", "thunderbird_mcp.ocr", "--worker", "pdf" if pdf else "image"]
        kwargs = {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": subprocess.DEVNULL}
        if os.name == "posix":
            kwargs["start_new_session"] = True
        process = subprocess.Popen(command, **kwargs)
        try:
            output, _ = process.communicate(payload, timeout=settings["timeout"])
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            elif os.name == "nt":
                # Stop renderers/OCR descendants too; killing only Python would
                # leave a child processing an untrusted attachment.
                try:
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False)
                finally:
                    process.kill()
            else:
                process.kill()
            process.communicate()
            return _empty("too_large", "ocr_time_limit")
        if process.returncode or len(output) > MAX_CHARS * 8:
            return _empty("too_large", "ocr_process_resource_limit")
        result = json.loads(output)
        if not isinstance(result, dict) or not isinstance(result.get("text"), str) or len(result["text"]) > MAX_CHARS:
            return _empty("error", "ocr_invalid_worker_result")
        return result
    except Exception:
        return _empty("error", "ocr_engine_failed")


def _run(command, *, deadline: float, stdout=subprocess.DEVNULL):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    result = subprocess.run(command, stdout=stdout, stderr=subprocess.DEVNULL,
                            timeout=remaining, check=False)
    if result.returncode:
        raise ValueError("OCR subprocess failed")
    return result


def _recognize(path: Path, root: Path, number: int, settings: dict, deadline: float) -> str:
    output = root / f"text-{number}"
    _run([settings["tesseract"], str(path), str(output), "-l", "+".join(settings["languages"]),
          "--psm", "3", "quiet"], deadline=deadline)
    with output.with_suffix(".txt").open("rb") as stream:
        raw = stream.read(MAX_CHARS * 4 + 1)
    return raw.decode("utf-8", "replace")


def _extract_in_worker(payload: bytes, pdf: bool, settings: dict) -> dict:
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    deadline = time.monotonic() + settings["timeout"]
    texts, pages, truncated = [], 0, False
    with tempfile.TemporaryDirectory(prefix="thunderbird-ocr-") as temporary:
        root = Path(temporary)
        if pdf:
            source = root / "input.pdf"
            source.write_bytes(payload)
            details = _run([settings["pdfinfo"], str(source)], deadline=deadline, stdout=subprocess.PIPE).stdout
            if len(details) > 64 * 1024:
                raise ValueError("Oversized PDF metadata")
            match = re.search(rb"^Pages:\s*(\d+)\s*$", details, re.M)
            if not match:
                raise ValueError("Missing PDF page count")
            total = int(match[1])
            truncated = total > settings["pages"]
            for page in range(1, min(total, settings["pages"]) + 1):
                target = root / f"page-{page}"
                _run([settings["pdftoppm"], "-f", str(page), "-l", str(page), "-r", "150", "-scale-to", str(MAX_EDGE),
                      "-singlefile", "-png", str(source), str(target)], deadline=deadline)
                texts.append(_recognize(target.with_suffix(".png"), root, page, settings, deadline))
                pages += 1
                if sum(map(len, texts)) > MAX_CHARS:
                    truncated = True
                    break
        else:
            with Image.open(io.BytesIO(payload)) as source:
                total = getattr(source, "n_frames", 1)
                truncated = total > settings["pages"]
                for page in range(min(total, settings["pages"])):
                    source.seek(page)
                    if source.width * source.height > MAX_PIXELS or source.width <= 0 or source.height <= 0:
                        raise MemoryError("OCR image pixel limit")
                    frame = source.convert("RGB")
                    frame.thumbnail((MAX_EDGE, MAX_EDGE))
                    target = root / f"page-{page}.png"
                    frame.save(target, format="PNG")
                    texts.append(_recognize(target, root, page, settings, deadline))
                    pages += 1
                    if sum(map(len, texts)) > MAX_CHARS:
                        truncated = True
                        break
        text = " ".join(re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", "\n".join(texts)).split())
        return {"text": text[:MAX_CHARS], "status": "extracted" if text else "empty",
                "truncated": truncated or len(text) > MAX_CHARS, "method": "ocr", "engine": "tesseract",
                "languages": settings["languages"], "pages_processed": pages}


def _worker(pdf: bool):
    os.umask(0o077)
    os.environ["OMP_THREAD_LIMIT"] = "1"
    os.environ["OMP_NUM_THREADS"] = "1"
    if os.name == "posix":
        import resource
        if sys.platform.startswith("linux"):
            resource.setrlimit(resource.RLIMIT_AS, (MEMORY_BYTES, MEMORY_BYTES))
        resource.setrlimit(resource.RLIMIT_CPU, (25, 25))
        resource.setrlimit(resource.RLIMIT_FSIZE, (32 * 1024 * 1024,) * 2)
    try:
        payload = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(payload) > MAX_INPUT_BYTES:
            raise MemoryError
        result = _extract_in_worker(payload, pdf, _configuration())
    except ImportError:
        result = _empty("unavailable", "ocr_pillow_not_installed")
    except (MemoryError, subprocess.TimeoutExpired, TimeoutError):
        result = _empty("too_large", "ocr_process_resource_limit")
    except Exception:
        result = _empty("error", "ocr_document_could_not_be_processed")
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "--worker":
    _worker(sys.argv[2] == "pdf")
