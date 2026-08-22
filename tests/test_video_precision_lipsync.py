# -*- coding: utf-8 -*-

import importlib
import json
import pathlib
import sys
import tempfile
import unittest
from contextlib import nullcontext
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
video = importlib.import_module("content_domains.video")


class VideoPrecisionLipsyncTests(unittest.TestCase):
    def test_nginx_accepts_the_declared_100mb_upload_boundary(self):
        for relative in (
            "server/nginx-huangquechuanmei.conf",
            "deploy/nginx-huangquechuanmei.conf",
        ):
            source = (ROOT / relative).read_text(encoding="utf-8")
            start = source.index("location = /api/gen/video/lipsync-import")
            end = source.index("\n    }", start)
            location = source[start:end]
            self.assertIn("client_max_body_size 100m;", location)
            self.assertIn("limit_conn hq_cli_upload_conn 2;", location)
            self.assertIn("proxy_set_header X-HQ-Internal-Token \"\";", location)

    def test_validation_resolves_owned_assets_and_forces_precision(self):
        payload = {
            "mode": "lipsync", "video_asset_id": 7, "audio_asset_id": 8,
            "text": "新的口播文案", "lipsync_mode": "precision",
            "dynamic_duration": False, "ratio": "9:16", "resolution": "1080p",
        }
        with mock.patch.object(video, "_owned_video_asset", return_value={
                "video_file": "video/owned.mp4"}), \
             mock.patch.object(video, "_owned_audio_asset", return_value={
                "file": "audio/owned.mp3"}), \
             mock.patch.object(video, "_normalize_audio_file_ref", side_effect=lambda value, username=None: value):
            cleaned = video.validate_video_payload(payload, "fang")
        self.assertEqual("video/owned.mp4", cleaned["source_video_file"])
        self.assertEqual("audio/owned.mp3", cleaned["audio_file"])
        self.assertEqual("precision", cleaned["lipsync_mode"])
        self.assertTrue(cleaned["dynamic_duration"])
        self.assertNotIn("image_data", cleaned)

    def test_validation_rejects_non_precision_mode_before_paid_submission(self):
        payload = {
            "mode": "lipsync", "video_asset_id": 7, "audio_asset_id": 8,
            "lipsync_mode": "speed",
        }
        with mock.patch.object(video, "_owned_video_asset", return_value={
                "video_file": "video/owned.mp4"}), \
             mock.patch.object(video, "_owned_audio_asset", return_value={
                "file": "audio/owned.mp3"}), \
             mock.patch.object(video, "_normalize_audio_file_ref", side_effect=lambda value, username=None: value):
            with self.assertRaisesRegex(ValueError, "仅支持 HeyGen Precision"):
                video.validate_video_payload(payload, "fang")

    def test_api_create_uses_official_v3_precision_contract(self):
        captured = {}

        def request(method, path, body=None, headers=None, timeout=0, direct=False):
            captured.update({
                "method": method, "path": path, "body": json.loads(body),
                "headers": headers, "direct": direct,
            })
            return {"data": {"lipsync_id": "lip_123"}}

        with mock.patch.object(video, "_HEYGEN_ALLOW_API_WALLET", True), \
             mock.patch.object(video, "_heygen_request_json", side_effect=request):
            lipsync_id = video._heygen_create_lipsync(
                "video_asset", "audio_asset", direct=True, route="api_wallet")
        self.assertEqual("lip_123", lipsync_id)
        self.assertEqual(("POST", "/lipsyncs"), (captured["method"], captured["path"]))
        self.assertEqual({"type": "asset_id", "asset_id": "video_asset"}, captured["body"]["video"])
        self.assertEqual({"type": "asset_id", "asset_id": "audio_asset"}, captured["body"]["audio"])
        self.assertEqual("precision", captured["body"]["mode"])
        self.assertTrue(captured["body"]["enable_dynamic_duration"])
        self.assertTrue(captured["body"]["keep_the_same_format"])
        self.assertEqual("cfr", captured["body"]["fps_mode"])
        self.assertTrue(captured["direct"])

    def test_mcp_create_maps_dynamic_duration_to_camel_case(self):
        with mock.patch.object(video, "_heygen_mcp_enabled", return_value=True), \
             mock.patch.object(video, "_heygen_mcp_call", return_value={
                "data": {"lipsyncId": "lip_mcp"}}) as call:
            self.assertEqual("lip_mcp", video._heygen_create_lipsync(
                "video_asset", "audio_asset", route="mcp_oauth"))
        arguments = call.call_args.args[1]
        self.assertEqual("precision", arguments["mode"])
        self.assertTrue(arguments["enableDynamicDuration"])
        self.assertTrue(arguments["keepTheSameFormat"])
        self.assertEqual("cfr", arguments["fpsMode"])

    def test_poll_returns_completed_precision_output(self):
        with mock.patch.object(video, "_heygen_request_json", return_value={
                "data": {"status": "completed", "video_url": "https://example.test/final.mp4"}}):
            result = video._heygen_poll_lipsync("lip_123", direct=True, deadline_s=5)
        self.assertEqual("https://example.test/final.mp4", result["video_url"])

    def test_post_submit_failure_never_creates_a_second_paid_lipsync(self):
        with tempfile.TemporaryDirectory() as directory:
            source = pathlib.Path(directory) / "source.mp4"
            audio = pathlib.Path(directory) / "audio.mp3"
            source.write_bytes(b"source")
            audio.write_bytes(b"audio")
            create = mock.Mock(return_value="lip_paid")
            with mock.patch.object(video, "_resolve_out_file", side_effect=lambda rel: {
                    "video/source.mp4": source, "audio/voice.mp3": audio,
                    }.get(str(rel))), \
                 mock.patch.object(video, "_ensure_heygen_audio_mp3", return_value=audio), \
                 mock.patch.object(video, "_heygen_require_paid_route", return_value="api_wallet"), \
                 mock.patch.object(video, "_HEYGEN_DIRECT", True), \
                 mock.patch.object(video, "HEYGEN_API_KEY", "test-key"), \
                 mock.patch.object(video, "_heygen_upload_asset", side_effect=["video_asset", "audio_asset"]), \
                 mock.patch.object(video, "_heygen_retry_net", side_effect=lambda fn, _label: fn()), \
                 mock.patch.object(video, "heygen_slot", side_effect=lambda _label: nullcontext()), \
                 mock.patch.object(video, "_heygen_retry_429", side_effect=lambda fn, _label: fn()), \
                 mock.patch.object(video, "_heygen_create_lipsync", create), \
                 mock.patch.object(video, "_heygen_poll_lipsync", side_effect=TimeoutError("poll timeout")):
                with self.assertRaisesRegex(video.HeyGenBilledError, "lip_paid"):
                    video.generate_heygen_precision_lipsync(
                        "video/source.mp4", "audio/voice.mp3")
            create.assert_called_once_with(
                "video_asset", "audio_asset", direct=True, route="api_wallet",
                dynamic_duration=True,
            )

    def test_upload_failure_stops_before_the_paid_create(self):
        with tempfile.TemporaryDirectory() as directory:
            source = pathlib.Path(directory) / "source.mp4"
            audio = pathlib.Path(directory) / "audio.mp3"
            source.write_bytes(b"source")
            audio.write_bytes(b"audio")
            create = mock.Mock()
            with mock.patch.object(video, "_resolve_out_file", side_effect=lambda rel: {
                    "video/source.mp4": source, "audio/voice.mp3": audio,
                    }.get(str(rel))), \
                 mock.patch.object(video, "_ensure_heygen_audio_mp3", return_value=audio), \
                 mock.patch.object(video, "_heygen_require_paid_route", return_value="api_wallet"), \
                 mock.patch.object(video, "_HEYGEN_DIRECT", True), \
                 mock.patch.object(video, "HEYGEN_API_KEY", "test-key"), \
                 mock.patch.object(video, "_heygen_upload_asset", side_effect=video.HeyGenNetworkError("upload failed")), \
                 mock.patch.object(video, "_heygen_retry_net", side_effect=lambda fn, _label: fn()), \
                 mock.patch.object(video, "_heygen_create_lipsync", create):
                with self.assertRaises(video.HeyGenNetworkError):
                    video.generate_heygen_precision_lipsync(
                        "video/source.mp4", "audio/voice.mp3")
            create.assert_not_called()

    def test_precision_cost_is_per_complete_audio_second(self):
        body = {"mode": "lipsync", "audio_file": "audio/owned.mp3"}
        with mock.patch.object(video, "_talking_estimate_seconds", return_value=12.2), \
             mock.patch.object(video.pricing, "get_price", return_value=6):
            self.assertEqual(78, video.video_cost(body))
            self.assertEqual(78, video.talking_actual_cost({
                "mode": "lipsync", "duration": 12.2,
            }))
        self.assertEqual(6, body["_lipsync_second_points"])


if __name__ == "__main__":
    unittest.main()
