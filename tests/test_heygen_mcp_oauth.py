import importlib
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from types import SimpleNamespace

if os.name == "nt":
    sys.modules.setdefault("fcntl", SimpleNamespace(
        LOCK_EX=1, LOCK_NB=2, LOCK_UN=8, flock=lambda *_args: None,
    ))

SERVER = str(Path(__file__).resolve().parents[1] / "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

video = importlib.import_module("content_domains.video")


class HeyGenMcpOAuthTests(unittest.TestCase):
    @staticmethod
    def _private_stat(path):
        current = os.stat(path)
        return os.stat_result((current.st_mode & ~0o077, *current[1:]))

    def test_paid_create_never_silently_falls_back_to_api_wallet(self):
        with patch.object(video, "_HEYGEN_MCP_CREDENTIALS", ""), \
             patch.object(video, "_HEYGEN_ALLOW_API_WALLET", False), \
             patch.object(video, "_heygen_request_json") as api_request, \
             patch.object(video, "_heygen_mcp_call") as mcp_call:
            with self.assertRaisesRegex(
                    video.HeyGenMCPAuthError, "已阻止回退到 API 钱包"):
                video._heygen_create_video(
                    "image-asset", "audio-asset", "1080p", "9:16", "medium",
                    direct=True,
                )
            with self.assertRaisesRegex(
                    video.HeyGenMCPAuthError, "已阻止回退到 API 钱包"):
                video._heygen_create_cinematic_video(
                    ["look-1"], [], "9:16", "720p", 15, direct=True,
                )
        api_request.assert_not_called()
        mcp_call.assert_not_called()

    def test_api_wallet_requires_explicit_operator_opt_in(self):
        response = {"data": {"video_id": "api-wallet-video"}}
        with patch.object(video, "_HEYGEN_MCP_CREDENTIALS", ""), \
             patch.object(video, "_HEYGEN_ALLOW_API_WALLET", True), \
             patch.object(video, "HEYGEN_API_KEY", "configured"), \
             patch.object(video, "_heygen_request_json", return_value=response) as request:
            video_id = video._heygen_create_video(
                "image-asset", "audio-asset", "720p", "9:16", "medium",
                direct=True,
            )
        self.assertEqual(video_id, "api-wallet-video")
        request.assert_called_once()

    def test_explicit_api_wallet_route_never_uses_mcp_assets_or_create(self):
        response = {"data": {"video_id": "api-wallet-video"}}
        with patch.object(video, "_HEYGEN_MCP_CREDENTIALS", "/secure/oauth.json"), \
             patch.object(video, "_HEYGEN_ALLOW_API_WALLET", True), \
             patch.object(video, "HEYGEN_API_KEY", "configured"), \
             patch.object(video, "_heygen_mcp_call") as mcp_call, \
             patch.object(video, "_heygen_request_json", return_value=response) as api_request:
            video_id = video._heygen_create_video(
                "image-asset", "audio-asset", "1080p", "9:16", "medium",
                direct=True, route="api_wallet",
            )
        self.assertEqual(video_id, "api-wallet-video")
        api_request.assert_called_once()
        mcp_call.assert_not_called()

    def test_missing_oauth_is_a_definitive_pre_billing_rejection(self):
        error = video.HeyGenMCPAuthError("套餐 OAuth 未配置")
        self.assertTrue(video._definitive_heygen_create_rejection(error))

    def test_mcp_plan_credit_error_is_definitive_and_does_not_leak_detail(self):
        requests = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                result = {
                    "jsonrpc": "2.0", "id": "x", "result": {
                        "content": [{
                            "type": "text",
                            "text": (
                                "MOVIO_PAYMENT_INSUFFICIENT_CREDIT: "
                                "insufficient premium credits; internal-marker"
                            ),
                        }],
                        "isError": True,
                    },
                }
                return ("data: " + json.dumps(result) + "\n\n").encode()

        class Opener:
            def open(self, request, **_kwargs):
                requests.append(request)
                return Response()

        with patch.object(video, "_heygen_mcp_access_token", return_value="token"), \
             patch.object(video, "_heygen_direct_opener", return_value=Opener()):
            with self.assertRaises(video.HeyGenMCPPlanCreditsExhausted) as rejected:
                video._heygen_mcp_call("create_video_from_image", {})

        self.assertTrue(video._definitive_heygen_create_rejection(rejected.exception))
        self.assertEqual(len(requests), 1)
        self.assertNotIn("internal-marker", str(rejected.exception))

    def test_generic_mcp_tool_error_remains_ambiguous(self):
        self.assertFalse(video._heygen_mcp_plan_credits_exhausted(
            "Please try different images or prompts. No credits charged."
        ))
        error = RuntimeError("HeyGen MCP 工具失败: temporary provider timeout")
        self.assertFalse(video._definitive_heygen_create_rejection(error))

    def test_expired_oauth_refreshes_and_stays_private(self):
        requests = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600}).encode()

        class Opener:
            def open(self, request, **_kwargs):
                requests.append(request)
                return Response()

        with tempfile.TemporaryDirectory() as directory:
            credentials = Path(directory) / "heygen-mcp.json"
            credentials.write_text(json.dumps({
                "client_id": "client", "access_token": "old-access",
                "refresh_token": "old-refresh", "expires_at": 1,
            }))
            credentials.chmod(0o600)
            with patch.object(video, "_HEYGEN_MCP_CREDENTIALS", str(credentials)), \
                 patch.object(video, "_heygen_direct_opener", return_value=Opener()), \
                 patch.object(Path, "stat", autospec=True, side_effect=self._private_stat), \
                 patch.object(video.os, "fchmod", create=True), \
                 patch.object(video.time, "time", return_value=1000):
                self.assertEqual(video._heygen_mcp_access_token(), "new-access")
                self.assertEqual(video._heygen_mcp_access_token(), "new-access")
            saved = json.loads(credentials.read_text())
            self.assertEqual(saved["refresh_token"], "new-refresh")
            if os.name != "nt":
                self.assertEqual(os.stat(credentials).st_mode & 0o077, 0)
                self.assertEqual(os.stat(str(credentials) + ".lock").st_mode & 0o077, 0)
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0].get_header("User-agent"), "huangque-content/1.0")

    def test_one_time_refresh_token_is_not_reused(self):
        requests = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({"access_token": "last-access", "expires_in": 3600}).encode()

        class Opener:
            def open(self, request, **_kwargs):
                requests.append(request)
                return Response()

        with tempfile.TemporaryDirectory() as directory:
            credentials = Path(directory) / "heygen-mcp.json"
            credentials.write_text(json.dumps({
                "client_id": "client", "access_token": "old-access",
                "refresh_token": "one-time-refresh", "expires_at": 1,
            }))
            credentials.chmod(0o600)
            with patch.object(video, "_HEYGEN_MCP_CREDENTIALS", str(credentials)), \
                 patch.object(video, "_heygen_direct_opener", return_value=Opener()), \
                 patch.object(Path, "stat", autospec=True, side_effect=self._private_stat), \
                 patch.object(video.os, "fchmod", create=True), \
                 patch.object(video.time, "time", return_value=1000):
                self.assertEqual(video._heygen_mcp_access_token(), "last-access")
            saved = json.loads(credentials.read_text())
            self.assertEqual(saved["refresh_token"], "")
            with patch.object(video, "_HEYGEN_MCP_CREDENTIALS", str(credentials)), \
                 patch.object(Path, "stat", autospec=True, side_effect=self._private_stat), \
                 patch.object(video.os, "fchmod", create=True), \
                 patch.object(video.time, "time", return_value=5000):
                with self.assertRaisesRegex(video.HeyGenMCPAuthError, "不可刷新"):
                    video._heygen_mcp_access_token()
            self.assertEqual(len(requests), 1)

    def test_mcp_transport_sets_cloudflare_safe_user_agent(self):
        requests = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'event: message\ndata: {"jsonrpc":"2.0","id":"x","result":{"content":[{"type":"text","text":"{\\"ok\\":true}"}],"isError":false}}\n\n'

        class Opener:
            def open(self, request, **_kwargs):
                requests.append(request)
                return Response()

        with patch.object(video, "_heygen_mcp_access_token", return_value="token"), \
             patch.object(video, "_heygen_direct_opener", return_value=Opener()):
            self.assertEqual(video._heygen_mcp_call("get_current_user", {}), {"ok": True})
        self.assertEqual(requests[0].get_header("User-agent"), "huangque-content/1.0")

    def test_mcp_asset_contract_uses_live_tools_list_schema(self):
        schema = {
            "type": "object",
            "properties": {
                "filename": {"type": "string"},
                "contentType": {"type": "string"},
                "sizeBytes": {"type": "integer"},
            },
            "required": ["filename", "sizeBytes"],
        }
        with patch.object(video, "_heygen_mcp_rpc", return_value={
                "tools": [{"name": "create_asset_upload", "inputSchema": schema}]
        }) as rpc:
            self.assertIs(video._heygen_mcp_asset_upload_contract(), schema)
        rpc.assert_called_once_with("tools/list", {}, timeout=30)

    def test_mcp_asset_contract_rejects_unknown_required_fields(self):
        schema = {
            "type": "object",
            "properties": {
                "filename": {"type": "string"},
                "contentType": {"type": "string"},
                "sizeBytes": {"type": "integer"},
                "workspaceSecret": {"type": "string"},
            },
            "required": ["filename", "sizeBytes", "workspaceSecret"],
        }
        with patch.object(video, "_heygen_mcp_rpc", return_value={
                "tools": [{"name": "create_asset_upload", "inputSchema": schema}]
        }), patch.object(video, "_heygen_mcp_call") as call:
            with self.assertRaisesRegex(
                    video.HeyGenMCPContractError, "契约已更新"):
                video._heygen_mcp_asset_upload_contract()
        call.assert_not_called()

    def test_mcp_asset_validation_error_is_redacted_and_pre_billing(self):
        private = "https://private.example/user-material.png?secret=value"
        detail = "Input validation error: filename field required " + private
        with patch.object(video, "_heygen_mcp_rpc", return_value={
                "content": [{"type": "text", "text": detail}], "isError": True,
        }):
            with self.assertRaises(video.HeyGenMCPContractError) as rejected:
                video._heygen_mcp_call("create_asset_upload", {})
        self.assertNotIn(private, str(rejected.exception))
        self.assertIn("视频任务尚未提交", str(rejected.exception))

    def test_cinematic_create_and_poll_use_exact_mcp_contract(self):
        calls = []

        def call(tool, arguments, timeout=90):
            calls.append((tool, arguments))
            if tool == "create_video_from_cinematic_avatar":
                return {"video_id": "mcp-video-1"}
            return {"id": "mcp-video-1", "status": "completed", "video_url": "https://example/video.mp4"}

        with patch.object(video, "_HEYGEN_MCP_CREDENTIALS", "/secure/heygen-mcp.json"), \
             patch.object(video, "_heygen_mcp_call", side_effect=call):
            video_id = video._heygen_create_cinematic_video(
                ["look-1"], ["asset-1"], "16:9", "720p", 13,
                prompt="模仿参考动作", enhance_prompt=False,
            )
            info = video._heygen_poll_video(video_id, deadline_s=30, mcp=True)

        self.assertEqual(video_id, "mcp-video-1")
        self.assertEqual(info["video_url"], "https://example/video.mp4")
        self.assertEqual(calls[0], ("create_video_from_cinematic_avatar", {
            "prompt": "模仿参考动作", "avatarId": ["look-1"],
            "aspectRatio": "16:9", "resolution": "720p", "autoDuration": False,
            "duration": 13, "enhancePrompt": False, "title": "follow_reference_motion",
            "references": [{"type": "asset_id", "asset_id": "asset-1"}],
        }))
        self.assertEqual(calls[1], ("get_video", {"videoId": "mcp-video-1"}))

    def test_plain_video_create_uses_plan_credits_via_exact_mcp_contract(self):
        with patch.object(video, "_HEYGEN_MCP_CREDENTIALS", "/secure/heygen-mcp.json"), \
             patch.object(video, "_heygen_mcp_call", return_value={"video_id": "plain-mcp-1"}) as call:
            video_id = video._heygen_create_video(
                "image-asset", "audio-asset", "720p", "9:16", "medium", direct=True,
                route="mcp_oauth", image_url="https://files.heygen.com/image.jpg",
                audio_url="https://files.heygen.com/audio.mp3")
        self.assertEqual(video_id, "plain-mcp-1")
        arguments = call.call_args.args[1]
        self.assertEqual(call.call_args.args[0], "create_video_from_image")
        self.assertEqual(arguments, {
            "title": arguments["title"],
            "image": {"type": "url", "url": "https://files.heygen.com/image.jpg"},
            "audioUrl": "https://files.heygen.com/audio.mp3",
            "resolution": "720p", "aspectRatio": "9:16",
            "fit": "cover", "expressiveness": "medium", "outputFormat": "mp4",
        })

    def test_mcp_assets_use_signed_put_complete_bulk_status_and_get_url(self):
        calls = []
        puts = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b""

        class Opener:
            def open(self, request, **_kwargs):
                puts.append(request)
                return Response()

        def call(tool, arguments, timeout=90):
            calls.append((tool, arguments))
            if tool == "create_asset_upload":
                suffix = "image" if arguments["contentType"].startswith("image/") else "audio"
                return {
                    "assetId": suffix + "-asset",
                    "uploadUrl": "https://upload.heygen.com/signed/" + suffix,
                }
            if tool == "bulk_asset_statuses":
                return {"assets": [
                    {"assetId": "image-asset", "status": "completed"},
                    {"assetId": "audio-asset", "status": "completed"},
                ]}
            if tool == "get_asset":
                return {"assetId": arguments["assetId"],
                        "url": "https://files.heygen.com/" + arguments["assetId"]}
            return {"ok": True}

        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "avatar.jpg"
            audio = Path(directory) / "speech.mp3"
            image.write_bytes(b"\xff\xd8\xff\xe0" + b"x" * 32)
            audio.write_bytes(b"ID3" + b"x" * 32)
            contract = {
                "type": "object",
                "properties": {
                    "filename": {"type": "string"},
                    "contentType": {"type": "string"},
                    "sizeBytes": {"type": "integer"},
                },
                "required": ["filename", "sizeBytes"],
            }
            with patch.object(video, "_heygen_mcp_asset_upload_contract",
                              return_value=contract), \
                 patch.object(video, "_heygen_mcp_call", side_effect=call), \
                 patch.object(video, "_heygen_mcp_presigned_upload_opener",
                              return_value=Opener()), \
                 patch.object(video, "_heygen_direct_opener") as api_opener:
                result = video._heygen_mcp_prepare_assets(image, audio)

        self.assertEqual(result, {
            "image_asset_id": "image-asset", "audio_asset_id": "audio-asset",
            "image_url": "https://files.heygen.com/image-asset",
            "audio_url": "https://files.heygen.com/audio-asset",
        })
        self.assertEqual(len(puts), 2)
        self.assertTrue(all(request.get_method() == "PUT" for request in puts))
        create_calls = [arguments for tool, arguments in calls
                        if tool == "create_asset_upload"]
        self.assertEqual(create_calls, [
            {"filename": "avatar.jpg", "contentType": "image/jpeg", "sizeBytes": 36},
            {"filename": "speech.mp3", "contentType": "audio/mpeg", "sizeBytes": 35},
        ])
        self.assertTrue(all("fileName" not in arguments for arguments in create_calls))
        self.assertEqual([request.get_header("Content-length") for request in puts],
                         ["36", "35"])
        self.assertIn(("complete_asset_upload", {"assetId": "image-asset"}), calls)
        self.assertIn(("complete_asset_upload", {"assetId": "audio-asset"}), calls)
        self.assertIn(("bulk_asset_statuses", {
            "assetIds": "image-asset,audio-asset",
        }), calls)
        api_opener.assert_not_called()

    def test_mcp_presigned_upload_bypasses_process_proxy(self):
        with patch.object(video.urllib.request, "build_opener") as build:
            video._heygen_mcp_presigned_upload_opener()
        build.assert_called_once()
        proxy_handler = build.call_args.args[0]
        self.assertIsInstance(proxy_handler, video.urllib.request.ProxyHandler)
        self.assertEqual({}, proxy_handler.proxies)

    def test_mcp_presigned_403_is_sanitized_and_never_completed(self):
        private_url = "https://storage.example/upload?signature=private-value"
        contract = {
            "type": "object",
            "properties": {
                "filename": {"type": "string"},
                "contentType": {"type": "string"},
                "sizeBytes": {"type": "integer"},
            },
            "required": ["filename", "sizeBytes"],
        }

        class Opener:
            def open(self, request, **_kwargs):
                raise urllib.error.HTTPError(
                    request.full_url, 403, "Forbidden", {},
                    io.BytesIO(b"<Error><Code>AccessDenied</Code></Error>"),
                )

        calls = []

        def call(tool, arguments, timeout=90):
            calls.append((tool, arguments))
            return {"assetId": "asset-403", "uploadUrl": private_url}

        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "avatar.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 24)
            with patch.object(video, "_heygen_mcp_call", side_effect=call), \
                 patch.object(video, "_heygen_mcp_presigned_upload_opener",
                              return_value=Opener()), \
                 patch("builtins.print") as log:
                with self.assertRaisesRegex(
                        RuntimeError, "HTTP 403 AccessDenied") as rejected:
                    video._heygen_mcp_begin_asset(image, "image/png", contract)

        text = " ".join(str(value) for call_args in log.call_args_list
                        for value in call_args.args)
        self.assertIn("storage.example", text)
        self.assertIn("AccessDenied", text)
        self.assertNotIn("private-value", text)
        self.assertNotIn("private-value", str(rejected.exception))
        self.assertEqual(["create_asset_upload"], [tool for tool, _ in calls])

    def test_mcp_presigned_5xx_retries_same_asset_then_completes_once(self):
        contract = {
            "type": "object",
            "properties": {
                "filename": {"type": "string"},
                "contentType": {"type": "string"},
                "sizeBytes": {"type": "integer"},
            },
            "required": ["filename", "sizeBytes"],
        }
        requests = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b""

        class Opener:
            def open(self, request, **_kwargs):
                requests.append(request.full_url)
                if len(requests) == 1:
                    raise urllib.error.HTTPError(
                        request.full_url, 503, "Unavailable", {},
                        io.BytesIO(b"<Error><Code>SlowDown</Code></Error>"),
                    )
                return Response()

        calls = []

        def call(tool, arguments, timeout=90):
            calls.append((tool, arguments))
            if tool == "create_asset_upload":
                return {
                    "assetId": "asset-retry",
                    "uploadUrl": "https://storage.example/signed?secret=hidden",
                }
            return {"ok": True}

        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "avatar.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 24)
            with patch.object(video, "_HEYGEN_MCP_UPLOAD_ATTEMPTS", 2), \
                 patch.object(video, "_heygen_mcp_call", side_effect=call), \
                 patch.object(video, "_heygen_mcp_presigned_upload_opener",
                              return_value=Opener()), \
                 patch("builtins.print"):
                self.assertEqual(
                    "asset-retry",
                    video._heygen_mcp_begin_asset(image, "image/png", contract),
                )

        self.assertEqual(2, len(requests))
        self.assertEqual(requests[0], requests[1])
        self.assertEqual(1, sum(tool == "create_asset_upload" for tool, _ in calls))
        self.assertEqual(1, sum(tool == "complete_asset_upload" for tool, _ in calls))

    def test_mcp_asset_short_read_stops_before_provider_call(self):
        contract = {
            "type": "object",
            "properties": {
                "filename": {"type": "string"},
                "contentType": {"type": "string"},
                "sizeBytes": {"type": "integer"},
            },
            "required": ["filename", "sizeBytes"],
        }
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "avatar.png"
            image.write_bytes(b"123456")
            with patch.object(Path, "read_bytes", return_value=b"123"), \
                 patch.object(video, "_heygen_mcp_call") as call:
                with self.assertRaisesRegex(ValueError, "读取不完整"):
                    video._heygen_mcp_begin_asset(image, "image/png", contract)
        call.assert_not_called()

    def test_mcp_missing_and_empty_assets_stop_before_provider_call(self):
        contract = {
            "type": "object",
            "properties": {
                "filename": {"type": "string"},
                "contentType": {"type": "string"},
                "sizeBytes": {"type": "integer"},
            },
            "required": ["filename", "sizeBytes"],
        }
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.png"
            empty = Path(directory) / "empty.png"
            empty.write_bytes(b"")
            with patch.object(video, "_heygen_mcp_call") as call:
                with self.assertRaisesRegex(ValueError, "素材文件不存在"):
                    video._heygen_mcp_begin_asset(missing, "image/png", contract)
                with self.assertRaisesRegex(ValueError, "读取不完整"):
                    video._heygen_mcp_begin_asset(empty, "image/png", contract)
        call.assert_not_called()

    def test_mcp_asset_failure_or_timeout_never_reaches_create(self):
        with patch.object(video, "_heygen_mcp_call", return_value={
                "assets": [{"assetId": "a", "status": "failed"}]
        }) as call, patch.object(video.time, "monotonic", side_effect=[0, 0]):
            with self.assertRaisesRegex(RuntimeError, "素材处理失败"):
                video._heygen_mcp_wait_assets(["a"])
        call.assert_called_once_with(
            "bulk_asset_statuses", {"assetIds": "a"}, timeout=30,
        )

        with patch.object(video, "_HEYGEN_MCP_ASSET_TIMEOUT", 0), \
             patch.object(video, "_heygen_mcp_call") as poll, \
             patch.object(video.time, "monotonic", return_value=0):
            with self.assertRaisesRegex(TimeoutError, "素材处理超时"):
                video._heygen_mcp_wait_assets(["a"])
        poll.assert_not_called()

    def test_mcp_resolution_is_downgraded_before_paid_create(self):
        with patch.object(video, "_HEYGEN_MCP_MAX_RESOLUTION", "720p"):
            self.assertEqual(
                video._heygen_actual_resolution("1080p", "mcp_oauth"), "720p",
            )
            self.assertEqual(
                video._heygen_actual_resolution("720p", "mcp_oauth"), "720p",
            )
            self.assertEqual(
                video._heygen_actual_resolution("1080p", "api_wallet"), "1080p",
            )

    def test_mcp_resume_polls_same_account_without_upload_or_create(self):
        state = {
            "provider": "mcp_oauth", "provider_transport": "mcp",
            "provider_video_id": "oauth-video", "actual_resolution": "720p",
            "image_asset_id": "img", "audio_asset_id": "aud",
        }
        lifecycle = {"state": state}
        with patch.object(video, "_heygen_mcp_prepare_assets") as prepare, \
             patch.object(video, "_heygen_create_video") as create, \
             patch.object(video, "_heygen_request_json") as api, \
             patch.object(video, "_heygen_poll_video", return_value={
                 "status": "completed", "video_url": "https://files.heygen.com/video.mp4",
             }) as poll, \
             patch.object(video, "_download_video_file_direct", return_value="video/result.mp4"), \
             patch.object(video, "_extract_first_frame_cover", return_value=None):
            result = video.generate_heygen_video_recoverable(
                "missing", "missing", "1080p", "9:16", "medium", lifecycle,
            )
        prepare.assert_not_called()
        create.assert_not_called()
        api.assert_not_called()
        self.assertTrue(poll.call_args.kwargs["mcp"])
        self.assertEqual(result["provider"], "mcp_oauth")
        self.assertEqual(result["actual_resolution"], "720p")

    def test_mcp_new_job_persists_route_before_single_create(self):
        events = []
        lifecycle = {
            "state": {},
            "on_submitting": lambda data: events.append(("submitting", data)),
            "on_submitted": lambda data: events.append(("submitted", data)),
            "on_completed": lambda data: events.append(("completed", data)),
        }
        assets = {
            "image_asset_id": "oauth-image", "audio_asset_id": "oauth-audio",
            "image_url": "https://files.heygen.com/image.jpg",
            "audio_url": "https://files.heygen.com/audio.mp3",
        }
        with patch.object(video, "_HEYGEN_MCP_CREDENTIALS", "/secure/oauth.json"), \
             patch.object(video, "_HEYGEN_MCP_MAX_RESOLUTION", "720p"), \
             patch.object(video, "preflight_heygen_image_file", return_value={
                 "path": Path("avatar.jpg"), "mime": "image/jpeg",
             }), \
             patch.object(video, "preflight_heygen_audio_file", return_value=Path("speech.mp3")), \
             patch.object(video, "_ensure_heygen_audio_mp3", return_value=Path("speech.mp3")), \
             patch.object(video, "_heygen_mcp_prepare_assets", return_value=assets), \
             patch.object(video, "_heygen_create_video", return_value="oauth-video") as create, \
             patch.object(video, "_heygen_poll_video", return_value={
                 "status": "completed", "video_url": "https://files.heygen.com/video.mp4",
             }) as poll, \
             patch.object(video, "_download_video_file_direct", return_value="video/result.mp4"), \
             patch.object(video, "_extract_first_frame_cover", return_value=None):
            result = video.generate_heygen_video_recoverable(
                "avatar.jpg", "speech.mp3", "1080p", "9:16", "medium", lifecycle,
            )

        create.assert_called_once()
        self.assertEqual(create.call_args.kwargs["route"], "mcp_oauth")
        self.assertEqual(create.call_args.args[2], "720p")
        self.assertEqual(create.call_args.kwargs["image_url"], assets["image_url"])
        self.assertEqual(create.call_args.kwargs["audio_url"], assets["audio_url"])
        self.assertTrue(poll.call_args.kwargs["mcp"])
        self.assertEqual([name for name, _ in events], [
            "submitting", "submitted", "completed",
        ])
        self.assertEqual(events[0][1]["provider"], "mcp_oauth")
        self.assertEqual(events[0][1]["actual_resolution"], "720p")
        self.assertEqual(result["actual_resolution"], "720p")

    def test_mcp_material_failure_stops_before_paid_create(self):
        lifecycle = {"state": {}, "on_submitting": lambda _data: self.fail(
            "provider boundary must not be entered")}
        with patch.object(video, "_HEYGEN_MCP_CREDENTIALS", "/secure/oauth.json"), \
             patch.object(video, "preflight_heygen_image_file", return_value={
                 "path": Path("avatar.jpg"), "mime": "image/jpeg",
             }), \
             patch.object(video, "preflight_heygen_audio_file", return_value=Path("speech.mp3")), \
             patch.object(video, "_ensure_heygen_audio_mp3", return_value=Path("speech.mp3")), \
             patch.object(video, "_heygen_mcp_prepare_assets",
                          side_effect=RuntimeError("素材处理失败")), \
             patch.object(video, "_heygen_create_video") as create:
            with self.assertRaisesRegex(RuntimeError, "素材处理失败"):
                video.generate_heygen_video_recoverable(
                    "avatar.jpg", "speech.mp3", "720p", "9:16", "medium", lifecycle,
                )
        create.assert_not_called()

    def test_photo_avatar_create_and_status_use_exact_mcp_contract(self):
        calls = []

        def call(tool, arguments, timeout=90):
            calls.append((tool, arguments))
            if tool == "create_photo_avatar":
                return {"avatar_item": {"id": "look-1"}, "avatar_group": {"id": "group-1"}}
            return {"id": "look-1", "status": "completed"}

        with patch.object(video, "_HEYGEN_MCP_CREDENTIALS", "/secure/heygen-mcp.json"), \
             patch.object(video, "_heygen_mcp_call", side_effect=call):
            look_id, group_id = video._heygen_create_photo_avatar("image-asset", direct=True)
            status, message = video._heygen_look_status(look_id, group_id, direct=True)

        self.assertEqual((look_id, group_id, status, message),
                         ("look-1", "group-1", "completed", ""))
        self.assertEqual(calls[0][0], "create_photo_avatar")
        self.assertEqual(calls[0][1]["file"], {"type": "asset_id", "asset_id": "image-asset"})
        self.assertEqual(calls[1], ("get_avatar_look", {"lookId": "look-1"}))

    def test_plain_video_oauth_failure_does_not_repeat_on_relay(self):
        with patch.object(video, "_HEYGEN_DIRECT", True), \
             patch.object(video, "HEYGEN_API_KEY", "key"), \
             patch.object(video, "generate_heygen_video_direct",
                          side_effect=video.HeyGenMCPAuthError("不可刷新")), \
             patch.object(video, "_resolve_out_file") as relay:
            with self.assertRaises(video.HeyGenMCPAuthError):
                video.generate_heygen_video("i.jpg", "a.mp3", "1080p", "9:16", "medium")
        relay.assert_not_called()

    def test_plain_video_poll_never_depends_on_mcp_oauth(self):
        failed = {"data": {"id": "plain-video", "status": "failed",
                           "failure_code": "MOVIO_PAYMENT_INSUFFICIENT_CREDIT"}}
        with patch.object(video, "_HEYGEN_MCP_CREDENTIALS", "/secure/heygen-mcp.json"), \
             patch.object(video, "_heygen_mcp_call") as mcp_call, \
             patch.object(video, "_heygen_request_json", return_value=failed):
            with self.assertRaisesRegex(RuntimeError, "MOVIO_PAYMENT_INSUFFICIENT_CREDIT"):
                video._heygen_poll_video("plain-video", deadline_s=30)
        mcp_call.assert_not_called()

    def test_mcp_poll_errors_never_cross_account_or_repeat_create(self):
        errors = [
            video.HeyGenMCPAuthError("HTTP 401"),
            video.HeyGenMCPAuthError("HTTP 403"),
            RuntimeError("HTTP 404"),
            RuntimeError("HTTP 500"),
        ]
        for error in errors:
            with self.subTest(error=str(error)), \
                 patch.object(video, "_heygen_mcp_call", side_effect=error), \
                 patch.object(video, "_heygen_request_json") as api_get, \
                 patch.object(video, "_heygen_create_video") as create:
                with self.assertRaises(type(error)):
                    video._heygen_poll_video(
                        "mcp-video", direct=True, deadline_s=30, mcp=True,
                    )
            api_get.assert_not_called()
            create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
