"""Verify that the opt-in client check requires actual successful tool calls."""

import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("mail_client_smoke", Path(__file__).resolve().parents[1] / "scripts/test_clients.py")
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


class ClientTraceTests(unittest.TestCase):
    def test_codex_requires_completed_mcp_calls(self):
        events = [
            {"type": "item.completed", "item": {"type": "mcp_tool_call", "tool": "search_email", "status": "completed"}},
            {"type": "item.completed", "item": {"type": "mcp_tool_call", "tool": "get_email", "status": "failed", "error": "missing"}},
            {"type": "item.started", "item": {"type": "mcp_tool_call", "tool": "get_email_reference", "status": "in_progress"}},
        ]
        self.assertEqual(smoke._tool_calls(events), {"search_email"})

    def test_opencode_requires_completed_tool_results(self):
        events = [
            {"type": "tool_use", "part": {"tool": "thunderbird_search_email", "state": {"status": "completed"}}},
            {"type": "tool_use", "part": {"tool": "thunderbird_get_email", "state": {"status": "error"}}},
        ]
        self.assertEqual(smoke._tool_calls(events), {"search_email"})

    def test_claude_requires_successful_results_not_just_requested_tools(self):
        events = [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "a", "name": "mcp__thunderbird__search_email"},
                {"type": "tool_use", "id": "b", "name": "mcp__thunderbird__get_email"},
                {"type": "tool_use", "id": "c", "name": "mcp__thunderbird__get_email_reference"},
            ]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "a", "is_error": False},
                {"type": "tool_result", "tool_use_id": "b", "is_error": True},
            ]}},
        ]
        self.assertEqual(smoke._tool_calls(events), {"search_email"})


if __name__ == "__main__":
    unittest.main()
