import importlib
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


class _Response:
    def __init__(self, body=b"{}", headers=None):
        self.body = body
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body


class GeminiReverseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server_dir = str(Path(__file__).resolve().parents[1] / "server")
        if server_dir not in sys.path:
            sys.path.insert(0, server_dir)
        cls.breakdown = importlib.import_module("content_domains.breakdown")

    def _shot(self, start, end, cut=False):
        labels = ("bridge-river", "studio-circle", "forest-bicycle", "city-train")
        label = labels[int(start) % len(labels)]
        facts = {}
        evidence = {}
        for key in self.breakdown._GEMINI_FACT_FIELDS:
            if key in self.breakdown._GEMINI_OPTIONAL_FACT_FIELDS:
                facts[key] = "not_applicable"
                evidence[key] = []
            else:
                facts[key] = ((label + " ") * 5) + "observed attribute %d" % len(key)
                evidence[key] = [round(start + 0.1, 1)]
        evidence["action_end"] = [round(end - 0.1, 1)]
        return {
            "start_seconds": start,
            "end_seconds": end,
            "cut_from_previous": cut,
            "facts": facts,
            "evidence_seconds": evidence,
            "generation_advice": {
                "aspect_ratio": "16:9",
                "fps": "24",
                "camera_control": "preserve observed camera motion",
                "negative_prompt": "no extra subjects",
            },
        }

    def _provider_response(self, count=1):
        shots = [self._shot(float(i), float(i + 1), i > 0) for i in range(count)]
        text = json.dumps({"shots": shots})
        return {"candidates": [{"content": {"parts": [{"text": text}]}}]}

    def test_model_endpoint_video_mime_structured_schema_and_no_fallback(self):
        captured = {}

        def fake_request(url, body, api_key, **_kwargs):
            captured.update(url=url, body=body, api_key=api_key)
            return self._provider_response()

        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=repo_root) as temp_dir, \
                mock.patch.dict(os.environ, {"GEMINI_API_KEY": "mock-key"}), \
                mock.patch.object(self.breakdown, "_gemini_json_request", side_effect=fake_request), \
                mock.patch.object(self.breakdown, "_chat_multimodal", side_effect=AssertionError("GLM forbidden")):
            media_path = Path(temp_dir) / "sample.mp4"
            media_path.write_bytes(b"not-a-real-video")
            result = self.breakdown._gemini_reverse_prompt_from_media(
                str(media_path), "video/mp4", "sample", 1.0, "local", ""
            )

        self.assertEqual(result["model"], "gemini-3.1-pro-preview")
        self.assertEqual(result["provider"], "google")
        self.assertIn("/v1beta/models/gemini-3.1-pro-preview:generateContent", captured["url"])
        self.assertEqual(captured["api_key"], "mock-key")
        part = captured["body"]["contents"][0]["parts"][0]
        self.assertEqual(part["inline_data"]["mime_type"], "video/mp4")
        config = captured["body"]["generationConfig"]
        self.assertEqual(config["responseMimeType"], "application/json")
        self.assertEqual(config["responseJsonSchema"], self.breakdown._gemini_reverse_schema())

    def test_one_to_four_shots_are_gap_free_and_directly_assembled(self):
        for count in range(1, 5):
            raw = json.dumps({"shots": [
                self._shot(float(i), float(i + 1), i > 0) for i in range(count)
            ]})
            result = self.breakdown._parse_gemini_reverse_result(raw, float(count))
            prompt = self.breakdown._assemble_reverse_prompt(result["entries"], result["windows"])
            self.assertEqual(len(result["entries"]), count)
            self.assertIn("subject:", prompt)
            self.assertIn("action:", prompt)
            self.assertIn("generation advice:", prompt)
            self.assertEqual(result["entries"][0]["readiness"]["ready"], 17)
            quality = self.breakdown._gemini_quality_dimensions(result)
            self.assertGreaterEqual(quality["generation_readiness"]["percent"], 90)
            self.assertFalse(quality["end_to_end_similarity_claimed"])

    def test_truncated_or_non_schema_json_is_rejected_without_guessing(self):
        with self.assertRaisesRegex(ValueError, "complete JSON"):
            self.breakdown._parse_gemini_reverse_result('{"shots":[', 1.0)
        with self.assertRaisesRegex(ValueError, "root does not match schema"):
            self.breakdown._parse_gemini_reverse_result('{"shots":[],"draft":"x"}', 1.0)

    def test_action_requires_evidence_at_both_shot_endpoints(self):
        shot = self._shot(0.0, 1.0)
        shot["evidence_seconds"]["action_end"] = [0.2]
        with self.assertRaisesRegex(ValueError, "both shot endpoints"):
            self.breakdown._parse_gemini_reverse_result(
                json.dumps({"shots": [shot]}), 1.0
            )

    def test_generation_advice_is_strictly_typed_formatted_and_bounded(self):
        invalid_values = (
            ("aspect_ratio", 169),
            ("aspect_ratio", "wide"),
            ("fps", None),
            ("fps", "29.97"),
            ("camera_control", ""),
            ("camera_control", "x" * 161),
            ("negative_prompt", ""),
            ("negative_prompt", "x" * 241),
        )
        for key, value in invalid_values:
            with self.subTest(key=key, value_type=type(value).__name__):
                shot = self._shot(0.0, 1.0)
                shot["generation_advice"][key] = value
                with self.assertRaisesRegex(ValueError, "generation advice"):
                    self.breakdown._parse_gemini_reverse_result(json.dumps({"shots": [shot]}), 1.0)

    def test_visible_subtitles_do_not_masquerade_as_asr_sound(self):
        shot = self._shot(0.0, 1.0)
        shot["facts"]["subtitles"] = "\u753b\u9762\u6587\u5b57\u201c\u65b0\u54c1\u53d1\u5e03\u201d"
        shot["evidence_seconds"]["subtitles"] = [0.4]
        result = self.breakdown._parse_gemini_reverse_result(
            json.dumps({"shots": [shot]}, ensure_ascii=False), 1.0
        )
        self.assertEqual(result["entries"][0]["fields"]["sound"], "")
        self.assertIn("visible subtitles/text", result["entries"][0]["text"])

    def test_unrelated_sound_is_rejected_against_current_shot_asr(self):
        shot = self._shot(0.0, 1.0)
        shot["facts"]["sound"] = "\u6fc0\u6602\u6447\u6eda\u4e50"
        shot["evidence_seconds"]["sound"] = [0.4]
        result = self.breakdown._parse_gemini_reverse_result(
            json.dumps({"shots": [shot]}, ensure_ascii=False), 1.0
        )
        with self.assertRaises(ValueError):
            self.breakdown._validate_gemini_reverse_entries(
                result, ["frame-%d.jpg" % index for index in range(8)], "\u6b22\u8fce\u5149\u4e34"
            )

    def test_4xx_has_no_retry_and_429_retries_same_provider_once(self):
        request = self.breakdown.urllib.request.Request("https://example.invalid")
        bad_request = urllib.error.HTTPError(request.full_url, 400, "bad", {}, io.BytesIO())
        with mock.patch.object(self.breakdown.urllib.request, "urlopen", side_effect=bad_request) as opened:
            with self.assertRaisesRegex(RuntimeError, "HTTP 400"):
                self.breakdown._gemini_open(request)
        self.assertEqual(opened.call_count, 1)

        limited = urllib.error.HTTPError(request.full_url, 429, "limited", {}, io.BytesIO())
        with mock.patch.object(
            self.breakdown.urllib.request, "urlopen", side_effect=[limited, _Response()]
        ) as opened:
            response = self.breakdown._gemini_open(request)
        self.assertIsInstance(response, _Response)
        self.assertEqual(opened.call_count, 2)

    def test_transient_error_matrix_retries_once_without_provider_fallback(self):
        request = self.breakdown.urllib.request.Request("https://example.invalid")
        transient = (
            urllib.error.HTTPError(request.full_url, 429, "limited", {}, io.BytesIO()),
            urllib.error.HTTPError(request.full_url, 500, "server", {}, io.BytesIO()),
            urllib.error.URLError("network"),
            TimeoutError("timeout"),
        )
        for error in transient:
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(
                    self.breakdown.urllib.request, "urlopen", side_effect=[error, _Response()],
                ) as opened, mock.patch.object(
                    self.breakdown, "_chat_multimodal",
                    side_effect=AssertionError("GLM/OpenAI fallback forbidden"),
                ):
                    self.assertIsInstance(self.breakdown._gemini_open(request), _Response)
                self.assertEqual(opened.call_count, 2)

    def test_non_retryable_4xx_never_calls_other_provider(self):
        request = self.breakdown.urllib.request.Request("https://example.invalid")
        error = urllib.error.HTTPError(request.full_url, 400, "bad", {}, io.BytesIO())
        with mock.patch.object(
            self.breakdown.urllib.request, "urlopen", side_effect=error,
        ) as opened, mock.patch.object(
            self.breakdown, "_chat_multimodal",
            side_effect=AssertionError("GLM/OpenAI fallback forbidden"),
        ):
            with self.assertRaisesRegex(RuntimeError, "HTTP 400"):
                self.breakdown._gemini_open(request)
        self.assertEqual(opened.call_count, 1)

    def test_validation_retry_reuses_original_media_not_rejected_draft(self):
        captured = []
        invalid = {"candidates": [{"content": {"parts": [{"text": "REJECTED-DRAFT"}]}}]}
        responses = iter([invalid, self._provider_response()])

        def fake_request(_url, body, _api_key, **_kwargs):
            captured.append(body)
            return next(responses)

        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=repo_root) as temp_dir, \
                mock.patch.dict(os.environ, {"GEMINI_API_KEY": "mock-key"}), \
                mock.patch.object(self.breakdown, "_gemini_json_request", side_effect=fake_request):
            media_path = Path(temp_dir) / "sample.mp4"
            media_path.write_bytes(b"not-a-real-video")
            result = self.breakdown._gemini_reverse_prompt_from_media(
                str(media_path), "video/mp4", "sample", 1.0, "local", ""
            )
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(captured[0]["contents"][0]["parts"][0], captured[1]["contents"][0]["parts"][0])
        retry_prompt = captured[1]["contents"][0]["parts"][1]["text"]
        self.assertIn("failed validation", retry_prompt)
        self.assertNotIn("REJECTED-DRAFT", retry_prompt)

    def test_media_size_limit_fails_before_upload_or_generation(self):
        with mock.patch.object(self.breakdown.os.path, "getsize", return_value=self.breakdown._GEMINI_MAX_MEDIA_BYTES + 1), \
                mock.patch.object(self.breakdown, "_gemini_upload_file") as upload:
            with self.assertRaisesRegex(ValueError, "size"):
                self.breakdown._gemini_media_part("unused", "video/mp4", 30, "mock-key")
        upload.assert_not_called()

    def test_uploaded_file_is_cleaned_when_generation_fails(self):
        uploaded = {"name": "files/test", "uri": "https://example.invalid/test", "mime_type": "video/mp4"}
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=repo_root) as temp_dir, \
                mock.patch.dict(os.environ, {"GEMINI_API_KEY": "mock-key"}), \
                mock.patch.object(self.breakdown, "_gemini_media_part", return_value=({"file_data": {}}, uploaded)), \
                mock.patch.object(self.breakdown, "_gemini_wait_for_file_active", return_value=uploaded), \
                mock.patch.object(self.breakdown, "_gemini_json_request", side_effect=RuntimeError("provider failed")), \
                mock.patch.object(self.breakdown, "_gemini_delete_file") as cleanup:
            media_path = Path(temp_dir) / "sample.mp4"
            media_path.write_bytes(b"x")
            with self.assertRaisesRegex(RuntimeError, "provider failed"):
                self.breakdown._gemini_reverse_prompt_from_media(
                    str(media_path), "video/mp4", "sample", 20.0, "local", ""
                )
        cleanup.assert_called_once()
        self.assertEqual(cleanup.call_args.args, (uploaded, "mock-key"))
        self.assertIsNone(cleanup.call_args.kwargs["heartbeat"])

    def test_uploaded_file_cleanup_covers_poll_and_deadline_failures(self):
        uploaded = {"name": "files/test", "uri": "https://sensitive.example/full", "mime_type": "video/mp4"}
        failures = (
            RuntimeError("Gemini Files API media processing did not complete"),
            RuntimeError("Gemini Files API could not process the media"),
            TimeoutError("analysis deadline exhausted"),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__ + str(failure)):
                with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "mock-key"}), \
                        mock.patch.object(self.breakdown, "_gemini_media_part", return_value=({"file_data": {}}, uploaded)), \
                        mock.patch.object(self.breakdown, "_gemini_wait_for_file_active", side_effect=failure), \
                        mock.patch.object(self.breakdown, "_gemini_delete_file") as cleanup:
                    with self.assertRaises(type(failure)):
                        self.breakdown._gemini_reverse_prompt_from_media(
                            "unused.mp4", "video/mp4", "sample", 20.0, "local", ""
                        )
                cleanup.assert_called_once()
                self.assertEqual(cleanup.call_args.args, (uploaded, "mock-key"))

    def test_uploaded_file_cleanup_covers_schema_failure_and_success(self):
        uploaded = {"name": "files/test", "uri": "https://sensitive.example/full", "mime_type": "video/mp4"}
        invalid = {"candidates": [{"content": {"parts": [{"text": "not-json"}]}}]}
        cases = (([invalid, invalid], ValueError), ([self._provider_response()], None))
        for responses, expected_error in cases:
            with self.subTest(expected_error=expected_error):
                with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "mock-key"}), \
                        mock.patch.object(self.breakdown, "_gemini_media_part", return_value=({"file_data": {}}, uploaded)), \
                        mock.patch.object(self.breakdown, "_gemini_wait_for_file_active", return_value=uploaded), \
                        mock.patch.object(self.breakdown, "_gemini_json_request", side_effect=responses), \
                        mock.patch.object(self.breakdown, "_gemini_delete_file") as cleanup:
                    if expected_error:
                        with self.assertRaises(expected_error):
                            self.breakdown._gemini_reverse_prompt_from_media(
                                "unused.mp4", "video/mp4", "sample", 1.0, "local", ""
                            )
                    else:
                        result = self.breakdown._gemini_reverse_prompt_from_media(
                            "unused.mp4", "video/mp4", "sample", 1.0, "local", ""
                        )
                        self.assertEqual(result["model"], "gemini-3.1-pro-preview")
                cleanup.assert_called_once()

    def test_upload_failure_has_no_delete(self):
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "mock-key"}), \
                mock.patch.object(self.breakdown, "_gemini_media_part", side_effect=RuntimeError("upload failed")), \
                mock.patch.object(self.breakdown, "_gemini_delete_file") as cleanup:
            with self.assertRaisesRegex(RuntimeError, "upload failed"):
                self.breakdown._gemini_reverse_prompt_from_media(
                    "unused.mp4", "video/mp4", "sample", 20.0, "local", ""
                )
        cleanup.assert_not_called()

    def test_delete_failure_is_sanitized_and_does_not_raise(self):
        uploaded = {"name": "files/test", "uri": "https://sensitive.example/full", "mime_type": "video/mp4"}
        output = io.StringIO()
        with mock.patch.object(self.breakdown, "_gemini_open", side_effect=RuntimeError("secret-url-and-key")), \
                redirect_stdout(output):
            self.breakdown._gemini_delete_file(uploaded, "mock-secret-key")
        logged = output.getvalue()
        self.assertIn("cleanup failed", logged)
        self.assertNotIn("mock-secret-key", logged)
        self.assertNotIn(uploaded["uri"], logged)

    def test_files_api_waits_until_active(self):
        pending = _Response(json.dumps({"state": "PROCESSING"}).encode())
        active = _Response(json.dumps({"state": "ACTIVE", "uri": "https://files.example/ready"}).encode())
        with mock.patch.object(self.breakdown, "_gemini_open", side_effect=[pending, active]), \
                mock.patch.object(self.breakdown.time, "sleep"):
            result = self.breakdown._gemini_wait_for_file_active(
                {"name": "files/test", "uri": "https://files.example/pending", "mime_type": "video/mp4"},
                "mock-key",
            )
        self.assertEqual(result["uri"], "https://files.example/ready")

    def test_missing_key_fails_before_any_provider_call(self):
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=repo_root) as temp_dir, \
                mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(self.breakdown, "_gemini_json_request") as request:
            media_path = Path(temp_dir) / "sample.mp4"
            media_path.write_bytes(b"x")
            with self.assertRaisesRegex(RuntimeError, "GEMINI_API_KEY"):
                self.breakdown._gemini_reverse_prompt_from_media(
                    str(media_path), "video/mp4", "sample", 1.0, "local", ""
                )
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
