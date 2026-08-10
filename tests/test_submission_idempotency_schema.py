import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path


SERVER_DIR = str(Path(__file__).resolve().parents[1] / "server")
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

from content_domains import submission_idempotency


class _BarrierCursor:
    def __init__(self, cursor, barrier):
        self._cursor = cursor
        self._barrier = barrier

    def fetchall(self):
        rows = self._cursor.fetchall()
        self._barrier.wait(timeout=5)
        return rows


class _BarrierConnection:
    def __init__(self, connection, barrier):
        self._connection = connection
        self._barrier = barrier
        self._blocked_schema_read = False

    def execute(self, sql, parameters=()):
        cursor = self._connection.execute(sql, parameters)
        if (
            not self._blocked_schema_read
            and sql.strip().lower().startswith("pragma table_info")
        ):
            self._blocked_schema_read = True
            return _BarrierCursor(cursor, self._barrier)
        return cursor

    def __getattr__(self, name):
        return getattr(self._connection, name)


class SubmissionIdempotencySchemaTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp_dir.name) / "schema.sqlite3")
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("""CREATE TABLE submission_idempotency(
                username TEXT NOT NULL, endpoint TEXT NOT NULL,
                idem_key TEXT NOT NULL, request_hash TEXT NOT NULL,
                response_json TEXT, created_at INTEGER, updated_at INTEGER,
                PRIMARY KEY(username, endpoint, idem_key))""")
            connection.commit()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_concurrent_legacy_schema_migration_is_idempotent(self):
        worker_count = 8
        barrier = threading.Barrier(worker_count)
        errors = []

        def migrate():
            connection = sqlite3.connect(self.path, timeout=5)
            wrapped = _BarrierConnection(connection, barrier)
            try:
                submission_idempotency.ensure_table(wrapped)
                connection.commit()
            except Exception as error:
                errors.append(error)
            finally:
                connection.close()

        workers = [threading.Thread(target=migrate) for _ in range(worker_count)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)

        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual([], errors)
        with closing(sqlite3.connect(self.path)) as connection:
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(submission_idempotency)"
                ).fetchall()
            }
            indexes = {
                row[1]
                for row in connection.execute(
                    "PRAGMA index_list(submission_idempotency)"
                ).fetchall()
            }
        self.assertTrue(set(submission_idempotency._ATTEMPT_COLUMNS) <= columns)
        self.assertIn("idx_submission_idempotency_job", indexes)

    def test_unrelated_alter_table_errors_remain_fatal(self):
        class BrokenConnection:
            def execute(self, sql, parameters=()):
                raise sqlite3.OperationalError("disk I/O error")

        with self.assertRaisesRegex(sqlite3.OperationalError, "disk I/O"):
            submission_idempotency._add_column_if_missing(
                BrokenConnection(), "job_id", "INTEGER"
            )


if __name__ == "__main__":
    unittest.main()
