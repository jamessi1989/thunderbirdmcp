"""Foreground service tests never start a real background worker."""

import contextlib
import io
import json
import unittest
from unittest.mock import patch

from thunderbird_mcp import service


class ServiceTests(unittest.TestCase):
    def test_scheduler_once_logs_counts_without_delivery_payloads(self):
        output = io.StringIO()
        result = {"count": 1, "schedules": [{"status": "completed", "result": {"subject": "private subject", "accepted_recipients": ["private@example.test"]}}]}
        with patch.object(service.runtime, "configure_session_environment"), \
                patch.object(service.automation, "run_scheduled", return_value=result) as tick, \
                contextlib.redirect_stdout(output):
            code = service.main(["scheduler", "--once"])
        self.assertEqual(code, 0)
        tick.assert_called_once_with(limit=10)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["outcomes"], {"completed": 1})
        self.assertNotIn("private", output.getvalue())

    def test_watch_once_forwards_explicit_idle_and_history_options(self):
        result = {"mode": "idle", "baseline": False, "folder_reset": False, "recorded": 2, "more_pending": False, "cursor": 10}
        with patch.object(service.runtime, "configure_session_environment"), \
                patch.object(service.automation, "watch_mail", return_value=result) as tick, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(service.main(["watch", "--once", "--idle", "30", "--folder", "Drafts", "--include-existing"]), 0)
        tick.assert_called_once_with(account=None, folder="Drafts", wait_seconds=30, include_existing=True)

    def test_errors_are_redacted_and_once_returns_failure(self):
        output = io.StringIO()
        with patch.object(service.runtime, "configure_session_environment"), \
                patch.object(service.automation, "run_scheduled", side_effect=RuntimeError("private server reply and secret")), \
                contextlib.redirect_stdout(output):
            self.assertEqual(service.main(["scheduler", "--once"]), 1)
        self.assertEqual(json.loads(output.getvalue())["error"], {"type": "RuntimeError"})
        self.assertNotIn("secret", output.getvalue())

    def test_continuous_service_stops_cleanly_on_keyboard_interrupt(self):
        with patch.object(service.runtime, "configure_session_environment"), \
                patch.object(service.automation, "run_scheduled", side_effect=KeyboardInterrupt), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(service.main(["scheduler"]), 130)


if __name__ == "__main__":
    unittest.main()
