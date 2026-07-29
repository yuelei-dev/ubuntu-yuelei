import io
import importlib
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import ExitStack, closing
from pathlib import Path
from unittest import mock


class _Handler:
    def __init__(
        self, path, body=b"", content_type="application/json",
        idem_key="", json_body=None,
    ):
        self.path = path
        self.headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
        }
        if idem_key:
            self.headers["Idempotency-Key"] = idem_key
        if not content_type.startswith("application/json"):
            self.headers["X-File-Name"] = "source.jpg"
        self.rfile = io.BytesIO(body)
        self.json_body = json_body
        self.responses = []

    def _token(self):
        return "token"

    def _json_body(self):
        return self.json_body

    def _json_body_strict(self):
        return self.json_body

    def _send(self, status, body):
        self.responses.append((status, body))
        return status, body


class _Points:
    class AuthPointsError(Exception):
        def __init__(self, status, detail):
            super().__init__(detail)
            self.status = status
            self.detail = detail

    def __init__(self, balance=100):
        self.balance = balance
        self.deductions = []
        self.refunds = []
        self.cost_calls = []
        self.fail_deduct = False
        self.fail_refunds = 0

    def get_points(self, username):
        return self.balance

    def deduct_points(self, username, amount, reason=""):
        if self.fail_deduct:
            raise self.AuthPointsError(503, "deduct unavailable")
        if self.balance < amount:
            raise self.AuthPointsError(402, "点数不足")
        self.balance -= amount
        self.deductions.append((username, amount, reason))
        return self.balance

    def refund_points(self, username, amount, reason=""):
        if self.fail_refunds:
            self.fail_refunds -= 1
            raise self.AuthPointsError(503, "refund unavailable")
        self.balance += amount
        self.refunds.append((username, amount, reason))
        return self.balance

    def safe_refund_points(self, username, amount, reason=""):
        try:
            return self.refund_points(username, amount, reason)
        except Exception:
            return self.balance

    def cost_of(self, kind, body):
        self.cost_calls.append((kind, body))
        return 20

    def settle_breakdown_batch(self, *args):
        return 0


class BreakdownLocalUploadIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[1]
        server = str(cls.root / "server")
        if server not in sys.path:
            sys.path.insert(0, server)
        cls.breakdown = importlib.import_module("content_domains.breakdown")
        cls.core = importlib.import_module("content_domains.core")
        cls.jobs_store = importlib.import_module("content_domains.jobs_store")
        cls.local_upload = importlib.import_module(
            "content_domains.local_reverse_upload"
        )
        cls.submission_idempotency = importlib.import_module(
            "content_domains.submission_idempotency"
        )

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.database = self.base / "jobs.db"
        self.out_dir = self.base / "content_out"
        self.out_dir.mkdir()
        self.points = _Points()
        self.queued = []
        with closing(self.jdb()) as connection:
            connection.execute("""CREATE TABLE jobs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT, username TEXT, cost INTEGER,
                status TEXT DEFAULT 'pending', payload TEXT, result TEXT,
                error TEXT, created_at INTEGER, updated_at INTEGER,
                deleted INTEGER DEFAULT 0, refunded INTEGER DEFAULT 0,
                owner TEXT
            )""")
            self.breakdown._ensure_upload_table(connection)
            self.submission_idempotency.ensure_table(connection)
            connection.commit()

    def tearDown(self):
        self.temp.cleanup()

    def jdb(self):
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def image_body():
        return b"\xff\xd8\xff" + b"x" * 29

    def upload_handler(self, idem_key="local-upload-1234"):
        return _Handler(
            "/api/gen/breakdown/local-upload?media_type=image",
            self.image_body(), "image/jpeg", idem_key,
        )

    def patches(self, enqueue=None):
        stack = ExitStack()
        stack.enter_context(mock.patch.object(self.core, "jdb", self.jdb))
        stack.enter_context(mock.patch.object(self.core, "OUT_DIR", self.out_dir))
        stack.enter_context(mock.patch.object(
            self.core, "verify", return_value={"username": "alice"}
        ))
        stack.enter_context(mock.patch.object(
            self.core, "_domains", return_value=(mock.Mock(), self.points, mock.Mock())
        ))
        stack.enter_context(mock.patch.object(
            self.core.feature_flags, "require_enabled"
        ))
        stack.enter_context(mock.patch.object(
            self.core, "is_shutting_down", return_value=False
        ))
        stack.enter_context(mock.patch.object(
            self.core, "_must_change_password", return_value=False
        ))
        stack.enter_context(mock.patch.object(
            self.core, "_user_active_job_count", return_value=0
        ))
        stack.enter_context(mock.patch.object(
            self.core, "HANDLERS",
            {"breakdown": self.breakdown.gen_breakdown},
        ))
        queue_effect = enqueue or (
            lambda job_id, kind, mode: self.queued.append(
                (job_id, kind, mode)
            ) or True
        )
        stack.enter_context(mock.patch.object(
            self.core, "enqueue_job", side_effect=queue_effect
        ))
        return stack

    def submit_upload(self, handler=None, enqueue=None):
        handler = handler or self.upload_handler()
        with self.patches(enqueue=enqueue):
            result = self.core.H.do_POST(handler)
        return handler, result

    def job_row(self, job_id):
        with closing(self.jdb()) as connection:
            return connection.execute(
                "SELECT * FROM jobs WHERE id=?", (job_id,)
            ).fetchone()

    def binding_row(self, job_id):
        with closing(self.jdb()) as connection:
            return connection.execute(
                "SELECT * FROM breakdown_uploads WHERE job_id=?", (job_id,)
            ).fetchone()

    def payment_state(self, job_id):
        row = self.job_row(job_id)
        payload = json.loads(row["payload"] or "{}")
        return (payload.get("_local_upload_payment") or {}).get("state")

    def test_real_handler_creates_bound_job_and_real_worker_completes(self):
        self.assertFalse(hasattr(self.jobs_store, "create_paid_job"))
        handler, result = self.submit_upload()
        self.assertEqual(result[0], 200)
        job_id = result[1]["job_id"]
        row = self.job_row(job_id)
        payload = json.loads(row["payload"])
        binding = self.binding_row(job_id)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["username"], "alice")
        self.assertEqual(row["owner"], self.core.SERVICE_OWNER)
        self.assertEqual(self.payment_state(job_id), "paid")
        self.assertEqual(payload["upload_token"], binding["token"])
        self.assertEqual(binding["media_type"], "image")
        self.assertEqual(
            Path(binding["path"]).resolve().parent,
            (self.out_dir / "_breakdown_uploads").resolve(),
        )
        self.assertEqual(self.points.deductions, [(
            "alice", 20, "job:breakdown"
        )])

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(self.core, "jdb", self.jdb))
            stack.enter_context(mock.patch.object(
                self.core, "OUT_DIR", self.out_dir
            ))
            stack.enter_context(mock.patch.object(
                self.core, "_domains",
                return_value=(mock.Mock(), self.points, mock.Mock()),
            ))
            stack.enter_context(mock.patch.object(
                self.core, "HANDLERS",
                {"breakdown": self.breakdown.gen_breakdown},
            ))
            stack.enter_context(mock.patch.object(
                self.core, "_start_job_heartbeat", return_value=lambda: None
            ))
            stack.enter_context(mock.patch.object(
                self.core, "_recover_pending_jobs"
            ))
            stack.enter_context(mock.patch.object(
                self.core.assets_store, "record_asset"
            ))
            stack.enter_context(mock.patch.object(
                self.breakdown, "_heartbeat"
            ))
            reverse = stack.enter_context(mock.patch.object(
                self.breakdown, "_reverse_result_from_frames",
                return_value={"type": "breakdown_reverse", "prompt": "verified"},
            ))
            self.core.run_job(job_id)

        self.assertEqual(self.job_row(job_id)["status"], "done")
        self.assertIsNone(self.binding_row(job_id))
        self.assertFalse(Path(binding["path"]).exists())
        self.assertEqual(len(reverse.call_args.args[1]), 8)
        self.assertEqual(self.points.refunds, [])

    def test_job_and_binding_are_durable_before_deduction(self):
        observed = {}
        original_deduct = self.points.deduct_points

        def inspect_then_deduct(username, amount, reason=""):
            with closing(self.jdb()) as connection:
                job = connection.execute("SELECT * FROM jobs").fetchone()
                binding = connection.execute(
                    "SELECT * FROM breakdown_uploads"
                ).fetchone()
            observed.update({
                "job_id": job["id"] if job else None,
                "status": job["status"] if job else None,
                "owner": job["owner"] if job else None,
                "binding_job_id": binding["job_id"] if binding else None,
            })
            return original_deduct(username, amount, reason)

        with mock.patch.object(
            self.points, "deduct_points", side_effect=inspect_then_deduct
        ):
            _, result = self.submit_upload()
        self.assertEqual(result[0], 200)
        self.assertEqual(observed["status"], "pending")
        self.assertEqual(observed["job_id"], observed["binding_job_id"])
        self.assertEqual(
            observed["owner"],
            "%s:local-upload-reserved" % self.core.SERVICE_OWNER,
        )

    def test_public_json_private_fields_are_rejected_before_charge_or_job(self):
        owned = self.out_dir / "_breakdown_uploads" / ("a" * 32 + ".jpg")
        owned.parent.mkdir()
        owned.write_bytes(self.image_body())
        forbidden = {
            "url": "https://www.douyin.com/video/1234567890123456789",
            "local_media_path": str(owned),
            "local_media_type": "image",
            "upload_token": "a" * 32,
            "_username": "alice",
            "_job_id": 1,
            "_trusted_local_upload": True,
            "_local_upload_payment": {"state": "refund_pending"},
        }
        handler = _Handler(
            "/api/gen/breakdown", b"{}", "application/json",
            json_body=forbidden,
        )
        with self.patches():
            result = self.core.H.do_POST(handler)
        self.assertEqual(result[0], 400)
        self.assertIn("专用上传接口", result[1]["detail"])
        self.assertEqual(self.points.cost_calls, [])
        self.assertEqual(self.points.deductions, [])
        with closing(self.jdb()) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
                0,
            )
        self.assertTrue(owned.exists())

    def test_binding_rejects_cross_user_and_cross_job_without_cleanup(self):
        _, result = self.submit_upload()
        job_id = result[1]["job_id"]
        binding = self.binding_row(job_id)
        base_payload = {
            "upload_token": binding["token"], "media_type": "image",
        }
        with mock.patch.object(self.core, "jdb", self.jdb), mock.patch.object(
            self.core, "OUT_DIR", self.out_dir
        ):
            with self.assertRaisesRegex(ValueError, "不属于当前任务"):
                self.breakdown._do_local_reverse({
                    **base_payload, "_username": "mallory", "_job_id": job_id,
                }, binding["token"])
            with self.assertRaisesRegex(ValueError, "不属于当前任务"):
                self.breakdown._do_local_reverse({
                    **base_payload, "_username": "alice",
                    "_job_id": job_id + 1,
                }, binding["token"])
        self.assertIsNotNone(self.binding_row(job_id))
        self.assertTrue(Path(binding["path"]).exists())

    def test_tampered_canonical_path_is_rejected_without_deleting_outside(self):
        _, result = self.submit_upload()
        job_id = result[1]["job_id"]
        binding = self.binding_row(job_id)
        outside = self.base / "outside.jpg"
        outside.write_bytes(self.image_body())
        with closing(self.jdb()) as connection:
            connection.execute(
                "UPDATE breakdown_uploads SET path=? WHERE job_id=?",
                (str(outside), job_id),
            )
            connection.commit()
        with mock.patch.object(self.core, "jdb", self.jdb), mock.patch.object(
            self.core, "OUT_DIR", self.out_dir
        ):
            with self.assertRaisesRegex(ValueError, "不匹配"):
                self.breakdown._do_local_reverse({
                    "upload_token": binding["token"], "media_type": "image",
                    "_username": "alice", "_job_id": job_id,
                }, binding["token"])
        self.assertTrue(outside.exists())
        self.assertIsNone(self.binding_row(job_id))
        self.assertFalse(Path(binding["path"]).exists())

    def test_missing_file_fails_once_refunds_once_and_cleans_binding(self):
        _, result = self.submit_upload()
        job_id = result[1]["job_id"]
        binding = self.binding_row(job_id)
        Path(binding["path"]).unlink()
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(self.core, "jdb", self.jdb))
            stack.enter_context(mock.patch.object(
                self.core, "OUT_DIR", self.out_dir
            ))
            stack.enter_context(mock.patch.object(
                self.core, "_domains",
                return_value=(mock.Mock(), self.points, mock.Mock()),
            ))
            stack.enter_context(mock.patch.object(
                self.core, "HANDLERS",
                {"breakdown": self.breakdown.gen_breakdown},
            ))
            stack.enter_context(mock.patch.object(
                self.core, "_start_job_heartbeat", return_value=lambda: None
            ))
            stack.enter_context(mock.patch.object(
                self.core, "_recover_pending_jobs"
            ))
            self.core.run_job(job_id)
            self.core.run_job(job_id)
        row = self.job_row(job_id)
        self.assertEqual(row["status"], "error")
        self.assertEqual(row["refunded"], 1)
        self.assertEqual(len(self.points.refunds), 1)
        self.assertIsNone(self.binding_row(job_id))

    def test_invalid_video_duration_creates_no_job_charge_or_binding(self):
        body = b"\x00\x00\x00\x18ftypisom" + b"x" * 20
        handler = _Handler(
            "/api/gen/breakdown/local-upload?media_type=video",
            body, "video/mp4", "local-video-1234",
        )
        with mock.patch.object(
            self.local_upload, "_video_duration",
            side_effect=ValueError("视频最长支持 2 分钟"),
        ):
            handler, result = self.submit_upload(handler)
        self.assertEqual(result[0], 400)
        self.assertEqual(self.points.deductions, [])
        self.assertEqual(self.points.refunds, [])
        with closing(self.jdb()) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM breakdown_uploads"
                ).fetchone()[0],
                0,
            )
        self.assertEqual(
            list((self.out_dir / "_breakdown_uploads").glob("*")), []
        )

    def test_queue_failure_uses_job_cas_refund_and_cleans_everything(self):
        handler, result = self.submit_upload(enqueue=lambda *args: False)
        self.assertEqual(result[0], 429)
        with closing(self.jdb()) as connection:
            row = connection.execute("SELECT * FROM jobs").fetchone()
            binding_count = connection.execute(
                "SELECT COUNT(*) FROM breakdown_uploads"
            ).fetchone()[0]
            idem_count = connection.execute(
                "SELECT COUNT(*) FROM submission_idempotency"
            ).fetchone()[0]
        self.assertEqual(row["status"], "error")
        self.assertEqual(row["refunded"], 1)
        self.assertEqual(binding_count, 0)
        self.assertEqual(idem_count, 0)
        self.assertEqual(len(self.points.refunds), 1)
        self.assertEqual(self.points.balance, 100)
        self.assertEqual(
            list((self.out_dir / "_breakdown_uploads").glob("*")), []
        )

    def test_idempotent_replay_returns_same_job_without_second_charge(self):
        first, first_result = self.submit_upload()
        second = self.upload_handler()
        second, second_result = self.submit_upload(second)
        self.assertEqual(first_result, second_result)
        self.assertEqual(first_result[0], 200)
        self.assertEqual(len(self.points.deductions), 1)
        with closing(self.jdb()) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM breakdown_uploads"
                ).fetchone()[0],
                1,
            )
        self.assertEqual(
            len(list((self.out_dir / "_breakdown_uploads").glob("*"))), 1
        )

    def test_idempotency_in_progress_returns_reserved_job_id(self):
        _, first_result = self.submit_upload()
        job_id = first_result[1]["job_id"]
        with closing(self.jdb()) as connection:
            connection.execute(
                "UPDATE submission_idempotency SET response_json=NULL"
                " WHERE username=? AND endpoint=? AND idem_key=?",
                (
                    "alice", self.local_upload.UPLOAD_ENDPOINT,
                    "local-upload-1234",
                ),
            )
            connection.commit()
        _, second_result = self.submit_upload()
        self.assertEqual(second_result[0], 409)
        self.assertEqual(
            second_result[1],
            {
                "detail": "相同上传正在受理，请稍后查询",
                "code": "idempotency_in_progress",
                "retry_after_ms": 1000,
                "job_id": job_id,
            },
        )
        self.assertEqual(len(self.points.deductions), 1)

    def test_idempotency_conflict_never_creates_or_charges_second_job(self):
        _, first_result = self.submit_upload()
        conflicting = _Handler(
            "/api/gen/breakdown/local-upload?media_type=image",
            self.image_body() + b"different", "image/jpeg",
            "local-upload-1234",
        )
        _, conflict_result = self.submit_upload(conflicting)
        self.assertEqual(first_result[0], 200)
        self.assertEqual(conflict_result[0], 409)
        self.assertEqual(
            conflict_result[1]["code"], "idempotency_conflict"
        )
        self.assertEqual(len(self.points.deductions), 1)
        with closing(self.jdb()) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
                1,
            )

    def test_job_reservation_failure_never_deducts_or_refunds(self):
        calls = {"count": 0}
        original = self.breakdown._ensure_upload_table

        def fail_second(connection):
            calls["count"] += 1
            if calls["count"] == 2:
                raise sqlite3.OperationalError("insert unavailable")
            return original(connection)

        with mock.patch.object(
            self.breakdown, "_ensure_upload_table", side_effect=fail_second
        ):
            handler, result = self.submit_upload()
        self.assertEqual(result[0], 500)
        self.assertEqual(self.points.deductions, [])
        self.assertEqual(self.points.refunds, [])
        self.assertEqual(self.points.balance, 100)
        with closing(self.jdb()) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM submission_idempotency"
                ).fetchone()[0],
                0,
            )

    def test_deduct_failure_rejects_reserved_job_without_refund(self):
        self.points.fail_deduct = True
        _, result = self.submit_upload()
        self.assertEqual(result[0], 500)
        with closing(self.jdb()) as connection:
            row = connection.execute("SELECT * FROM jobs").fetchone()
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM breakdown_uploads"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM submission_idempotency"
                ).fetchone()[0],
                0,
            )
        self.assertEqual(row["status"], "error")
        self.assertEqual(row["refunded"], 0)
        self.assertEqual(self.payment_state(row["id"]), "deduct_failed")
        self.assertEqual(self.points.deductions, [])
        self.assertEqual(self.points.refunds, [])
        self.assertEqual(self.points.balance, 100)

    def test_post_deduct_activation_failure_uses_persistent_refund_job(self):
        with mock.patch.object(
            self.local_upload, "_activate_reserved_job",
            side_effect=sqlite3.OperationalError("activation unavailable"),
        ):
            _, result = self.submit_upload()
        self.assertEqual(result[0], 500)
        with closing(self.jdb()) as connection:
            row = connection.execute("SELECT * FROM jobs").fetchone()
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM breakdown_uploads"
                ).fetchone()[0],
                0,
            )
        self.assertEqual(row["status"], "error")
        self.assertEqual(row["refunded"], 1)
        self.assertEqual(self.payment_state(row["id"]), "refund_pending")
        self.assertEqual(len(self.points.deductions), 1)
        self.assertEqual(len(self.points.refunds), 1)
        self.assertEqual(self.points.balance, 100)

    def test_refund_failure_stays_durable_and_reconciles_once(self):
        self.points.fail_refunds = 1
        _, result = self.submit_upload(enqueue=lambda *args: False)
        self.assertEqual(result[0], 429)
        with closing(self.jdb()) as connection:
            row = connection.execute("SELECT * FROM jobs").fetchone()
            idem_count = connection.execute(
                "SELECT COUNT(*) FROM submission_idempotency"
            ).fetchone()[0]
        self.assertEqual(row["status"], "error")
        self.assertEqual(row["refunded"], 0)
        self.assertEqual(self.payment_state(row["id"]), "refund_pending")
        self.assertEqual(idem_count, 1)
        self.assertEqual(self.points.balance, 80)
        self.assertEqual(self.points.refunds, [])

        invalid = _Handler(
            "/api/gen/breakdown/local-upload?media_type=image",
            b"invalid", "application/octet-stream",
        )
        with self.patches():
            invalid_result = self.core.H.do_POST(invalid)
        self.assertEqual(invalid_result[0], 400)
        self.assertEqual(self.job_row(row["id"])["refunded"], 1)
        self.assertEqual(len(self.points.refunds), 1)
        self.assertEqual(self.points.balance, 100)
        with closing(self.jdb()) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM submission_idempotency"
                ).fetchone()[0],
                0,
            )
        self.assertEqual(
            self.local_upload.reconcile_pending_refunds(
                self.jdb, self.jobs_store, self.points
            ),
            0,
        )
        self.assertEqual(len(self.points.refunds), 1)

    def test_unresolved_refund_replay_cannot_charge_same_key_again(self):
        self.points.fail_refunds = 2
        _, first = self.submit_upload(enqueue=lambda *args: False)
        self.assertEqual(first[0], 429)
        with closing(self.jdb()) as connection:
            job_id = connection.execute("SELECT id FROM jobs").fetchone()[0]
        _, replay = self.submit_upload()
        self.assertEqual(replay[0], 200)
        self.assertEqual(replay[1]["job_id"], job_id)
        self.assertEqual(len(self.points.deductions), 1)
        self.assertEqual(self.points.refunds, [])
        with closing(self.jdb()) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
                1,
            )

    def test_repeated_paid_rejection_never_refunds_twice(self):
        _, result = self.submit_upload()
        job_id = result[1]["job_id"]
        binding = self.binding_row(job_id)
        with mock.patch.object(self.core, "jdb", self.jdb), mock.patch.object(
            self.core, "_domains",
            return_value=(mock.Mock(), self.points, mock.Mock()),
        ):
            first = self.local_upload._reject_paid_job(
                self.core._reject_pending_job, self.jdb, self.jobs_store,
                self.points, self.out_dir, binding["token"], "alice", job_id,
                "local-upload-1234", "first failure",
            )
            second = self.local_upload._reject_paid_job(
                self.core._reject_pending_job, self.jdb, self.jobs_store,
                self.points, self.out_dir, binding["token"], "alice", job_id,
                "local-upload-1234", "duplicate failure",
            )
        self.assertEqual(first, (True, True))
        self.assertEqual(second, (False, False))
        self.assertEqual(self.job_row(job_id)["refunded"], 1)
        self.assertEqual(len(self.points.refunds), 1)
        self.assertEqual(self.points.balance, 100)

    def test_terminal_claim_loss_cleanup_is_idempotent(self):
        _, result = self.submit_upload()
        job_id = result[1]["job_id"]
        binding = self.binding_row(job_id)
        with mock.patch.object(self.core, "jdb", self.jdb), mock.patch.object(
            self.core, "_domains",
            return_value=(mock.Mock(), self.points, mock.Mock()),
        ):
            self.assertTrue(
                self.core._reject_pending_job(
                    job_id, "alice", 20, "cancelled before claim"
                )
            )
            self.assertFalse(
                self.core._reject_pending_job(
                    job_id, "alice", 20, "duplicate reject"
                )
            )
        self.assertEqual(len(self.points.refunds), 1)
        self.assertEqual(
            self.local_upload.cleanup_stale_uploads(self.jdb, self.out_dir), 1
        )
        self.assertEqual(
            self.local_upload.cleanup_stale_uploads(self.jdb, self.out_dir), 0
        )
        self.assertIsNone(self.binding_row(job_id))
        self.assertFalse(Path(binding["path"]).exists())


if __name__ == "__main__":
    unittest.main()
