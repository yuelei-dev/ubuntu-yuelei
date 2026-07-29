import io
import importlib
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import ExitStack, closing
from pathlib import Path
from unittest import mock


class _Handler:
    def __init__(self, body, media_type="image", content_type="image/jpeg"):
        self.path = "/api/gen/breakdown/local?media_type=" + media_type
        self.headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
        }
        self.rfile = io.BytesIO(body)
        self.responses = []

    def _send(self, status, body):
        self.responses.append((status, body))
        return status, body


class BreakdownRuntimeCompatibilityTests(unittest.TestCase):
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

    def test_runtime_public_abi_is_present(self):
        for name in (
            "validate_breakdown_payload",
            "handle_local_upload",
            "gen_breakdown",
            "_do_local_reverse",
        ):
            self.assertTrue(callable(getattr(self.breakdown, name, None)), name)
        for name in ("heygen_proxy", "post_image_json", "post_json_idempotent"):
            self.assertTrue(callable(getattr(self.egress, name, None)), name)

    def test_public_link_validation_resolves_before_charge(self):
        payload = self.breakdown.validate_breakdown_payload({
            "url": "https://www.douyin.com/video/1234567890123456789",
            "mode": "reverse_prompt",
        })
        self.assertEqual(payload["mode"], "reverse_prompt")
        self.assertEqual(payload["_resolved_link"]["platform"], "douyin")
        self.assertEqual(payload["_resolved_link"]["id"], "1234567890123456789")

    def test_public_link_validation_rejects_private_path_and_upload_token(self):
        for private_field in ("local_path", "local_media_path", "upload_token"):
            with self.subTest(private_field=private_field):
                with self.assertRaisesRegex(ValueError, "专用上传接口"):
                    self.breakdown.validate_breakdown_payload({
                        "url": "https://www.douyin.com/video/1234567890123456789",
                        private_field: "not-public",
                    })

    def test_upload_token_dispatches_before_link_parsing(self):
        payload = {
            "upload_token": "a" * 32,
            "media_type": "video",
            "_username": "alice",
            "_job_id": 7,
        }
        with mock.patch.object(
            self.breakdown, "_do_local_reverse", return_value={"ok": True}
        ) as local:
            self.assertEqual(self.breakdown.gen_breakdown(payload), {"ok": True})
        local.assert_called_once_with(payload, "a" * 32)

    def test_main_local_upload_queue_abi_dispatches_to_same_engine(self):
        payload = {
            "mode": "local_reverse",
            "local_media_path": "server-owned-upload.mp4",
            "local_media_type": "video",
            "_username": "alice",
            "_job_id": 8,
        }
        with mock.patch.object(
            self.breakdown, "_do_legacy_local_reverse", return_value={"ok": True}
        ) as local:
            self.assertEqual(self.breakdown.gen_breakdown(payload), {"ok": True})
        local.assert_called_once_with(payload)

    def test_resolved_link_is_reused_without_second_parse(self):
        payload = {
            "url": "https://www.douyin.com/video/1234567890123456789",
            "mode": "reverse_prompt",
            "_resolved_link": {
                "url": "https://www.douyin.com/video/1234567890123456789",
                "platform": "douyin",
                "id": "1234567890123456789",
                "note_type": "video",
            },
        }
        fake_tikhub = mock.Mock()
        fake_tikhub.parse_link.side_effect = AssertionError("must not re-resolve")
        with mock.patch.dict(sys.modules, {"tikhub": fake_tikhub}):
            with mock.patch.object(
                self.breakdown, "_do_breakdown", return_value={"ok": True}
            ) as run:
                result = self.breakdown.gen_breakdown(payload)
        self.assertEqual(result, {"ok": True})
        run.assert_called_once()

    def test_local_video_uses_eight_frame_reverse_engine_and_cleans_token(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "jobs.db"
            upload_root = root / "_breakdown_uploads"
            upload_root.mkdir()
            token = "b" * 32
            source = upload_root / (token + ".mp4")
            source.write_bytes(b"\x00\x00\x00\x18ftypisom")
            connect = self._jdb(database)
            with closing(connect()) as connection:
                self.breakdown._ensure_upload_table(connection)
                connection.execute(
                    "INSERT INTO breakdown_uploads"
                    "(token,username,suffix,job_id,created_at) VALUES(?,?,?,?,?)",
                    (token, "alice", ".mp4", 41, 1),
                )
                connection.commit()
            frames_dir = root / "frames"
            frames_dir.mkdir()
            frames = []
            for index in range(8):
                frame = frames_dir / ("frame-%d.jpg" % index)
                frame.write_bytes(b"frame")
                frames.append(str(frame))
            captured = {}

            def reverse(payload, model_frames, **kwargs):
                captured["frames"] = list(model_frames)
                captured["kwargs"] = kwargs
                return {"type": "breakdown_reverse", "prompt": "verified"}

            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(self.core, "OUT_DIR", root))
                stack.enter_context(mock.patch.object(self.core, "jdb", connect))
                stack.enter_context(mock.patch.object(
                    self.breakdown, "_probe_duration", return_value=12.5
                ))
                stack.enter_context(mock.patch.object(
                    self.breakdown,
                    "_extract_frames",
                    return_value=(str(frames_dir), frames),
                ))
                stack.enter_context(mock.patch.object(
                    self.breakdown, "_reverse_result_from_frames",
                    side_effect=reverse,
                ))
                stack.enter_context(mock.patch.object(
                    self.breakdown, "_heartbeat"
                ))
                result = self.breakdown._do_local_reverse({
                    "upload_token": token,
                    "media_type": "video",
                    "_username": "alice",
                    "_job_id": 41,
                }, token)

            self.assertEqual(result["prompt"], "verified")
            self.assertEqual(captured["frames"], frames)
            self.assertEqual(captured["kwargs"]["duration"], 12.5)
            self.assertEqual(captured["kwargs"]["platform"], "local")
            self.assertFalse(source.exists())
            self.assertFalse(frames_dir.exists())
            with closing(connect()) as connection:
                count = connection.execute(
                    "SELECT COUNT(*) FROM breakdown_uploads"
                ).fetchone()[0]
            self.assertEqual(count, 0)

    def test_local_image_uses_auditable_eight_frame_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "jobs.db"
            upload_root = root / "_breakdown_uploads"
            upload_root.mkdir()
            token = "c" * 32
            source = upload_root / (token + ".jpg")
            source.write_bytes(b"\xff\xd8\xfftest")
            connect = self._jdb(database)
            with closing(connect()) as connection:
                self.breakdown._ensure_upload_table(connection)
                connection.execute(
                    "INSERT INTO breakdown_uploads"
                    "(token,username,suffix,job_id,created_at) VALUES(?,?,?,?,?)",
                    (token, "alice", ".jpg", 42, 1),
                )
                connection.commit()
            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(self.core, "OUT_DIR", root))
                stack.enter_context(mock.patch.object(self.core, "jdb", connect))
                reverse = stack.enter_context(mock.patch.object(
                    self.breakdown,
                    "_reverse_result_from_frames",
                    return_value={"type": "breakdown_reverse"},
                ))
                stack.enter_context(mock.patch.object(
                    self.breakdown, "_heartbeat"
                ))
                self.breakdown._do_local_reverse({
                    "upload_token": token,
                    "media_type": "image",
                    "_username": "alice",
                    "_job_id": 42,
                }, token)
            model_frames = reverse.call_args.args[1]
            self.assertEqual(len(model_frames), 8)
            self.assertEqual(set(model_frames), {str(source)})

    def test_main_legacy_local_upload_is_confined_and_uses_reverse_engine(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            upload_root = root / "reverse_uploads"
            upload_root.mkdir()
            source = upload_root / "trusted.jpg"
            source.write_bytes(b"\xff\xd8\xfftest")
            payload = {
                "mode": "local_reverse",
                "local_media_path": str(source),
                "local_media_type": "image",
                "source_title": "white-card.jpg",
                "_username": "alice",
                "_job_id": 43,
            }
            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(self.core, "OUT_DIR", root))
                reverse = stack.enter_context(mock.patch.object(
                    self.breakdown,
                    "_reverse_result_from_frames",
                    return_value={"type": "breakdown_reverse"},
                ))
                stack.enter_context(mock.patch.object(
                    self.breakdown, "_heartbeat"
                ))
                result = self.breakdown.gen_breakdown(payload)
            self.assertEqual(result["type"], "breakdown_reverse")
            model_frames = reverse.call_args.args[1]
            self.assertEqual(len(model_frames), 8)
            self.assertEqual(set(model_frames), {str(source)})
            self.assertEqual(reverse.call_args.kwargs["title"], "white-card.jpg")
            self.assertFalse(source.exists())

    def test_main_legacy_local_upload_rejects_paths_outside_owned_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "reverse_uploads").mkdir()
            outside = root / "outside.jpg"
            outside.write_bytes(b"\xff\xd8\xfftest")
            with mock.patch.object(self.core, "OUT_DIR", root):
                with self.assertRaisesRegex(ValueError, "本地素材已失效"):
                    self.breakdown.gen_breakdown({
                        "mode": "local_reverse",
                        "local_media_path": str(outside),
                        "local_media_type": "image",
                        "_username": "alice",
                        "_job_id": 44,
                    })
            self.assertTrue(outside.exists())

    def test_main_http_upload_route_and_worker_payload_have_compat_bridge(self):
        core_source = (
            self.root / "server/content_domains/core.py"
        ).read_text(encoding="utf-8")
        upload_source = (
            self.root / "server/content_domains/local_reverse_upload.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'p == "/api/gen/breakdown/local-upload"', core_source
        )
        self.assertIn('"local_media_path": str(path)', upload_source)
        self.assertIn(
            "/api/gen/breakdown/local-upload?media_type=", self.script
        )
        self.assertTrue(callable(self.breakdown._do_legacy_local_reverse))

    def test_local_upload_charges_twenty_enqueues_and_persists_owner(self):
        class FakePoints:
            class AuthPointsError(Exception):
                status = 500

            def __init__(self):
                self.deductions = []

            def cost_of(self, kind, body):
                return 20

            def deduct_points(self, username, cost, reason):
                self.deductions.append((username, cost, reason))
                return 80

            def refund_points(self, *args):
                raise AssertionError("successful submit must not refund")

            def public_error_body(self, exc, cost):
                return {"detail": str(exc), "cost": cost}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "jobs.db"
            connect = self._jdb(database)
            points = FakePoints()
            queued = []

            def create_paid_job(
                jdb, deduct, refund, kind, username, cost, payload, owner,
                before_commit=None,
            ):
                points_left = deduct(username, cost, "job:breakdown submit:test")
                with closing(jdb()) as connection:
                    before_commit(connection, 77)
                    connection.commit()
                return 77, points_left

            handler = _Handler(b"\xff\xd8\xff" + b"x" * 13)
            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(self.core, "OUT_DIR", root))
                stack.enter_context(mock.patch.object(self.core, "jdb", connect))
                stack.enter_context(mock.patch.object(
                    self.core, "_domains", return_value=(None, points, None)
                ))
                stack.enter_context(mock.patch.object(
                    self.core.feature_flags, "require_enabled"
                ))
                stack.enter_context(mock.patch.object(
                    self.core, "is_shutting_down", return_value=False
                ))
                stack.enter_context(mock.patch.object(
                    self.core, "_user_active_job_count", return_value=0
                ))
                stack.enter_context(mock.patch.object(
                    self.core, "_submission_lock", threading.Lock()
                ))
                stack.enter_context(mock.patch.object(
                    self.core.jobs_store, "create_paid_job",
                    side_effect=create_paid_job, create=True,
                ))
                stack.enter_context(mock.patch.object(
                    self.core, "enqueue_job",
                    side_effect=lambda *args: queued.append(args) or True,
                ))
                status, response = self.breakdown.handle_local_upload(
                    handler, {"username": "alice"}
                )

            self.assertEqual(status, 200)
            self.assertEqual(response["job_id"], 77)
            self.assertEqual(response["cost"], 20)
            self.assertEqual(points.deductions[0][:2], ("alice", 20))
            self.assertEqual(queued, [(77, "breakdown", "reverse_prompt")])
            with closing(connect()) as connection:
                row = connection.execute(
                    "SELECT username,job_id FROM breakdown_uploads"
                ).fetchone()
            self.assertEqual((row["username"], row["job_id"]), ("alice", 77))

    def test_reverse_success_is_not_treated_as_partial_batch_refund(self):
        refunds = []
        with mock.patch.object(
            self.points, "safe_refund_points",
            side_effect=lambda *args: refunds.append(args),
        ):
            self.points.settle_breakdown_batch(
                "alice", 20,
                {"type": "breakdown_reverse", "errors": [{"detail": "none"}]},
                99,
            )
        self.assertEqual(refunds, [])

    def test_egress_timeout_override_is_per_physical_attempt(self):
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
            with mock.patch.object(self.egress, "_opener", return_value=Opener()):
                result = self.egress.post_json_idempotent(
                    "https://official", "https://relay", "/chat",
                    b"{}", {}, timeout=37,
                )
        self.assertEqual(result, {"ok": True})
        self.assertEqual(calls, [37])

    def test_script_keeps_runtime_guards_and_explicit_reference_mapping(self):
        for marker in (
            "function _pendingSubmission(",
            "function _confirmSubmission(",
            "function _resumeActiveVideoJob(",
            "function applyIP12Handoff(",
            "function reverseReferenceThumbnailIndices(bd)",
            "function reverseReferenceImages(bd)",
            "thumbs[index-1]||null",
        ):
            self.assertIn(marker, self.script)
        self.assertNotIn("frame_thumbnails)||[]).slice(0,4)", self.script)

    def test_script_delegates_model_selection_to_untouched_video_backend(self):
        self.assertIn("channel:'micro'", self.script)
        self.assertNotIn("seedance-2.0-fast", self.script)
        self.assertNotIn("doubao-seedance-2-0-260128", self.script)

    def test_tikhub_retains_safe_length_and_truncation_contract(self):
        source = (self.root / "server/tikhub.py").read_text(encoding="utf-8")
        self.assertIn('declared_raw = r.headers.get("Content-Length")', source)
        self.assertIn("下载响应截断", source)
        self.assertIn(
            "def download_to_file(url, deadline_ts, dest_path, max_bytes=",
            source,
        )
        self.assertIn("if got > max_bytes:", source)
        self.assertIn("fresh=False", source)


if __name__ == "__main__":
    unittest.main()
