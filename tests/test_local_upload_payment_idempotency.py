# -*- coding: utf-8 -*-
import importlib
import io
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import ExitStack, closing
from pathlib import Path
from unittest import mock


class _IdempotentPoints:
    def __init__(self, auth_error, fail_first_charge=False, reject_status=None):
        self.AuthPointsError = auth_error
        self.fail_first_charge = fail_first_charge
        self.reject_status = reject_status
        self.calls = []
        self.applied = {}
        self._lock = threading.Lock()

    @staticmethod
    def breakdown_local_upload_cost():
        return 20

    @staticmethod
    def get_points(username):
        return 100

    def deduct_points(self, username, amount, reason="", transaction_key=""):
        with self._lock:
            self.calls.append((username, amount, reason, transaction_key))
            if self.reject_status:
                raise self.AuthPointsError(self.reject_status, "charge rejected")
            first = transaction_key not in self.applied
            if first:
                self.applied[transaction_key] = (username, amount)
            if first and self.fail_first_charge:
                raise self.AuthPointsError(502, "response lost")
            return 100 - int(amount)


class LocalUploadPaymentIdempotencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[1]
        server = str(cls.root / "server")
        if server not in sys.path:
            sys.path.insert(0, server)
        cls.breakdown = importlib.import_module("content_domains.breakdown")
        cls.core = importlib.import_module("content_domains.core")
        cls.points = importlib.import_module("content_domains.points")

    @staticmethod
    def _jdb(path):
        def connect():
            connection = sqlite3.connect(path, timeout=10)
            connection.row_factory = sqlite3.Row
            return connection
        return connect

    @staticmethod
    def _create_database(path):
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("""CREATE TABLE jobs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT, username TEXT, cost INTEGER,
                status TEXT DEFAULT 'pending', payload TEXT, result TEXT, error TEXT,
                created_at INTEGER, updated_at INTEGER,
                deleted INTEGER DEFAULT 0, refunded INTEGER DEFAULT 0,
                owner TEXT, service_sha TEXT
            )""")
            connection.commit()

    @staticmethod
    def _handler(data, key="local-idem-key-0001", username="alice"):
        handler = mock.Mock()
        handler.path = "/api/gen/breakdown/local-upload?media_type=image"
        handler.headers = {
            "Content-Type": "image/jpeg",
            "Content-Length": str(len(data)),
        }
        if key is not None:
            handler.headers["Idempotency-Key"] = key
        handler.rfile = io.BytesIO(data)
        handler.username = username
        return handler

    def _context(self, root, connect, points, enqueue=True):
        stack = ExitStack()
        stack.enter_context(mock.patch.object(self.core, "OUT_DIR", root))
        stack.enter_context(mock.patch.object(self.core, "jdb", connect))
        stack.enter_context(mock.patch.object(
            self.core, "_domains", return_value=(mock.Mock(), points, mock.Mock())
        ))
        stack.enter_context(mock.patch.object(
            self.core.feature_flags, "require_enabled", return_value=None
        ))
        stack.enter_context(mock.patch.object(
            self.core, "is_shutting_down", return_value=False
        ))
        stack.enter_context(mock.patch.object(
            self.core, "_user_active_job_count", return_value=0
        ))
        stack.enter_context(mock.patch.object(
            self.core, "enqueue_job", return_value=enqueue
        ))
        return stack

    def test_missing_key_is_rejected_before_body_or_database(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "jobs.db"
            self._create_database(database)
            connect = self._jdb(database)
            points = _IdempotentPoints(self.points.AuthPointsError)
            handler = self._handler(b"\xff\xd8\xffjpeg", key=None)
            with self._context(root, connect, points):
                self.breakdown.handle_local_upload(handler, {"username": "alice"})
            self.assertEqual(handler._send.call_args.args[0], 400)
            self.assertEqual(handler.rfile.tell(), 0)
            self.assertEqual(points.calls, [])
            with closing(connect()) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)

    def test_same_key_response_loss_replays_charge_and_job(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "jobs.db"
            self._create_database(database)
            connect = self._jdb(database)
            points = _IdempotentPoints(
                self.points.AuthPointsError, fail_first_charge=True
            )
            first = self._handler(b"\xff\xd8\xffsame")
            second = self._handler(b"\xff\xd8\xffsame")
            with self._context(root, connect, points):
                self.breakdown.handle_local_upload(first, {"username": "alice"})
                self.breakdown.handle_local_upload(second, {"username": "alice"})
            self.assertEqual(first._send.call_args.args[0], 202)
            self.assertEqual(second._send.call_args.args[0], 200)
            self.assertEqual(first._send.call_args.args[1]["job_id"], 1)
            self.assertEqual(second._send.call_args.args[1]["job_id"], 1)
            self.assertEqual(len(points.applied), 1)
            self.assertEqual([call[3] for call in points.calls], [
                "breakdown:1:charge", "breakdown:1:charge",
            ])
            with closing(connect()) as connection:
                row = connection.execute("SELECT status FROM jobs WHERE id=1").fetchone()
                self.assertEqual(row["status"], "pending")
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)

    def test_new_key_same_file_is_a_new_paid_job(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "jobs.db"
            self._create_database(database)
            connect = self._jdb(database)
            points = _IdempotentPoints(self.points.AuthPointsError)
            first = self._handler(b"\xff\xd8\xffsame", key="local-new-key-0001")
            second = self._handler(b"\xff\xd8\xffsame", key="local-new-key-0002")
            with self._context(root, connect, points):
                self.breakdown.handle_local_upload(first, {"username": "alice"})
                self.breakdown.handle_local_upload(second, {"username": "alice"})
            self.assertNotEqual(
                first._send.call_args.args[1]["job_id"],
                second._send.call_args.args[1]["job_id"],
            )
            self.assertEqual(len(points.applied), 2)
            with closing(connect()) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 2)

    def test_same_key_different_file_conflicts_without_second_charge(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "jobs.db"
            self._create_database(database)
            connect = self._jdb(database)
            points = _IdempotentPoints(self.points.AuthPointsError)
            first = self._handler(b"\xff\xd8\xfffirst")
            second = self._handler(b"\xff\xd8\xffsecond")
            with self._context(root, connect, points):
                self.breakdown.handle_local_upload(first, {"username": "alice"})
                self.breakdown.handle_local_upload(second, {"username": "alice"})
            self.assertEqual(second._send.call_args.args[0], 409)
            self.assertEqual(second._send.call_args.args[1]["code"], "idempotency_conflict")
            self.assertEqual(len(points.applied), 1)

    def test_same_key_is_isolated_by_username(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "jobs.db"
            self._create_database(database)
            connect = self._jdb(database)
            points = _IdempotentPoints(self.points.AuthPointsError)
            alice = self._handler(b"\xff\xd8\xffsame")
            bob = self._handler(b"\xff\xd8\xffsame")
            with self._context(root, connect, points):
                self.breakdown.handle_local_upload(alice, {"username": "alice"})
                self.breakdown.handle_local_upload(bob, {"username": "bob"})
            self.assertEqual(alice._send.call_args.args[0], 200)
            self.assertEqual(bob._send.call_args.args[0], 200)
            self.assertNotEqual(
                alice._send.call_args.args[1]["job_id"],
                bob._send.call_args.args[1]["job_id"],
            )
            self.assertEqual(len(points.applied), 2)

    def test_activation_database_failure_recovers_with_same_charge_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "jobs.db"
            self._create_database(database)
            connect = self._jdb(database)
            points = _IdempotentPoints(self.points.AuthPointsError)
            first = self._handler(b"\xff\xd8\xffsame")
            second = self._handler(b"\xff\xd8\xffsame")
            original = self.breakdown._complete_local_idempotency
            calls = {"count": 0}

            def fail_once(*args, **kwargs):
                calls["count"] += 1
                if calls["count"] == 1:
                    raise sqlite3.OperationalError("activation commit unavailable")
                return original(*args, **kwargs)

            with self._context(root, connect, points), mock.patch.object(
                self.breakdown, "_complete_local_idempotency", side_effect=fail_once
            ):
                self.breakdown.handle_local_upload(first, {"username": "alice"})
                self.breakdown.handle_local_upload(second, {"username": "alice"})
            self.assertEqual(first._send.call_args.args[0], 202)
            self.assertEqual(second._send.call_args.args[0], 200)
            self.assertEqual(len(points.applied), 1)
            self.assertEqual([call[3] for call in points.calls], [
                "breakdown:1:charge", "breakdown:1:charge",
            ])

    def test_restart_reconcile_activates_reserved_job_without_second_charge(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "jobs.db"
            self._create_database(database)
            connect = self._jdb(database)
            points = _IdempotentPoints(
                self.points.AuthPointsError, fail_first_charge=True
            )
            handler = self._handler(b"\xff\xd8\xffsame")
            with self._context(root, connect, points):
                self.breakdown.handle_local_upload(handler, {"username": "alice"})
                self.assertEqual(handler._send.call_args.args[0], 202)
                self.assertEqual(self.breakdown.reconcile_local_upload_submissions(), 1)
            self.assertEqual(len(points.applied), 1)
            with closing(connect()) as connection:
                self.assertEqual(
                    connection.execute("SELECT status FROM jobs WHERE id=1").fetchone()[0],
                    "pending",
                )

    def test_terminal_charge_rejection_is_not_refunded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "jobs.db"
            self._create_database(database)
            connect = self._jdb(database)
            points = _IdempotentPoints(
                self.points.AuthPointsError, reject_status=402
            )
            handler = self._handler(b"\xff\xd8\xffsame")
            with self._context(root, connect, points):
                self.breakdown.handle_local_upload(handler, {"username": "alice"})
                self.assertEqual(handler._send.call_args.args[0], 402)
                self.assertEqual(self.core._retry_failed_refunds(), 0)
            with closing(connect()) as connection:
                row = connection.execute("SELECT status,cost,refunded FROM jobs").fetchone()
            self.assertEqual((row["status"], row["cost"], row["refunded"]), ("error", 0, 0))

    def test_missing_reserved_source_fails_before_charge(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "jobs.db"
            self._create_database(database)
            connect = self._jdb(database)
            points = _IdempotentPoints(self.points.AuthPointsError)
            source = root / "_breakdown_uploads" / ("a" * 32 + ".jpg")
            source.parent.mkdir()
            source.write_bytes(b"\xff\xd8\xffsame")
            body = {"upload_token": "a" * 32, "media_type": "image", "mode": "reverse_prompt"}
            with self._context(root, connect, points):
                state, _ = self.breakdown._reserve_local_upload(
                    self.core, "alice", "local-missing-key-1",
                    {"media_type": "image", "content_sha256": "hash"}, body,
                    "a" * 32, ".jpg", 20,
                )
                self.assertEqual(state, "new")
                source.unlink()
                self.assertEqual(self.breakdown.reconcile_local_upload_submissions(), 0)
            self.assertEqual(points.calls, [])
            with closing(connect()) as connection:
                row = connection.execute("SELECT status,cost FROM jobs").fetchone()
            self.assertEqual((row["status"], row["cost"]), ("error", 0))

    def test_concurrent_same_key_creates_one_job_and_one_charge(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "jobs.db"
            self._create_database(database)
            connect = self._jdb(database)
            points = _IdempotentPoints(self.points.AuthPointsError)
            handlers = [self._handler(b"\xff\xd8\xffsame") for _ in range(6)]
            errors = []
            with self._context(root, connect, points):
                threads = [threading.Thread(
                    target=lambda item=h: self.breakdown.handle_local_upload(
                        item, {"username": "alice"}
                    ),
                ) for h in handlers]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=10)
                    if thread.is_alive():
                        errors.append("thread did not finish")
            self.assertEqual(errors, [])
            self.assertEqual(len(points.applied), 1)
            with closing(connect()) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)
            self.assertEqual({h._send.call_args.args[1]["job_id"] for h in handlers}, {1})


class RefundRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        server = str(root / "server")
        if server not in sys.path:
            sys.path.insert(0, server)
        cls.core = importlib.import_module("content_domains.core")

    @staticmethod
    def _jdb(path):
        def connect():
            connection = sqlite3.connect(path)
            connection.row_factory = sqlite3.Row
            return connection
        return connect

    def test_refund_response_loss_replays_same_key_and_unrelated_errors_do_not_starve(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "jobs.db"
            connect = self._jdb(database)
            with closing(connect()) as connection:
                connection.execute("""CREATE TABLE jobs(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, username TEXT,
                    cost INTEGER, status TEXT, payload TEXT, result TEXT, error TEXT,
                    created_at INTEGER, updated_at INTEGER, deleted INTEGER DEFAULT 0,
                    refunded INTEGER DEFAULT 0, owner TEXT
                )""")
                for _ in range(100):
                    connection.execute(
                        "INSERT INTO jobs(kind,username,cost,status,refunded,owner) "
                        "VALUES('video','u',20,'error',0,'leadgen')"
                    )
                target = connection.execute(
                    "INSERT INTO jobs(kind,username,cost,status,refunded,owner) "
                    "VALUES('breakdown','alice',20,'error',0,'content')"
                ).lastrowid
                connection.commit()

            class RefundPoints:
                calls = []
                applied = set()

                @classmethod
                def refund_points(cls, username, amount, reason="", transaction_key=""):
                    cls.calls.append(transaction_key)
                    first = transaction_key not in cls.applied
                    cls.applied.add(transaction_key)
                    if first:
                        raise RuntimeError("response lost after commit")
                    return 100

            with mock.patch.object(self.core, "jdb", connect), mock.patch.object(
                self.core, "_domains", return_value=(mock.Mock(), RefundPoints, mock.Mock())
            ):
                self.assertEqual(self.core._retry_failed_refunds(limit=1), 0)
                with closing(connect()) as connection:
                    self.assertEqual(
                        connection.execute("SELECT refunded FROM jobs WHERE id=?", (target,)).fetchone()[0],
                        0,
                    )
                self.assertEqual(self.core._retry_failed_refunds(limit=1), 1)
                self.assertEqual(self.core._retry_failed_refunds(limit=1), 0)

            expected = "job:%d:refund" % target
            self.assertEqual(RefundPoints.calls, [expected, expected])
            self.assertEqual(RefundPoints.applied, {expected})
            with closing(connect()) as connection:
                self.assertEqual(
                    connection.execute("SELECT refunded FROM jobs WHERE id=?", (target,)).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM jobs WHERE owner='leadgen' AND refunded<>0"
                    ).fetchone()[0],
                    0,
                )

    def test_process_exit_after_refund_claim_converges_on_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "jobs.db"
            connect = self._jdb(database)
            with closing(connect()) as connection:
                target = connection.execute(
                    "CREATE TABLE jobs(id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT,"
                    "username TEXT,cost INTEGER,status TEXT,refunded INTEGER DEFAULT 0)"
                )
                target = connection.execute(
                    "INSERT INTO jobs(kind,username,cost,status,refunded) "
                    "VALUES('breakdown','alice',20,'error',0)"
                ).lastrowid
                connection.commit()

            class RestartedPoints:
                crash = True
                calls = []

                @classmethod
                def refund_points(cls, username, amount, reason="", transaction_key=""):
                    cls.calls.append(transaction_key)
                    if cls.crash:
                        raise SystemExit("process exited after lease claim")
                    return 100

            with mock.patch.object(self.core, "jdb", connect), mock.patch.object(
                self.core, "_domains", return_value=(mock.Mock(), RestartedPoints, mock.Mock())
            ):
                with self.assertRaises(SystemExit):
                    self.core._refund_once(target, "alice", 20)
                with closing(connect()) as connection:
                    pending = connection.execute(
                        "SELECT refunded,refund_lease_token FROM jobs WHERE id=?",
                        (target,),
                    ).fetchone()
                    self.assertEqual(pending["refunded"], 2)
                    self.assertTrue(pending["refund_lease_token"])
                    connection.execute(
                        "UPDATE jobs SET refund_lease_until=0 WHERE id=?", (target,)
                    )
                    connection.commit()
                RestartedPoints.crash = False
                # Historical rows created before jobs.owner existed are owned by
                # content and remain eligible for content-only reconciliation.
                self.assertEqual(self.core._retry_failed_refunds(limit=10), 1)
                self.assertEqual(self.core._retry_failed_refunds(limit=10), 0)

            expected = "job:%d:refund" % target
            self.assertEqual(RestartedPoints.calls, [expected, expected])
            with closing(connect()) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT refunded FROM jobs WHERE id=?", (target,)
                    ).fetchone()[0],
                    1,
                )


if __name__ == "__main__":
    unittest.main()
