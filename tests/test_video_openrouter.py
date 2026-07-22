import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from content_domains import video_openrouter


class _Response:
    def __init__(self, body):
        self.body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.body


class OpenRouterVideoTests(unittest.TestCase):
    def test_download_headers_use_server_side_key(self):
        with patch.object(video_openrouter, "OPENROUTER_API_KEY", "test-key"):
            self.assertEqual(
                video_openrouter.download_headers(
                    "https://openrouter.ai/api/v1/videos/job/content"
                ),
                {"Authorization": "Bearer test-key"},
            )

    def test_download_headers_reject_third_party_and_plain_http_urls(self):
        with patch.object(video_openrouter, "OPENROUTER_API_KEY", "test-key"):
            self.assertEqual(
                video_openrouter.download_headers("https://cdn.example/video.mp4"), {}
            )
            self.assertEqual(
                video_openrouter.download_headers(
                    "http://openrouter.ai/api/v1/videos/job/content"
                ),
                {},
            )
            self.assertEqual(
                video_openrouter.download_headers(
                    "https://openrouter.ai/api/v1/chat/completions"
                ),
                {},
            )

    def test_generate_maps_model_references_and_result(self):
        responses = [
            _Response({"id": "or-job-1", "status": "pending"}),
            _Response({"id": "or-job-1", "status": "completed",
                       "unsigned_urls": ["https://cdn.openrouter.ai/video.mp4"],
                       "usage": {"cost": 0.35}}),
        ]
        heartbeat = Mock()
        clock = iter([0, 0, 1])
        opener = Mock()
        opener.open.side_effect = responses
        with patch.object(video_openrouter, "OPENROUTER_API_KEY", "test-key"), \
             patch.object(video_openrouter, "_opener", return_value=opener):
            result = video_openrouter.generate(
                "grok-imagine-video", "demo", 5, "9:16", "720p",
                image_urls=["https://cos.example/one.jpg", "https://cos.example/two.jpg"],
                job_id=7, heartbeat=heartbeat,
                now=lambda: next(clock), sleep=lambda _: None,
            )
        request = opener.open.call_args_list[0].args[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, "https://openrouter.ai/api/v1/videos")
        self.assertEqual(payload["model"], "x-ai/grok-imagine-video")
        self.assertEqual(len(payload["input_references"]), 2)
        self.assertEqual(result["provider"], "openrouter")
        self.assertEqual(result["source_video_url"], "https://cdn.openrouter.ai/video.mp4")
        self.assertEqual(heartbeat.call_args_list[0].args[:2], (7, "openrouter_pending"))

    def test_version_15_uses_first_frame(self):
        responses = [
            _Response({"id": "or-job-15"}),
            _Response({"status": "completed", "unsigned_urls": ["https://cdn.example/v.mp4"]}),
        ]
        clock = iter([0, 0, 1])
        opener = Mock()
        opener.open.side_effect = responses
        with patch.object(video_openrouter, "OPENROUTER_API_KEY", "test-key"), \
             patch.object(video_openrouter, "_opener", return_value=opener):
            video_openrouter.generate(
                "grok-imagine-video-1.5", "demo", 5, "9:16", "1080p",
                image_urls=["https://cos.example/first.jpg"],
                now=lambda: next(clock), sleep=lambda _: None,
            )
        payload = json.loads(opener.open.call_args_list[0].args[0].data)
        self.assertEqual(payload["model"], "x-ai/grok-imagine-video-1.5")
        self.assertEqual(payload["frame_images"][0]["frame_type"], "first_frame")
        self.assertNotIn("aspect_ratio", payload)

    def test_resume_only_polls_existing_job(self):
        clock = iter([0, 0, 1])
        opener = Mock()
        opener.open.return_value = _Response({
            "status": "completed", "unsigned_urls": ["https://cdn.example/resumed.mp4"]
        })
        with patch.object(video_openrouter, "OPENROUTER_API_KEY", "test-key"), \
             patch.object(video_openrouter, "_opener", return_value=opener):
            result = video_openrouter.resume(
                "or-existing", "grok-imagine-video", 10,
                now=lambda: next(clock), sleep=lambda _: None,
            )
        self.assertEqual(result["request_id"], "or-existing")
        self.assertEqual(opener.open.call_count, 1)
        self.assertEqual(opener.open.call_args.args[0].get_method(), "GET")

    def test_rejects_cross_host_poll_or_request_url(self):
        with self.assertRaisesRegex(RuntimeError, "不可信"):
            video_openrouter._safe_url("https://attacker.example/videos/job")

    def test_balance_error_is_actionable(self):
        error = urllib.error.HTTPError(
            "https://openrouter.ai/api/v1/videos", 402, "payment", {},
            io.BytesIO(b'{"error":{"message":"insufficient credits"}}'),
        )
        opener = Mock()
        opener.open.side_effect = error
        with patch.object(video_openrouter, "OPENROUTER_API_KEY", "test-key"), \
             patch.object(video_openrouter, "_opener", return_value=opener):
            with self.assertRaisesRegex(RuntimeError, "余额不足"):
                video_openrouter.generate("grok-imagine-video", "demo", 5, "16:9", "480p")

    def test_create_network_failure_is_not_retried(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.URLError("connection reset")
        with patch.object(video_openrouter, "OPENROUTER_API_KEY", "test-key"), \
             patch.object(video_openrouter, "_opener", return_value=opener):
            with self.assertRaises(video_openrouter.TransientOpenRouterError):
                video_openrouter.generate("grok-imagine-video", "demo", 5, "16:9", "480p")
        self.assertEqual(opener.open.call_count, 1)
        self.assertEqual(opener.open.call_args.args[0].get_method(), "POST")

    def test_poll_network_failure_retries_get_only(self):
        opener = Mock()
        opener.open.side_effect = [
            _Response({"id": "or-job-1"}),
            urllib.error.URLError("temporary reset"),
            _Response({"status": "completed", "unsigned_urls": ["https://cdn.example/v.mp4"]}),
        ]
        clock = iter([0, 0, 1, 2, 3])
        with patch.object(video_openrouter, "OPENROUTER_API_KEY", "test-key"), \
             patch.object(video_openrouter, "_opener", return_value=opener):
            video_openrouter.generate(
                "grok-imagine-video", "demo", 5, "16:9", "480p",
                now=lambda: next(clock), sleep=lambda _: None,
            )
        methods = [call.args[0].get_method() for call in opener.open.call_args_list]
        self.assertEqual(methods, ["POST", "GET", "GET"])

    def test_poll_transient_http_failures_retry_get_only(self):
        for status in (429, 503):
            with self.subTest(status=status):
                opener = Mock()
                opener.open.side_effect = [
                    _Response({"id": "or-job-1"}),
                    urllib.error.HTTPError(
                        "https://openrouter.ai/api/v1/videos/or-job-1",
                        status, "temporary", {}, io.BytesIO(b'{"error":"temporary"}'),
                    ),
                    _Response({
                        "status": "completed",
                        "unsigned_urls": ["https://cdn.example/v.mp4"],
                    }),
                ]
                clock = iter([0, 0, 1, 2, 3])
                with patch.object(video_openrouter, "OPENROUTER_API_KEY", "test-key"), \
                     patch.object(video_openrouter, "_opener", return_value=opener):
                    video_openrouter.generate(
                        "grok-imagine-video", "demo", 5, "16:9", "480p",
                        now=lambda: next(clock), sleep=lambda _: None,
                    )
                methods = [call.args[0].get_method() for call in opener.open.call_args_list]
                self.assertEqual(methods, ["POST", "GET", "GET"])

    def test_completed_without_unsigned_url_fails(self):
        opener = Mock()
        opener.open.side_effect = [_Response({"id": "or-job-1"}), _Response({"status": "completed"})]
        clock = iter([0, 0, 1])
        with patch.object(video_openrouter, "OPENROUTER_API_KEY", "test-key"), \
             patch.object(video_openrouter, "_opener", return_value=opener):
            with self.assertRaisesRegex(RuntimeError, "未返回"):
                video_openrouter.generate(
                    "grok-imagine-video", "demo", 5, "16:9", "480p",
                    now=lambda: next(clock), sleep=lambda _: None,
                )

    def test_rejects_plain_http_api_url(self):
        with self.assertRaisesRegex(RuntimeError, "不可信"):
            video_openrouter._safe_url("http://openrouter.ai/api/v1/videos/job")


if __name__ == "__main__":
    unittest.main()
