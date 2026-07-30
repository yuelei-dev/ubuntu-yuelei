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
        labels = ("A" * 24, "B" * 24, "C" * 24, "D" * 24)
        label = labels[int(start) % len(labels)]
        facts = {}
        evidence = {}
        for key in self.breakdown._GEMINI_FACT_FIELDS:
            if key in self.breakdown._GEMINI_OPTIONAL_FACT_FIELDS:
                facts[key] = "not_applicable"
                evidence[key] = []
            else:
                facts[key] = "%s observed %s" % (label, key.replace("_", " "))
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
        shots = []
        for i in range(count):
            shot = self._shot(float(i), float(i + 1), i > 0)
            facts = [
                {
                    "key": key,
                    "value": shot["facts"][key],
                    "evidence_seconds": shot["evidence_seconds"][key],
                }
                for key in self.breakdown._GEMINI_FACT_FIELDS
            ]
            shots.append({
                "start_seconds": shot["start_seconds"],
                "end_seconds": shot["end_seconds"],
                "cut_from_previous": shot["cut_from_previous"],
                "facts": facts,
                "generation_advice": shot["generation_advice"],
            })
        text = json.dumps({"shots": shots})
        return {"candidates": [{"content": {"parts": [{"text": text}]}}]}

    def test_model_endpoint_video_mime_json_mode_and_no_fallback(self):
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
        self.assertEqual(config["maxOutputTokens"], 32768)
        self.assertEqual(config["thinkingConfig"], {"thinkingLevel": "medium"})
        self.assertNotIn("responseFormat", config)
        self.assertNotIn("responseSchema", config)
        self.assertEqual(
            config["responseJsonSchema"],
            self.breakdown._gemini_reverse_provider_schema(),
        )

    def test_gemini31_request_uses_compatible_rest_json_schema(self):
        body = self.breakdown._gemini_request_body(
            {"file_data": {
                "mime_type": "video/mp4",
                "file_uri": "https://generativelanguage.googleapis.com/v1beta/files/mock",
            }},
            "sample", 15.0, "local", "",
        )
        config = body["generationConfig"]
        self.assertEqual(config["responseMimeType"], "application/json")
        self.assertEqual(config["maxOutputTokens"], 32768)
        self.assertEqual(config["thinkingConfig"], {"thinkingLevel": "medium"})
        self.assertNotIn("responseFormat", config)
        self.assertNotIn("responseSchema", config)
        self.assertEqual(
            config["responseJsonSchema"],
            self.breakdown._gemini_reverse_provider_schema(),
        )

    def test_instruction_carries_the_complete_strict_output_contract(self):
        instruction = self.breakdown._gemini_reverse_instruction(
            "sample", 4.0, "local", "",
        )
        self.assertIn('exactly the root key "shots"', instruction)
        self.assertIn("no markdown or wrapper", instruction)
        self.assertIn("complete minified JSON object", instruction)
        self.assertIn("no indentation or line breaks", instruction)
        self.assertIn("do not repeat the same description", instruction)
        self.assertIn(
            "start_seconds, end_seconds, cut_from_previous, facts, and generation_advice",
            instruction,
        )
        self.assertIn(
            "key, value, and evidence_seconds",
            instruction,
        )
        self.assertIn(
            "1-3 timestamps inside the current shot",
            instruction,
        )
        self.assertIn(
            "[] for unknown/not_applicable",
            instruction,
        )
        self.assertIn("wardrobe for a non-person/no visible clothing", instruction)
        self.assertIn("sound when Verified ASR is (none)", instruction)
        self.assertIn("subtitles when no text is visibly readable", instruction)
        self.assertIn("continuity for the first shot", instruction)
        self.assertIn("Their evidence_seconds must then be []", instruction)
        self.assertIn("visible non-person object or geometric shape is the subject", instruction)
        self.assertIn("unchanged subject at action start", instruction)
        self.assertIn("shot-specific visible object, color, position, or empty image region", instruction)
        self.assertIn("instead of a generic 'no distinct layer' template", instruction)
        self.assertIn("Never invent a person, wardrobe, object, depth layer, or motion", instruction)
        self.assertIn("compare every shot pair", instruction)
        self.assertIn("every non-sentinel value must contain shot-specific visible evidence", instruction)
        self.assertIn("never invent differences merely to avoid duplication", instruction)
        self.assertIn(
            "aspect_ratio, fps, camera_control, and negative_prompt",
            instruction,
        )
        self.assertIn(
            ", ".join(self.breakdown._GEMINI_FACT_FIELDS),
            instruction,
        )

    def test_provider_schema_uses_compact_fact_rows_without_duplicate_evidence_tree(self):
        schema = self.breakdown._gemini_reverse_schema()
        shot_schema = schema["properties"]["shots"]["items"]
        self.assertEqual(
            shot_schema["required"],
            [
                "start_seconds", "end_seconds", "cut_from_previous", "facts",
                "generation_advice",
            ],
        )
        self.assertNotIn("evidence_seconds", shot_schema["properties"])
        facts_schema = shot_schema["properties"]["facts"]
        self.assertEqual(facts_schema["minItems"], len(self.breakdown._GEMINI_FACT_FIELDS))
        self.assertEqual(facts_schema["maxItems"], len(self.breakdown._GEMINI_FACT_FIELDS))
        self.assertEqual(
            facts_schema["items"]["properties"]["key"]["enum"],
            list(self.breakdown._GEMINI_FACT_FIELDS),
        )
        self.assertLess(len(json.dumps(schema).encode("utf-8")), 4000)

    def test_provider_schema_omits_only_live_incompatible_array_bounds(self):
        schema = self.breakdown._gemini_reverse_provider_schema()
        encoded = json.dumps(schema)
        self.assertNotIn('"minItems"', encoded)
        self.assertNotIn('"maxItems"', encoded)
        self.assertIn('"additionalProperties": false', encoded)
        self.assertIn('"enum"', encoded)
        self.assertIn('"minimum"', encoded)
        self.assertEqual(
            schema["properties"]["shots"]["items"]["required"],
            [
                "start_seconds", "end_seconds", "cut_from_previous", "facts",
                "generation_advice",
            ],
        )
        self.assertLess(len(encoded.encode("utf-8")), 1500)

    def test_compact_fact_rows_expand_deterministically_and_reject_duplicates(self):
        response = self._provider_response()
        raw = response["candidates"][0]["content"]["parts"][0]["text"]
        parsed = self.breakdown._parse_gemini_reverse_result(raw, 1.0)
        self.assertEqual(parsed["entries"][0]["readiness"]["ready"], 17)

        payload = json.loads(raw)
        payload["shots"][0]["facts"][-1]["key"] = payload["shots"][0]["facts"][0]["key"]
        with self.assertRaisesRegex(ValueError, "unique and complete"):
            self.breakdown._parse_gemini_reverse_result(json.dumps(payload), 1.0)

    def test_one_to_four_shots_are_gap_free_and_directly_assembled(self):
        for count in range(1, 5):
            raw = json.dumps({"shots": [
                self._shot(float(i), float(i + 1), i > 0) for i in range(count)
            ]})
            result = self.breakdown._parse_gemini_reverse_result(raw, float(count))
            prompt = self.breakdown._assemble_reverse_prompt(
                result["entries"],
                result["windows"],
                enforce_length_limits=False,
            )
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

    def test_more_than_three_evidence_timestamps_is_rejected_with_field_name(self):
        shot = self._shot(0.0, 1.0)
        shot["evidence_seconds"]["subject_identity"] = [0.1, 0.2, 0.3, 0.4]
        with self.assertRaisesRegex(
            ValueError,
            "shot 1 subject_identity evidence must contain at most 3 timestamps",
        ):
            self.breakdown._parse_gemini_reverse_result(
                json.dumps({"shots": [shot]}), 1.0
            )

    def test_action_requires_evidence_at_both_shot_endpoints(self):
        shot = self._shot(0.0, 1.0)
        shot["evidence_seconds"]["action_end"] = [0.2]
        with self.assertRaisesRegex(ValueError, "both shot endpoints"):
            self.breakdown._parse_gemini_reverse_result(
                json.dumps({"shots": [shot]}), 1.0
            )

    def test_readiness_error_names_unresolved_slots_without_authorizing_guessing(self):
        shot = self._shot(0.0, 1.0)
        for key in ("direction_speed", "camera_movement"):
            shot["facts"][key] = "unknown"
            shot["evidence_seconds"][key] = []
        with self.assertRaises(ValueError) as raised:
            self.breakdown._parse_gemini_reverse_result(
                json.dumps({"shots": [shot]}), 1.0
            )
        message = str(raised.exception)
        self.assertIn("shot 1 generation readiness is below 90 percent", message)
        self.assertIn("direction_speed", message)
        self.assertIn("camera_movement", message)
        retry = self.breakdown._gemini_reverse_instruction(
            "sample", 1.0, "local", "", message,
        )
        self.assertIn("only when visible evidence supports them", retry)
        self.assertIn("otherwise keep unknown and allow strict failure", retry)

    def test_generation_advice_is_strictly_typed_formatted_and_bounded(self):
        invalid_values = (
            ("aspect_ratio", 169),
            ("aspect_ratio", "wide"),
            ("fps", None),
            ("fps", "29.97"),
            ("camera_control", ""),
            ("negative_prompt", ""),
        )
        for key, value in invalid_values:
            with self.subTest(key=key, value_type=type(value).__name__):
                shot = self._shot(0.0, 1.0)
                shot["generation_advice"][key] = value
                with self.assertRaisesRegex(ValueError, "generation advice"):
                    self.breakdown._parse_gemini_reverse_result(json.dumps({"shots": [shot]}), 1.0)

    def test_gemini_field_length_limits_stay_removed_through_final_prompt_assembly(self):
        schema_text = json.dumps(self.breakdown._gemini_reverse_schema())
        self.assertNotIn("maxLength", schema_text)
        shot = self._shot(0.0, 1.0)
        shot["facts"]["subject_appearance"] = "x" * 1600
        shot["generation_advice"]["camera_control"] = "y" * 300
        shot["generation_advice"]["negative_prompt"] = "z" * 300
        result = self.breakdown._parse_gemini_reverse_result(
            json.dumps({"shots": [shot]}), 1.0,
        )
        self.assertGreater(
            len(result["entries"][0]["text"]),
            self.breakdown._REVERSE_MAX_SEGMENT_CHARS,
        )
        prompt = self.breakdown._assemble_reverse_prompt(
            result["entries"],
            result["windows"],
            enforce_length_limits=False,
        )
        self.assertIn("x" * 1600, prompt)
        with self.assertRaises(ValueError):
            self.breakdown._assemble_reverse_prompt(
                result["entries"],
                result["windows"],
            )

    def test_gemini_final_prompt_total_length_is_bounded_for_downstream_models(self):
        entry = {
            "text": "x" * (self.breakdown._REVERSE_MAX_TOTAL_CHARS + 1),
            "fields": {},
        }
        with self.assertRaisesRegex(ValueError, "总长度"):
            self.breakdown._assemble_reverse_prompt(
                [entry],
                [(0.0, 1.0, "0.0-1.0s")],
                enforce_length_limits=False,
            )

    def test_gemini_total_response_has_only_a_transport_safety_bound(self):
        oversized = {"candidates": [{"content": {"parts": [{
            "text": "x" * (self.breakdown._GEMINI_MAX_RESPONSE_BYTES + 1),
        }]}}]}
        with self.assertRaisesRegex(ValueError, "total output limit"):
            self.breakdown._gemini_candidate_text(oversized)

    def test_gemini_max_tokens_finish_reason_is_rejected_without_salvage(self):
        truncated = {"candidates": [{
            "finishReason": "MAX_TOKENS",
            "content": {"parts": [{"text": '{"shots":['}]},
        }]}
        with self.assertRaisesRegex(ValueError, "truncated at the output token limit"):
            self.breakdown._gemini_candidate_text(truncated)

    def test_projected_inline_payload_over_safety_limit_uses_files_api(self):
        source_bytes = 14_172_348
        with mock.patch.object(self.breakdown.os.path, "getsize", return_value=source_bytes):
            projected = self.breakdown._gemini_inline_payload_bytes(
                "unused", "video/mp4", "sample", 15.0, "local", "",
            )
        self.assertGreater(projected, self.breakdown._GEMINI_INLINE_MAX_REQUEST_BYTES)
        uploaded = {"name": "files/test", "uri": "https://files.example/test"}
        with mock.patch.object(self.breakdown.os.path, "getsize", return_value=source_bytes), \
                mock.patch.object(self.breakdown, "_gemini_upload_file", return_value=uploaded) as upload:
            part, result = self.breakdown._gemini_media_part(
                "unused", "video/mp4", 15.0, "mock-key", inline_payload_bytes=projected,
            )
        upload.assert_called_once()
        self.assertEqual(result, uploaded)
        self.assertIn("file_data", part)


    def test_visible_subtitles_do_not_masquerade_as_asr_sound(self):
        shot = self._shot(0.0, 1.0)
        shot["facts"]["subtitles"] = "\u753b\u9762\u6587\u5b57\u201c\u65b0\u54c1\u53d1\u5e03\u201d"
        shot["evidence_seconds"]["subtitles"] = [0.4]
        result = self.breakdown._parse_gemini_reverse_result(
            json.dumps({"shots": [shot]}, ensure_ascii=False), 1.0
        )
        self.assertEqual(result["entries"][0]["fields"]["sound"], "")
        self.assertIn("visible subtitles/text", result["entries"][0]["text"])

    def test_unrelated_sound_is_omitted_against_current_shot_asr(self):
        shot = self._shot(0.0, 1.0)
        shot["facts"]["sound"] = "\u6fc0\u6602\u6447\u6eda\u4e50"
        shot["evidence_seconds"]["sound"] = [0.4]
        result = self.breakdown._parse_gemini_reverse_result(
            json.dumps({"shots": [shot]}, ensure_ascii=False), 1.0
        )
        before = dict(result["entries"][0]["readiness"])
        self.breakdown._bind_gemini_sound_evidence(
            result, "[0.0-1.0] \u6b22\u8fce\u5149\u4e34",
        )
        entry = result["entries"][0]
        self.assertEqual(entry["fields"]["sound"], "")
        self.assertEqual(entry["evidence_seconds"]["sound"], [])
        self.assertNotIn("\u6fc0\u6602\u6447\u6eda\u4e50", entry["text"])
        self.assertEqual(entry["readiness"]["applicable"], before["applicable"] - 1)
        self.assertEqual(entry["readiness"]["ready"], before["ready"] - 1)
        self.assertIn(
            {"field": "sound", "reason": "segment_asr_mismatch"},
            entry["omitted_unsupported_fields"],
        )
        self.breakdown._validate_gemini_reverse_entries(
            result,
            ["frame-%d.jpg" % index for index in range(8)],
            "[0.0-1.0] \u6b22\u8fce\u5149\u4e34",
        )

    def test_sound_without_segment_asr_is_omitted_without_claiming_silence(self):
        shot = self._shot(0.0, 1.0)
        shot["facts"]["sound"] = "\u8212\u7f13\u80cc\u666f\u97f3\u4e50"
        shot["evidence_seconds"]["sound"] = [0.4]
        result = self.breakdown._parse_gemini_reverse_result(
            json.dumps({"shots": [shot]}, ensure_ascii=False), 1.0,
        )
        self.breakdown._bind_gemini_sound_evidence(result, "")
        entry = result["entries"][0]
        self.assertEqual(entry["fields"]["sound"], "")
        self.assertEqual(entry["evidence_seconds"]["sound"], [])
        self.assertNotIn("verified sound/ASR", entry["text"])
        self.assertNotIn("\u672a\u68c0\u6d4b\u5230\u58f0\u97f3", entry["text"])
        self.assertIn(
            {"field": "sound", "reason": "no_segment_asr"},
            entry["omitted_unsupported_fields"],
        )
        self.breakdown._validate_gemini_reverse_entries(
            result,
            ["frame-%d.jpg" % index for index in range(8)],
            "",
        )

    def test_unknown_sound_omission_does_not_decrement_ready_twice(self):
        shot = self._shot(0.0, 1.0)
        shot["facts"]["sound"] = "unknown"
        shot["evidence_seconds"]["sound"] = []
        result = self.breakdown._parse_gemini_reverse_result(
            json.dumps({"shots": [shot]}, ensure_ascii=False), 1.0,
        )
        entry = result["entries"][0]
        self.assertEqual(
            entry["readiness"],
            {"applicable": 18, "ready": 17},
        )

        self.breakdown._bind_gemini_sound_evidence(result, "")

        self.assertEqual(entry["fields"]["sound"], "")
        self.assertEqual(
            entry["readiness"],
            {"applicable": 17, "ready": 17},
        )
        self.assertIn(
            {"field": "sound", "reason": "no_segment_asr"},
            entry["omitted_unsupported_fields"],
        )
        quality = self.breakdown._gemini_quality_dimensions(result)
        self.assertEqual(
            quality["generation_readiness"],
            {"ready": 17, "applicable": 17, "percent": 100.0},
        )

    def test_sound_matching_current_segment_asr_is_retained(self):
        shot = self._shot(0.0, 1.0)
        shot["facts"]["sound"] = "\u4eba\u7269\u8bf4\u51fa\u201c\u6b22\u8fce\u5149\u4e34\u201d"
        shot["evidence_seconds"]["sound"] = [0.4]
        result = self.breakdown._parse_gemini_reverse_result(
            json.dumps({"shots": [shot]}, ensure_ascii=False), 1.0,
        )
        before = dict(result["entries"][0]["readiness"])
        self.breakdown._bind_gemini_sound_evidence(
            result, "[0.0-1.0] \u6b22\u8fce\u5149\u4e34",
        )
        entry = result["entries"][0]
        self.assertEqual(
            entry["fields"]["sound"],
            "\u4eba\u7269\u8bf4\u51fa\u201c\u6b22\u8fce\u5149\u4e34\u201d",
        )
        self.assertIn("verified sound/ASR", entry["text"])
        self.assertEqual(entry["readiness"], before)
        self.assertNotIn("omitted_unsupported_fields", entry)

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

    def test_http_error_summary_keeps_safe_google_fields_and_redacts_secrets(self):
        request = self.breakdown.urllib.request.Request("https://example.invalid/private")
        body = json.dumps({"error": {
            "code": 400,
            "status": "INVALID_ARGUMENT",
            "message": (
                "bad schema at https://secret.example/path "
                "api_key=AQ.secret-value-123456789 "
                "Authorization: Bearer TOPSECRET123456 "
                "token=SECONDSECRET987 access_token=THIRDSECRET654 "
                "secret=FOURTHSECRET321"
            ),
        }}).encode()
        error = urllib.error.HTTPError(request.full_url, 400, "bad", {}, io.BytesIO(body))
        with mock.patch.object(self.breakdown.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(RuntimeError) as raised:
                self.breakdown._gemini_open(request)
        message = str(raised.exception)
        self.assertIn("Gemini HTTP 400", message)
        self.assertIn("INVALID_ARGUMENT", message)
        self.assertIn("[redacted-url]", message)
        self.assertIn("[redacted-credential]", message)
        self.assertNotIn("secret.example", message)
        self.assertNotIn("AQ.secret", message)
        self.assertNotIn("TOPSECRET123456", message)
        self.assertNotIn("SECONDSECRET987", message)
        self.assertNotIn("THIRDSECRET654", message)
        self.assertNotIn("FOURTHSECRET321", message)
        self.assertGreaterEqual(message.count("[redacted-credential]"), 5)


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

    def test_duplicate_prompt_is_rejected_inside_the_single_validation_retry(self):
        first = self._provider_response(count=2)
        payload = json.loads(first["candidates"][0]["content"]["parts"][0]["text"])
        for index, row in enumerate(payload["shots"][1]["facts"]):
            row["value"] = payload["shots"][0]["facts"][index]["value"]
        first["candidates"][0]["content"]["parts"][0]["text"] = json.dumps(payload)
        responses = iter([first, self._provider_response(count=2)])
        captured = []

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
                str(media_path), "video/mp4", "sample", 2.0, "local", ""
            )
        self.assertEqual(result["attempts"], 2)
        retry_prompt = captured[1]["contents"][0]["parts"][1]["text"]
        self.assertIn("内容重复", retry_prompt)
        self.assertIn("shot 1 (0.0-1.0s)", retry_prompt)
        self.assertIn("shot 2 (1.0-2.0s)", retry_prompt)
        self.assertIn("merge the intervals into one shot", retry_prompt)
        self.assertIn("Never invent a difference", retry_prompt)
        self.assertNotIn(first["candidates"][0]["content"]["parts"][0]["text"], retry_prompt)

    def test_duplicate_guard_compares_structured_facts_not_shared_scaffolding(self):
        response = self._provider_response(count=2)
        payload = json.loads(response["candidates"][0]["content"]["parts"][0]["text"])
        first_rows = payload["shots"][0]["facts"]
        second_rows = payload["shots"][1]["facts"]
        for index, row in enumerate(second_rows):
            row["value"] = first_rows[index]["value"]
        identical = self.breakdown._parse_gemini_reverse_result(
            json.dumps(payload), 2.0,
        )["entries"]
        self.assertTrue(self.breakdown._reverse_segments_are_duplicate(
            identical[1], identical[0],
        ))

        distinct = {
            "subject_identity": "粉色连帽裙人物",
            "subject_appearance": "粉色几何人物轮廓位于画面中央",
            "foreground": "下方深灰色地面横带",
            "midground": "粉色人物占据中央区域",
            "background": "深蓝夜空与四盏橙色灯笼",
        }
        for row in second_rows:
            if row["key"] in distinct:
                row["value"] = distinct[row["key"]]
        entries = self.breakdown._parse_gemini_reverse_result(
            json.dumps(payload), 2.0,
        )["entries"]
        self.assertEqual(
            self.breakdown._reverse_text_similarity(
                entries[1]["fields"]["action"],
                entries[0]["fields"]["action"],
            ),
            1.0,
        )
        self.assertFalse(self.breakdown._reverse_segments_are_duplicate(
            entries[1], entries[0],
        ))

    def test_full_segment_validation_accepts_distinct_shots_with_shared_scaffolding(self):
        response = self._provider_response(count=2)
        payload = json.loads(response["candidates"][0]["content"]["parts"][0]["text"])
        first_rows = payload["shots"][0]["facts"]
        second_rows = payload["shots"][1]["facts"]
        for index, row in enumerate(second_rows):
            row["value"] = first_rows[index]["value"]

        distinct = {
            "subject_identity": "粉色连帽裙人物",
            "subject_appearance": "粉色几何人物轮廓位于画面中央",
            "foreground": "下方深灰色地面横带",
            "midground": "粉色人物占据中央区域",
            "background": "深蓝夜空与四盏橙色灯笼",
        }
        for row in second_rows:
            if row["key"] in distinct:
                row["value"] = distinct[row["key"]]
        entries = self.breakdown._parse_gemini_reverse_result(
            json.dumps(payload, ensure_ascii=False), 2.0,
        )["entries"]

        # The deterministic labels, advice, camera, lighting, and action remain
        # shared, reproducing the high rendered-text similarity seen in the
        # isolated run. Observable subject and scene facts still distinguish
        # the shots and must control the duplicate decision.
        self.assertGreaterEqual(
            self.breakdown._reverse_text_similarity(
                entries[1]["text"], entries[0]["text"],
            ),
            self.breakdown._REVERSE_DUPLICATE_SEQUENCE_THRESHOLD,
        )
        self.breakdown._validate_reverse_segment_evidence(
            entries[0], [], [], 1, enforce_length_limit=False,
        )
        self.breakdown._validate_reverse_segment_evidence(
            entries[1], [entries[0]], [], 2, enforce_length_limit=False,
        )

        for index, row in enumerate(second_rows):
            row["value"] = first_rows[index]["value"]
        identical = self.breakdown._parse_gemini_reverse_result(
            json.dumps(payload), 2.0,
        )["entries"]
        with self.assertRaisesRegex(ValueError, "内容重复"):
            self.breakdown._validate_reverse_segment_evidence(
                identical[1], [identical[0]], [], 2,
                enforce_length_limit=False,
            )

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
