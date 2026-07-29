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

    def get_points(self, username):
        return self.balance

    def deduct_points(self, username, amount, reason=""):
        if self.balance < amount:
            raise self.AuthPointsError(402, "点数不足")
        self.balance -= amount
        self.deductions.append((username, amount, reason))
        return self.balance

    def safe_refund_points(self, username, amount, reason=""):
        self.balance += amount
        self.refunds.append((username, amount, reason))
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

    def test_insert_failure_directly_refunds_and_aborts_idempotency(self):
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
        self.assertEqual(len(self.points.deductions), 1)
        self.assertEqual(len(self.points.refunds), 1)
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
