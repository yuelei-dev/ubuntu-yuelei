import json
import pathlib
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

from content_domains import (
    core,
    jobs_store,
    script_to_video,
    script_video_montage,
    submission_idempotency,
    upstream_guard,
    video,
)


class SmartMontageSubmissionHttpTests(unittest.TestCase):
    def test_key_digest_and_original_request_idempotency_contract(self):
        class FakePointsError(Exception):
            def __init__(self, status, detail):
                self.status = status
                self.detail = detail

        class FakePoints:
            AuthPointsError = FakePointsError

            def __init__(self):
                self.deductions = []
                self.refunds = []
                self.fail_refunds = False

            @staticmethod
            def cost_of(_kind, _body):
                return 150

            def deduct_points(self, username, cost, reason, transaction_key=""):
                self.deductions.append(
                    (username, cost, reason, transaction_key)
                )
                return 1000 - cost

            def refund_points(self, username, cost, reason, transaction_key=""):
                self.refunds.append((username, cost, reason, transaction_key))
                if self.fail_refunds:
                    raise ConnectionError("refund confirmation unavailable")
                return 1000

        points = FakePoints()
        fake_short_drama = SimpleNamespace(
            RevisionConflict=type("RevisionConflict", (Exception,), {}),
            _http_error=lambda *_args, **_kwargs: None,
            fail_linked_character_reference_job=lambda *_args, **_kwargs: None,
            short_drama_production=SimpleNamespace(
                fail_linked_job=lambda *_args, **_kwargs: None,
            ),
        )
        server = None
        with tempfile.TemporaryDirectory() as raw, ExitStack() as stack:
            database = str(pathlib.Path(raw) / "jobs.db")
            stack.enter_context(mock.patch.object(core, "JOB_DB", database))
            stack.enter_context(mock.patch.object(
                core, "verify",
                lambda _token: {
                    "username": "fang", "must_change": False, "points": 1000,
                },
            ))
            stack.enter_context(mock.patch.object(
                core, "_domains", lambda: (None, points, video),
            ))
            stack.enter_context(mock.patch.object(
                core, "_dispatch_short_drama", lambda *_args, **_kwargs: False,
            ))
            stack.enter_context(mock.patch.object(
                core, "_short_drama_domain", lambda: fake_short_drama,
            ))
            stack.enter_context(mock.patch.object(
                core.feature_flags, "require_enabled", lambda _kind: None,
            ))
            stack.enter_context(mock.patch.object(
                core.miniprogram_security, "check_payload", lambda _body: None,
            ))
            stack.enter_context(mock.patch.object(
                upstream_guard, "exhausted_reason", lambda *_args: None,
            ))
            stack.enter_context(mock.patch.object(
                core.cli_gateway, "reject_changed_cost", lambda *_args: False,
            ))
            stack.enter_context(mock.patch.object(
                core, "enqueue_job", lambda *_args: True,
            ))
            stack.enter_context(mock.patch.object(
                video, "record_video_pending_asset", lambda *_args: None,
            ))
            stack.enter_context(mock.patch.object(
                core, "MAX_USER_ACTIVE_JOBS", 5,
            ))
            stack.enter_context(mock.patch.object(
                core, "HANDLERS",
                {"script_to_video": script_to_video.gen_script_to_video},
            ))
            core._shutting_down.clear()

            with closing(sqlite3.connect(database)) as connection:
                connection.execute("""CREATE TABLE jobs(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT, username TEXT, cost INTEGER,
                    status TEXT DEFAULT 'pending', payload TEXT,
                    result TEXT, error TEXT, created_at INTEGER,
                    updated_at INTEGER, deleted INTEGER DEFAULT 0,
                    refunded INTEGER DEFAULT 0, owner TEXT
                )""")
                connection.commit()

            server = ThreadingHTTPServer(("127.0.0.1", 0), core.H)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            stack.callback(server.server_close)
            stack.callback(server.shutdown)
            endpoint = "http://127.0.0.1:%d/api/gen/script_to_video" % (
                server.server_address[1],
            )

            def post(body, key=""):
                headers = {
                    "Authorization": "Bearer test",
                    "Content-Type": "application/json",
                }
                if key:
                    headers["Idempotency-Key"] = key
                request = urllib.request.Request(
                    endpoint,
                    data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                    method="POST",
                    headers=headers,
                )
                try:
                    with urllib.request.urlopen(request, timeout=5) as response:
                        return response.status, json.loads(response.read())
                except urllib.error.HTTPError as error:
                    return error.code, json.loads(error.read() or b"{}")

            def get_job(job_id):
                request = urllib.request.Request(
                    endpoint.rsplit("/", 1)[0] + "/job/" + str(job_id),
                    headers={"Authorization": "Bearer test"},
                )
                try:
                    with urllib.request.urlopen(request, timeout=5) as response:
                        return response.status, json.loads(response.read())
                except urllib.error.HTTPError as error:
                    return error.code, json.loads(error.read() or b"{}")

            request = {
                "pipeline": "smart_montage",
                "copy": "专业评估理解真实需求，温和护理让状态自然稳定。",
                "style": "luxe",
                "ratio": "16:9",
            }
            preview = script_to_video.smart_montage_plan_response(request)
            request["plan_digest"] = preview["plan_digest"]

            status, response = post(request)
            self.assertEqual(400, status)
            self.assertEqual("idempotency_key_required", response["code"])
            self.assertEqual([], points.deductions)

            key = "smart-montage-luxe-http-0001"
            status, accepted = post(request, key)
            self.assertEqual(200, status)
            self.assertEqual(150, accepted["cost"])
            self.assertEqual(1, len(points.deductions))
            self.assertTrue(points.deductions[0][3].endswith(":" + key))

            canonical_request = script_to_video.normalize_smart_montage_submission(
                request,
            )
            with closing(core.jdb()) as connection:
                claim = connection.execute(
                    "SELECT request_hash,response_json FROM submission_idempotency "
                    "WHERE username='fang' AND endpoint=? AND idem_key=?",
                    ("/api/gen/script_to_video", key),
                ).fetchone()
                job_payload = json.loads(connection.execute(
                    "SELECT payload FROM jobs WHERE id=?", (accepted["job_id"],),
                ).fetchone()[0])
            self.assertEqual(
                submission_idempotency._request_hash(canonical_request),
                claim["request_hash"],
            )
            self.assertNotEqual(
                submission_idempotency._request_hash(job_payload),
                claim["request_hash"],
            )

            # A completed request replays before the versioned planner runs.
            with mock.patch.object(
                script_video_montage,
                "plan_script_video",
                side_effect=AssertionError("planner must not run during replay"),
            ):
                status, replay = post(request, key)
            self.assertEqual(200, status)
            self.assertEqual(accepted, replay)
            self.assertEqual(1, len(points.deductions))

            original_planner = script_video_montage.plan_script_video

            def upgraded_planner(payload):
                plan = original_planner(payload)
                plan["planner_version"] += "_upgraded"
                plan["scenes"][0]["headline"] += "新"
                return plan

            with mock.patch.object(
                script_video_montage,
                "plan_script_video",
                side_effect=upgraded_planner,
            ):
                status, stale = post(
                    request, "smart-montage-luxe-http-0002",
                )
            self.assertEqual(409, status)
            self.assertEqual("plan_digest_mismatch", stale["code"])
            self.assertEqual(1, len(points.deductions))
            with closing(core.jdb()) as connection:
                self.assertEqual(
                    1,
                    connection.execute(
                        "SELECT COUNT(*) FROM submission_idempotency"
                    ).fetchone()[0],
                )

            # A charged insert failure with an unconfirmed refund is accepted as
            # a durable compensation tracker.  The browser stores this job id
            # and polls it instead of rotating the submission key.
            with closing(core.jdb()) as connection:
                connection.execute("""CREATE TRIGGER reject_pending_smart_montage
                    BEFORE INSERT ON jobs WHEN NEW.status='pending'
                    BEGIN SELECT RAISE(FAIL, 'insert failed'); END""")
                connection.commit()
            points.fail_refunds = True
            pending_key = "smart-montage-insert-refund-pending"
            deductions_before = len(points.deductions)
            status, pending = post(request, pending_key)
            self.assertEqual(202, status)
            self.assertEqual("pending", pending["refund_state"])
            self.assertIsInstance(pending["job_id"], int)
            self.assertEqual(deductions_before + 1, len(points.deductions))
            status, pending_job = get_job(pending["job_id"])
            self.assertEqual(200, status)
            self.assertEqual("error", pending_job["status"])
            self.assertEqual("pending", pending_job["refund_state"])

            status, pending_replay = post(request, pending_key)
            self.assertEqual(202, status)
            self.assertEqual(pending, pending_replay)
            self.assertEqual(deductions_before + 1, len(points.deductions))

            points.fail_refunds = False
            self.assertEqual(
                1,
                jobs_store.retry_failed_refunds(core.jdb, core._refund_once),
            )
            status, refunded_job = get_job(pending["job_id"])
            self.assertEqual(200, status)
            self.assertEqual("refunded", refunded_job["refund_state"])
            with closing(core.jdb()) as connection:
                connection.execute("DROP TRIGGER reject_pending_smart_montage")
                connection.commit()

            # A full render queue has already created and charged its job.  If
            # refund confirmation is unavailable, return the same queryable
            # tracker contract instead of the rotatable queue_full error.
            points.fail_refunds = True
            queue_key = "smart-montage-queue-refund-pending"
            deductions_before = len(points.deductions)
            with mock.patch.object(core, "enqueue_job", lambda *_args: False):
                status, queue_pending = post(request, queue_key)
            self.assertEqual(202, status)
            self.assertEqual("pending", queue_pending["refund_state"])
            self.assertEqual(deductions_before + 1, len(points.deductions))
            status, queue_job = get_job(queue_pending["job_id"])
            self.assertEqual(200, status)
            self.assertEqual("pending", queue_job["refund_state"])
            with mock.patch.object(core, "enqueue_job", lambda *_args: False):
                status, queue_replay = post(request, queue_key)
            self.assertEqual(202, status)
            self.assertEqual(queue_pending, queue_replay)
            self.assertEqual(deductions_before + 1, len(points.deductions))

            # Once Auth has explicitly confirmed the refund, queue_full becomes
            # terminal and it is safe for the client to rotate the key.
            points.fail_refunds = False
            refunded_queue_key = "smart-montage-queue-refund-confirmed"
            deductions_before = len(points.deductions)
            with mock.patch.object(core, "enqueue_job", lambda *_args: False):
                status, refunded_queue = post(request, refunded_queue_key)
            self.assertEqual(429, status)
            self.assertEqual("queue_full", refunded_queue["code"])
            self.assertIs(True, refunded_queue["operation_terminal"])
            self.assertEqual(deductions_before + 1, len(points.deductions))
            with mock.patch.object(core, "enqueue_job", lambda *_args: False):
                status, refunded_queue_replay = post(
                    request, refunded_queue_key,
                )
            self.assertEqual(429, status)
            self.assertEqual(refunded_queue, refunded_queue_replay)
            self.assertEqual(deductions_before + 1, len(points.deductions))

            # Asset registration is another post-charge failure and follows the
            # identical durable-refund contract.
            points.fail_refunds = True
            asset_key = "smart-montage-asset-refund-pending"
            deductions_before = len(points.deductions)
            with mock.patch.object(
                video, "record_video_pending_asset",
                side_effect=RuntimeError("asset registration failed"),
            ):
                status, asset_pending = post(request, asset_key)
            self.assertEqual(202, status)
            self.assertEqual("pending", asset_pending["refund_state"])
            self.assertEqual(deductions_before + 1, len(points.deductions))
            status, asset_job = get_job(asset_pending["job_id"])
            self.assertEqual(200, status)
            self.assertEqual("pending", asset_job["refund_state"])
            status, asset_replay = post(request, asset_key)
            self.assertEqual(202, status)
            self.assertEqual(asset_pending, asset_replay)
            self.assertEqual(deductions_before + 1, len(points.deductions))

            # If both the job insert and compensation record are unavailable,
            # the claim remains replayable but deliberately exposes none of the
            # fields that authorize browser-side key rotation.
            ambiguous_key = "smart-montage-refund-untracked"
            ambiguous_error = jobs_store.PaidJobInsertError(
                "untracked", "operator-visible-reference",
            )
            with mock.patch.object(
                jobs_store, "create_paid_job", side_effect=ambiguous_error,
            ):
                status, ambiguous = post(request, ambiguous_key)
            self.assertEqual(202, status)
            self.assertEqual("pending", ambiguous["refund_state"])
            self.assertEqual(
                "operator-visible-reference", ambiguous["compensation_ref"],
            )
            for unsafe_field in (
                    "submission_ref", "job_id", "code", "operation_terminal"):
                self.assertNotIn(unsafe_field, ambiguous)
            status, ambiguous_replay = post(request, ambiguous_key)
            self.assertEqual(202, status)
            self.assertEqual(ambiguous, ambiguous_replay)

if __name__ == "__main__":
    unittest.main()
