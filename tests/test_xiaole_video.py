import base64
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


class XiaoleVideoTests(unittest.TestCase):
    def setUp(self):
        server_dir = str(Path(__file__).resolve().parents[1] / "server")
        if server_dir not in sys.path:
            sys.path.insert(0, server_dir)
        from content_domains import video
        self.video = video

    def test_generate_xiaole_video_sends_size_without_aspect_ratio(self):
        calls = []

        def fake_request(method, path, body=None, timeout=90):
            calls.append((method, path, body, timeout))
            if method == "POST":
                return {"code": 200, "data": {"request_id": "rid-1", "status_url": "/status/rid-1"}}
            return {"data": {"status": "completed", "output": {"videos": [{"url": "https://cdn.example/video.mp4"}]}}}

        with patch.object(self.video, "_xiaole_request", side_effect=fake_request), \
             patch.object(self.video, "_download_xiaole_video", return_value="video/grok_demo.mp4"), \
             patch.object(self.video, "GROK_VIDEO_PROVIDER", "xiaole"):
            result = self.video.generate_xiaole_video("Grok Image Video", "demo", size="1280x720", prefix="grok")

        self.assertEqual(result["video_file"], "video/grok_demo.mp4")
        self.assertEqual(calls[0][2]["input"]["size"], "1280x720")
        self.assertNotIn("aspect_ratio", calls[0][2]["input"])

    def test_xiaole_download_candidates_prefers_tunnel_over_relay(self):
        import os as _os
        url = "https://vidgen.x.ai/abc/video.mp4"
        with patch.dict(_os.environ, {"HEYGEN_RELAY_BASE": "https://heygen.zelong.vip"}, clear=False):
            cands = self.video._xiaole_download_candidates(url, "http://127.0.0.1:10809")
        # ① 快隧道优先：原始 URL + 隧道代理
        self.assertEqual(cands[0][0], url)
        self.assertEqual(cands[0][2], "http://127.0.0.1:10809")
        # ② heygen 中转兜底：走 relay /cdn/，不强制代理(None)
        self.assertIn("heygen.zelong.vip/cdn/vidgen.x.ai/", cands[1][0])
        self.assertIsNone(cands[1][2])
        # ③ 最后直连原始 URL
        self.assertEqual(cands[-1][0], url)
        self.assertIsNone(cands[-1][2])

    def test_xiaole_download_candidates_no_tunnel_is_legacy_order(self):
        import os as _os
        url = "https://vidgen.x.ai/abc/video.mp4"
        with patch.dict(_os.environ, {"HEYGEN_RELAY_BASE": "https://heygen.zelong.vip"}, clear=False):
            cands = self.video._xiaole_download_candidates(url, "")
        # 无隧道 → 退化为老行为：heygen 中转在前、直连兜底，无隧道档
        self.assertNotIn("10809", str(cands))
        self.assertIn("heygen.zelong.vip/cdn/", cands[0][0])
        self.assertIsNone(cands[0][2])
        self.assertEqual(cands[-1][0], url)
        self.assertIsNone(cands[-1][2])

    def test_authenticated_download_header_is_not_forwarded_to_relay(self):
        import os as _os
        url = "https://openrouter.ai/api/v1/videos/job/content?index=0"
        with patch.dict(_os.environ, {"HEYGEN_RELAY_BASE": "https://relay.example"}, clear=False):
            cands = self.video._xiaole_download_candidates(
                url, "http://127.0.0.1:10809",
                origin_headers={"Authorization": "Bearer secret"},
            )
        self.assertEqual(cands[0][1]["Authorization"], "Bearer secret")
        self.assertNotIn("Authorization", cands[1][1])
        self.assertEqual(cands[-1][1]["Authorization"], "Bearer secret")

    def test_cross_origin_redirect_strips_authorization(self):
        import urllib.request
        handler = self.video._OriginAuthRedirectHandler()
        request = urllib.request.Request(
            "https://openrouter.ai/api/v1/videos/job/content",
            headers={"Authorization": "Bearer secret"},
        )
        redirected = handler.redirect_request(
            request, None, 302, "Found", {}, "https://cdn.example/video.mp4"
        )
        self.assertNotIn("Authorization", redirected.headers)

    def test_gen_xiaole_video_maps_ratio_to_size_and_defaults_unknown_ratio(self):
        calls = []

        def fake_request(method, path, body=None, timeout=90):
            calls.append((method, path, body, timeout))
            if method == "POST":
                return {"code": 200, "data": {"request_id": "rid-1", "status_url": "/status/rid-1"}}
            return {"data": {"status": "completed", "output": {"videos": [{"url": "https://cdn.example/video.mp4"}]}}}

        with patch.object(self.video, "_xiaole_request", side_effect=fake_request), \
             patch.object(self.video, "_download_xiaole_video", return_value="video/grok_demo.mp4"), \
             patch.object(self.video, "GROK_VIDEO_PROVIDER", "xiaole"):
            ok = self.video.gen_xiaole_video({"channel": "grok", "prompt": "demo", "ratio": "1:1"})
            fallback = self.video.gen_xiaole_video({"channel": "grok", "prompt": "demo", "ratio": "2:3"})

        self.assertEqual(ok["ratio"], "1:1")
        self.assertEqual(calls[0][2]["input"]["size"], "1024x1024")
        self.assertEqual(fallback["ratio"], "9:16")
        self.assertEqual(calls[2][2]["input"]["size"], "720x1280")
        self.assertNotIn("aspect_ratio", calls[0][2]["input"])
        self.assertNotIn("aspect_ratio", calls[2][2]["input"])

    def test_xiaole_ratio_channel_error_matches_supplier_size_message(self):
        self.assertTrue(self.video._is_xiaole_ratio_channel_error(
            '视频接口失败: HTTP 404 {"code":404,"message":"当前模型暂无支持该视频参数的可用渠道：渠道不支持当前视频尺寸"}'
        ))

    def test_generate_xiaole_video_normalizes_supplier_size_error(self):
        with patch.object(
            self.video,
            "_xiaole_request",
            side_effect=RuntimeError('视频接口失败: HTTP 404 {"code":404,"message":"当前模型暂无支持该视频参数的可用渠道：渠道不支持当前视频尺寸"}')
        ):
            with self.assertRaisesRegex(RuntimeError, "当前仅部分比例可用，请优先尝试 16:9（横屏）"):
                self.video.generate_xiaole_video("Grok Image Video", "demo", size="720x1280", prefix="grok")

    def test_validate_micro_duration(self):
        for duration in (5, 10, 15):
            body = self.video.validate_xiaole_video_payload({
                "channel": "micro", "prompt": "cinematic demo", "duration": duration,
            })
            self.assertEqual(body["duration"], duration)
        with self.assertRaisesRegex(ValueError, "5、10 或 15"):
            self.video.validate_xiaole_video_payload({
                "channel": "micro", "prompt": "cinematic demo", "duration": 7,
            })

    def test_validate_micro_uses_official_seedance_contract(self):
        body = self.video.validate_xiaole_video_payload({
            "channel": "micro", "prompt": "cinematic demo", "duration": 15,
            "reference_images": ["https://example.com/ref.jpg"],
        })
        self.assertEqual(body["model"], "doubao-seedance-2-0-260128")
        self.assertEqual(body["duration"], 15)
        self.assertEqual(body["ratio"], "9:16")
        self.assertEqual(body["resolution"], "720p")

    @staticmethod
    def _seedance_png_data(tag=b""):
        import io as _io
        from PIL import Image
        shade = (tag[0] if tag else 0) % 256
        buf = _io.BytesIO()
        Image.new("RGB", (8, 8), (shade, 128, 255 - shade)).save(buf, "PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")

    def _stage_mocks(self, put_side_effect=None):
        from content_domains import cos

        signed = "https://bucket-1250000000.cos.ap-guangzhou.myqcloud.com/seedance/reference/x?q-sign-algorithm=sha1&q-sign-time=1"
        return [
            patch.object(self.video, "seedance_reference_upload_is_open", return_value=True),
            patch.object(cos, "enabled", return_value=True),
            patch.object(cos, "put_bytes", side_effect=put_side_effect),
            patch.object(self.video, "_seedance_cos_presign", return_value=signed),
        ]

    def test_validate_micro_keeps_data_images_local_until_staging(self):
        from content_domains import cos

        refs = [self._seedance_png_data(str(index).encode("ascii")) for index in range(4)]
        with patch.object(cos, "put_bytes") as put:
            body = self.video.validate_xiaole_video_payload({
                "channel": "micro", "prompt": "demo", "duration": 5,
                "reference_images": refs,
            }, "fang")
        put.assert_not_called()   # 校验阶段不做任何网络上传
        self.assertEqual(refs, body["reference_images"])

    def test_stage_seedance_references_uploads_private_and_returns_signed_urls(self):
        refs = [self._seedance_png_data(str(index).encode("ascii")) for index in range(4)]
        patches = self._stage_mocks()
        with patches[0], patches[1], patches[2] as put, patches[3]:
            first = self.video.validate_xiaole_video_payload({
                "channel": "micro", "prompt": "demo", "duration": 5, "reference_images": refs}, "fang")
            second = self.video.validate_xiaole_video_payload({
                "channel": "micro", "prompt": "demo", "duration": 5, "reference_images": refs}, "fang")
            first_keys = self.video.stage_seedance_references(first, "fang")
            second_keys = self.video.stage_seedance_references(second, "fang")

        self.assertEqual(8, put.call_count)
        for call in put.call_args_list:
            self.assertIs(call.kwargs.get("private"), True)   # 强制私有 ACL
        first_keys_uploaded = [call.args[1] for call in put.call_args_list[:4]]
        second_keys_uploaded = [call.args[1] for call in put.call_args_list[4:]]
        self.assertEqual(first_keys_uploaded, second_keys_uploaded)   # 确定性对象键
        self.assertEqual(4, len(set(first_keys_uploaded)))
        self.assertRegex(first_keys_uploaded[0], r"^seedance/reference/[0-9a-f]{16}/[0-9a-f]{64}\.png$")
        self.assertEqual(first_keys, first_keys_uploaded)
        self.assertEqual(first["reference_images"], second["reference_images"])
        for url in first["reference_images"]:
            self.assertTrue(url.startswith("https://"))
            self.assertIn("q-sign-algorithm", url)   # 短期签名 URL，不是裸公开直链
        self.assertNotIn("data:", str(first["reference_images"]))

    def test_stage_seedance_references_partial_failure_cleans_uploaded_batch(self):
        refs = [self._seedance_png_data(str(index).encode("ascii")) for index in range(3)]
        patches = self._stage_mocks(put_side_effect=[None, None, RuntimeError("cos boom")])
        with patches[0], patches[1], patches[2] as put, patches[3], \
             patch.object(self.video, "_seedance_cos_delete") as delete:
            body = self.video.validate_xiaole_video_payload({
                "channel": "micro", "prompt": "demo", "duration": 5, "reference_images": refs}, "fang")
            with self.assertRaisesRegex(self.video.SeedanceReferenceUnavailable, "上传失败.*未扣点"):
                self.video.stage_seedance_references(body, "fang")

        self.assertEqual(3, put.call_count)
        uploaded_keys = [call.args[1] for call in put.call_args_list[:2]]
        self.assertEqual(2, delete.call_count)   # 本批已上传对象必须全部清理
        self.assertEqual(uploaded_keys, [call.args[0] for call in delete.call_args_list])
        self.assertEqual(refs, body["reference_images"])   # 失败不回退、不改写

    def test_stage_seedance_references_unavailable_is_explicit(self):
        from content_domains import cos

        with patch.object(self.video, "seedance_reference_upload_is_open", return_value=False), \
             patch.object(cos, "put_bytes") as put:
            body = {"channel": "micro", "reference_images": [self._seedance_png_data()]}
            with self.assertRaisesRegex(self.video.SeedanceReferenceUnavailable, "未配置.*未扣点"):
                self.video.stage_seedance_references(body, "fang")
        put.assert_not_called()

    def test_stage_seedance_references_upload_failure_never_falls_back_to_data_url(self):
        patches = self._stage_mocks(put_side_effect=ModuleNotFoundError("qcloud_cos"))
        ref = self._seedance_png_data()
        with patches[0], patches[1], patches[2], patches[3]:
            body = {"channel": "micro", "reference_images": [ref]}
            with self.assertRaisesRegex(self.video.SeedanceReferenceUnavailable, "上传失败.*未扣点"):
                self.video.stage_seedance_references(body, "fang")
        self.assertEqual([ref], body["reference_images"])   # body 未被半成品 URL 污染

    def test_stage_seedance_references_skips_non_micro_channels(self):
        from content_domains import cos

        with patch.object(cos, "put_bytes") as put:
            body = {"channel": "grok", "reference_images": ["data:image/png;base64,AAAA"]}
            self.assertEqual([], self.video.stage_seedance_references(body, "fang"))
        put.assert_not_called()

    def test_cleanup_staged_seedance_references_is_best_effort(self):
        with patch.object(self.video, "_seedance_cos_delete",
                          side_effect=[None, RuntimeError("already gone")]) as delete:
            self.video.cleanup_staged_seedance_references(["k1", "k2"])
        self.assertEqual(2, delete.call_count)   # 单个失败不阻断其余清理

    def test_validate_micro_accepts_public_and_authorized_asset_references(self):
        import tempfile
        from content_domains import assets_store

        with tempfile.TemporaryDirectory() as td, \
             patch.object(assets_store, "ASSET_DB", str(Path(td) / "assets.db")), \
             patch.object(assets_store, "_initialized", False):
            assets_store.init_assets()
            from contextlib import closing as _closing
            with _closing(assets_store.adb()) as c:
                c.execute("INSERT INTO assets(id,kind,stage,username,created_at) VALUES(120,'collect','material','fang',1)")
                c.execute("INSERT INTO assets(id,kind,stage,username,created_at) VALUES(130,'collect','material','other',1)")
                c.commit()
            refs = ["https://cdn.example/ref.jpg", "asset://asset-120"]
            body = self.video.validate_xiaole_video_payload({
                "channel": "micro", "prompt": "demo", "duration": 5,
                "reference_images": refs,
            }, "fang")
            self.assertEqual(refs, body["reference_images"])
            for ref, owner in (("asset://asset-130", "fang"),     # 别人的素材
                               ("asset://asset-990", "fang"),     # 不存在
                               ("asset://asset-120", "other")):   # 归属不符
                with self.subTest(ref=ref, owner=owner):
                    with self.assertRaisesRegex(ValueError, "不存在或未授权"):
                        self.video.validate_xiaole_video_payload({
                            "channel": "micro", "prompt": "demo", "duration": 5,
                            "reference_images": [ref],
                        }, owner)

    def test_validate_micro_rejects_local_or_malformed_references(self):
        for ref in ("/api/gen/file/ref.jpg", "file:///tmp/ref.jpg", "http://127.0.0.1/ref.jpg",
                    "http://localhost/ref.jpg", "asset://reference/2"):
            with self.subTest(ref=ref):
                with self.assertRaisesRegex(ValueError, "公网|asset://"):
                    self.video.validate_xiaole_video_payload({
                        "channel": "micro", "prompt": "demo", "duration": 5,
                        "reference_images": [ref],
                    }, "fang")

    def test_validate_micro_rejects_spoofed_or_corrupt_images(self):
        import io as _io
        from PIL import Image

        def data_url(mime, raw):
            return "data:%s;base64,%s" % (mime, base64.b64encode(raw).decode("ascii"))

        png_buf, jpg_buf = _io.BytesIO(), _io.BytesIO()
        Image.new("RGB", (8, 8)).save(png_buf, "PNG")
        Image.new("RGB", (8, 8)).save(jpg_buf, "JPEG")
        truncated_jpg = jpg_buf.getvalue()[:-64]
        cases = [
            data_url("image/png", jpg_buf.getvalue()),     # 损坏/伪装探针：JPEG 声明成 image/png
            data_url("image/jpeg", png_buf.getvalue()),    # PNG 声明成 image/jpeg
            data_url("image/jpeg", truncated_jpg),         # 截断的 JPEG
            data_url("image/png", b"\x89PNG\r\n\x1a\n" + b"not-a-real-png"),  # 只有魔数
        ]
        for ref in cases:
            with self.subTest(ref=ref[:40]):
                with self.assertRaisesRegex(ValueError, "无效|损坏|不一致"):
                    self.video.validate_xiaole_video_payload({
                        "channel": "micro", "prompt": "demo", "duration": 5,
                        "reference_images": [ref],
                    }, "fang")

    def test_validate_micro_fails_closed_when_pillow_missing(self):
        ref = self._seedance_png_data()
        with patch.dict(sys.modules, {"PIL": None, "PIL.Image": None}):
            with self.assertRaisesRegex(self.video.SeedanceReferenceUnavailable, "校验组件不可用.*未扣点"):
                self.video.validate_xiaole_video_payload({
                    "channel": "micro", "prompt": "demo", "duration": 5,
                    "reference_images": [ref],
                }, "fang")

    def test_seedance_worker_defense_rejects_unstaged_data_url(self):
        with patch("content_domains.video_seedance.generate") as generate:
            with self.assertRaisesRegex(ValueError, "公网 URL"):
                self.video.gen_xiaole_video({
                    "channel": "micro", "prompt": "demo", "duration": 5,
                    "reference_images": [self._seedance_png_data()],
                })
        generate.assert_not_called()

    def test_seedance_reference_health_requires_cos_and_sdk(self):
        from content_domains import cos

        with patch.object(cos, "enabled", return_value=True), \
             patch.object(self.video.importlib.util, "find_spec", return_value=object()):
            self.assertTrue(self.video.seedance_reference_upload_is_open())
        with patch.object(cos, "enabled", return_value=False):
            self.assertFalse(self.video.seedance_reference_upload_is_open())
        with patch.object(cos, "enabled", return_value=True), \
             patch.object(self.video.importlib.util, "find_spec", return_value=None):
            self.assertFalse(self.video.seedance_reference_upload_is_open())

    def test_content_service_dependency_manifest_pins_cos_sdk(self):
        root = Path(__file__).resolve().parents[1]
        requirements = (root / "deploy/requirements-content.txt").read_text(encoding="utf-8")
        self.assertIn("cos-python-sdk-v5==1.9.44", requirements)

    def test_gen_micro_uses_official_seedance_without_shared_provider(self):
        fake = {
            "request_id": "seedance-1",
            "source_video_url": "https://example.com/micro.mp4",
            "model": "doubao-seedance-2-0-260128",
            "duration": 15,
        }
        payload = self.video.validate_xiaole_video_payload({
            "channel": "micro",
            "prompt": "cinematic demo",
            "duration": 15,
            "ratio": "4:3",
            "resolution": "1080p",
        })
        with patch("content_domains.video_seedance.generate", return_value=fake) as generate, \
             patch.object(self.video, "_download_xiaole_video", return_value="video/seedance.mp4"), \
             patch.object(self.video, "_extract_first_frame_cover", return_value=None):
            result = self.video.gen_xiaole_video(payload)
        self.assertEqual(generate.call_args.kwargs["duration"], 15)
        self.assertEqual(generate.call_args.kwargs["model"], "doubao-seedance-2-0-260128")
        self.assertEqual(generate.call_args.kwargs["ratio"], "4:3")
        self.assertEqual(generate.call_args.kwargs["resolution"], "1080p")
        self.assertEqual(result["provider_video_id"], "seedance-1")
        self.assertEqual(result["duration"], 15)
        self.assertEqual(result["ratio"], "4:3")
        self.assertEqual(result["resolution"], "1080p")

    def test_validate_official_grok_parameters(self):
        with patch.object(self.video, "GROK_VIDEO_PROVIDER", "xai"):
            body = self.video.validate_xiaole_video_payload({
                "channel": "grok", "prompt": "cinematic demo", "ratio": "2:3",
                "duration": 15, "resolution": "720p", "model": "grok-imagine-video",
            })
        self.assertEqual(body["ratio"], "2:3")
        self.assertEqual(body["duration"], 15)

    def test_validate_official_edit_verifies_server_side_duration(self):
        source = "data:video/mp4;base64,AAAA"
        with patch.object(self.video, "GROK_VIDEO_PROVIDER", "xai"), \
             patch.object(self.video, "_probe_data_video_duration", return_value=8.6):
            body = self.video.validate_xiaole_video_payload({"channel": "grok", "operation": "edit",
                                                              "prompt": "change person", "reference_video_data": source})
        self.assertEqual(body["source_duration"], 8.6)
        self.assertEqual(body["model"], "grok-imagine-video")

    def test_validate_official_edit_rejects_over_8_7_seconds(self):
        with patch.object(self.video, "GROK_VIDEO_PROVIDER", "xai"), \
             patch.object(self.video, "_probe_data_video_duration", return_value=8.71):
            with self.assertRaisesRegex(ValueError, "8.7"):
                self.video.validate_xiaole_video_payload({"channel": "grok", "operation": "edit", "prompt": "demo",
                                                          "reference_video_data": "data:video/mp4;base64,AAAA"})

    def test_validate_official_grok_rejects_over_max_references(self):
        with patch.object(self.video, "GROK_VIDEO_PROVIDER", "xai"):
            n = self.video.XIAOLE_MAX_REF + 1
            with self.assertRaisesRegex(ValueError, "最多支持%d张" % self.video.XIAOLE_MAX_REF):
                self.video.validate_xiaole_video_payload({
                    "channel": "grok", "prompt": "cinematic demo",
                    "reference_images": ["https://a/%d.jpg" % i for i in range(n)],
                })

    def test_validate_official_grok_accepts_multiple_references(self):
        with patch.object(self.video, "GROK_VIDEO_PROVIDER", "xai"):
            cleaned = self.video.validate_xiaole_video_payload({
                "channel": "grok", "prompt": "cinematic demo",
                "reference_images": ["https://a/1.jpg", "https://a/2.jpg", "https://a/3.jpg"],
            })
            self.assertEqual(len(cleaned["reference_images"]), 3)

    def test_validate_video_15_requires_reference(self):
        with patch.object(self.video, "GROK_VIDEO_PROVIDER", "xai"):
            with self.assertRaisesRegex(ValueError, "仅支持图生视频"):
                self.video.validate_xiaole_video_payload({
                    "channel": "grok", "prompt": "cinematic demo",
                    "model": "grok-imagine-video-1.5",
                })

    def test_gen_grok_official_preserves_result_contract(self):
        fake = {
            "request_id": "xai-1", "model": "grok-imagine-video",
            "source_video_url": "https://vidgen.x.ai/demo.mp4", "duration": 10,
        }
        with patch.object(self.video, "GROK_VIDEO_PROVIDER", "xai"), \
             patch("content_domains.video_xai.generate", return_value=fake) as generate, \
             patch.object(self.video, "_download_xiaole_video", return_value="video/grok_xai_demo.mp4"), \
             patch.object(self.video, "_extract_first_frame_cover", return_value="video/grok_xai_demo_cover.jpg"), \
             patch.object(self.video, "public_url", return_value="https://cos.example/cover.jpg"):
            result = self.video.gen_xiaole_video({
                "channel": "grok", "prompt": "cinematic demo", "ratio": "9:16",
                "duration": 10, "resolution": "720p", "model": "grok-imagine-video",
            })
        self.assertEqual(result["video_file"], "video/grok_xai_demo.mp4")
        self.assertEqual(result["provider_video_id"], "xai-1")
        self.assertEqual(result["model"], "grok-imagine-video")
        self.assertEqual(result["duration"], 10)
        generate.assert_called_once()

    def test_grok_uses_openrouter_only_after_safe_xai_create_failure(self):
        from content_domains import video_xai

        fallback = {
            "request_id": "or-1", "model": "grok-imagine-video",
            "source_video_url": "https://openrouter.ai/api/v1/videos/or-1/content",
            "duration": 10, "provider": "openrouter",
        }
        with patch.object(self.video, "GROK_VIDEO_PROVIDER", "xai"), \
             patch("content_domains.video_xai.generate",
                   side_effect=video_xai.XaiCreateUnavailableError("xAI quota")), \
             patch("content_domains.video_openrouter.available", return_value=True), \
             patch("content_domains.video_openrouter.generate", return_value=fallback) as generate, \
             patch("content_domains.video_openrouter.download_headers",
                   return_value={"Authorization": "Bearer test"}), \
             patch.object(self.video, "_download_xiaole_video", return_value="video/grok_or.mp4") as download, \
             patch.object(self.video, "_extract_first_frame_cover", return_value=None):
            result = self.video.gen_xiaole_video({
                "channel": "grok", "prompt": "cinematic demo", "ratio": "9:16",
                "duration": 10, "resolution": "720p", "model": "grok-imagine-video",
            })
        generate.assert_called_once()
        download.assert_called_once_with(
            fallback["source_video_url"], "grok_openrouter",
            origin_headers={"Authorization": "Bearer test"},
        )
        self.assertEqual(result["provider_video_id"], "or-1")
        self.assertEqual(result["video_file"], "video/grok_or.mp4")

    def test_missing_openrouter_key_rethrows_original_xai_error(self):
        from content_domains import video_xai

        original = video_xai.XaiCreateUnavailableError("xAI quota")
        with patch.object(self.video, "GROK_VIDEO_PROVIDER", "xai"), \
             patch("content_domains.video_xai.generate", side_effect=original), \
             patch("content_domains.video_openrouter.available", return_value=False), \
             patch("content_domains.video_openrouter.generate") as fallback:
            with self.assertRaises(video_xai.XaiCreateUnavailableError) as raised:
                self.video.gen_xiaole_video({
                    "channel": "grok", "prompt": "cinematic demo", "ratio": "9:16",
                    "duration": 10, "resolution": "720p", "model": "grok-imagine-video",
                })
        self.assertIs(raised.exception, original)
        fallback.assert_not_called()

    def test_grok_does_not_fallback_after_ambiguous_xai_network_failure(self):
        with patch.object(self.video, "GROK_VIDEO_PROVIDER", "xai"), \
             patch("content_domains.video_xai.generate",
                   side_effect=RuntimeError("xAI视频网络异常: connection reset")), \
             patch("content_domains.video_openrouter.generate") as fallback:
            with self.assertRaisesRegex(RuntimeError, "网络异常"):
                self.video.gen_xiaole_video({
                    "channel": "grok", "prompt": "cinematic demo", "ratio": "9:16",
                    "duration": 10, "resolution": "720p", "model": "grok-imagine-video",
                })
        fallback.assert_not_called()

    def test_grok_does_not_fallback_after_xai_poll_credential_failure(self):
        from content_domains import video_xai

        with patch.object(self.video, "GROK_VIDEO_PROVIDER", "xai"), \
             patch("content_domains.video_xai.generate",
                   side_effect=video_xai.XaiCredentialError("poll token expired")), \
             patch("content_domains.video_openrouter.generate") as fallback:
            with self.assertRaises(video_xai.XaiCredentialError):
                self.video.gen_xiaole_video({
                    "channel": "grok", "prompt": "cinematic demo", "ratio": "9:16",
                    "duration": 10, "resolution": "720p", "model": "grok-imagine-video",
                })
        fallback.assert_not_called()

    def test_grok_does_not_fallback_after_successful_xai_download_failure(self):
        generated = {
            "request_id": "xai-1", "model": "grok-imagine-video",
            "source_video_url": "https://vidgen.x.ai/demo.mp4", "duration": 10,
        }
        with patch.object(self.video, "GROK_VIDEO_PROVIDER", "xai"), \
             patch("content_domains.video_xai.generate", return_value=generated), \
             patch("content_domains.video_openrouter.generate") as fallback, \
             patch.object(self.video, "_download_xiaole_video",
                          side_effect=RuntimeError("视频下载失败")):
            with self.assertRaisesRegex(RuntimeError, "下载失败"):
                self.video.gen_xiaole_video({
                    "channel": "grok", "prompt": "cinematic demo", "ratio": "9:16",
                    "duration": 10, "resolution": "720p", "model": "grok-imagine-video",
                })
        fallback.assert_not_called()

    def test_gen_grok_official_edit_uploads_source_and_preserves_contract(self):
        fake = {"request_id": "edit-1", "model": "grok-imagine-video",
                "source_video_url": "https://vidgen.x.ai/edit.mp4", "duration": 6.2}
        with patch.object(self.video, "GROK_VIDEO_PROVIDER", "xai"), \
             patch.object(self.video, "_save_data_file", return_value="video/source.mp4"), \
             patch.object(self.video, "public_url", side_effect=["https://cos.example/source.mp4", "https://cos.example/cover.jpg"]), \
             patch.object(self.video, "_file_url", return_value="/api/files/video/source.mp4"), \
             patch("content_domains.video_xai.edit", return_value=fake) as edit, \
             patch("content_domains.video_openrouter.generate") as fallback, \
             patch.object(self.video, "_download_xiaole_video", return_value="video/edit.mp4"), \
             patch.object(self.video, "_extract_first_frame_cover", return_value="video/edit_cover.jpg"):
            result = self.video.gen_xiaole_video({"channel": "grok", "operation": "edit", "prompt": "change person",
                                                  "reference_video_data": "data:video/mp4;base64,AAAA", "source_duration": 6.2})
        self.assertEqual(result["operation"], "edit")
        self.assertEqual(result["reference_video_file"], "video/source.mp4")
        self.assertIsNone(result["resolution"])
        edit.assert_called_once()
        fallback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
