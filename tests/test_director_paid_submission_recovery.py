import concurrent.futures
import json
import pathlib
import queue
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import ExitStack, closing
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from content_domains import breakdown, core, jobs_store, submission_idempotency, upstream_guard


PNG = b"\x89PNG\r\n\x1a\npaid-recovery-fixture"


class FakePointsError(Exception):
    def __init__(self, status, detail):
        self.status = int(status)
        self.detail = str(detail)


class FakePoints:
    AuthPointsError = FakePointsError

    def __init__(self):
        self.deductions = []
        self.refunds = []
        self.ledger = {}
        self.refund_ledger = {}
        self.lose_next_refund_response = False

    @staticmethod
    def cost_of(kind, _body):
        return 20 if kind == "breakdown" else 10

    def deduct_points(self, username, cost, reason="", transaction_key=""):
        if transaction_key in self.ledger:
            return self.ledger[transaction_key]["after_points"]
        result = {
            "username": username, "delta": -int(cost),
            "after_points": 1000 - int(cost),
        }
        self.ledger[transaction_key] = result
        self.deductions.append((username, int(cost), transaction_key))
        return result["after_points"]

    def get_points_transaction(self, transaction_key):
        return self.ledger.get(transaction_key)

    def refund_points(self, username, cost, reason="", transaction_key=""):
        if transaction_key in self.refund_ledger:
            return 1000
        self.refund_ledger[transaction_key] = True
        self.refunds.append((username, int(cost), transaction_key))
        if self.lose_next_refund_response:
            self.lose_next_refund_response = False
            raise ConnectionError("refund response lost after commit")
        return 1000

    @staticmethod
    def public_error_body(error, cost):
        return {"detail": error.detail, "need": int(cost)}


class PaidSubmissionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.temp = tempfile.TemporaryDirectory()
        self.stack.callback(self.temp.cleanup)
        root = pathlib.Path(self.temp.name)
        self.database = str(root / "jobs.db")
        self.points = FakePoints()
        self.enqueue_allowed = True
        self.fake_video = SimpleNamespace(
            SeedanceReferenceUnavailable=type(
                "SeedanceReferenceUnavailable", (Exception,), {},
            ),
            prepare_xiaole_reference_submission=(
                lambda *_args, **_kwargs: ([], False, None)
            ),
        )
        fake_short_drama = SimpleNamespace(
            RevisionConflict=type("RevisionConflict", (Exception,), {}),
            fail_linked_character_reference_job=lambda *_args, **_kwargs: None,
            short_drama_production=SimpleNamespace(
                fail_linked_job=lambda *_args, **_kwargs: None,
            ),
        )
        self.stack.enter_context(mock.patch.object(core, "JOB_DB", self.database))

        def test_jdb():
            connection = sqlite3.connect(self.database, timeout=30)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=30000")
            return connection

        self.stack.enter_context(mock.patch.object(core, "jdb", test_jdb))
        self.stack.enter_context(mock.patch.object(core, "OUT_DIR", root / "out"))
        self.stack.enter_context(mock.patch.object(
            core, "verify", lambda token: {
                "username": str(token or ""), "must_change": False,
                "points": 1000,
            } if token else None,
        ))
        self.stack.enter_context(mock.patch.object(
            core, "_domains", lambda: (None, self.points, self.fake_video),
        ))
        self.stack.enter_context(mock.patch.object(
            core, "_dispatch_short_drama", lambda *_args, **_kwargs: False,
        ))
        self.stack.enter_context(mock.patch.object(
            core, "_short_drama_domain", lambda: fake_short_drama,
        ))
        self.stack.enter_context(mock.patch.object(
            core.feature_flags, "require_enabled", lambda _kind: None,
        ))
        self.stack.enter_context(mock.patch.object(
            core.miniprogram_security, "check_payload", lambda _body: None,
        ))
        self.stack.enter_context(mock.patch.object(
            upstream_guard, "exhausted_reason", lambda *_args: None,
        ))
        self.stack.enter_context(mock.patch.object(
            core.cli_gateway, "reject_changed_cost", lambda *_args: False,
        ))
        self.stack.enter_context(mock.patch.object(
            core, "enqueue_job",
            lambda *_args, **_kwargs: bool(self.enqueue_allowed),
        ))
        self.stack.enter_context(mock.patch.object(core, "MAX_USER_ACTIVE_JOBS", 20))
        self.stack.enter_context(mock.patch.object(
            core, "HANDLERS", {"copy": lambda payload: payload},
        ))
        core._shutting_down.clear()
        with closing(sqlite3.connect(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("""CREATE TABLE jobs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT, username TEXT, cost INTEGER,
                status TEXT DEFAULT 'pending', payload TEXT,
                result TEXT, error TEXT, created_at INTEGER,
                updated_at INTEGER, deleted INTEGER DEFAULT 0,
                refunded INTEGER DEFAULT 0, owner TEXT
            )""")
            submission_idempotency.ensure_table(connection)
            connection.commit()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), core.H)
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True,
        )
        self.thread.start()
        self.stack.callback(self.server.server_close)
        self.stack.callback(self.server.shutdown)
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def tearDown(self):
        self.stack.close()

    def _restart_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), core.H)
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True,
        )
        self.thread.start()
        self.stack.callback(self.server.server_close)
        self.stack.callback(self.server.shutdown)
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def _request(self, path, data, key, token="fang", content_type="application/json"):
        headers = {
            "Authorization": "Bearer " + token,
            "Content-Type": content_type,
            "Idempotency-Key": key,
        }
        request = urllib.request.Request(
            self.base + path, data=data, method="POST", headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"{}")
        except Exception as error:
            return 0, {"error": type(error).__name__}

    def _copy(self, key, prompt="夏季通勤防晒", token="fang"):
        return self._request(
            "/api/gen/copy",
            json.dumps({"prompt": prompt, "format": "script"}).encode(),
            key, token=token,
        )

    def _upload(self, key, data=PNG, token="fang"):
        return self._request(
            "/api/gen/breakdown/local-upload?media_type=image",
            data, key, token=token, content_type="image/png",
        )

    def _job_count(self):
        with closing(core.jdb()) as connection:
            return connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

    def test_copy_recovers_charge_confirmation_and_response_loss(self):
        original_mark = core._idempotency_mark_charged
        mark_calls = 0

        def crash_after_charge(*args, **kwargs):
            nonlocal mark_calls
            result = original_mark(*args, **kwargs)
            mark_calls += 1
            if mark_calls == 1:
                raise SystemExit("simulated process exit after charge")
            return result

        key = "copy-charge-recovery-0001"
        with mock.patch.object(
                core, "_idempotency_mark_charged", side_effect=crash_after_charge):
            first_status, _ = self._copy(key)
        self.assertEqual(0, first_status)
        status, accepted = self._copy(key)
        self.assertEqual(200, status)
        self.assertEqual(1, len(self.points.deductions))
        self.assertEqual(1, self._job_count())

        original_complete = core._idempotency_complete
        completed = False

        def crash_before_response(*args, **kwargs):
            nonlocal completed
            if not completed:
                completed = True
                raise SystemExit("simulated process exit before response snapshot")
            return original_complete(*args, **kwargs)

        second_key = "copy-response-recovery-0002"
        with mock.patch.object(
                core, "_idempotency_complete", side_effect=crash_before_response):
            lost_status, _ = self._copy(second_key, prompt="秋季通勤护理")
        self.assertEqual(0, lost_status)
        replay_status, replay = self._copy(second_key, prompt="秋季通勤护理")
        self.assertEqual(200, replay_status)
        self.assertEqual(accepted["cost"], replay["cost"])
        self.assertEqual(2, len(self.points.deductions))
        self.assertEqual(2, self._job_count())

    def test_local_upload_recovers_charge_and_linked_response_loss(self):
        original_mark = core._idempotency_mark_charged
        crashed = False

        def crash_after_charge(*args, **kwargs):
            nonlocal crashed
            result = original_mark(*args, **kwargs)
            if not crashed:
                crashed = True
                raise SystemExit("simulated process exit after upload charge")
            return result

        key = "local-charge-recovery-0001"
        with mock.patch.object(
                core, "_idempotency_mark_charged", side_effect=crash_after_charge):
            first_status, first = self._upload(key)
        self.assertEqual(0, first_status, first)
        self._restart_server()
        status, accepted = self._upload(key)
        self.assertEqual(200, status)
        self.assertEqual(1, len(self.points.deductions))
        self.assertEqual(1, self._job_count())

        original_complete = core._idempotency_complete
        complete_calls = 0

        def lose_first_response(*args, **kwargs):
            nonlocal complete_calls
            complete_calls += 1
            if complete_calls == 1:
                raise SystemExit("simulated process exit after job link")
            return original_complete(*args, **kwargs)

        second_key = "local-response-recovery-0002"
        with mock.patch.object(
                core, "_idempotency_complete", side_effect=lose_first_response):
            lost_status, _ = self._upload(second_key, data=PNG + b"-two")
        self.assertEqual(0, lost_status)
        replay_status, replay = self._upload(second_key, data=PNG + b"-two")
        self.assertEqual(200, replay_status)
        self.assertNotEqual(accepted["job_id"], replay["job_id"])
        self.assertEqual(2, len(self.points.deductions))
        self.assertEqual(2, self._job_count())

    def test_same_key_concurrency_creates_one_copy_job_and_one_charge(self):
        key = "copy-concurrent-recovery-0001"
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(lambda _index: self._copy(key), range(10)))
        self.assertEqual({200}, {status for status, _ in results})
        self.assertEqual(1, len({body["job_id"] for _, body in results}))
        self.assertEqual(1, len(self.points.deductions))
        self.assertEqual(1, self._job_count())

    def test_same_key_concurrency_creates_one_local_upload_job_and_one_charge(self):
        key = "local-concurrent-recovery-0001"
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(lambda _index: self._upload(key), range(10)))
        self.assertEqual({200}, {status for status, _ in results})
        self.assertEqual(1, len({body["job_id"] for _, body in results}))
        self.assertEqual(1, len(self.points.deductions))
        self.assertEqual(1, self._job_count())

    def test_local_upload_key_scope_conflict_and_new_intent(self):
        key = "local-key-scope-recovery-0001"
        status, first = self._upload(key)
        self.assertEqual(200, status)
        conflict_status, conflict = self._upload(key, data=PNG + b"-changed")
        self.assertEqual(409, conflict_status)
        self.assertEqual("idempotency_conflict", conflict["code"])
        other_user_status, other_user = self._upload(key, token="other")
        self.assertEqual(200, other_user_status)
        new_key_status, new_intent = self._upload(
            "local-key-scope-recovery-0002",
        )
        self.assertEqual(200, new_key_status)
        self.assertEqual(3, len({
            first["job_id"], other_user["job_id"], new_intent["job_id"],
        }))
        self.assertEqual(3, len(self.points.deductions))
        self.assertEqual(3, self._job_count())

    def test_queue_failure_refunds_once_and_replays_terminal_result(self):
        self.enqueue_allowed = False
        key = "local-queue-refund-recovery-0001"
        status, failed = self._upload(key)
        self.assertEqual(429, status)
        self.assertEqual("refunded", failed["refund_state"])
        replay_status, replay = self._upload(key)
        self.assertEqual(429, replay_status)
        self.assertEqual(failed, replay)
        self.assertEqual(1, len(self.points.deductions))
        self.assertEqual(1, len(self.points.refunds))
        self.assertEqual(1, self._job_count())
        with closing(core.jdb()) as connection:
            row = connection.execute(
                "SELECT status,refunded FROM jobs WHERE id=?",
                (failed["job_id"],),
            ).fetchone()
        self.assertEqual(("error", 1), tuple(row))

    def test_refund_response_loss_converges_after_restart_without_second_refund(self):
        self.enqueue_allowed = False
        self.points.lose_next_refund_response = True
        key = "local-refund-recovery-0001"
        status, pending = self._upload(key)
        self.assertEqual(202, status)
        self.assertEqual("pending", pending["refund_state"])
        self.assertEqual(1, len(self.points.refunds))
        self._restart_server()
        self.assertTrue(core._refund_once(
            pending["job_id"], "fang", pending["cost"],
        ))
        self.assertEqual(1, len(self.points.refunds))
        with closing(core.jdb()) as connection:
            row = connection.execute(
                "SELECT status,refunded FROM jobs WHERE id=?",
                (pending["job_id"],),
            ).fetchone()
        self.assertEqual(("error", 1), tuple(row))

    def test_local_and_copy_retry_after_job_insert_failure_do_not_recharge(self):
        original_create = jobs_store.create_job_after_charge
        calls = 0

        def fail_first_create(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise sqlite3.OperationalError("simulated local INSERT failure")
            return original_create(*args, **kwargs)

        with mock.patch.object(
                jobs_store, "create_job_after_charge", side_effect=fail_first_create):
            first_status, first = self._copy("copy-insert-recovery-0001")
        self.assertEqual(503, first_status, first)
        copy_status, _ = self._copy("copy-insert-recovery-0001")
        self.assertEqual(200, copy_status)

        calls = 0
        with mock.patch.object(
                jobs_store, "create_job_after_charge", side_effect=fail_first_create):
            upload_status, upload = self._upload("local-insert-recovery-0001")
        self.assertEqual(503, upload_status, upload)
        recovered_status, _ = self._upload("local-insert-recovery-0001")
        self.assertEqual(200, recovered_status)
        self.assertEqual(2, len(self.points.deductions))
        self.assertEqual(2, self._job_count())


if __name__ == "__main__":
    unittest.main()
