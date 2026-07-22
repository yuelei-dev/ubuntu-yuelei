import base64
import io
import json
import pathlib
import sys
import unittest
import urllib.error
from contextlib import redirect_stdout
from unittest.mock import patch


ROOT = pathlib.Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from content_domains import video


def _data_url(seed="avatar"):
    raw = b"\x89PNG\r\n\x1a\n" + seed.encode("ascii")
    return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")


class TalkingMotionPromptValidationTests(unittest.TestCase):
    def _payload(self, **updates):
        payload = {
            "mode": "text",
            "image_data": _data_url(),
            "text": "口播文案",
            "voice": "voice-demo",
        }
        payload.update(updates)
        return payload

    def test_prompts_default_blank_and_are_trimmed(self):
        absent = video.validate_video_payload(self._payload())
        blank = video.validate_video_payload(self._payload(
            motion_prompt_original="  \n ", motion_prompt=" \t "))
        trimmed = video.validate_video_payload(self._payload(
            motion_prompt_original="  先微笑，再点头  ", motion_prompt="  Smile, then nod.  "))

        self.assertEqual("", absent["motion_prompt_original"])
        self.assertEqual("", absent["motion_prompt"])
        self.assertEqual("", blank["motion_prompt_original"])
        self.assertEqual("", blank["motion_prompt"])
        self.assertEqual("先微笑，再点头", trimmed["motion_prompt_original"])
        self.assertEqual("Smile, then nod.", trimmed["motion_prompt"])

    def test_prompts_require_strict_strings(self):
        for field in ("motion_prompt_original", "motion_prompt"):
            for bad in (None, 12, True, ["点头"], {"action": "点头"}):
                with self.subTest(field=field, value=bad):
                    with self.assertRaisesRegex(ValueError, field):
                        video.validate_video_payload(self._payload(**{field: bad}))

    def test_prompt_accepts_exactly_500_characters_and_rejects_501(self):
        for field in ("motion_prompt_original", "motion_prompt"):
            with self.subTest(field=field, length=500):
                cleaned = video.validate_video_payload(self._payload(**{field: "动" * 500}))
                self.assertEqual(500, len(cleaned[field]))
            with self.subTest(field=field, length=501):
                with self.assertRaisesRegex(ValueError, "500"):
                    video.validate_video_payload(self._payload(**{field: "动" * 501}))

    def test_confirmed_prompt_forces_avatar_iv_resolution_and_ratio_during_validation(self):
        cleaned = video.validate_video_payload(self._payload(
            motion_prompt="  Smile and nod.  ", resolution="720p", ratio="16:9"
        ))
        self.assertEqual("1080p", cleaned["resolution"])
        self.assertEqual("9:16", cleaned["ratio"])

        fast = video.validate_video_payload(self._payload(
            motion_prompt="   ", resolution="720p", ratio="16:9"
        ))
        self.assertEqual("720p", fast["resolution"])
        self.assertEqual("16:9", fast["ratio"])

    def test_batch_propagates_both_common_prompt_fields_without_changing_count(self):
        payload = self._payload(
            motion_prompt_original="  自然微笑并轻轻点头  ",
            motion_prompt="  Smile naturally and nod gently.  ",
            resolution="720p",
            ratio="16:9",
            avatars=[
                {"image_data": _data_url("one"), "label": "形象一"},
                {"image_data": _data_url("two"), "label": "形象二"},
            ],
        )
        items = video.validate_video_batch_payload(payload)

        self.assertEqual(2, len(items))
        self.assertEqual(["自然微笑并轻轻点头"] * 2,
                         [item["motion_prompt_original"] for item in items])
        self.assertEqual(["Smile naturally and nod gently."] * 2,
                         [item["motion_prompt"] for item in items])
        self.assertEqual(["1080p"] * 2, [item["resolution"] for item in items])
        self.assertEqual(["9:16"] * 2, [item["ratio"] for item in items])


