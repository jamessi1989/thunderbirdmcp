"""Bounded OCR tests use synthetic pixels and fake installed executables."""

import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from thunderbird_mcp import attachments, ocr


class OCRTests(unittest.TestCase):
    def setUp(self):
        environment = {key: value for key, value in os.environ.items() if not key.startswith("TB_OCR")}
        self.environment = patch.dict(os.environ, environment, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        try:
            from PIL import Image
        except ImportError:
            return
        # _extract_in_worker normally runs in an isolated process; direct unit
        # calls must restore that process-local Pillow setting themselves.
        pixels = patch.object(Image, "MAX_IMAGE_PIXELS", Image.MAX_IMAGE_PIXELS)
        pixels.start()
        self.addCleanup(pixels.stop)

    def settings(self):
        return {"languages": ["eng"], "pages": 3, "timeout": 20,
                "tesseract": "synthetic-tesseract", "pdfinfo": "synthetic-pdfinfo", "pdftoppm": "synthetic-pdftoppm"}

    def test_opt_in_missing_engine_languages_and_renderer_are_explicit(self):
        with patch.object(ocr.subprocess, "Popen") as process:
            self.assertEqual(ocr.extract_text(b"image")["reason"], "ocr_disabled")
        process.assert_not_called()
        with patch.dict(os.environ, TB_OCR="1"), patch.object(ocr, "_configuration", return_value={**self.settings(), "tesseract": None}):
            self.assertEqual(ocr.extract_text(b"image")["reason"], "ocr_engine_not_installed")
        with patch.dict(os.environ, TB_OCR="1"), patch.object(ocr, "_configuration", return_value={**self.settings(), "pdfinfo": None}):
            self.assertEqual(ocr.extract_text(b"%PDF", pdf=True)["reason"], "ocr_pdf_renderer_not_installed")
        with patch.dict(os.environ, TB_OCR="1"), patch.object(ocr, "_configuration", return_value=self.settings()), \
             patch.object(ocr, "_languages", return_value=["spa"]), patch.object(ocr.subprocess, "Popen") as process:
            self.assertEqual(ocr.extract_text(b"image")["reason"], "ocr_requested_language_not_installed")
        process.assert_not_called()

    def test_configuration_limits_and_status_do_not_expose_paths(self):
        for key, value in (("TB_OCR_MAX_PAGES", "6"), ("TB_OCR_TIMEOUT", "99"), ("TB_OCR_LANGUAGES", "eng+--shell")):
            with patch.dict(os.environ, {key: value}), self.assertRaises(ValueError):
                ocr._configuration()
        settings = {**self.settings(), "tesseract": "/private/local/user/tesseract"}
        with patch.object(ocr, "_configuration", return_value=settings), patch.object(ocr, "_languages", return_value=["eng", "spa"]):
            status = ocr.status()
        self.assertNotIn("/private", json.dumps(status))
        self.assertFalse(status["downloads"])
        self.assertEqual(status["available_languages"], ["eng", "spa"])

    def test_worker_result_and_timeout_remain_bounded(self):
        process = Mock()
        process.returncode = 0
        process.communicate.return_value = (json.dumps({"text": "Recognized fixture", "status": "extracted", "truncated": False, "method": "ocr"}).encode(), None)
        with patch.dict(os.environ, TB_OCR="1"), patch.object(ocr, "_configuration", return_value=self.settings()), \
             patch.object(ocr, "_languages", return_value=["eng"]), patch.object(ocr.subprocess, "Popen", return_value=process) as popen:
            result = ocr.extract_text(b"synthetic-image")
        self.assertEqual(result["text"], "Recognized fixture")
        self.assertEqual(process.communicate.call_args.args, (b"synthetic-image",))
        self.assertNotIn("shell", popen.call_args.kwargs)
        process.communicate.side_effect = [subprocess.TimeoutExpired("synthetic", 20), (b"", None)]
        process.pid = 99999
        with patch.dict(os.environ, TB_OCR="1"), patch.object(ocr, "_configuration", return_value=self.settings()), \
             patch.object(ocr, "_languages", return_value=["eng"]), patch.object(ocr.subprocess, "Popen", return_value=process), \
             patch.object(ocr.subprocess, "run", return_value=Mock(returncode=0)), \
             patch.object(ocr.os, "killpg", create=True) as kill:
            result = ocr.extract_text(b"synthetic-image")
        self.assertEqual(result["reason"], "ocr_time_limit")
        if os.name == "posix":
            kill.assert_called_once()

    def test_image_decode_uses_real_pixels_and_bounds_output(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Optional Pillow extra is not installed")
        stream = io.BytesIO()
        Image.new("RGB", (120, 60), "white").save(stream, format="PNG")
        paths = []
        def recognize(path, root, number, settings, deadline):
            self.assertTrue(path.is_file())
            paths.append(path)
            return "x" * (ocr.MAX_CHARS + 1)
        with patch.object(ocr, "_recognize", side_effect=recognize):
            result = ocr._extract_in_worker(stream.getvalue(), False, self.settings())
        self.assertEqual(len(result["text"]), ocr.MAX_CHARS)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["pages_processed"], 1)
        self.assertFalse(paths[0].exists())
        with patch.object(ocr, "MAX_PIXELS", 100), self.assertRaises(Exception):
            ocr._extract_in_worker(stream.getvalue(), False, self.settings())

    def test_pdf_renderer_and_tesseract_have_fixed_argument_boundaries(self):
        try:
            import PIL
        except ImportError:
            self.skipTest("Optional Pillow extra is not installed")
        commands = []
        def run(command, **kwargs):
            commands.append(command)
            return Mock(stdout=b"Pages: 12\n")
        with patch.object(ocr, "_run", side_effect=run), patch.object(ocr, "_recognize", return_value="OCR page"):
            result = ocr._extract_in_worker(b"synthetic-pdf", True, {**self.settings(), "pages": 2})
        self.assertEqual(result["pages_processed"], 2)
        self.assertTrue(result["truncated"])
        renderers = commands[1:]
        self.assertEqual(len(renderers), 2)
        self.assertTrue(all(command[command.index("-scale-to") + 1] == "3000" for command in renderers))
        self.assertEqual([command[command.index("-f") + 1] for command in renderers], ["1", "2"])

    def test_attachment_ocr_only_falls_back_for_images_and_textless_pdf(self):
        recognized = {"text": "Scannedfixturequasar", "status": "extracted", "truncated": False, "method": "ocr"}
        with patch.dict(os.environ, TB_OCR="1"), patch.object(ocr, "extract_text", return_value=recognized) as run:
            self.assertEqual(attachments.extract_text(b"image", "fixture.png")["method"], "ocr")
            with patch.object(attachments, "_pdf", return_value={"text": "", "truncated": False}):
                self.assertEqual(attachments.extract_text(b"%PDF", "fixture.pdf")["text"], "Scannedfixturequasar")
            with patch.object(attachments, "_pdf", return_value={"text": "Existing PDF text", "truncated": False}):
                self.assertEqual(attachments.extract_text(b"%PDF", "text.pdf")["text"], "Existing PDF text")
        self.assertEqual(run.call_count, 2)

    @unittest.skipUnless(os.environ.get("TB_RUN_OCR_SMOKE") == "1", "Set TB_RUN_OCR_SMOKE=1 with installed Tesseract/Poppler for the real engine check")
    def test_installed_engine_recognizes_synthetic_png_and_scanned_pdf(self):
        from PIL import Image, ImageDraw, ImageFont
        image = Image.new("RGB", (1600, 300), "white")
        ImageDraw.Draw(image).text((50, 90), "MAILDIR OCR QUASAR 2026", fill="black",
                                  font=ImageFont.load_default(size=64))
        with patch.dict(os.environ, TB_OCR="1", TB_OCR_LANGUAGES="eng"):
            for fmt, name in (("PNG", "synthetic.png"), ("PDF", "synthetic.pdf")):
                stream = io.BytesIO()
                image.save(stream, format=fmt)
                result = attachments.extract_text(stream.getvalue(), name)
                self.assertEqual(result["status"], "extracted", result)
                self.assertIn("QUASAR", result["text"])


if __name__ == "__main__":
    unittest.main()
