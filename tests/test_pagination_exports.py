import contextlib
import os
from pathlib import Path
from unittest.mock import patch
from thunderbird_mcp import exports, index, insights, pagination, policy
from test_index import MailFixture, mail


class PaginationExportTests(MailFixture):
    def setUp(self):
        super().setUp()
        contexts = contextlib.ExitStack()
        self.addCleanup(contexts.close)
        contexts.enter_context(patch.dict(os.environ, {
            "TB_MAIL_INDEX_DB": str(self.db), "TB_SEARCH_SNAPSHOTS_DB": str(self.root / "snapshots.sqlite"),
            "TB_EXPORTS_DB": str(self.root / "exports.sqlite"), "TB_OPERATIONS_DB": str(self.root / "operations.sqlite")}))
        contexts.enter_context(patch.object(insights.outgoing, "infer_account", return_value=None))
        contexts.enter_context(patch.object(index, "find_profile", return_value=self.profile))
        contexts.enter_context(patch.object(insights.outgoing, "_prefs", return_value={}))

    def test_snapshot_continuation_is_stable_across_index_updates(self):
        self.folder("INBOX", [mail(str(i), subject="quantum project") for i in range(5)])
        self.refresh()
        first = pagination.search_page("quantum", limit=2, deduplicate_threads=False)
        self.assertEqual(first["snapshot_results"], 5)
        self.folder("INBOX", [mail("new", subject="quantum project")])
        self.refresh()
        second = pagination.search_page(cursor=first["next_cursor"], limit=2)
        third = pagination.search_page(cursor=second["next_cursor"], limit=2)
        refs = [row["reference"] for page in (first, second, third) for row in page["results"]]
        self.assertEqual(len(set(refs)), 5)
        self.assertIsNone(third["next_cursor"])

    def test_cursor_cannot_cross_principals_and_reports_ignored_terms(self):
        self.folder("INBOX", [mail(str(i), subject="quantum project") for i in range(3)])
        self.refresh()
        with policy.context(policy.Policy(principal="one")):
            first = pagination.search_page("the quantum missing98765", limit=1)
        self.assertIn("the", first["diagnostics"]["ignored_keyword_terms"])
        self.assertIn("missing98765", first["diagnostics"]["unmatchable_keyword_terms"])
        with policy.context(policy.Policy(principal="two")), self.assertRaises(PermissionError):
            pagination.search_page(cursor=first["next_cursor"])

    def test_export_frozen_bytes_collision_plan_and_checksum_resume(self):
        self.folder("INBOX", [mail("one", body="Reviewed original")])
        self.refresh()
        item = {"reference": "<one@example.test>"}
        plan = exports.plan_export([item, item], str(self.root / "output"))
        self.assertEqual(len({v["path"] for v in plan["files"]}), 2)
        self.assertFalse((self.root / "output").exists())
        self.folder("INBOX", [mail("one", body="Externally changed")])
        result = exports.execute_export(plan["plan_id"])
        self.assertEqual({v["status"] for v in result["files"]}, {"completed"})
        self.assertIn(b"Reviewed original", Path(result["files"][0]["path"]).read_bytes())
        resumed = exports.execute_export(plan["plan_id"], resume=True)
        self.assertEqual(resumed["errors"], [])
        Path(result["files"][0]["path"]).write_bytes(b"unrelated")
        failed = exports.execute_export(plan["plan_id"], resume=True)
        self.assertEqual(failed["files"][0]["status"], "failed")
        self.assertEqual(Path(failed["files"][0]["path"]).read_bytes(), b"unrelated")

    def test_export_never_overwrites_unreviewed_collision(self):
        self.folder("INBOX", [mail()])
        self.refresh()
        plan = exports.plan_export([{"reference": "<one@example.test>"}], str(self.root / "out"))
        destination = Path(plan["files"][0]["path"])
        destination.parent.mkdir()
        destination.write_bytes(b"new file")
        result = exports.execute_export(plan["plan_id"])
        self.assertEqual(result["files"][0]["status"], "failed")
        self.assertEqual(destination.read_bytes(), b"new file")
