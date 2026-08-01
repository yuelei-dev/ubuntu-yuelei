import sys
import unittest
from pathlib import Path
from unittest.mock import patch


class OfficialSeedanceAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server_dir = str(Path(__file__).resolve().parents[1] / "server")
        if server_dir not in sys.path:
            sys.path.insert(0, server_dir)
        from content_domains import video_seedance
        cls.seedance = video_seedance

    def test_available_requires_dedicated_ark_key(self):
        with patch.object(self.seedance, "ARK_API_KEY", ""):
            self.assertFalse(self.seedance.available())
        with patch.object(self.seedance, "ARK_API_KEY", "ark-test-key"):
            self.assertTrue(self.seedance.available())

    def test_payload_uses_official_model_and_reference_schema(self):
        payload = self.seedance._build_payload(
            self.seedance.SEEDANCE_MODEL,
            "cinematic demo",
            10,
            "9:16",
            "720p",
            True,
            ["https://cdn.example/1.jpg", "asset://asset-reference-2"],
        )
        self.assertEqual(payload["model"], "doubao-seedance-2-0-260128")
        self.assertEqual(payload["duration"], 10)
        self.assertEqual(payload["ratio"], "9:16")
        self.assertEqual(payload["resolution"], "720p")
        self.assertTrue(payload["generate_audio"])
        self.assertEqual(payload["content"][0], {"type": "text", "text": "cinematic demo"})
        self.assertEqual(len(payload["content"]), 3)

    def test_payload_rejects_data_local_and_malformed_asset_references(self):
        for ref in ("data:image/png;base64,AAAA", "/api/gen/file/ref.png", "asset://reference/2"):
            with self.subTest(ref=ref):
                with self.assertRaisesRegex(ValueError, "公网 URL"):
                    self.seedance._build_payload(
                        self.seedance.SEEDANCE_MODEL, "demo", 5, "9:16", "720p", True, [ref]
                    )

    def test_generate_creates_once_then_polls_known_task(self):
        calls = []

        def fake_request(_opener, method, path, body=None, timeout=90):
            calls.append((method, path, body, timeout))
            if method == "POST":
                return {"id": "task-1", "status": "queued"}
            return {
                "id": "task-1",
                "status": "succeeded",
                "model": self.seedance.SEEDANCE_MODEL,
                "duration": 5,
                "ratio": "9:16",
                "resolution": "720p",
                "content": {"video_url": "https://cdn.example/result.mp4"},
            }

        with patch.object(self.seedance, "_opener", return_value=object()), \
                patch.object(self.seedance, "_request_json", side_effect=fake_request):
            result = self.seedance.generate(
                prompt="cinematic demo",
                duration=5,
                ratio="9:16",
                resolution="720p",
            )

        self.assertEqual([item[0] for item in calls], ["POST", "GET"])
        self.assertEqual(
            calls[1][1],
            "/contents/generations/tasks/task-1",
        )
        self.assertEqual(result["request_id"], "task-1")
        self.assertEqual(result["source_video_url"], "https://cdn.example/result.mp4")

    def test_error_summary_redacts_common_credentials(self):
        with patch.object(self.seedance, "ARK_API_KEY", "ARKSECRET123"):
            text = self.seedance._safe_text(
                "Authorization: Bearer TOPSECRET123 token=SECONDSECRET "
                "access_token=THIRDSECRET ARKSECRET123"
            )
        for secret in (
            "TOPSECRET123",
            "SECONDSECRET",
            "THIRDSECRET",
            "ARKSECRET123",
        ):
            self.assertNotIn(secret, text)

    def test_error_summary_redacts_signed_url_query_credentials(self):
        text = self.seedance._safe_text(
            "COS https://bucket.example/ref.jpg?q-ak=AKID123&q-signature=COSSECRET "
            "S3 https://s3.example/ref.jpg?X-Amz-Credential=USER%2Fscope&X-Amz-Signature=AWSSECRET"
        )
        self.assertIn("https://bucket.example/ref.jpg?[REDACTED]", text)
        self.assertIn("https://s3.example/ref.jpg?[REDACTED]", text)
        for secret in ("AKID123", "COSSECRET", "USER%2Fscope", "AWSSECRET"):
            self.assertNotIn(secret, text)


if __name__ == "__main__":
    unittest.main()
