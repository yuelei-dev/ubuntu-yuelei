# -*- coding: utf-8 -*-
import importlib
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
