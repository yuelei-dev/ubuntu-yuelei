# -*- coding: utf-8 -*-
"""imggen_api 提示词反推（/api/gen/reverse）的输出截断修复。

线上复现：gemini-2.5-flash 默认开启思考，思考 token 计入 maxOutputTokens，
旧预算 500 被思考吃光，提示词只输出二十来字即以“，”等顿号截断，用户看到
一堆 dangling 符号。修复：thinkingBudget=0 关思考 + maxOutputTokens 提到
1024 + _clean_reverse_prompt 清洗围栏/引号/悬空半句 + MAX_TOKENS 判失败退点。
"""
import importlib, io, json, os, sys, tempfile, unittest, urllib.error
from pathlib import Path

# 同 test_imggen_job_cas：imggen_api 导入时就 OUT_DIR.mkdir()，必须先指走
os.environ.setdefault("CONTENT_OUT", tempfile.mkdtemp(prefix="hq-imggen-out-"))


class _FakeResp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _gemini_payload(text, finish="STOP"):
    return {"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": finish}]}


class ReversePromptTests(unittest.TestCase):
    def setUp(self):
        server_dir = str(Path(__file__).resolve().parents[1] / "server")
        if server_dir not in sys.path:
            sys.path.insert(0, server_dir)
        self.m = importlib.import_module("imggen_api")
        self._orig_key = self.m.GEMINI_KEY
        self._orig_urlopen = self.m.urllib.request.urlopen
        self.m.GEMINI_KEY = "test-key"
        self.sent = []          # 捕获发给 Gemini 的请求体

    def tearDown(self):
        self.m.GEMINI_KEY = self._orig_key
        self.m.urllib.request.urlopen = self._orig_urlopen

    def _stub_urlopen(self, payload=None, http_error=None):
        def fake(req, timeout=0):
            self.sent.append(json.loads(req.data.decode("utf-8")))
            if http_error is not None:
                raise http_error
            return _FakeResp(payload)
        self.m.urllib.request.urlopen = fake

    # ---- 请求体：关思考 + 足够预算 ----
    def test_request_disables_thinking(self):
        self._stub_urlopen(payload=_gemini_payload("一条完整的中文提示词。"))
        self.m.gen_reverse("aW1n")
        cfg = self.sent[0]["generationConfig"]
        self.assertEqual(cfg.get("thinkingConfig"), {"thinkingBudget": 0})
        self.assertGreaterEqual(cfg.get("maxOutputTokens", 0), 1024)

    def test_returns_prompt_text(self):
        self._stub_urlopen(payload=_gemini_payload("都市轻熟女性，杂志级打光，奶油肌质感。"))
        self.assertEqual(self.m.gen_reverse("aW1n"), "都市轻熟女性，杂志级打光，奶油肌质感。")

    # ---- 清洗逻辑 ----
    def test_clean_strips_trailing_dangling_clause(self):
        # 截断残句：回退到上一个句读
        self.assertEqual(self.m._clean_reverse_prompt("完整的一句提示词。被截断的后半截，"),
                         "完整的一句提示词。")
        # 通篇没有句读：直接剥掉结尾顿号
        self.assertEqual(self.m._clean_reverse_prompt("时尚亚洲青年，街头潮服，微仰角，"),
                         "时尚亚洲青年，街头潮服，微仰角")

    def test_clean_strips_fence_and_quotes(self):
        self.assertEqual(self.m._clean_reverse_prompt("```\n“一条提示词。”\n```"), "一条提示词。")
        self.assertEqual(self.m._clean_reverse_prompt('"一条提示词。"'), "一条提示词。")

    def test_clean_collapses_whitespace(self):
        self.assertEqual(self.m._clean_reverse_prompt("  主体描写。\n\n  光影色调。  "),
                         "主体描写。 光影色调。")

    # ---- 失败路径 ----
    def test_max_tokens_still_counts_as_failure(self):
        self._stub_urlopen(payload=_gemini_payload("被截断的提示词", finish="MAX_TOKENS"))
        with self.assertRaises(ValueError):
            self.m.gen_reverse("aW1n")

    def test_empty_candidates_raise(self):
        self._stub_urlopen(payload={"error": {"message": "boom"}})
        with self.assertRaises(ValueError):
            self.m.gen_reverse("aW1n")

    def test_http_error_raises(self):
        err = urllib.error.HTTPError("http://x", 429, "rate", {}, io.BytesIO(b"quota"))
        self._stub_urlopen(http_error=err)
        with self.assertRaises(ValueError):
            self.m.gen_reverse("aW1n")


if __name__ == "__main__":
    unittest.main()