class TalkingMotionPromptProviderTests(unittest.TestCase):
    def _gen_payload(self, **updates):
        payload = {
            "_username": "fang",
            "_job_id": 8,
            "mode": "text",
            "image_data": _data_url(),
            "text": "hello",
            "voice": "voice-demo",
            "resolution": "1080p",
            "ratio": "9:16",
            "motion": "medium",
            "motion_prompt_original": "",
            "motion_prompt": "",
        }
        payload.update(updates)
        return payload

    def _gen_common_patches(self):
        return (
            patch.object(video, "HEYGEN_API_KEY", "configured"),
            patch.object(video, "_save_data_file", return_value="image/avatar.jpg"),
            patch.object(video, "gen_audio", return_value={"file": "audio/voice.mp3", "url": "/audio.mp3"}),
            patch.object(video, "public_url", return_value="https://cdn.example/out.mp4"),
            patch.object(video, "_file_url", side_effect=lambda value: "/api/gen/file/" + str(value or "")),
        )

    def test_empty_prompt_uses_only_existing_fast_image_path(self):
        p1, p2, p3, p4, p5 = self._gen_common_patches()
        with p1, p2, p3, p4, p5, \
                patch.object(video, "generate_heygen_video",
                             return_value={"video_file": "video/out.mp4", "duration": 12}) as fast, \
                patch.object(video, "generate_heygen_avatar_iv_video") as avatar_iv, \
                patch.object(video, "_heygen_create_photo_avatar") as create_avatar:
            result = video.gen_video(self._gen_payload(motion_prompt="  "))

        fast.assert_called_once_with("image/avatar.jpg", "audio/voice.mp3", "1080p", "9:16", "medium")
        avatar_iv.assert_not_called()
        create_avatar.assert_not_called()
        self.assertNotIn("motion_prompt_enabled", result)
        self.assertNotIn("provider_path", result)

    def test_fast_generator_submits_only_image_video_shape(self):
        with patch.object(video, "_HEYGEN_DIRECT", False), \
                patch.object(video, "_resolve_out_file", side_effect=lambda value: pathlib.Path(value)), \
                patch.object(video, "_ensure_heygen_image_jpg", side_effect=lambda fp: fp), \
                patch.object(video, "_ensure_heygen_audio_mp3", side_effect=lambda fp: fp), \
                patch.object(video, "_heygen_upload_asset", side_effect=["image-asset", "audio-asset"]), \
                patch.object(video, "_heygen_create_video", return_value="video-fast") as fast_submit, \
                patch.object(video, "_heygen_create_photo_avatar") as create_avatar, \
                patch.object(video, "_heygen_create_avatar_iv_video") as avatar_iv_submit, \
                patch.object(video, "_heygen_poll_video", return_value={"video_url": "https://cdn.example/out.mp4"}), \
                patch.object(video, "_download_video_file", return_value="video/out.mp4"), \
                patch.object(video, "_extract_first_frame_cover", return_value=None):
            video.generate_heygen_video(
                "image/avatar.jpg", "audio/voice.mp3", "1080p", "9:16", "medium"
            )

        fast_submit.assert_called_once_with(
            "image-asset", "audio-asset", "1080p", "9:16", "medium")
        create_avatar.assert_not_called()
        avatar_iv_submit.assert_not_called()

    def test_saved_ready_avatar_reuses_provider_look_and_calls_only_avatar_iv_path(self):
        payload = self._gen_payload(
            image_data="", avatar_id="9", motion_prompt_original="点头", motion_prompt="Nod gently.",
            resolution="720p", ratio="16:9",
        )
        p1, _, p3, p4, p5 = self._gen_common_patches()
        with p1, p3, p4, p5, \
                patch.object(video, "get_video_avatar", return_value={
                    "id": 9, "image_file": "image/saved.jpg", "status": "ready",
                    "provider_avatar_id": "look-ready", "provider_avatar_group_id": "group-ready",
                }), \
                patch.object(video, "generate_heygen_video") as fast, \
                patch.object(video, "generate_heygen_avatar_iv_video",
                             return_value={"video_file": "video/out.mp4", "duration": 12,
                                           "provider_path": "talking_avatar_iv",
                                           "motion_prompt_enabled": True}) as avatar_iv:
            result = video.gen_video(payload)

        fast.assert_not_called()
        avatar_iv.assert_called_once_with(
            "image/saved.jpg", "audio/voice.mp3", "Nod gently.", "medium",
            avatar_look_id="look-ready",
        )
        self.assertEqual("talking_avatar_iv", result["provider_path"])
        self.assertTrue(result["motion_prompt_enabled"])
        self.assertNotIn("motion_prompt", result)
        self.assertEqual("1080p", result["resolution"])
        self.assertEqual("9:16", result["ratio"])

    def test_avatar_iv_submitter_uses_exact_flat_heygen_body(self):
        captured = {}

        def fake_request(method, path, body, headers, timeout, direct, redact_values=None):
            captured.update({
                "method": method, "path": path, "body": json.loads(body),
                "headers": headers, "timeout": timeout, "direct": direct,
                "redact_values": redact_values,
            })
            return {"data": {"video_id": "video-1"}}

        with patch.object(video, "_heygen_request_json", side_effect=fake_request):
            video_id = video._heygen_create_avatar_iv_video(
                "look-1", "audio-1", "Smile and nod.", "high", direct=True
            )

        self.assertEqual("video-1", video_id)
        self.assertEqual("POST", captured["method"])
        self.assertEqual("/videos", captured["path"])
        self.assertTrue(captured["direct"])
        self.assertEqual(("Smile and nod.",), captured["redact_values"])
        self.assertEqual({
            "type": "avatar",
            "avatar_id": "look-1",
            "audio_asset_id": "audio-1",
            "resolution": "1080p",
            "aspect_ratio": "9:16",
            "fit": "cover",
            "motion_prompt": "Smile and nod.",
            "expressiveness": "high",
            "engine": {"type": "avatar_iv"},
            "output_format": "mp4",
        }, captured["body"])

    def test_avatar_iv_http_error_redacts_prompt_from_stdout_and_exception(self):
        prompt = "先自然挥手，然后轻轻点头并微笑"
        echoed = json.dumps({
            "error": "invalid motion_prompt",
            "motion_prompt": prompt,
            "detail": "轻轻点头并微笑",
        }, ensure_ascii=False).encode("utf-8")

        class ErrorOpener:
            def open(self, request, timeout=None):
                raise urllib.error.HTTPError(
                    request.full_url, 400, "Bad Request", {}, io.BytesIO(echoed)
                )

        output = io.StringIO()
        with patch.object(video, "HEYGEN_API_KEY", "configured"), \
                patch.object(video, "_heygen_direct_opener", return_value=ErrorOpener()), \
                redirect_stdout(output):
            with self.assertRaises(RuntimeError) as raised:
                video._heygen_create_avatar_iv_video(
                    "look-1", "audio-1", prompt, "medium", direct=True
                )

        exposed = output.getvalue() + "\n" + str(raised.exception)
        for sensitive in (prompt, "自然挥手", "轻轻点头", "点头并微笑"):
            self.assertNotIn(sensitive, exposed)
        self.assertIn("响应已脱敏", exposed)

    def test_avatar_iv_missing_video_id_never_dumps_provider_response(self):
        prompt = "先自然挥手，然后轻轻点头并微笑"
        provider_response = {
            "data": {"motion_prompt": prompt},
            "error": "动作片段：轻轻点头并微笑",
        }
        with patch.object(video, "_heygen_request_json", return_value=provider_response):
            with self.assertRaises(RuntimeError) as raised:
                video._heygen_create_avatar_iv_video(
                    "look-1", "audio-1", prompt, "medium", direct=False
                )

        exposed = str(raised.exception)
        for sensitive in (prompt, "自然挥手", "轻轻点头", "点头并微笑"):
            self.assertNotIn(sensitive, exposed)
        self.assertIn("响应已脱敏", exposed)

    def test_uploaded_image_creates_and_waits_for_photo_avatar_before_paid_submit(self):
        events = []

        def upload(fp, direct=False):
            events.append(("upload", fp.name, direct))
            return "image-asset" if fp.name == "avatar.jpg" else "audio-asset"

        with patch.object(video, "_HEYGEN_DIRECT", False), \
                patch.object(video, "_resolve_out_file", side_effect=lambda value: pathlib.Path(value)), \
                patch.object(video, "_ensure_heygen_image_jpg", side_effect=lambda fp: fp), \
                patch.object(video, "_ensure_heygen_audio_mp3", side_effect=lambda fp: fp), \
                patch.object(video, "_heygen_upload_asset", side_effect=upload), \
                patch.object(video, "_heygen_create_photo_avatar",
                             side_effect=lambda asset, direct=False: events.append(("create", asset, direct)) or ("look-1", "group-1")), \
                patch.object(video, "_heygen_wait_photo_avatar",
                             side_effect=lambda look, group, direct=False: events.append(("wait", look, group, direct)) or True), \
                patch.object(video, "_heygen_create_avatar_iv_video",
                             side_effect=lambda look, audio, prompt, motion, direct=False: events.append(("submit", look, audio, prompt, motion, direct)) or "video-1"), \
                patch.object(video, "_heygen_poll_video", return_value={"video_url": "https://cdn.example/out.mp4", "duration": 8}), \
                patch.object(video, "_download_video_file", return_value="video/out.mp4"), \
                patch.object(video, "_extract_first_frame_cover", return_value=None):
            result = video.generate_heygen_avatar_iv_video(
                "image/avatar.jpg", "audio/voice.mp3", "Smile and nod.", "medium"
            )

        self.assertLess([e[0] for e in events].index("create"), [e[0] for e in events].index("wait"))
        self.assertLess([e[0] for e in events].index("wait"), [e[0] for e in events].index("submit"))
        self.assertEqual("talking_avatar_iv", result["provider_path"])
        self.assertTrue(result["motion_prompt_enabled"])

    def test_ready_saved_look_skips_image_upload_avatar_creation_and_wait(self):
        def resolve(value):
            if value == "image/local-preview-missing.jpg":
                raise AssertionError("复用 ready provider look 时不应读取本地图片")
            return pathlib.Path(value)

        with patch.object(video, "_HEYGEN_DIRECT", False), \
                patch.object(video, "_resolve_out_file", side_effect=resolve), \
                patch.object(video, "_ensure_heygen_audio_mp3", side_effect=lambda fp: fp), \
                patch.object(video, "_heygen_upload_asset", return_value="audio-asset") as upload, \
                patch.object(video, "_heygen_create_photo_avatar") as create_avatar, \
                patch.object(video, "_heygen_wait_photo_avatar") as wait_avatar, \
                patch.object(video, "_heygen_create_avatar_iv_video", return_value="video-1") as submit, \
                patch.object(video, "_heygen_poll_video", return_value={"video_url": "https://cdn.example/out.mp4"}), \
                patch.object(video, "_download_video_file", return_value="video/out.mp4"), \
                patch.object(video, "_extract_first_frame_cover", return_value=None):
            video.generate_heygen_avatar_iv_video(
                "image/local-preview-missing.jpg", "audio/voice.mp3", "Smile.", "low",
                avatar_look_id="look-ready",
            )

        create_avatar.assert_not_called()
        wait_avatar.assert_not_called()
        upload.assert_called_once()
        submit.assert_called_once_with("look-ready", "audio-asset", "Smile.", "low", direct=False)

    def test_avatar_readiness_failure_never_submits_or_falls_back_to_fast_path(self):
        with patch.object(video, "_HEYGEN_DIRECT", False), \
                patch.object(video, "_resolve_out_file", side_effect=lambda value: pathlib.Path(value)), \
                patch.object(video, "_ensure_heygen_image_jpg", side_effect=lambda fp: fp), \
                patch.object(video, "_ensure_heygen_audio_mp3", side_effect=lambda fp: fp), \
                patch.object(video, "_heygen_upload_asset", side_effect=["image-asset", "audio-asset"]), \
                patch.object(video, "_heygen_create_photo_avatar", return_value=("look-1", "group-1")), \
                patch.object(video, "_heygen_wait_photo_avatar", side_effect=RuntimeError("not ready")), \
                patch.object(video, "_heygen_create_avatar_iv_video") as submit, \
                patch.object(video, "_heygen_create_video") as fast_submit:
            with self.assertRaisesRegex(RuntimeError, "not ready"):
                video.generate_heygen_avatar_iv_video(
                    "image/avatar.jpg", "audio/voice.mp3", "Smile.", "medium"
                )

        submit.assert_not_called()
        fast_submit.assert_not_called()

    def test_avatar_creation_failure_never_invokes_paid_submission(self):
        with patch.object(video, "_HEYGEN_DIRECT", False), \
                patch.object(video, "_resolve_out_file", side_effect=lambda value: pathlib.Path(value)), \
                patch.object(video, "_ensure_heygen_image_jpg", side_effect=lambda fp: fp), \
                patch.object(video, "_ensure_heygen_audio_mp3", side_effect=lambda fp: fp), \
                patch.object(video, "_heygen_upload_asset", return_value="image-asset"), \
                patch.object(video, "_heygen_create_photo_avatar", side_effect=RuntimeError("create failed")), \
                patch.object(video, "_heygen_wait_photo_avatar") as wait_avatar, \
                patch.object(video, "_heygen_create_avatar_iv_video") as submit, \
                patch.object(video, "_heygen_create_video") as fast_submit:
            with self.assertRaisesRegex(RuntimeError, "create failed"):
                video.generate_heygen_avatar_iv_video(
                    "image/avatar.jpg", "audio/voice.mp3", "Smile.", "medium"
                )

        wait_avatar.assert_not_called()
        submit.assert_not_called()
        fast_submit.assert_not_called()

    def test_billed_failure_propagates_without_second_attempt(self):
        with patch.object(video, "_HEYGEN_DIRECT", True), \
                patch.object(video, "HEYGEN_API_KEY", "configured"), \
                patch.object(video, "_resolve_out_file", return_value=pathlib.Path("audio/voice.mp3")), \
                patch.object(video, "_ensure_heygen_audio_mp3", side_effect=lambda fp: fp), \
                patch.object(video, "_heygen_upload_asset", return_value="audio-asset"), \
                patch.object(video, "_heygen_create_avatar_iv_video", return_value="video-paid") as submit, \
                patch.object(video, "_heygen_poll_video", side_effect=RuntimeError("poll failed")):
            with self.assertRaises(video.HeyGenBilledError):
                video.generate_heygen_avatar_iv_video(
                    "image/avatar.jpg", "audio/voice.mp3", "Smile.", "medium",
                    avatar_look_id="look-ready",
                )

        submit.assert_called_once_with(
            "look-ready", "audio-asset", "Smile.", "medium", direct=True)

    def test_ambiguous_submit_network_failure_never_falls_back_or_submits_twice(self):
        def uncertain_submit(*args, **kwargs):
            try:
                raise TimeoutError("read timed out after request delivery")
            except TimeoutError as cause:
                raise video.HeyGenNetworkError("submission response lost") from cause

        with patch.object(video, "_HEYGEN_DIRECT", True), \
                patch.object(video, "HEYGEN_API_KEY", "configured"), \
                patch.object(video, "_resolve_out_file", return_value=pathlib.Path("audio/voice.mp3")), \
                patch.object(video, "_ensure_heygen_audio_mp3", side_effect=lambda fp: fp), \
                patch.object(video, "_heygen_upload_asset", return_value="audio-asset"), \
                patch.object(video, "_heygen_create_avatar_iv_video", side_effect=uncertain_submit) as submit:
            with self.assertRaises(video.HeyGenBilledError):
                video.generate_heygen_avatar_iv_video(
                    "image/avatar.jpg", "audio/voice.mp3", "Smile.", "medium",
                    avatar_look_id="look-ready",
                )

        self.assertEqual(1, submit.call_count)

    def test_avatar_iv_poll_failure_redacts_provider_echo_and_never_resubmits(self):
        prompt = "先自然挥手，然后轻轻点头并微笑"
        provider_failed = {
            "data": {
                "status": "failed",
                "error": "provider echoed motion_prompt: " + prompt,
                "detail": "动作片段：轻轻点头并微笑",
            }
        }
        output = io.StringIO()
        with patch.object(video, "_HEYGEN_DIRECT", True), \
                patch.object(video, "HEYGEN_API_KEY", "configured"), \
                patch.object(video, "_resolve_out_file", return_value=pathlib.Path("audio/voice.mp3")), \
                patch.object(video, "_ensure_heygen_audio_mp3", side_effect=lambda fp: fp), \
                patch.object(video, "_heygen_upload_asset", return_value="audio-asset"), \
                patch.object(video, "_heygen_create_avatar_iv_video", return_value="video-paid") as submit, \
                patch.object(video, "_heygen_request_json", return_value=provider_failed) as request_json, \
                redirect_stdout(output):
            with self.assertRaises(video.HeyGenBilledError) as raised:
                video.generate_heygen_avatar_iv_video(
                    "image/avatar.jpg", "audio/voice.mp3", prompt, "medium",
                    avatar_look_id="look-ready",
                )

        self.assertEqual(1, submit.call_count)
        self.assertEqual((prompt,), request_json.call_args.kwargs["redact_values"])
        exposed = output.getvalue() + "\n" + str(raised.exception)
        for sensitive in (prompt, "自然挥手", "轻轻点头", "点头并微笑"):
            self.assertNotIn(sensitive, exposed)
        self.assertIn("响应已脱敏", output.getvalue())
        self.assertIn("轮询或成片处理失败", str(raised.exception))

    def test_direct_pre_submit_failure_can_fall_back_to_relay_once(self):
        expected = {"video_file": "video/out.mp4", "provider_path": "talking_avatar_iv",
                    "motion_prompt_enabled": True}
        with patch.object(video, "_HEYGEN_DIRECT", True), \
                patch.object(video, "HEYGEN_API_KEY", "configured"), \
                patch.object(video, "_generate_heygen_avatar_iv_attempt",
                             side_effect=[RuntimeError("direct unavailable"), expected]) as attempt:
            result = video.generate_heygen_avatar_iv_video(
                "image/avatar.jpg", "audio/voice.mp3", "Smile.", "medium"
            )

        self.assertIs(expected, result)
        self.assertEqual([True, False], [call.kwargs["direct"] for call in attempt.call_args_list])


if __name__ == "__main__":
    unittest.main()
