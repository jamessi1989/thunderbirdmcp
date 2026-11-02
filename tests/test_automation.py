"""Automation tests use isolated databases and synthetic transports only."""

from __future__ import annotations

import datetime as dt
import os
import smtplib
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

from thunderbird_mcp import automation, live, operations, outgoing, policy


class AutomationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "profile").mkdir()
        environment = patch.dict(os.environ, {
            "TB_PROFILE": str(self.root / "profile"), "TB_DB": str(self.root / "index.sqlite"),
            "TB_AUTOMATION_DB": str(self.root / "automation.sqlite"),
            "TB_OUTBOX_DB": str(self.root / "outbox.sqlite"),
            "TB_OPERATIONS_DB": str(self.root / "operations.sqlite"),
            "TB_FROM": "me@example.test", "TB_IMAP_HOST": "imap.example.test",
            "TB_IMAP_USER": "test", "TB_SMTP_HOST": "smtp.example.test", "TB_SMTP_USER": "test",
        }, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.now = dt.datetime(2026, 11, 2, 12, tzinfo=dt.timezone.utc).timestamp()
        clock = patch.object(automation.time, "time", return_value=self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def draft(self):
        return outgoing.prepare_draft(["recipient@example.test"], "Scheduled synthetic mail", "Reviewed synthetic body")

    def schedule(self, draft=None, seconds=60):
        draft = draft or self.draft()
        due = dt.datetime.fromtimestamp(self.now + seconds, dt.timezone.utc).isoformat()
        return automation.schedule_draft(draft["draft_id"], draft["revision"], due, approved=True)

    @contextmanager
    def smtp(self, *, error=None):
        smtp = MagicMock()
        smtp.sendmail.return_value = {}
        smtp.sendmail.side_effect = error
        with patch.object(outgoing, "smtp_session") as session, patch.object(outgoing, "_append_sent"):
            session.return_value.__enter__.return_value = smtp
            yield smtp

    @contextmanager
    def mailbox(self, uids, validity=11):
        with patch.object(live, "_session") as session, patch.object(live, "_select", return_value=validity), \
                patch.object(live, "_search_uids", return_value=uids):
            yield session.return_value.__enter__.return_value

    def test_schedule_requires_explicit_approval_revision_and_future_time(self):
        draft = self.draft()
        due = "2026-11-02T12:01:00+00:00"
        with self.assertRaisesRegex(ValueError, "approved=True"):
            automation.schedule_draft(draft["draft_id"], 1, due)
        with self.assertRaisesRegex(ValueError, "revision"):
            automation.schedule_draft(draft["draft_id"], 99, due, approved=True)
        with self.assertRaisesRegex(ValueError, "future"):
            automation.schedule_draft(draft["draft_id"], 1, "2026-11-02T12:00:00Z", approved=True)
        with self.assertRaisesRegex(ValueError, "timezone"):
            automation.schedule_draft(draft["draft_id"], 1, "2026-11-02T13:00:00", approved=True)

    def test_timezones_and_daylight_saving_are_explicit(self):
        first, zone = automation._due_time("2026-11-02T14:00:00", "Europe/Madrid")
        second, _ = automation._due_time("2026-11-02T13:00:00Z", None)
        self.assertEqual(first, second)
        self.assertEqual(zone, "Europe/Madrid")
        for local in ("2026-03-29T02:30:00", "2026-10-25T02:30:00"):
            with self.subTest(local=local), self.assertRaisesRegex(ValueError, "ambiguous or nonexistent"):
                automation._due_time(local, "Europe/Madrid")
        with self.assertRaisesRegex(ValueError, "offset does not match"):
            automation._due_time("2026-11-02T14:00:00+05:00", "Europe/Madrid")

    def test_due_job_delivers_once_and_records_operation(self):
        schedule = self.schedule()
        with self.smtp() as smtp:
            self.assertEqual(automation.run_scheduled()["count"], 0)
            with patch.object(automation.time, "time", return_value=self.now + 61):
                first = automation.run_scheduled()
                second = automation.run_scheduled()
        smtp.sendmail.assert_called_once()
        self.assertEqual(first["schedules"][0]["status"], "completed")
        self.assertEqual(second["count"], 0)
        operation = operations.get_operation(schedule["operation_id"])
        self.assertEqual(operation["status"], "completed")
        self.assertTrue(operation["result"]["sent"])

    def test_native_api_schedule_uses_provider_transport_and_shared_claim(self):
        from thunderbird_mcp import providers
        due = dt.datetime.fromtimestamp(self.now + 60, dt.timezone.utc).isoformat()
        for transport in ("gmail", "graph"):
            draft = self.draft()
            scheduled = automation.schedule_draft(draft["draft_id"], 1, due, approved=True, transport=transport)
            self.assertEqual(scheduled["transport"], transport)
            with patch.object(providers, "_request", return_value={"id": "synthetic-provider-message"}) as api, \
                    self.smtp() as smtp, patch.object(automation.time, "time", return_value=self.now + 61):
                result = automation.run_scheduled()
                repeated = outgoing.send_draft(draft["draft_id"])
            self.assertEqual(result["schedules"][0]["status"], "completed")
            self.assertEqual(api.call_args.args[1], "/messages/send" if transport == "gmail" else "/sendMail")
            smtp.sendmail.assert_not_called()
            self.assertTrue(repeated["duplicate_prevented"])
        with self.assertRaisesRegex(ValueError, "transport"):
            automation.schedule_draft(self.draft()["draft_id"], 1, due, approved=True, transport="unknown")

    def test_approval_policy_is_captured_and_allowlist_revocation_cannot_claim(self):
        approved = policy.Policy(tools=frozenset({"schedule_draft", "run_scheduled", "send_draft"}))
        draft = self.draft()
        with policy.context(approved):
            schedule = self.schedule(draft)
        with patch.dict(os.environ, {"TB_MCP_TOOLS": "schedule_draft,run_scheduled"}), self.smtp() as smtp, \
                patch.object(automation.time, "time", return_value=self.now + 61):
            result = automation.run_scheduled()
        self.assertEqual(result["schedules"][0]["status"], "blocked_by_policy")
        self.assertEqual(automation.get_schedule(schedule["schedule_id"])["status"], "queued")
        smtp.sendmail.assert_not_called()

    def test_same_schedule_is_idempotent_and_changed_time_requires_cancel(self):
        draft = self.draft()
        first, second = self.schedule(draft), self.schedule(draft)
        self.assertEqual(first["schedule_id"], second["schedule_id"])
        self.assertTrue(second["duplicate_prevented"])
        with self.assertRaisesRegex(ValueError, "cancel"):
            self.schedule(draft, seconds=120)
        cancelled = automation.cancel_schedule(first["schedule_id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertFalse(cancelled["effects_reversed"])
        replacement = self.schedule(draft, seconds=120)
        self.assertNotEqual(first["schedule_id"], replacement["schedule_id"])

    def test_edited_draft_needs_new_approval_and_never_uses_stale_revision(self):
        draft = self.draft()
        schedule = self.schedule(draft)
        outgoing.edit_draft(draft["draft_id"], 1, {"body": "Changed after approval"})
        with self.smtp() as smtp, patch.object(automation.time, "time", return_value=self.now + 61):
            result = automation.run_scheduled()
        smtp.sendmail.assert_not_called()
        self.assertEqual(result["schedules"][0]["status"], "failed")
        self.assertEqual(outgoing.get_draft(draft["draft_id"])["status"], "prepared")
        self.assertEqual(automation.get_schedule(schedule["schedule_id"])["result"]["error"]["type"], "ValueError")

    def test_uncertain_delivery_and_interrupted_claim_are_never_retried(self):
        schedule = self.schedule()
        with self.smtp(error=smtplib.SMTPServerDisconnected("response lost")) as smtp, \
                patch.object(automation.time, "time", return_value=self.now + 61):
            first = automation.run_scheduled()
            second = automation.run_scheduled()
        self.assertEqual(first["schedules"][0]["status"], "uncertain")
        self.assertEqual(second["count"], 0)
        smtp.sendmail.assert_called_once()
        interrupted = self.schedule()
        with automation._db() as conn:
            conn.execute("UPDATE schedules SET status='running' WHERE id=?", (interrupted["schedule_id"],))
            conn.commit()
        with self.smtp() as smtp, patch.object(automation.time, "time", return_value=self.now + 61):
            self.assertEqual(automation.run_scheduled()["count"], 0)
        smtp.sendmail.assert_not_called()
        self.assertEqual(automation.get_schedule(interrupted["schedule_id"])["status"], "running")

    def test_concurrent_scheduler_ticks_cannot_send_same_job_twice(self):
        self.schedule()
        nested = []
        with self.smtp() as smtp, patch.object(automation.time, "time", return_value=self.now + 61):
            def while_sending(*args):
                nested.append(automation.run_scheduled())
                return {}
            smtp.sendmail.side_effect = while_sending
            automation.run_scheduled()
        self.assertEqual(nested[0]["count"], 0)
        smtp.sendmail.assert_called_once()

    def test_cancel_queued_or_operation_prevents_delivery(self):
        first, second = self.schedule(), self.schedule()
        automation.cancel_schedule(first["schedule_id"])
        operations.request_cancel(second["operation_id"])
        with self.smtp() as smtp, patch.object(automation.time, "time", return_value=self.now + 61):
            result = automation.run_scheduled()
        smtp.sendmail.assert_not_called()
        self.assertEqual(result["schedules"][0]["status"], "cancelled")

    def test_policy_revocation_blocks_background_delivery_and_preserves_queue(self):
        schedule = self.schedule()
        with patch.dict(os.environ, {"TB_MCP_PROFILE": "read-only"}), self.smtp() as smtp, \
                patch.object(automation.time, "time", return_value=self.now + 61):
            result = automation.run_scheduled()
        self.assertEqual(result["schedules"][0]["status"], "blocked_by_policy")
        self.assertEqual(automation.get_schedule(schedule["schedule_id"])["status"], "queued")
        smtp.sendmail.assert_not_called()
        with patch.dict(os.environ, {"TB_MCP_PROFILE": "draft-only"}), self.assertRaises(PermissionError):
            self.schedule()

    def test_session_principal_cannot_inspect_another_schedule(self):
        with policy.context(policy.Policy(principal="alice")):
            schedule = self.schedule()
        with policy.context(policy.Policy(principal="bob")):
            with self.assertRaises(PermissionError):
                automation.get_schedule(schedule["schedule_id"])
            self.assertEqual(automation.list_schedules()["count"], 0)

    def test_watcher_baselines_and_resumes_new_uid_events_without_duplicates(self):
        with self.mailbox([3, 1, 2]):
            baseline = automation.watch_mail()
        self.assertTrue(baseline["baseline"])
        self.assertEqual(automation.list_mail_events()["count"], 0)
        with self.mailbox([1, 2, 3, 4, 5]):
            self.assertEqual(automation.watch_mail()["recorded"], 2)
            self.assertEqual(automation.watch_mail()["recorded"], 0)
        page = automation.list_mail_events(limit=1)
        self.assertTrue(page["more"])
        second = automation.list_mail_events(after=page["cursor"])
        self.assertEqual([event["uid"] for event in page["events"] + second["events"]], [4, 5])
        locator = live.parse_reference(second["events"][0]["reference"])
        self.assertEqual((locator.account, locator.folder, locator.uidvalidity, locator.uid), ("environment", "INBOX", 11, 5))

    def test_watcher_initial_history_is_explicit_and_large_backlog_resumes(self):
        with patch.object(automation, "_MAX_EVENTS_PER_TICK", 2), self.mailbox([1, 2, 3, 4, 5]):
            first = automation.watch_mail(include_existing=True)
            second = automation.watch_mail()
            third = automation.watch_mail()
        self.assertEqual([first["recorded"], second["recorded"], third["recorded"]], [2, 2, 1])
        self.assertTrue(first["more_pending"])
        self.assertFalse(third["more_pending"])
        self.assertEqual(automation.list_mail_events()["count"], 5)

    def test_uidvalidity_reset_is_explicit_and_does_not_relabel_old_uids(self):
        with self.mailbox([1], validity=11):
            automation.watch_mail(include_existing=True)
        with self.mailbox([1, 2], validity=12):
            reset = automation.watch_mail()
        self.assertTrue(reset["folder_reset"])
        events = automation.list_mail_events()["events"]
        self.assertEqual([event["type"] for event in events], ["new_mail", "folder_reset"])
        self.assertEqual(events[0]["uidvalidity"], 11)
        self.assertEqual(events[1]["uidvalidity"], 12)
        self.assertIsNone(events[1]["reference"])

    def test_idle_is_opt_in_after_baseline_and_events_are_principal_scoped(self):
        with policy.context(policy.Policy(principal="alice")), self.mailbox([1]), patch.object(automation, "_idle_wait", return_value="idle") as idle:
            automation.watch_mail(wait_seconds=30)
            idle.assert_not_called()
            self.assertEqual(automation.watch_mail(wait_seconds=30)["mode"], "idle")
            idle.assert_called_once()
        with policy.context(policy.Policy(principal="alice")), self.mailbox([1, 2]):
            automation.watch_mail()
            self.assertEqual(automation.list_mail_events()["count"], 1)
        with policy.context(policy.Policy(principal="bob")):
            self.assertEqual(automation.list_mail_events()["count"], 0)

    def test_idle_protocol_wait_is_bounded_and_done_is_always_sent(self):
        connection = MagicMock()
        connection.capability.return_value = ("OK", [b"IMAP4rev1 IDLE"])
        connection._new_tag.return_value = b"A001"
        connection.tagged_commands = {b"A001": None}
        def response():
            if connection.send.call_args.args[0] == b"DONE\r\n":
                connection.tagged_commands[b"A001"] = ("OK", [b"done"])
                return b"A001 OK done"
            return None
        connection._get_response.side_effect = response
        with patch.object(automation.select, "select", return_value=([], [], [])) as wait:
            self.assertEqual(automation._idle_wait(connection, 20), "idle")
        self.assertEqual([call.args[0] for call in connection.send.call_args_list], [b"A001 IDLE\r\n", b"DONE\r\n"])
        self.assertEqual(wait.call_args.args[3], 20)
        connection.capability.return_value = ("OK", [b"IMAP4rev1"])
        self.assertEqual(automation._idle_wait(connection, 20), "poll")


if __name__ == "__main__":
    unittest.main()
