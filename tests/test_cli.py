import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from thunderbird_mcp import cli


class CliTests(unittest.TestCase):
    def test_send_without_confirmation_only_previews(self):
        with patch("thunderbird_mcp.server.get_draft", return_value={"draft_id": "abc", "body": "Reviewed text"}) as get, patch("thunderbird_mcp.server.send_draft") as send:
            result = cli.dispatch(cli.parser().parse_args(["send", "abc"]))
        get.assert_called_once_with("abc")
        send.assert_not_called()
        self.assertTrue(result["preview"])
        self.assertIn("--confirm", result["next_step"])

    def test_confirmed_send_dispatches_exact_draft(self):
        with patch("thunderbird_mcp.server.send_draft", return_value={"sent": True}) as send:
            self.assertTrue(cli.dispatch(cli.parser().parse_args(["send", "abc", "--confirm"]))["sent"])
        send.assert_called_once_with("abc", retry_failed=False)

    def test_body_stdin_and_attachment_argument(self):
        with patch("sys.stdin", io.StringIO("First line\nSecond line")), patch("thunderbird_mcp.server.draft_email", return_value={"draft_id": "abc"}) as draft:
            cli.dispatch(cli.parser().parse_args(["draft", "--to", "person@example.test", "--subject", "A subject", "--body-file", "-", "--attach", "file with spaces.txt"]))
        self.assertEqual(draft.call_args.kwargs["body"], "First line\nSecond line")
        self.assertEqual(draft.call_args.kwargs["attachments"], ["file with spaces.txt"])

    def test_json_before_and_after_subcommand(self):
        self.assertTrue(cli.parser().parse_args(["--json", "status"]).json)
        self.assertTrue(cli.parser().parse_args(["status", "--json"]).json)
        self.assertFalse(cli.parser().parse_args(["status"]).json)

    def test_message_terminal_controls_are_not_executed(self):
        output = cli.render({"subject": "mail\x1b]52;c;payload\x07", "body": "before\x1b[2Jafter\x9b"}, cli.parser().parse_args(["read", "1"]))
        self.assertNotIn("\x1b", output)
        self.assertNotIn("\x07", output)
        self.assertNotIn("\x9b", output)
        self.assertIn("\\x1b", output)

    def test_json_escapes_terminal_controls_without_losing_unicode(self):
        value = {"body": "café\x9b\x1b"}
        text = cli.render(value, cli.parser().parse_args(["--json", "read", "1"]))
        self.assertNotIn("\x9b", text)
        self.assertNotIn("\x1b", text)
        self.assertIn("café", text)
        self.assertEqual(json.loads(text), value)

    def test_search_reports_semantic_fallback(self):
        result = {"results": [], "diagnostics": {"warnings": ["Semantic search unavailable"]}}
        self.assertIn("Semantic search unavailable", cli.render(result, cli.parser().parse_args(["search", "test"])))

    def test_search_joins_words_and_preserves_filters(self):
        with patch("thunderbird_mcp.server.search_email", return_value={"results": []}) as search:
            cli.dispatch(cli.parser().parse_args(["search", "project", "meeting", "from:example", "--limit", "4"]))
        self.assertEqual(search.call_args.args, ("project meeting from:example",))
        self.assertEqual(search.call_args.kwargs["limit"], 4)

    def test_search_can_list_recent_mail_without_semantics(self):
        with patch("thunderbird_mcp.server.search_email", return_value={"results": []}) as search:
            cli.dispatch(cli.parser().parse_args(["search", "--keyword", "--all-messages", "--sender", "person"]))
        self.assertEqual(search.call_args.args, ("",))
        self.assertFalse(search.call_args.kwargs["semantic"])
        self.assertFalse(search.call_args.kwargs["deduplicate_threads"])
        self.assertEqual(search.call_args.kwargs["sender"], "person")

    def test_send_preview_preserves_prior_delivery_audit(self):
        audit = {"sent": True, "sent_copy_saved": False}
        with patch("thunderbird_mcp.server.get_draft", return_value={"status": "sent", "delivery": audit}):
            result = cli.dispatch(cli.parser().parse_args(["send", "abc"]))
        self.assertTrue(result["preview"])
        self.assertEqual(result["delivery"], audit)

    def test_failed_health_or_incomplete_delivery_has_nonzero_exit(self):
        cases = [(["status"], {"ok": False}), (["send", "abc", "--confirm"], {"status": "partially_sent", "sent": True}), (["send", "abc", "--confirm"], {"status": "uncertain", "sent": None})]
        for args, result in cases:
            with self.subTest(result=result), patch("thunderbird_mcp.cli.dispatch", return_value=result), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(args), 1)

    def test_lookup_failure_has_no_traceback(self):
        output = io.StringIO()
        with patch("thunderbird_mcp.cli.dispatch", side_effect=LookupError("message missing")), contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(["read", "missing"]), 1)
        self.assertIn("Error: message missing", output.getvalue())

    def test_errors_are_nonzero_machine_readable(self):
        output = io.StringIO()
        with patch("thunderbird_mcp.cli.dispatch", side_effect=ValueError("bad reference")), contextlib.redirect_stdout(output):
            result = cli.main(["--json", "read", "missing"])
        self.assertEqual(result, 1)
        self.assertEqual(json.loads(output.getvalue()), {"error": "bad reference"})

    def test_attachment_export_passes_explicit_absolute_destination(self):
        with tempfile.TemporaryDirectory() as root, patch("thunderbird_mcp.server.export_attachment", return_value={"saved": True}) as export:
            destination = str(Path(root) / "attachment.pdf")
            cli.dispatch(cli.parser().parse_args(["export-attachment", "thunderbird://key", "part-1", destination]))
        export.assert_called_once_with("thunderbird://key", "part-1", destination=destination)


if __name__ == "__main__":
    unittest.main()
