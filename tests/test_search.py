"""Search filtering, vector cache invalidation, and model failure recovery."""

import numpy as np
from unittest.mock import patch

from thunderbird_mcp import index, search
from test_index import MailFixture, mail


class SearchTests(MailFixture):
    def setUp(self):
        super().setUp()
        self.folder("INBOX", [mail("one", body="alpha quasar", **{"To": "reader@example.test", "Date": "Mon, 02 Nov 2026 10:00:00 +0000"}),
                              mail("two", body="beta nebula", **{"To": "other@example.test", "Date": "Sun, 01 Nov 2026 10:00:00 +0000"})])
        self.refresh()

    def find(self, query, **kwargs):
        return search.search(query, use=("fts",), db_path=self.db, **kwargs)

    def test_date_and_recipient_filters(self):
        self.assertEqual(len(self.find("", since="2026-11-02", until="2026-11-02")), 1)
        self.assertEqual(len(self.find("", recipient="reader@")), 1)
        self.assertEqual(len(self.find('to:"reader@example.test" before:2026-11-03')), 1)
        for query in ("before:invalid", "after:2026-99-99", "folder:", 'folder:"unfinished', "from:a from:b"):
            with self.subTest(query=query), self.assertRaises(ValueError):
                self.find(query)
        with self.assertRaises(ValueError):
            self.find("", since="2026-11-03", until="2026-11-01")

    def test_filter_like_wildcards_are_literal(self):
        self.assertEqual(self.find("", folder="%"), [])
        self.assertEqual(self.find("", sender="_"), [])

    def test_same_subject_unrelated_messages_not_collapsed(self):
        result = self.find("Project")
        self.assertEqual(len(result), 2)
        self.assertTrue(all(r["reference"].startswith("thunderbird://message/") for r in result))

    def test_limits_reject_zero_negative_and_huge_values(self):
        for limit in (0, -1, 201, True):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.find("", limit=limit)

    def test_empty_vector_table_does_not_fail(self):
        conn = index.connect(self.db)
        search._embedding_schema(conn, "embeddings")
        self.assertEqual(search._VectorCache().get(conn)[0], [])
        conn.close()

    def test_vector_cache_reloads_same_count_updates_and_separate_databases(self):
        conn = index.connect(self.db)
        search._embedding_schema(conn, "embeddings")
        conn.execute("INSERT INTO embeddings(key,vec) VALUES (?,?)", ("a", np.array([1, 0], dtype=np.float32).tobytes()))
        conn.commit()
        cache = search._VectorCache()
        self.assertEqual(cache.get(conn)[1].tolist(), [[1.0, 0.0]])
        conn.execute("UPDATE embeddings SET vec=?", (np.array([0, 1], dtype=np.float32).tobytes(),))
        conn.commit()
        self.assertEqual(cache.get(conn)[1].tolist(), [[0.0, 1.0]])
        conn.close()
        other = index.connect(self.root / "other.sqlite")
        search._embedding_schema(other, "embeddings")
        other.execute("INSERT INTO embeddings(key,vec) VALUES (?,?)", ("b", np.array([1, 0], dtype=np.float32).tobytes()))
        other.commit()
        self.assertEqual(cache.get(other)[0], ["b"])
        other.close()

    def test_model_error_keeps_keyword_results(self):
        with patch.object(search._cache, "get", return_value=(["<one@example.test>"], np.ones((1, 2)))), \
             patch.object(search, "_embedder", side_effect=RuntimeError("model missing")):
            result = search.search("quasar", use=("fts", "vec"), db_path=self.db)
        self.assertEqual(len(result), 1)
        self.assertEqual(result.diagnostics["semantic"], "unavailable")
        self.assertTrue(result.diagnostics["warnings"])

    def test_no_result_does_not_load_reranker(self):
        with patch.object(search, "_cross_encoder") as model:
            self.assertEqual(search.search("unfindablewordxyz", use=("fts", "rerank"), db_path=self.db), [])
            model.assert_not_called()

    def test_missing_message_id_vectors_can_find_message(self):
        self.folder("INBOX", [mail(None, body="a distinct body")])
        self.refresh()
        key = self.rows()[0]["stable_key"]
        class FakeModel:
            def embed(self, documents):
                return iter([np.array([1, 0], dtype=np.float32)])
        with patch.object(search._cache, "get", return_value=([key], np.array([[1, 0]], dtype=np.float32))), \
             patch.object(search, "_embedder", return_value=FakeModel()):
            result = search.search("meaning", use=("vec",), db_path=self.db)
        self.assertEqual(len(result), 1)
        self.assertIn("sha256", result[0]["reference"])

    def test_embeddings_are_invalidated_when_message_content_changes(self):
        conn = index.connect(self.db)
        search._embedding_schema(conn, "embeddings")
        key = self.rows()[0]["stable_key"]
        conn.execute("INSERT INTO embeddings(key,vec) VALUES (?,?)", (key, np.ones(2, dtype=np.float32).tobytes()))
        conn.commit()
        conn.execute("UPDATE emails SET body='revised synthetic body' WHERE stable_key=?", (key,))
        self.assertEqual(conn.execute("SELECT count(*) FROM embeddings").fetchone()[0], 0)
        conn.close()
