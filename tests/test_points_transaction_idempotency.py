import importlib
import json
import os
import sqlite3
import tempfile
import threading
import urllib.error
import urllib.request
import unittest
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock


class PointsTransactionIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = os.environ.get("HQ_TEST_AUTH_DB")
        os.environ["HQ_TEST_AUTH_DB"] = os.path.join(self.tmp.name, "users.db")

        import server.auth_server as auth_server

        self.auth = importlib.reload(auth_server)
        self.auth.DB = os.environ["HQ_TEST_AUTH_DB"]
        self.auth.init_db()
        with closing(sqlite3.connect(self.auth.DB)) as connection:
            connection.executemany(
                "INSERT INTO users(username,pw_hash,pw_salt,display_name,points,role,must_change) "
                "VALUES(?,?,?,?,?,'member',0)",
                (("fang", "h", "s", "fang", 10),
                 ("other", "h", "s", "other", 10)),
            )
            connection.commit()

    def tearDown(self):
        if self.old_db is None:
            os.environ.pop("HQ_TEST_AUTH_DB", None)
        else:
            os.environ["HQ_TEST_AUTH_DB"] = self.old_db
        self.tmp.cleanup()

    def _points(self, username="fang"):
        with closing(sqlite3.connect(self.auth.DB)) as connection:
            return connection.execute(
                "SELECT points FROM users WHERE username=?", (username,)
            ).fetchone()[0]

    def _count(self, table):
        with closing(sqlite3.connect(self.auth.DB)) as connection:
            return connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]

    def test_keyed_deduct_replays_original_snapshot_after_response_loss(self):
        key = "job:9001:charge"
        first = self.auth.deduct_points("fang", 3, "first reason", key)
        replay = self.auth.deduct_points("fang", 3, "different reason", key)

        self.assertEqual(first, replay)
        self.assertEqual(first[0]["points"], 7)
        self.assertEqual(self._points(), 7)
        self.assertEqual(self._count("points_audit"), 1)
        self.assertEqual(self._count("points_transactions"), 1)

    def test_keyed_refund_replays_without_double_refund(self):
        key = "job:9001:refund"
        first = self.auth.refund_points("fang", 4, "failed", key)
        replay = self.auth.refund_points("fang", 4, "changed reason", key)

        self.assertEqual(first, replay)
        self.assertEqual(first[0]["points"], 14)
        self.assertEqual(self._points(), 14)
        self.assertEqual(self._count("points_audit"), 1)
        self.assertEqual(self._count("points_transactions"), 1)

    def test_same_key_conflicts_on_user_direction_or_amount_but_not_reason(self):
        key = "job:9002:charge"
        self.auth.deduct_points("fang", 2, "reason-a", key)
        self.auth.deduct_points("fang", 2, "reason-b", key)

        for call in (
                lambda: self.auth.deduct_points("other", 2, "x", key),
                lambda: self.auth.refund_points("fang", 2, "x", key),
                lambda: self.auth.deduct_points("fang", 3, "x", key)):
            with self.assertRaises(self.auth.PointsTransactionConflict):
                call()
        self.assertEqual(self._points(), 8)
        self.assertEqual(self._points("other"), 10)
        self.assertEqual(self._count("points_audit"), 1)

    def test_concurrent_same_key_changes_balance_once(self):
        key = "job:9003:charge"
        results = []
        failures = []
        lock = threading.Lock()

        def worker():
            try:
                result = self.auth.deduct_points("fang", 1, "parallel", key)
                with lock:
                    results.append(result)
            except Exception as exc:  # pragma: no cover - assertion reports it
                with lock:
                    failures.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(failures, [])
        self.assertEqual(len(results), 16)
        self.assertTrue(all(item == results[0] for item in results))
        self.assertEqual(self._points(), 9)
        self.assertEqual(self._count("points_audit"), 1)
        self.assertEqual(self._count("points_transactions"), 1)

    def test_insufficient_rejection_is_stable_and_has_no_fake_audit(self):
        key = "job:9004:charge"
        first = self.auth.deduct_points("fang", 20, "too much", key)
        self.assertEqual(first, (None, "insufficient"))
        with closing(sqlite3.connect(self.auth.DB)) as connection:
            connection.execute("UPDATE users SET points=100 WHERE username='fang'")
            connection.commit()

        replay = self.auth.deduct_points("fang", 20, "now affordable", key)
        self.assertEqual(replay, (None, "insufficient"))
        self.assertEqual(self._points(), 100)
        self.assertEqual(self._count("points_audit"), 0)
        self.assertEqual(self._count("points_transactions"), 1)

    def test_unknown_user_rejection_is_stable_after_user_is_created(self):
        key = "job:9005:charge"
        self.assertEqual(
            self.auth.deduct_points("later", 1, "missing", key),
            (None, "not_found"),
        )
        with closing(sqlite3.connect(self.auth.DB)) as connection:
            connection.execute(
                "INSERT INTO users(username,pw_hash,pw_salt,display_name,points,role,must_change) "
                "VALUES('later','h','s','later',10,'member',0)"
            )
            connection.commit()
        self.assertEqual(
            self.auth.deduct_points("later", 1, "created", key),
            (None, "not_found"),
        )
        self.assertEqual(self._points("later"), 10)
        self.assertEqual(self._count("points_audit"), 0)

    def test_restart_replays_committed_transaction_snapshot(self):
        key = "job:9006:charge"
        first = self.auth.deduct_points("fang", 4, "before restart", key)
        self.auth = importlib.reload(self.auth)
        self.auth.DB = os.environ["HQ_TEST_AUTH_DB"]
        self.auth.init_db()

        replay = self.auth.deduct_points("fang", 4, "after restart", key)
        self.assertEqual(replay, first)
        self.assertEqual(self._points(), 6)
        self.assertEqual(self._count("points_audit"), 1)

    def test_database_failure_rolls_back_balance_audit_and_snapshot(self):
        key = "job:9007:charge"
        original = self.auth._record_points_transaction

        def fail_after_balance(*args, **kwargs):
            raise sqlite3.OperationalError("snapshot disk failure")

        with mock.patch.object(
                self.auth, "_record_points_transaction", side_effect=fail_after_balance):
            with self.assertRaises(sqlite3.OperationalError):
                self.auth.deduct_points("fang", 3, "fault", key)

        self.assertEqual(self._points(), 10)
        self.assertEqual(self._count("points_audit"), 0)
        self.assertEqual(self._count("points_transactions"), 0)
        self.auth._record_points_transaction = original

    def test_migration_without_legacy_audit_key_is_idempotent(self):
        self.auth.init_db()
        self.auth.init_db()
        with closing(sqlite3.connect(self.auth.DB)) as connection:
            audit_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(points_audit)")
            }
            tx_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(points_transactions)")
            }
        self.assertNotIn("transaction_key", audit_columns)
        self.assertIn("transaction_key", tx_columns)
        self.assertEqual(self._count("points_transactions"), 0)

    def test_migration_imports_legacy_audit_key_without_changing_balance(self):
        key = "legacy:123:charge"
        with closing(sqlite3.connect(self.auth.DB)) as connection:
            connection.execute("ALTER TABLE points_audit ADD COLUMN transaction_key TEXT")
            connection.execute(
                "INSERT INTO points_audit"
                "(who_admin,username,delta,before_points,after_points,reason,created_at,transaction_key) "
                "VALUES('system','fang',-3,10,7,'legacy',123,?)", (key,)
            )
            connection.execute("UPDATE users SET points=7 WHERE username='fang'")
            connection.execute("DROP TABLE points_transactions")
            connection.commit()

        self.auth.init_db()
        self.auth.init_db()
        replay = self.auth.deduct_points("fang", 3, "new reason", key)
        self.assertEqual(replay[0]["points"], 7)
        self.assertEqual(self._points(), 7)
        self.assertEqual(self._count("points_audit"), 1)
        self.assertEqual(self._count("points_transactions"), 1)

    def _prepare_legacy_audit_rows(self, rows, unique=False):
        with closing(sqlite3.connect(self.auth.DB)) as connection:
            connection.execute("ALTER TABLE points_audit ADD COLUMN transaction_key TEXT")
            if unique:
                connection.execute(
                    "CREATE UNIQUE INDEX idx_points_audit_transaction_key "
                    "ON points_audit(transaction_key) WHERE transaction_key IS NOT NULL"
                )
            connection.executemany(
                "INSERT INTO points_audit"
                "(who_admin,username,delta,before_points,after_points,reason,created_at,transaction_key) "
                "VALUES('system',?,?,?,?,?,?,?)",
                rows,
            )
            connection.execute("DROP TABLE points_transactions")
            connection.commit()

    def test_migration_rejects_duplicate_key_with_different_balance_snapshots(self):
        key = "legacy:duplicate:balance"
        self._prepare_legacy_audit_rows((
            ("fang", -3, 10, 7, "first", 100, key),
            ("fang", -3, 7, 4, "second", 101, key),
        ))
        with closing(sqlite3.connect(self.auth.DB)) as connection:
            connection.execute("UPDATE users SET points=4 WHERE username='fang'")
            connection.commit()

        with self.assertRaises(self.auth.PointsTransactionConflict):
            self.auth.init_db()
        self.assertEqual(self._points(), 4)
        self.assertEqual(self._count("points_audit"), 2)
        with closing(sqlite3.connect(self.auth.DB)) as connection:
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='points_transactions'"
            ).fetchone()
        self.assertIsNone(table)

    def test_migration_rejects_duplicate_key_with_conflicting_parameters(self):
        key = "legacy:duplicate:params"
        self._prepare_legacy_audit_rows((
            ("fang", -3, 10, 7, "first", 100, key),
            ("other", 3, 10, 13, "second", 101, key),
        ))

        with self.assertRaises(self.auth.PointsTransactionConflict):
            self.auth.init_db()
        self.assertEqual(self._count("points_audit"), 2)

    def test_migration_accepts_single_legacy_row_with_unique_index(self):
        key = "legacy:unique:charge"
        self._prepare_legacy_audit_rows((
            ("fang", -3, 10, 7, "single", 100, key),
        ), unique=True)
        with closing(sqlite3.connect(self.auth.DB)) as connection:
            connection.execute("UPDATE users SET points=7 WHERE username='fang'")
            connection.commit()

        self.auth.init_db()
        replay = self.auth.deduct_points("fang", 3, "replay", key)
        self.assertEqual(replay[0]["points"], 7)
        self.assertEqual(self._count("points_audit"), 1)
        self.assertEqual(self._count("points_transactions"), 1)

    def test_migration_rejects_existing_snapshot_balance_or_time_mismatch(self):
        key = "legacy:snapshot:mismatch"
        self._prepare_legacy_audit_rows((
            ("fang", -3, 10, 7, "single", 100, key),
        ))
        self.auth.init_db()

        for sql in (
                "UPDATE points_transactions SET result_json='{\"username\":\"fang\",\"user_id\":1,\"points\":4}'",
                "UPDATE points_transactions SET created_at=101,updated_at=101"):
            with self.subTest(sql=sql):
                with closing(sqlite3.connect(self.auth.DB)) as connection:
                    connection.execute(sql)
                    connection.commit()
                with self.assertRaises(self.auth.PointsTransactionConflict):
                    self.auth.init_db()
                with closing(sqlite3.connect(self.auth.DB)) as connection:
                    connection.execute(
                        "UPDATE points_transactions SET result_json=?,created_at=100,updated_at=100",
                        (json.dumps({"username": "fang", "user_id": 1, "points": 7}),),
                    )
                    connection.commit()

    def test_existing_audit_transaction_key_column_records_new_key(self):
        with closing(sqlite3.connect(self.auth.DB)) as connection:
            connection.execute("ALTER TABLE points_audit ADD COLUMN transaction_key TEXT")
            connection.execute(
                "CREATE UNIQUE INDEX idx_points_audit_transaction_key "
                "ON points_audit(transaction_key) WHERE transaction_key IS NOT NULL"
            )
            connection.commit()

        key = "job:9009:charge"
        result, err = self.auth.deduct_points("fang", 2, "keyed", key)
        self.assertIsNone(err)
        self.assertEqual(result["points"], 8)
        with closing(sqlite3.connect(self.auth.DB)) as connection:
            audit_key = connection.execute(
                "SELECT transaction_key FROM points_audit"
            ).fetchone()[0]
        self.assertEqual(audit_key, key)

    def test_http_replay_is_identical_and_conflicts_are_explicit(self):
        self.auth.INTERNAL_TOKEN = "test-internal-token"
        server = ThreadingHTTPServer(("127.0.0.1", 0), self.auth.H)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = "http://127.0.0.1:%d/api/auth/points/deduct" % server.server_address[1]

        def post(payload):
            request = urllib.request.Request(
                url,
                data=json.dumps(payload).encode(),
                headers={
                    "Content-Type": "application/json",
                    "X-HQ-Internal-Token": "test-internal-token",
                },
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read())

        try:
            payload = {
                "username": "fang", "amount": 2,
                "reason": "first", "transaction_key": "job:9010:charge",
            }
            first = post(payload)
            payload["reason"] = "response was lost"
            replay = post(payload)
            self.assertEqual(first, replay)

            payload["amount"] = 3
            with self.assertRaises(urllib.error.HTTPError) as conflict:
                post(payload)
            self.assertEqual(conflict.exception.code, 409)

            payload["transaction_key"] = "bad key"
            with self.assertRaises(urllib.error.HTTPError) as invalid:
                post(payload)
            self.assertEqual(invalid.exception.code, 400)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_old_unkeyed_calls_keep_existing_behavior_and_do_not_snapshot(self):
        first, err = self.auth.deduct_points("fang", 2, "legacy deduct")
        self.assertIsNone(err)
        second, err = self.auth.refund_points("fang", 1, "legacy refund")
        self.assertIsNone(err)
        self.assertEqual(first["points"], 8)
        self.assertEqual(second["points"], 9)
        self.assertEqual(self._count("points_audit"), 2)
        self.assertEqual(self._count("points_transactions"), 0)

    def test_points_client_only_transmits_optional_key_when_present(self):
        import sys

        server_dir = str(Path(__file__).resolve().parents[1] / "server")
        if server_dir not in sys.path:
            sys.path.insert(0, server_dir)
        points_client = importlib.import_module("content_domains.points")

        calls = []

        def fake_request(path, payload=None, method="POST"):
            calls.append((path, payload, method))
            return {"points": 8}

        with mock.patch.object(points_client, "_auth_points_request", fake_request):
            self.assertEqual(points_client.deduct_points("fang", 2, "old"), 8)
            self.assertEqual(
                points_client.refund_points(
                    "fang", 2, "new", transaction_key="job:9008:refund"
                ),
                8,
            )

        self.assertNotIn("transaction_key", calls[0][1])
        self.assertEqual(calls[1][1]["transaction_key"], "job:9008:refund")


if __name__ == "__main__":
    unittest.main()
