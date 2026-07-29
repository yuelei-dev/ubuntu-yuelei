import importlib
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import ExitStack, closing
from pathlib import Path
from unittest import mock

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
            "_pendingSubmission('script-breakdown-local'",
            "'Idempotency-Key':localPending.key",
        ):
            self.assertIn(marker, self.script)
        self.assertNotIn("frame_thumbnails)||[]).slice(0,4)", self.script)

    def test_script_delegates_model_selection_to_untouched_video_backend(self):
        self.assertIn("channel:'micro'", self.script)
        self.assertNotIn("seedance-2.0-fast", self.script)
        self.assertNotIn("doubao-seedance-2-0-260128", self.script)

    def test_local_upload_keeps_idempotency_key_until_valid_job_json(self):
        block = self.script.split(
            "function _submitLocalReverse(mediaType,file,btn){", 1
        )[1].split("if(bdImagePick)", 1)[0]
        parsed = (
            "r.json().then(function(d){return {s:r.status,d:d};})"
        )
        confirmed = "_confirmSubmission(localPending);"
        self.assertIn(parsed, block)
        self.assertIn("if(!x.d.job_id)", block)
        self.assertIn(confirmed, block)
        self.assertLess(block.index(parsed), block.index(confirmed))
        self.assertLess(block.index("if(!x.d.job_id)"), block.index(confirmed))
        self.assertNotIn("if(response.ok)_confirmSubmission", block)

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
