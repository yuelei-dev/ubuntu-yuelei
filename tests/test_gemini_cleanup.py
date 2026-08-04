import importlib
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock


class GeminiCleanupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server_dir = str(Path(__file__).resolve().parents[1] / "server")
        if server_dir not in sys.path:
            sys.path.insert(0, server_dir)
        cls.cleanup = importlib.import_module("content_domains.gemini_cleanup")
        cls.breakdown = importlib.import_module("content_domains.breakdown")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "jobs.db")

        def connect():
            connection = sqlite3.connect(self.db_path)
            connection.row_factory = sqlite3.Row
            return connection

        self.jdb = connect

    def tearDown(self):
        self.temp.cleanup()

    def _rows(self):
        with closing(self.jdb()) as connection:
            return connection.execute(
                "SELECT * FROM gemini_file_cleanup_outbox"
            ).fetchall()

    def test_invalid_resource_name_is_rejected_before_provider_call(self):
        delete = mock.Mock()
        with self.assertRaisesRegex(ValueError, "resource name"):
            self.cleanup.delete_file(self.jdb, "../secret", delete)
        delete.assert_not_called()

    def test_failed_immediate_cleanup_retries_then_persists_once(self):
        delete = mock.Mock(side_effect=RuntimeError("provider unavailable"))
        sleep = mock.Mock()
        with mock.patch.object(
            self.cleanup, "RETRY_DELAYS_SECONDS", (0, 0)
        ):
            result = self.cleanup.delete_file(
                self.jdb, "files/test-resource", delete, sleep=sleep
            )
        self.assertEqual(result["status"], "pending_provider_cleanup")
        self.assertTrue(result["persisted"])
        self.assertEqual(delete.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["resource_name"], "files/test-resource")
        self.assertEqual(rows[0]["status"], "pending")

    def test_recovery_deletes_outbox_row_after_provider_success(self):
        self.cleanup.persist(self.jdb, "files/recover-me", 3, now=100)
        delete = mock.Mock(return_value="deleted")
        self.assertTrue(
            self.cleanup.drain_once(self.jdb, delete, now=131)
        )
        delete.assert_called_once_with("files/recover-me")
        self.assertEqual(self._rows(), [])

    def test_recovery_failure_reschedules_without_duplicate_row(self):
        self.cleanup.persist(self.jdb, "files/retry-me", 3, now=100)
        delete = mock.Mock(side_effect=RuntimeError("still unavailable"))
        self.assertTrue(
            self.cleanup.drain_once(self.jdb, delete, now=131)
        )
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "pending")
        self.assertEqual(rows[0]["attempts"], 4)
        self.assertGreater(rows[0]["next_retry_at"], 131)

    def test_worker_start_is_idempotent(self):
        original = self.cleanup._worker_started
        self.cleanup._worker_started = False
        try:
            with mock.patch.object(self.cleanup.threading, "Thread") as thread:
                thread.return_value.start.return_value = None
                self.assertTrue(
                    self.cleanup.start_worker(self.jdb, mock.Mock())
                )
                self.assertFalse(
                    self.cleanup.start_worker(self.jdb, mock.Mock())
                )
            thread.assert_called_once()
        finally:
            self.cleanup._worker_started = original

    def test_provider_404_is_idempotent_success(self):
        with mock.patch.object(
            self.breakdown,
            "_gemini_open",
            side_effect=RuntimeError("Gemini HTTP 404: NOT_FOUND"),
        ):
            self.assertEqual(
                self.breakdown._gemini_delete_resource(
                    "files/already-gone", "mock-key"
                ),
                "already_absent",
            )

    def test_core_starts_cleanup_worker(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "server/content_domains/core.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "gemini_cleanup.start_worker(jdb, breakdown._gemini_delete_resource)",
            source,
        )


if __name__ == "__main__":
    unittest.main()
