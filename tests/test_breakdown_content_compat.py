# -*- coding: utf-8 -*-
import importlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import ExitStack, closing
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
            "handle_local_upload",
            "gen_breakdown",
            "_do_local_reverse",
        ):
            self.assertTrue(callable(getattr(self.breakdown, name, None)), name)
        for name in ("heygen_proxy", "post_image_json", "post_json_idempotent"):
            self.assertTrue(callable(getattr(self.egress, name, None)), name)

    @staticmethod
    def _raw_upload_handler(data, media_type="image", content_type="image/jpeg",
                            idem_key="local-test-key-0001"):
        handler = mock.Mock()
        handler.path = "/api/gen/breakdown/local-upload?media_type=" + media_type
        handler.headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(data)),
        }
        if idem_key is not None:
            handler.headers["Idempotency-Key"] = idem_key
        handler.rfile = io.BytesIO(data)
        handler._token.return_value = "session-token"
        return handler

    @staticmethod
    def _create_jobs_database(path):
        connection = sqlite3.connect(path)
        connection.execute("""CREATE TABLE jobs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT, username TEXT, cost INTEGER,
            status TEXT DEFAULT 'pending', payload TEXT, result TEXT, error TEXT,
            created_at INTEGER, updated_at INTEGER,
            deleted INTEGER DEFAULT 0, refunded INTEGER DEFAULT 0,
            owner TEXT, service_sha TEXT
        )""")
        connection.commit()
        connection.close()

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

    def test_core_local_upload_authenticates_and_uses_bound_token_handler(self):
        handler = self._raw_upload_handler(b"")
        user = {"username": "alice", "must_change": False}
        with mock.patch.object(
            self.core, "_domains", return_value=(mock.Mock(), mock.Mock(), mock.Mock())
        ), mock.patch.object(
            self.core, "verify", return_value=user
        ), mock.patch.object(
            self.core, "_must_change_password", return_value=False
        ), mock.patch.object(
            self.breakdown, "handle_local_upload", return_value="accepted"
        ) as upload:
            result = self.core.H.do_POST(handler)

        self.assertEqual(result, "accepted")
        upload.assert_called_once_with(handler, user)

    def test_core_local_upload_rejects_unauthenticated_before_reading_body(self):
        handler = self._raw_upload_handler(b"secret")
        with mock.patch.object(
            self.core, "_domains", return_value=(mock.Mock(), mock.Mock(), mock.Mock())
        ), mock.patch.object(
            self.core, "verify", return_value=None
        ), mock.patch.object(self.breakdown, "handle_local_upload") as upload:
            self.core.H.do_POST(handler)

        upload.assert_not_called()
        self.assertEqual(handler.rfile.tell(), 0)
        self.assertEqual(handler._send.call_args.args[0], 401)

    def test_core_local_upload_has_no_missing_optional_module_dependency(self):
        source = (self.root / "server/content_domains/core.py").read_text(
            encoding="utf-8"
        )
        route = source.split(
            'if p == "/api/gen/breakdown/local-upload":', 1
        )[1].split('if p == "/api/gen/asset/favorite":', 1)[0]
        self.assertIn('fromlist=["handle_local_upload"]', route)
        self.assertIn(".handle_local_upload(self, user)", route)
        self.assertNotIn("local_reverse_upload", route)

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
                self.breakdown.handle_local_upload(handler, {"username": "alice"})

            self.assertEqual(handler._send.call_args.args[0], 200)
            self.assertEqual(handler._send.call_args.args[1]["points_left"], 80)
            deduct.assert_called_once_with(
                "alice", 20, "job:breakdown#1",
                transaction_key="breakdown:1:charge",
            )
            with closing(connect()) as connection:
                jobs = connection.execute(
                    "SELECT id,status,payload,service_sha FROM jobs"
                ).fetchall()
                uploads = connection.execute(
                    "SELECT token,username,job_id,suffix FROM breakdown_uploads"
                ).fetchall()
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["status"], "pending")
            self.assertEqual(jobs[0]["service_sha"], self.core.jobs_store.SERVICE_SHA)
            self.assertEqual(json.loads(jobs[0]["payload"])["upload_token"], uploads[0]["token"])
            self.assertEqual((uploads[0]["username"], uploads[0]["job_id"]),
                             ("alice", jobs[0]["id"]))
            self.assertTrue(
                (root / "_breakdown_uploads" /
                 (uploads[0]["token"] + uploads[0]["suffix"])).is_file()
            )

    def test_same_key_local_upload_replays_without_second_charge(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "jobs.db"
            self._create_jobs_database(database)
            connect = self._jdb(database)
            first = self._raw_upload_handler(b"\xff\xd8\xffsame-jpeg")
            second = self._raw_upload_handler(b"\xff\xd8\xffsame-jpeg")
            with self._local_upload_context(root, connect), mock.patch.object(
                self.points, "deduct_points", return_value=80
            ) as deduct:
                self.breakdown.handle_local_upload(first, {"username": "alice"})
                self.breakdown.handle_local_upload(second, {"username": "alice"})

            first_response = first._send.call_args.args[1]
            second_response = second._send.call_args.args[1]
            self.assertEqual(second_response["job_id"], first_response["job_id"])
            self.assertEqual(second_response, first_response)
            deduct.assert_called_once_with(
                "alice", 20, "job:breakdown#1",
                transaction_key="breakdown:1:charge",
            )
            with closing(connect()) as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM breakdown_uploads"
                    ).fetchone()[0], 1
                )

    def test_upload_table_migration_adds_idempotency_state_without_changing_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "jobs.db"
            connection = sqlite3.connect(database)
            connection.execute("""CREATE TABLE breakdown_uploads(
                token TEXT PRIMARY KEY, username TEXT NOT NULL,
                suffix TEXT NOT NULL, job_id INTEGER NOT NULL UNIQUE,
                created_at INTEGER NOT NULL
            )""")
            connection.execute(
                "INSERT INTO breakdown_uploads(token,username,suffix,job_id,created_at) "
                "VALUES('legacy','alice','.jpg',7,1)"
            )
            self.breakdown._ensure_upload_table(connection)
            columns = {
                row[1] for row in connection.execute(
                    "PRAGMA table_info(breakdown_uploads)"
                ).fetchall()
            }
            row = connection.execute(
                "SELECT token,idem_key,payment_state,charge_key,refund_key "
                "FROM breakdown_uploads"
            ).fetchone()
            connection.close()

            for column in ("idem_key", "payment_state", "charge_key", "refund_key"):
                self.assertIn(column, columns)
            self.assertEqual(row[0], "legacy")
            self.assertIsNone(row[1])
            self.assertEqual(row[2], "legacy")

    def test_local_upload_broken_response_keeps_job_binding_and_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "jobs.db"
            self._create_jobs_database(database)
            connect = self._jdb(database)
            handler = self._raw_upload_handler(b"\xff\xd8\xffvalid-jpeg")
            handler._send.side_effect = BrokenPipeError("client disconnected")
            with self._local_upload_context(root, connect), mock.patch.object(
                self.points, "deduct_points", return_value=80
            ) as deduct:
                with self.assertRaises(BrokenPipeError):
                    self.breakdown.handle_local_upload(handler, {"username": "alice"})

            deduct.assert_called_once()
            with closing(connect()) as connection:
                job = connection.execute(
                    "SELECT id,status,refunded FROM jobs"
                ).fetchone()
                upload = connection.execute(
                    "SELECT token,job_id,suffix FROM breakdown_uploads"
                ).fetchone()
            self.assertEqual((job["status"], job["refunded"]), ("pending", 0))
            self.assertEqual(upload["job_id"], job["id"])
            self.assertTrue(
                (root / "_breakdown_uploads" /
                 (upload["token"] + upload["suffix"])).is_file()
            )

    def test_local_upload_rejects_insufficient_points_before_reservation_or_charge(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "jobs.db"
            self._create_jobs_database(database)
            connect = self._jdb(database)
            handler = self._raw_upload_handler(b"\xff\xd8\xffvalid-jpeg")
            with self._local_upload_context(
                root, connect, points=0
            ), mock.patch.object(self.points, "deduct_points") as deduct:
                self.breakdown.handle_local_upload(handler, {"username": "alice"})

            self.assertEqual(handler._send.call_args.args[0], 402)
            self.assertEqual(handler.rfile.tell(), len(b"\xff\xd8\xffvalid-jpeg"))
            deduct.assert_not_called()
            with closing(connect()) as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0
                )

    def test_local_upload_price_is_independent_from_link_price_end_to_end(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "jobs.db"
            self._create_jobs_database(database)
            connect = self._jdb(database)
            insufficient = self._raw_upload_handler(
                b"\xff\xd8\xfflocal-price", idem_key="local-price-low-1"
            )
            accepted = self._raw_upload_handler(
                b"\xff\xd8\xfflocal-price", idem_key="local-price-ok-01"
            )
            with self._local_upload_context(root, connect, points=36), mock.patch.object(
                self.points, "breakdown_local_upload_cost", return_value=37
            ), mock.patch.object(self.points, "deduct_points") as deduct:
                self.breakdown.handle_local_upload(
                    insufficient, {"username": "alice"}
                )
            self.assertEqual(insufficient._send.call_args.args[0], 402)
            self.assertEqual(insufficient._send.call_args.args[1]["need"], 37)
            deduct.assert_not_called()

            with self._local_upload_context(root, connect, points=100), mock.patch.object(
                self.points, "breakdown_local_upload_cost", return_value=37
            ), mock.patch.object(
                self.points, "cost_of", return_value=11
            ), mock.patch.object(
                self.points, "deduct_points", return_value=63
            ) as deduct:
                self.breakdown.handle_local_upload(accepted, {"username": "alice"})
            self.assertEqual(accepted._send.call_args.args[0], 200)
            self.assertEqual(accepted._send.call_args.args[1]["cost"], 37)
            deduct.assert_called_once_with(
                "alice", 37, "job:breakdown#1",
                transaction_key="breakdown:1:charge",
            )

    def test_local_upload_invalid_or_overlong_video_never_charges(self):
        for probe_result, label in ((ValueError("invalid video"), "invalid"),
                                    (121.0, "overlong")):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                database = root / "jobs.db"
                self._create_jobs_database(database)
                connect = self._jdb(database)
                handler = self._raw_upload_handler(
                    b"\x00\x00\x00\x18ftypisom",
                    media_type="video", content_type="video/mp4",
                )
                patcher = mock.patch.object(
                    self.breakdown, "_probe_duration",
                    side_effect=probe_result if isinstance(probe_result, Exception) else None,
                    return_value=probe_result if not isinstance(probe_result, Exception) else None,
                )
                with self._local_upload_context(root, connect), patcher, mock.patch.object(
                    self.points, "deduct_points"
                ) as deduct:
                    self.breakdown.handle_local_upload(handler, {"username": "alice"})

                self.assertEqual(handler._send.call_args.args[0], 400)
                deduct.assert_not_called()
                with closing(connect()) as connection:
                    self.assertEqual(
                        connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0
                    )

    def test_local_upload_queue_failure_keeps_pending_job_for_scanner(self):
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
            ):
                self.breakdown.handle_local_upload(handler, {"username": "alice"})

            self.assertEqual(handler._send.call_args.args[0], 200)
            with closing(connect()) as connection:
                job = connection.execute(
                    "SELECT status,refunded FROM jobs"
                ).fetchone()
                uploads = connection.execute(
                    "SELECT COUNT(*) FROM breakdown_uploads"
                ).fetchone()[0]
            self.assertEqual((job["status"], job["refunded"]), ("pending", 0))
            self.assertEqual(uploads, 1)

    def test_local_upload_reservation_failure_never_charges_and_removes_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "jobs.db"
            self._create_jobs_database(database)
            connect = self._jdb(database)
            handler = self._raw_upload_handler(b"\xff\xd8\xffvalid-jpeg")

            with self._local_upload_context(root, connect), mock.patch.object(
                self.points, "deduct_points", return_value=80
            ) as deduct, mock.patch.object(
                self.core.jobs_store, "ensure_service_sha_column_on_conn",
                side_effect=sqlite3.OperationalError("job insert unavailable"),
            ):
                self.breakdown.handle_local_upload(handler, {"username": "alice"})

            self.assertEqual(handler._send.call_args.args[0], 500)
            deduct.assert_not_called()
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
