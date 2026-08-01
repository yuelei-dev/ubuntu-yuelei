# -*- coding: utf-8 -*-
import importlib
import hashlib
import io
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import ExitStack, closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock


class BreakdownContentCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[1]
        server = str(cls.root / "server")
        if server not in sys.path:
            sys.path.insert(0, server)
        cls.breakdown = importlib.import_module("content_domains.breakdown")
        cls.core = importlib.import_module("content_domains.core")
        cls.egress = importlib.import_module("content_domains.egress")
        cls.points = importlib.import_module("content_domains.points")
        cls.script = (cls.root / "site/workbench/script.html").read_text(
            encoding="utf-8"
        )

    @staticmethod
    def _jdb(path):
        def connect():
            connection = sqlite3.connect(path)
            connection.row_factory = sqlite3.Row
            return connection

        return connect

    def test_target_runtime_public_abi_is_present(self):
        for name in (
            "validate_breakdown_payload",
            "handle_local_upload_request",
            "handle_local_upload",
            "gen_breakdown",
            "_do_local_reverse",
        ):
            self.assertTrue(callable(getattr(self.breakdown, name, None)), name)
        for name in ("heygen_proxy", "post_image_json", "post_json_idempotent"):
            self.assertTrue(callable(getattr(self.egress, name, None)), name)

    def _local_upload_handler(self):
        handler = mock.Mock()
        handler.path = "/api/gen/breakdown/local-upload?media_type=video"
        handler.headers = {}
        handler._token.return_value = "session-token"
        return handler

    def test_core_local_upload_routes_to_upload_token_handler(self):
        handler = self._local_upload_handler()
        user = {"username": "route-user", "must_change": False}
        with mock.patch.object(
            self.core, "_domains", return_value=(mock.Mock(), mock.Mock(), mock.Mock())
        ), mock.patch.object(
            self.breakdown, "handle_local_upload_request", return_value="accepted"
        ) as upload:
            result = self.core.H.do_POST(handler)

        self.assertEqual(result, "accepted")
        upload.assert_called_once_with(handler)
        handler._send.assert_not_called()

    def test_core_local_upload_rejects_unauthenticated_before_upload(self):
        handler = self._local_upload_handler()
        with mock.patch.object(
            self.core, "verify", return_value=None
        ), mock.patch.object(
            self.breakdown, "handle_local_upload"
        ) as upload:
            self.breakdown.handle_local_upload_request(handler)

        upload.assert_not_called()
        handler._send.assert_called_once()
        self.assertEqual(handler._send.call_args.args[0], 401)

    def test_core_local_upload_requires_initial_password_change(self):
        handler = self._local_upload_handler()
        user = {"username": "route-user", "must_change": True}
        with mock.patch.object(
            self.core, "verify", return_value=user
        ), mock.patch.object(
            self.core, "_must_change_password", return_value=True
        ), mock.patch.object(
            self.breakdown, "handle_local_upload"
        ) as upload:
            self.breakdown.handle_local_upload_request(handler)

        upload.assert_not_called()
        handler._send.assert_called_once()
        self.assertEqual(handler._send.call_args.args[0], 403)

    def test_core_local_upload_has_no_optional_module_dependency(self):
        source = (self.root / "server/content_domains/core.py").read_text(
            encoding="utf-8"
        )
        route = source.split(
            'if p == "/api/gen/breakdown/local-upload":', 1
        )[1].split('if p == "/api/gen/asset/favorite":', 1)[0]
        self.assertIn(".breakdown", route)
        self.assertIn("handle_local_upload_request", route)
        self.assertNotIn("local_reverse_upload", route)

    @staticmethod
    def _create_jobs_database(path):
        connection = sqlite3.connect(path)
        connection.execute("""CREATE TABLE jobs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT, username TEXT, cost INTEGER,
            status TEXT DEFAULT 'pending', payload TEXT, result TEXT, error TEXT,
            created_at INTEGER, updated_at INTEGER,
            deleted INTEGER DEFAULT 0, refunded INTEGER DEFAULT 0, owner TEXT
        )""")
        connection.commit()
        connection.close()

    @staticmethod
    def _raw_upload_handler(
        data, media_type="image", content_type="image/jpeg",
        idem_key="local-upload-test-0001", file_name="demo.jpg",
        content_sha256=None,
    ):
        handler = mock.Mock()
        handler.path = (
            "/api/gen/breakdown/local-upload?media_type=" + media_type
        )
        handler.headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(data)),
            "X-File-Name": file_name,
            "X-Content-SHA256": content_sha256 or hashlib.sha256(data).hexdigest(),
        }
        if idem_key is not None:
            handler.headers["Idempotency-Key"] = idem_key
        handler.rfile = io.BytesIO(data)
        return handler

    def _local_upload_context(self, root, connect, *, points=100, enqueue=True):
        stack = ExitStack()
        stack.enter_context(mock.patch.object(self.core, "OUT_DIR", root))
        stack.enter_context(mock.patch.object(self.core, "jdb", connect))
        stack.enter_context(mock.patch.object(
            self.core, "_domains", return_value=(mock.Mock(), self.points, mock.Mock())
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
        stack.enter_context(mock.patch.object(
            self.points, "get_points", return_value=points
        ))
        return stack

    def test_real_local_upload_creates_one_job_token_and_charge(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "jobs.db"
            self._create_jobs_database(database)
            connect = self._jdb(database)
            handler = self._raw_upload_handler(b"\xff\xd8\xffvalid-jpeg")
            with self._local_upload_context(root, connect), mock.patch.object(
                self.points, "deduct_points", return_value=80
            ) as deduct:
                self.breakdown.handle_local_upload(
                    handler, {"username": "alice"}
                )

            self.assertEqual(handler._send.call_args.args[0], 200)
            response = handler._send.call_args.args[1]
            self.assertEqual(response["points_left"], 80)
            deduct.assert_called_once_with("alice", 20, "job:breakdown")
            with closing(connect()) as connection:
                jobs = connection.execute(
                    "SELECT id,status,payload FROM jobs"
                ).fetchall()
                uploads = connection.execute(
                    "SELECT token,username,job_id,suffix FROM breakdown_uploads"
                ).fetchall()
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["status"], "pending")
            self.assertEqual(json.loads(jobs[0]["payload"])["upload_token"], uploads[0]["token"])
            self.assertEqual(len(uploads), 1)
            self.assertEqual(uploads[0]["username"], "alice")
            self.assertEqual(uploads[0]["job_id"], jobs[0]["id"])
            self.assertTrue(
                (root / "_breakdown_uploads" /
                 (uploads[0]["token"] + uploads[0]["suffix"])).is_file()
            )

    def test_real_http_handler_replays_same_binary_key_without_second_charge(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "jobs.db"
            self._create_jobs_database(database)
            connect = self._jdb(database)
            data = b"\xff\xd8\xffvalid-http-jpeg"
            digest = hashlib.sha256(data).hexdigest()
            server = None
            with self._local_upload_context(root, connect), mock.patch.object(
                self.core, "verify",
                return_value={"username": "alice", "must_change": False},
            ), mock.patch.object(
                self.core, "_must_change_password", return_value=False
            ), mock.patch.object(
                self.points, "deduct_points", return_value=80
            ) as deduct:
                try:
                    server = ThreadingHTTPServer(("127.0.0.1", 0), self.core.H)
                    thread = threading.Thread(
                        target=server.serve_forever, daemon=True
                    )
                    thread.start()
                    url = (
                        "http://127.0.0.1:%d/api/gen/breakdown/local-upload"
                        "?media_type=image" % server.server_address[1]
                    )

                    def submit(payload, sha256=digest):
                        request = urllib.request.Request(
                            url, data=payload, method="POST", headers={
                                "Authorization": "Bearer test",
                                "Content-Type": "image/jpeg",
                                "X-File-Name": "demo.jpg",
                                "X-Content-SHA256": sha256,
                                "Idempotency-Key": "local-http-replay-0001",
                            },
                        )
                        with urllib.request.urlopen(request, timeout=5) as response:
                            return response.status, json.loads(response.read())

                    first = submit(data)
                    replay = submit(data)
                    self.assertEqual(first, replay)
                    self.assertEqual(first[0], 200)
                    with self.assertRaises(urllib.error.HTTPError) as conflict:
                        submit(
                            b"\xff\xd8\xffchanged-http-jpeg",
                            hashlib.sha256(
                                b"\xff\xd8\xffchanged-http-jpeg"
                            ).hexdigest(),
                        )
                    self.assertEqual(conflict.exception.code, 409)
                    self.assertEqual(
                        json.loads(conflict.exception.read())["code"],
                        "idempotency_conflict",
                    )
                finally:
                    if server:
                        server.shutdown()
                        server.server_close()

            deduct.assert_called_once_with("alice", 20, "job:breakdown")
            with closing(connect()) as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1
                )

    def test_local_upload_broken_response_keeps_queued_source_and_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "jobs.db"
            self._create_jobs_database(database)
            connect = self._jdb(database)
            handler = self._raw_upload_handler(b"\xff\xd8\xffvalid-jpeg")
            handler._send.side_effect = BrokenPipeError("client disconnected")
            with self._local_upload_context(root, connect), mock.patch.object(
                self.points, "deduct_points", return_value=80
            ) as deduct, mock.patch.object(
                self.points, "safe_refund_points", return_value=100
            ) as refund:
                with self.assertRaises(BrokenPipeError):
                    self.breakdown.handle_local_upload(
                        handler, {"username": "alice"}
                    )

                retry = self._raw_upload_handler(b"\xff\xd8\xffvalid-jpeg")
                self.breakdown.handle_local_upload(retry, {"username": "alice"})

            deduct.assert_called_once_with("alice", 20, "job:breakdown")
            refund.assert_not_called()
            self.assertEqual(retry._send.call_args.args[0], 200)
            self.assertEqual(retry._send.call_args.args[1]["job_id"], 1)
            self.assertEqual(retry.rfile.tell(), 0)
            with closing(connect()) as connection:
                jobs = connection.execute(
                    "SELECT id,status,refunded FROM jobs"
                ).fetchall()
                uploads = connection.execute(
                    "SELECT token,job_id,suffix FROM breakdown_uploads"
                ).fetchall()
            self.assertEqual(len(jobs), 1)
            self.assertEqual(
                (jobs[0]["status"], jobs[0]["refunded"]), ("pending", 0)
            )
            self.assertEqual(len(uploads), 1)
            self.assertEqual(uploads[0]["job_id"], jobs[0]["id"])
            self.assertTrue(
                (root / "_breakdown_uploads" /
                 (uploads[0]["token"] + uploads[0]["suffix"])).is_file()
            )

    def test_local_upload_requires_stable_idempotency_key_before_body(self):
        handler = self._raw_upload_handler(
            b"\xff\xd8\xffvalid-jpeg", idem_key=None
        )
        with mock.patch.object(self.points, "get_points") as get_points:
            self.breakdown.handle_local_upload(handler, {"username": "alice"})
        self.assertEqual(handler._send.call_args.args[0], 400)
        self.assertEqual(
            handler._send.call_args.args[1]["code"], "idempotency_key_required"
        )
        self.assertEqual(handler.rfile.tell(), 0)
        get_points.assert_not_called()

    def test_local_upload_same_key_different_file_conflicts_before_body(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "jobs.db"
            self._create_jobs_database(database)
            connect = self._jdb(database)
            first = self._raw_upload_handler(b"\xff\xd8\xffvalid-jpeg")
            changed = self._raw_upload_handler(
                b"\xff\xd8\xffdifferent-jpeg", file_name="changed.jpg"
            )
            with self._local_upload_context(root, connect), mock.patch.object(
                self.points, "deduct_points", return_value=80
            ) as deduct:
                self.breakdown.handle_local_upload(first, {"username": "alice"})
                self.breakdown.handle_local_upload(changed, {"username": "alice"})
            self.assertEqual(changed._send.call_args.args[0], 409)
            self.assertEqual(
                changed._send.call_args.args[1]["code"], "idempotency_conflict"
            )
            self.assertEqual(changed.rfile.tell(), 0)
            deduct.assert_called_once()
            with closing(connect()) as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1
                )

    def test_local_upload_same_key_in_progress_preserves_original_request(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "jobs.db"
            self._create_jobs_database(database)
            connect = self._jdb(database)
            handler = self._raw_upload_handler(b"\xff\xd8\xffvalid-jpeg")
            identity = {
                "media_type": "image",
                "content_type": "image/jpeg",
                "content_length": len(b"\xff\xd8\xffvalid-jpeg"),
                "file_name": "demo.jpg",
                "content_sha256": hashlib.sha256(
                    b"\xff\xd8\xffvalid-jpeg"
                ).hexdigest(),
            }
            with self._local_upload_context(root, connect):
                state, _ = self.core._idempotency_begin(
                    "alice", "/api/gen/breakdown/local-upload",
                    "local-upload-test-0001", identity,
                )
                self.assertEqual(state, "new")
                with mock.patch.object(self.points, "deduct_points") as deduct:
                    self.breakdown.handle_local_upload(
                        handler, {"username": "alice"}
                    )
            self.assertEqual(handler._send.call_args.args[0], 409)
            self.assertEqual(
                handler._send.call_args.args[1]["code"],
                "idempotency_in_progress",
            )
            self.assertEqual(handler.rfile.tell(), 0)
            deduct.assert_not_called()
            with closing(connect()) as connection:
                row = connection.execute(
                    "SELECT response_json FROM submission_idempotency"
                ).fetchone()
            self.assertIsNotNone(row)
            self.assertIsNone(row["response_json"])

    def test_local_upload_rejects_body_hash_mismatch_before_charge(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "jobs.db"
            self._create_jobs_database(database)
            connect = self._jdb(database)
            handler = self._raw_upload_handler(
                b"\xff\xd8\xffvalid-jpeg", content_sha256="0" * 64
            )
            with self._local_upload_context(root, connect), mock.patch.object(
                self.points, "deduct_points"
            ) as deduct:
                self.breakdown.handle_local_upload(
                    handler, {"username": "alice"}
                )
            self.assertEqual(handler._send.call_args.args[0], 400)
            self.assertIn("校验失败", handler._send.call_args.args[1]["detail"])
            deduct.assert_not_called()
            with closing(connect()) as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM submission_idempotency"
                    ).fetchone()[0], 0
                )

    def test_local_upload_precheck_failure_returns_json_without_reading_body(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "jobs.db"
            self._create_jobs_database(database)
            connect = self._jdb(database)
            handler = self._raw_upload_handler(b"\xff\xd8\xffvalid-jpeg")
            with self._local_upload_context(root, connect), mock.patch.object(
                self.points, "get_points", side_effect=RuntimeError("auth down")
            ), mock.patch.object(self.points, "deduct_points") as deduct:
                self.breakdown.handle_local_upload(
                    handler, {"username": "alice"}
                )
            self.assertEqual(handler._send.call_args.args[0], 502)
            self.assertEqual(
                handler._send.call_args.args[1]["code"],
                "local_upload_precheck_unavailable",
            )
            self.assertEqual(handler.rfile.tell(), 0)
            deduct.assert_not_called()
            with closing(connect()) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM submission_idempotency"
                    ).fetchone()[0], 0
                )

    def test_local_upload_rechecks_active_cap_after_stream_before_charge(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "jobs.db"
            self._create_jobs_database(database)
            connect = self._jdb(database)
            handler = self._raw_upload_handler(b"\xff\xd8\xffvalid-jpeg")
            with self._local_upload_context(root, connect), mock.patch.object(
                self.core, "_user_active_job_count",
                side_effect=[0, self.core.MAX_USER_ACTIVE_JOBS],
            ), mock.patch.object(self.points, "deduct_points") as deduct:
                self.breakdown.handle_local_upload(
                    handler, {"username": "alice"}
                )
            self.assertEqual(handler._send.call_args.args[0], 429)
            self.assertEqual(
                handler._send.call_args.args[1]["code"], "active_job_cap"
            )
            self.assertEqual(handler.rfile.tell(), len(b"\xff\xd8\xffvalid-jpeg"))
            deduct.assert_not_called()
            with closing(connect()) as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM submission_idempotency"
                    ).fetchone()[0], 0
                )

    def test_local_upload_enqueue_cas_loss_never_reports_false_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "jobs.db"
            self._create_jobs_database(database)
            connect = self._jdb(database)
            handler = self._raw_upload_handler(b"\xff\xd8\xffvalid-jpeg")
            with self._local_upload_context(
                root, connect, enqueue=False
            ), mock.patch.object(
                self.points, "deduct_points", return_value=80
            ) as deduct, mock.patch.object(
                self.core, "_reject_pending_job", return_value=False
            ):
                self.breakdown.handle_local_upload(
                    handler, {"username": "alice"}
                )
            self.assertEqual(handler._send.call_args.args[0], 202)
            response = handler._send.call_args.args[1]
            self.assertEqual(response["status"], "pending_reconciliation")
            self.assertEqual(response["job_id"], 1)
            deduct.assert_called_once()

    def test_local_upload_insufficient_points_rejects_before_body_or_database(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "jobs.db"
            self._create_jobs_database(database)
            connect = self._jdb(database)
            handler = self._raw_upload_handler(b"\xff\xd8\xffvalid-jpeg")
            with self._local_upload_context(
                root, connect, points=0
            ), mock.patch.object(self.points, "deduct_points") as deduct:
                self.breakdown.handle_local_upload(
                    handler, {"username": "alice"}
                )

            self.assertEqual(handler._send.call_args.args[0], 402)
            self.assertEqual(handler.rfile.tell(), 0)
            deduct.assert_not_called()
            with closing(connect()) as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0
                )
            self.assertFalse((root / "_breakdown_uploads").exists())

    def test_local_upload_invalid_or_overlong_video_never_charges(self):
        cases = (
            (ValueError("invalid video"), "invalid"),
            (121.0, "overlong"),
        )
        for probe_result, label in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                database = root / "jobs.db"
                self._create_jobs_database(database)
                connect = self._jdb(database)
                handler = self._raw_upload_handler(
                    b"\x00\x00\x00\x18ftypisom",
                    media_type="video", content_type="video/mp4",
                )
                probe = mock.patch.object(
                    self.breakdown, "_probe_duration",
                    side_effect=probe_result if isinstance(probe_result, Exception) else None,
                    return_value=probe_result if not isinstance(probe_result, Exception) else None,
                )
                with self._local_upload_context(root, connect), probe, mock.patch.object(
                    self.points, "deduct_points"
                ) as deduct:
                    self.breakdown.handle_local_upload(
                        handler, {"username": "alice"}
                    )

                self.assertEqual(handler._send.call_args.args[0], 400)
                deduct.assert_not_called()
                with closing(connect()) as connection:
                    self.assertEqual(
                        connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0
                    )
                upload_root = root / "_breakdown_uploads"
                self.assertFalse(upload_root.exists() and any(upload_root.iterdir()))

    def test_local_upload_queue_failure_refunds_once_and_cleans_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "jobs.db"
            self._create_jobs_database(database)
            connect = self._jdb(database)
            handler = self._raw_upload_handler(b"\xff\xd8\xffvalid-jpeg")
            with self._local_upload_context(
                root, connect, enqueue=False
            ), mock.patch.object(
                self.points, "deduct_points", return_value=80
            ) as deduct, mock.patch.object(
                self.points, "safe_refund_points", return_value=100
            ) as refund:
                self.breakdown.handle_local_upload(
                    handler, {"username": "alice"}
                )

            self.assertEqual(handler._send.call_args.args[0], 429)
            deduct.assert_called_once()
            refund.assert_called_once()
            with closing(connect()) as connection:
                job = connection.execute(
                    "SELECT status,refunded FROM jobs"
                ).fetchone()
                uploads = connection.execute(
                    "SELECT COUNT(*) FROM breakdown_uploads"
                ).fetchone()[0]
            self.assertEqual((job["status"], job["refunded"]), ("error", 1))
            self.assertEqual(uploads, 0)
            upload_root = root / "_breakdown_uploads"
            self.assertFalse(upload_root.exists() and any(upload_root.iterdir()))

    def test_local_upload_deduct_error_uses_current_points_error_abi(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "jobs.db"
            self._create_jobs_database(database)
            connect = self._jdb(database)
            handler = self._raw_upload_handler(b"\xff\xd8\xffvalid-jpeg")
            error = self.points.AuthPointsError(402, "点数不足", {"points": 3})
            with self._local_upload_context(root, connect), mock.patch.object(
                self.points, "deduct_points", side_effect=error
            ) as deduct:
                self.breakdown.handle_local_upload(
                    handler, {"username": "alice"}
                )

            self.assertEqual(handler._send.call_args.args[0], 402)
            self.assertEqual(handler._send.call_args.args[1]["detail"], "点数不足")
            deduct.assert_called_once()
            with closing(connect()) as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0
                )
            upload_root = root / "_breakdown_uploads"
            self.assertFalse(upload_root.exists() and any(upload_root.iterdir()))

    def test_public_link_validation_resolves_before_worker(self):
        payload = self.breakdown.validate_breakdown_payload({
            "url": "https://www.douyin.com/video/1234567890123456789",
            "mode": "reverse_prompt",
        })
        self.assertEqual(payload["mode"], "reverse_prompt")
        self.assertEqual(payload["_resolved_link"], {
            "url": "https://www.douyin.com/video/1234567890123456789",
            "platform": "douyin",
            "id": "1234567890123456789",
            "note_type": "video",
        })

    def test_public_json_cannot_supply_token_or_server_path(self):
        for field in ("upload_token", "local_path", "local_media_path"):
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, "专用上传接口"):
                    self.breakdown.validate_breakdown_payload({
                        "url": "https://www.douyin.com/video/1234567890123456789",
                        field: "client-controlled",
                    })

    def test_link_worker_reuses_server_resolved_identity(self):
        url = "https://www.douyin.com/video/1234567890123456789"
        payload = {
            "url": url,
            "mode": "reverse_prompt",
            "_resolved_link": {
                "url": url,
                "platform": "douyin",
                "id": "1234567890123456789",
                "note_type": "video",
            },
        }
        fake_tikhub = mock.Mock()
        fake_tikhub.parse_link.side_effect = AssertionError(
            "validated jobs must not resolve the link twice"
        )
        with mock.patch.dict(sys.modules, {"tikhub": fake_tikhub}):
            with mock.patch.object(
                self.breakdown, "_do_breakdown", return_value={"ok": True}
            ) as run:
                result = self.breakdown.gen_breakdown(payload)
        self.assertEqual(result, {"ok": True})
        self.assertEqual(run.call_args.args[1]["platform"], "douyin")
        self.assertEqual(run.call_args.args[1]["id"], "1234567890123456789")

    def test_upload_token_worker_requires_database_owner_and_job_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "jobs.db"
            upload_root = root / "_breakdown_uploads"
            upload_root.mkdir()
            token = "a" * 32
            source = upload_root / (token + ".jpg")
            source.write_bytes(b"\xff\xd8\xffsource")
            connect = self._jdb(database)
            with closing(connect()) as connection:
                self.breakdown._ensure_upload_table(connection)
                connection.execute(
                    "INSERT INTO breakdown_uploads"
                    "(token,username,suffix,job_id,created_at) VALUES(?,?,?,?,?)",
                    (token, "alice", ".jpg", 41, 1),
                )
                connection.commit()

            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(self.core, "OUT_DIR", root))
                stack.enter_context(mock.patch.object(self.core, "jdb", connect))
                for username, job_id in (("mallory", 41), ("alice", 42)):
                    with self.subTest(username=username, job_id=job_id):
                        with self.assertRaisesRegex(
                            ValueError, "不存在或不属于当前任务"
                        ):
                            self.breakdown.gen_breakdown({
                                "upload_token": token,
                                "media_type": "image",
                                "_username": username,
                                "_job_id": job_id,
                            })
            self.assertTrue(source.exists())

    def test_upload_token_worker_uses_same_eight_frame_accuracy_engine(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "jobs.db"
            upload_root = root / "_breakdown_uploads"
            upload_root.mkdir()
            token = "b" * 32
            source = upload_root / (token + ".jpg")
            source.write_bytes(b"\xff\xd8\xffsource")
            connect = self._jdb(database)
            with closing(connect()) as connection:
                self.breakdown._ensure_upload_table(connection)
                connection.execute(
                    "INSERT INTO breakdown_uploads"
                    "(token,username,suffix,job_id,created_at) VALUES(?,?,?,?,?)",
                    (token, "alice", ".jpg", 42, 1),
                )
                connection.commit()
            captured = {}

            def reverse(payload, frames, **kwargs):
                captured["payload"] = payload
                captured["frames"] = list(frames)
                captured["kwargs"] = kwargs
                return {
                    "type": "breakdown_reverse",
                    "reference_thumbnail_indices": [1],
                }

            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(self.core, "OUT_DIR", root))
                stack.enter_context(mock.patch.object(self.core, "jdb", connect))
                stack.enter_context(mock.patch.object(
                    self.breakdown,
                    "_reverse_result_from_frames",
                    side_effect=reverse,
                ))
                stack.enter_context(mock.patch.object(
                    self.breakdown, "_heartbeat"
                ))
                result = self.breakdown.gen_breakdown({
                    "upload_token": token,
                    "media_type": "image",
                    "_username": "alice",
                    "_job_id": 42,
                })

            self.assertEqual(result["type"], "breakdown_reverse")
            self.assertEqual(len(captured["frames"]), 8)
            self.assertEqual(set(captured["frames"]), {str(source)})
            self.assertEqual(captured["kwargs"]["platform"], "local")
            self.assertFalse(source.exists())
            with closing(connect()) as connection:
                remaining = connection.execute(
                    "SELECT COUNT(*) FROM breakdown_uploads"
                ).fetchone()[0]
            self.assertEqual(remaining, 0)

    def test_main_internal_local_media_worker_is_not_regressed(self):
        payload = {"local_media_path": "server-created", "mode": "local_reverse"}
        processor = mock.Mock()
        processor.gen_local_reverse.return_value = {"ok": "main"}
        with mock.patch.dict(
            sys.modules,
            {"content_domains.local_reverse_processor": processor},
        ):
            result = self.breakdown.gen_breakdown(payload)
        self.assertEqual(result, {"ok": "main"})
        processor.gen_local_reverse.assert_called_once_with(payload)

    def test_egress_timeout_override_remains_per_attempt(self):
        calls = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"ok":true}'

        class Opener:
            def open(self, request, timeout=None):
                calls.append(timeout)
                return Response()

        with mock.patch.object(
            self.egress, "channels",
            return_value=[("heygen", "https://relay", None, 210)],
        ):
            with mock.patch.object(
                self.egress, "_opener", return_value=Opener()
            ):
                result = self.egress.post_json_idempotent(
                    "https://official",
                    "https://relay",
                    "/chat",
                    b"{}",
                    {},
                    timeout=37,
                )
        self.assertEqual(result, {"ok": True})
        self.assertEqual(calls, [37])

    def test_script_preserves_runtime_features_and_explicit_reference_mapping(self):
        for marker in (
            "function _pendingSubmission(",
            "function _confirmSubmission(",
            "function _resumeActiveVideoJob(",
            "function applyIP12Handoff(",
            "function reverseReferenceThumbnailIndices(bd)",
            "function reverseReferenceImages(bd)",
            "thumbs[index-1]||null",
            "channel:choice.channel",
            "reverse_remake_video_channel",
            "reference_images:reverseRefs",
        ):
            self.assertIn(marker, self.script)
        self.assertNotIn("frame_thumbnails)||[]).slice(0,4)", self.script)
        self.assertNotIn("seedance-2.0-fast", self.script)
        self.assertNotIn("doubao-seedance-2-0-260128", self.script)

    def test_tikhub_is_unchanged_and_already_has_download_safety_contract(self):
        source = (self.root / "server/tikhub.py").read_text(encoding="utf-8")
        self.assertIn('declared_raw = r.headers.get("Content-Length")', source)
        self.assertIn("下载响应截断", source)
        self.assertIn(
            "def download_to_file(url, deadline_ts, dest_path, max_bytes=",
            source,
        )
        self.assertIn("fresh=False", source)


if __name__ == "__main__":
    unittest.main()
