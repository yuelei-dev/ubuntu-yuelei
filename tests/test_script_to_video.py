import importlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class ScriptToVideoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server_dir = str(Path(__file__).resolve().parents[1] / "server")
        if server_dir not in sys.path:
            sys.path.insert(0, server_dir)
        cls.script_to_video = importlib.import_module("content_domains.script_to_video")
        cls.video = importlib.import_module("content_domains.video")

    def setUp(self):
        self.orig_gen_video = self.video.gen_video
        self.orig_gen_xiaole_video = self.video.gen_xiaole_video
        self.orig_get_video_avatar = getattr(self.video, "get_video_avatar", None)
        self.orig_get_first_avatar = self.script_to_video._get_first_avatar

    def tearDown(self):
        self.video.gen_video = self.orig_gen_video
        self.video.gen_xiaole_video = self.orig_gen_xiaole_video
        if self.orig_get_video_avatar is not None:
            self.video.get_video_avatar = self.orig_get_video_avatar
        self.script_to_video._get_first_avatar = self.orig_get_first_avatar

    def test_drama_style_routes_to_grok_pipeline(self):
        calls = {}

        def fake_gen_xiaole_video(payload):
            calls["payload"] = payload
            return {"video_url": "https://example.test/drama.mp4"}

        self.video.gen_xiaole_video = fake_gen_xiaole_video
        self.script_to_video._get_first_avatar = lambda username: self.fail("剧情模式不应读取数字人形象")

        result = self.script_to_video.gen_script_to_video({
            "_username": "fang",
            "_job_id": 7,
            "style": "剧情",
            "scenes": [{"scene": "女生走进门店"}, {"scene": "镜头推近展示产品"}],
        })

        self.assertEqual(calls["payload"]["channel"], "grok")
        self.assertEqual(calls["payload"]["_username"], "fang")
        self.assertIn("女生走进门店", calls["payload"]["prompt"])
        self.assertIn("电影质感", calls["payload"]["prompt"])
        self.assertEqual(result["pipeline"], "grok")
        self.assertEqual(result["scene_count"], 2)
        self.assertEqual(result["type"], "script_to_video")

    def test_talking_style_uses_selected_avatar_id(self):
        calls = {}

        def fake_get_video_avatar(username, avatar_id):
            calls["avatar_lookup"] = (username, avatar_id)
            return {"id": avatar_id}

        def fake_gen_video(payload):
            calls["payload"] = payload
            return {"video_url": "https://example.test/talking.mp4"}

        self.video.get_video_avatar = fake_get_video_avatar
        self.video.gen_video = fake_gen_video
        self.script_to_video._get_first_avatar = lambda username: self.fail("显式 avatar_id 时不应回退到首个形象")

        result = self.script_to_video.gen_script_to_video({
            "_username": "fang",
            "_job_id": 8,
            "style": "种草",
            "avatar_id": "42",
            "scenes": [{"line": "第一句"}, {"line": "第二句"}],
        })

        self.assertEqual(calls["avatar_lookup"], ("fang", "42"))
        self.assertEqual(calls["payload"]["avatar_id"], "42")
        self.assertEqual(calls["payload"]["text"], "第一句\n\n第二句")
        self.assertEqual(result["pipeline"], "talking")
        self.assertEqual(result["type"], "script_to_video")

    def test_talking_passes_voice_through_and_defaults(self):
        """voice 参数透传 gen_video（个人音色 vip_xxx）；缺省回落默认音色"""
        calls = {}

        def fake_gen_video(payload):
            calls["payload"] = payload
            return {"video_url": "https://example.test/talking.mp4"}

        self.video.gen_video = fake_gen_video
        self.script_to_video._get_first_avatar = lambda username: {"id": 1}

        self.script_to_video.gen_script_to_video({
            "_username": "fang",
            "style": "口播",
            "voice": "vip_abc123",
            "scenes": [{"line": "第一句"}],
        })
        self.assertEqual(calls["payload"]["voice"], "vip_abc123")

        self.script_to_video.gen_script_to_video({
            "_username": "fang",
            "style": "口播",
            "scenes": [{"line": "第一句"}],
        })
        self.assertEqual(calls["payload"]["voice"], "S_d21F8OR62")

    def test_talking_pipeline_gets_real_duration_settlement(self):
        """run_job 的口播真实时长结算必须覆盖 script_to_video 的 talking 链路（剧情走 grok 不结算）"""
        core_src = (Path(__file__).resolve().parents[1] / "server/content_domains/core.py").read_text(encoding="utf-8")
        self.assertIn('"talking_with_materials"', core_src)

    def test_prepare_payload_reuses_assets_and_counts_only_missing_images(self):
        scenes = [
            {"scene": "外婆在枇杷树下洗果子", "line": "第一句"},
            {"scene": "小女孩接过黄色枇杷", "line": "第二句"},
        ]
        with mock.patch.object(
            self.script_to_video, "_match_image_asset",
            side_effect=["image/old.png", None],
        ):
            body = self.script_to_video.prepare_script_to_video_payload(
                {"scenes": scenes, "style": "口播"}, "fang",
            )
        self.assertEqual(body["material_generate_count"], 1)
        self.assertEqual(body["material_plan"][0]["source"], "asset")
        self.assertEqual(body["material_plan"][1]["source"], "generate")

    def test_prepare_payload_rejects_too_many_material_scenes_before_billing(self):
        scenes = [{"scene": "镜头%d" % i, "line": "台词"} for i in range(9)]
        with self.assertRaisesRegex(ValueError, "最多支持 8 个分镜"):
            self.script_to_video.prepare_script_to_video_payload(
                {"scenes": scenes, "style": "口播"}, "fang",
            )

    def test_talking_material_pipeline_composes_then_burns_subtitles(self):
        self.script_to_video._get_first_avatar = lambda username: {"id": 1}
        self.video.gen_video = lambda payload: {
            "video_file": "video/avatar.mp4", "video_url": "/old.mp4",
        }
        plan = [{"scene_index": 0, "prompt": "枇杷树", "source": "generate", "file": None}]
        materials = [{"scene_index": 0, "prompt": "枇杷树", "source": "generate", "file": "image/a.png"}]
        with mock.patch.object(self.script_to_video, "_material_images", return_value=materials), \
             mock.patch.object(self.script_to_video, "_compose_materials", return_value="video/mixed.mp4") as compose, \
             mock.patch.object(self.video, "burn_subtitle", return_value="video/final.mp4") as subtitle, \
             mock.patch.object(self.video, "public_url", return_value="/final.mp4"):
            result = self.script_to_video.gen_script_to_video({
                "_username": "fang", "scenes": [{"scene": "枇杷树", "line": "第一句"}],
                "material_plan": plan, "subtitle": True,
            })
        compose.assert_called_once()
        subtitle.assert_called_once()
        self.assertEqual(result["pipeline"], "talking_with_materials")
        self.assertEqual(result["video_file"], "video/final.mp4")
        self.assertEqual(result["material_generated_count"], 1)

    def test_failed_composition_cleans_only_newly_generated_materials(self):
        with tempfile.TemporaryDirectory() as raw:
            old_out = self.script_to_video.OUT_DIR
            self.script_to_video.OUT_DIR = Path(raw)
            generated = Path(raw) / "generated.png"
            reused = Path(raw) / "reused.png"
            generated.write_bytes(b"new")
            reused.write_bytes(b"old")
            try:
                self.script_to_video._cleanup_generated_materials([
                    {"source": "generate", "file": "generated.png"},
                    {"source": "asset", "file": "reused.png"},
                ])
                self.assertFalse(generated.exists())
                self.assertTrue(reused.exists())
            finally:
                self.script_to_video.OUT_DIR = old_out


if __name__ == "__main__":
    unittest.main()
