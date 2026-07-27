# -*- coding: utf-8 -*-
"""miniprogram_security：稳定版 token + 40001 自动刷新重试。

背景（20260727 线上事故）：双机共用同一 appid，旧版 /cgi-bin/token 每签发新
token 即让另一实例的缓存 token 失效（40001），而进程缓存最长 2 小时不刷新，
导致全网提交 503「内容安全服务暂时不可用」。修复：
  1. 换稳定版接口 /cgi-bin/stable_token（force_refresh=false 多实例共享 token）
  2. 检测收到 40001/40014/42001 → 失效缓存、换发新 token 重试一次
"""
import importlib, os, sys, unittest
from pathlib import Path


class _FakeWeChat:
    """按 URL 分发 stable_token / msg_sec_check 的可编程桩。"""

    def __init__(self):
        self.token_calls = []          # [(url, payload)]
        self.check_calls = []          # [(url, payload)]
        self.check_results = []        # 按调用顺序返回的 errcode
        self._n = 0

    def json_request(self, url, payload=None, headers=None, timeout=15):
        if "/cgi-bin/stable_token" in url:
            self.token_calls.append((url, dict(payload or {})))
            self._n += 1
            return {"access_token": "TOKEN-%d" % self._n, "expires_in": 7200}
        if "msg_sec_check" in url:
            self.check_calls.append((url, dict(payload or {})))
            code = self.check_results.pop(0) if self.check_results else 0
            return {"errcode": code, "errmsg": "stub-%s" % code}
        raise AssertionError("unexpected url: " + url)


class MiniprogramSecurityTests(unittest.TestCase):
    def setUp(self):
        server_dir = str(Path(__file__).resolve().parents[1] / "server")
        if server_dir not in sys.path:
            sys.path.insert(0, server_dir)
        self.m = importlib.import_module("content_domains.miniprogram_security")
        self._old_appid = os.environ.get("WX_MP_APPID")
        self._old_secret = os.environ.get("WX_MP_APPSECRET")
        os.environ["WX_MP_APPID"] = "wx-test-appid"
        os.environ["WX_MP_APPSECRET"] = "wx-test-secret"
        self.m._TOKEN_CACHE["value"] = ""
        self.m._TOKEN_CACHE["expires_at"] = 0
        self.wx = _FakeWeChat()
        self._orig_json_request = self.m._json_request
        self.m._json_request = self.wx.json_request

    def tearDown(self):
        self.m._json_request = self._orig_json_request
        self.m._TOKEN_CACHE["value"] = ""
        self.m._TOKEN_CACHE["expires_at"] = 0
        if self._old_appid is None:
            os.environ.pop("WX_MP_APPID", None)
        else:
            os.environ["WX_MP_APPID"] = self._old_appid
        if self._old_secret is None:
            os.environ.pop("WX_MP_APPSECRET", None)
        else:
            os.environ["WX_MP_APPSECRET"] = self._old_secret

    def test_token_uses_stable_endpoint_without_force_refresh(self):
        token = self.m.access_token()
        self.assertEqual(token, "TOKEN-1")
        url, payload = self.wx.token_calls[0]
        self.assertIn("/cgi-bin/stable_token", url)
        self.assertEqual(payload["force_refresh"], False)
        self.assertEqual(payload["grant_type"], "client_credential")

    def test_token_is_cached_until_expiry(self):
        self.assertEqual(self.m.access_token(), "TOKEN-1")
        self.assertEqual(self.m.access_token(), "TOKEN-1")
        self.assertEqual(len(self.wx.token_calls), 1)

    def test_check_text_recovers_from_40001_with_forced_refresh(self):
        self.wx.check_results = [40001, 0]
        self.m.check_text("今天天气不错")
        # 第一次用缓存/常规 token 被打掉 → 换发新 stable token 重试成功
        self.assertEqual([p["force_refresh"] for _, p in self.wx.token_calls], [False, True])
        self.assertEqual(len(self.wx.check_calls), 2)

    def test_check_text_double_40001_becomes_unavailable(self):
        self.wx.check_results = [40001, 40014]
        with self.assertRaises(self.m.SecurityUnavailable):
            self.m.check_text("今天天气不错")
        self.assertEqual(len(self.wx.check_calls), 2)

    def test_87014_rejected_without_retry(self):
        self.wx.check_results = [87014]
        with self.assertRaises(self.m.ContentRejected):
            self.m.check_text("违规内容")
        self.assertEqual(len(self.wx.check_calls), 1)

    def test_unconfigured_raises(self):
        os.environ.pop("WX_MP_APPID", None)
        os.environ.pop("WX_MP_APPSECRET", None)
        with self.assertRaises(self.m.SecurityUnavailable):
            self.m.access_token()

    def test_healthy_check_reuses_cached_token(self):
        self.wx.check_results = [0, 0]
        self.m.check_text("第一段文本")
        self.m.check_text("第二段文本")
        self.assertEqual(len(self.wx.token_calls), 1)
        self.assertEqual(len(self.wx.check_calls), 2)


if __name__ == "__main__":
    unittest.main()
