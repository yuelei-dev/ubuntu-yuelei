import importlib
import json
import os
import sys
import unittest
from pathlib import Path


class _FakeResponse:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self._payload


class ZhipuCopyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server_dir = str(Path(__file__).resolve().parents[1] / "server")
        if server_dir not in sys.path:
            sys.path.insert(0, server_dir)
        cls.text = importlib.import_module("content_domains.text")

    def test_zhipu_api_base_cannot_be_redirected_by_environment(self):
        previous = os.environ.get("ZHIPU_API_BASE")
        os.environ["ZHIPU_API_BASE"] = "https://example.invalid/api/paas/v4"
        try:
            importlib.reload(self.text)
            self.assertEqual(self.text.ZHIPU_API_BASE, "https://open.bigmodel.cn/api/paas/v4")
        finally:
            if previous is None:
                os.environ.pop("ZHIPU_API_BASE", None)
            else:
                os.environ["ZHIPU_API_BASE"] = previous
            importlib.reload(self.text)

    def test_plain_text_uses_zhipu_chat_completions(self):
        self.assertTrue(hasattr(self.text, "ZHIPU_API_KEY"))
        captured = {}

        class FakeOpener:
            def open(self, request, timeout=None):
                captured["request"] = request
                captured["timeout"] = timeout
                return _FakeResponse({"choices": [{"message": {"content": " 智谱结果 "}}]})

        original = (self.text.ZHIPU_API_KEY, self.text._NOPROXY)
        self.text.ZHIPU_API_KEY = "test-zhipu-key"
        self.text._NOPROXY = FakeOpener()
        try:
            result = self.text._chat("system", "user", 0.7)
        finally:
            self.text.ZHIPU_API_KEY, self.text._NOPROXY = original

        request = captured["request"]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(result, "智谱结果")
        self.assertEqual(self.text.ZHIPU_API_BASE, "https://open.bigmodel.cn/api/paas/v4")
        self.assertEqual(self.text.COPY_MODEL, "glm-4-plus")
        self.assertEqual(request.full_url, "https://open.bigmodel.cn/api/paas/v4/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-zhipu-key")
        self.assertEqual(body["model"], "glm-4-plus")
        self.assertEqual(captured["timeout"], 300)

    def test_plain_text_rejects_missing_zhipu_key(self):
        self.assertTrue(hasattr(self.text, "ZHIPU_API_KEY"))
        original = self.text.ZHIPU_API_KEY
        self.text.ZHIPU_API_KEY = ""
        try:
            with self.assertRaisesRegex(ValueError, "ZHIPU_API_KEY"):
                self.text._chat("system", "user", 0.7)
        finally:
            self.text.ZHIPU_API_KEY = original

    def test_plain_text_rejects_whitespace_zhipu_key(self):
        previous = os.environ.get("ZHIPU_API_KEY")
        os.environ["ZHIPU_API_KEY"] = "   \t"
        try:
            importlib.reload(self.text)
            with self.assertRaisesRegex(ValueError, "ZHIPU_API_KEY"):
                self.text._chat("system", "user", 0.7)
        finally:
            if previous is None:
                os.environ.pop("ZHIPU_API_KEY", None)
            else:
                os.environ["ZHIPU_API_KEY"] = previous
            importlib.reload(self.text)

    def test_regular_copy_uses_plain_text_chat_only(self):
        captured = {}
        original_chat = self.text._chat
        original_multimodal = self.text._chat_multimodal

        def fake_chat(sysmsg, usermsg, temp):
            captured["plain"] = (sysmsg, usermsg, temp)
            return "普通文案"

        self.text._chat = fake_chat
        self.text._chat_multimodal = lambda *args: self.fail("unexpected multimodal call")
        try:
            result = self.text.gen_copy({"prompt": "选题"})
        finally:
            self.text._chat = original_chat
            self.text._chat_multimodal = original_multimodal

        self.assertEqual(result["text"], "普通文案")
        self.assertEqual(captured["plain"][2], 0.9)

    def test_script_without_reference_images_uses_plain_text_chat(self):
        captured = {}
        original_chat = self.text._chat
        original_multimodal = self.text._chat_multimodal

        def fake_chat(sysmsg, usermsg, temp):
            captured["plain"] = (sysmsg, usermsg, temp)
            return '{"scenes":[{"dur":"3s","scene":"画面","line":"台词"}]}'

        self.text._chat = fake_chat
        self.text._chat_multimodal = lambda *args: self.fail("unexpected multimodal call")
        try:
            result = self.text.gen_copy({"prompt": "选题", "format": "script"})
        finally:
            self.text._chat = original_chat
            self.text._chat_multimodal = original_multimodal

        self.assertEqual(result["scenes"][0]["scene"], "画面")
        self.assertEqual(captured["plain"][2], 0.85)

    def test_script_with_reference_images_uses_multimodal_chat_only(self):
        captured = {}
        original_chat = self.text._chat
        original_multimodal = self.text._chat_multimodal

        def fake_multimodal(sysmsg, usermsg, image_data_urls, temp=0.85):
            captured["multimodal"] = (sysmsg, usermsg, image_data_urls, temp)
            return '{"scenes":[{"dur":"3s","scene":"参考画面","line":"台词"}]}'

        self.text._chat = lambda *args: self.fail("unexpected plain-text chat call")
        self.text._chat_multimodal = fake_multimodal
        try:
            result = self.text.gen_copy({
                "prompt": "选题",
                "format": "script",
                "reference_images": ["data:image/png;base64,AA=="],
            })
        finally:
            self.text._chat = original_chat
            self.text._chat_multimodal = original_multimodal

        self.assertEqual(result["scenes"][0]["scene"], "参考画面")
        self.assertEqual(captured["multimodal"][2], ["data:image/png;base64,AA=="])
        self.assertEqual(captured["multimodal"][3], 0.85)

    def test_multimodal_stays_on_openai_gpt4o(self):
        egress = importlib.import_module("content_domains.egress")
        captured = {}

        def fake_post_json(connect_base, host_base, path, body, headers):
            captured.update(connect_base=connect_base, host_base=host_base,
                            path=path, body=json.loads(body.decode("utf-8")), headers=headers)
            return {"choices": [{"message": {"content": "图文结果"}}]}

        original = egress.post_json
        egress.post_json = fake_post_json
        try:
            result = self.text._chat_multimodal("system", "user", ["data:image/png;base64,AA=="])
        finally:
            egress.post_json = original

        self.assertEqual(result, "图文结果")
        self.assertEqual(captured["path"], "/v1/chat/completions")
        self.assertEqual(captured["body"]["model"], "gpt-4o")
        self.assertEqual(captured["connect_base"], self.text.OPENAI_BASE)


if __name__ == "__main__":
    unittest.main()
