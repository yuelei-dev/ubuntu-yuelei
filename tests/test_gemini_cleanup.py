import hashlib
import importlib
import io
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import closing, redirect_stdout
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

    def _run_scans(self, scans, delete, now=None):
        calls = []

        def fake_sleep(_interval):
            calls.append(1)
            if len(calls) >= scans:
                raise RuntimeError("stop scanner")

        with self.assertRaisesRegex(RuntimeError, "stop scanner"):
            self.cleanup.scanner(
                self.jdb, delete, interval=0, sleep=fake_sleep, now=now
            )
        return calls

    def test_missing_key_scans_do_not_consume_retry_budget(self):
        self.cleanup.persist(self.jdb, "files/no-key", 3, now=100)
        delete = mock.Mock(return_value="deleted")
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": ""}):
            self._run_scans(3, delete, now=131)
        delete.assert_not_called()
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["attempts"], 3)
        self.assertEqual(rows[0]["status"], "pending")
        self.assertEqual(rows[0]["next_retry_at"], 130)
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "restored"}):
            self._run_scans(1, delete, now=131)
        delete.assert_called_once_with("files/no-key")
        self.assertEqual(self._rows(), [])

    def test_claim_audits_exhausted_rows_without_raw_resource_name(self):
        self.cleanup.persist(
            self.jdb, "files/maxed-out", self.cleanup.QUEUE_MAX_ATTEMPTS,
            now=100,
        )
        self.cleanup.persist(self.jdb, "files/retention-gone", 3, now=100)
        delete = mock.Mock()
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertFalse(
                self.cleanup.drain_once(
                    self.jdb, delete, now=100 + 47 * 3600 + 1
                )
            )
        delete.assert_not_called()
        self.assertEqual(self._rows(), [])
        audits = [
            line
            for line in output.getvalue().splitlines()
            if "retry_window_exhausted" in line
        ]
        self.assertEqual(len(audits), 2)
        for name in ("files/maxed-out", "files/retention-gone"):
            digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:12]
            match = [line for line in audits if digest in line]
            self.assertEqual(len(match), 1)
            self.assertNotIn(name, match[0])
        maxed = hashlib.sha256(b"files/maxed-out").hexdigest()[:12]
        line = next(line for line in audits if maxed in line)
        self.assertIn(
            '"attempts": %d' % self.cleanup.QUEUE_MAX_ATTEMPTS, line
        )

    def test_expired_lease_is_recovered_after_restart(self):
        self.cleanup.persist(self.jdb, "files/lease-me", 1, now=100)
        claimed = self.cleanup._claim(self.jdb, now=131)
        self.assertIsNotNone(claimed)
        row = self._rows()[0]
        self.assertEqual(row["status"], "deleting")
        lease_end = 131 + self.cleanup.QUEUE_LEASE_SECONDS
        self.assertEqual(row["lease_until"], lease_end)
        # 进程重启：ensure_table 回收过期租约，记录回到 pending
        self.cleanup.ensure_table(self.jdb, now=lease_end + 1)
        row = self._rows()[0]
        self.assertEqual(row["status"], "pending")
        delete = mock.Mock(return_value="deleted")
        self.assertTrue(
            self.cleanup.drain_once(self.jdb, delete, now=lease_end + 1)
        )
        delete.assert_called_once_with("files/lease-me")
        self.assertEqual(self._rows(), [])

    def test_concurrent_workers_claim_same_row_only_once(self):
        self.cleanup.persist(self.jdb, "files/race-me", 1, now=100)
        delete = mock.Mock(return_value="deleted")
        barrier = threading.Barrier(2)
        results = []

        def worker():
            barrier.wait(timeout=10)
            results.append(
                self.cleanup.drain_once(self.jdb, delete, now=131)
            )

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(delete.call_count, 1)
        self.assertEqual(self._rows(), [])

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
