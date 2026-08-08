import importlib
import subprocess
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
        cls.core = importlib.import_module("content_domains.core")
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
        self.assertEqual(calls["payload"]["model"], "grok-imagine-video")
        self.assertEqual(calls["payload"]["resolution"], "720p")
        self.assertEqual(calls["payload"]["_username"], "fang")
        self.assertIn("女生走进门店", calls["payload"]["prompt"])
        self.assertIn("电影质感", calls["payload"]["prompt"])
        self.assertEqual(result["pipeline"], "grok")
        self.assertEqual(result["scene_count"], 2)
        self.assertEqual(result["type"], "script_to_video")

    def test_smart_montage_uses_a_single_worker_local_render_pool(self):
        self.assertIs(
            self.core._pick_job_queue("script_to_video", "smart_montage"),
            self.core._smart_montage_job_queue,
        )
        self.assertIs(
            self.core._pick_job_queue("script_to_video", "talking"),
            self.core._talking_job_queue,
        )
        self.assertEqual(1, self.core.SMART_MONTAGE_JOB_WORKERS)
        self.assertEqual(12, self.core._smart_montage_job_queue.maxsize)

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

    def test_prepare_smart_montage_recomputes_and_freezes_all_fresh_scenes(self):
        request = {
            "pipeline": "smart_montage",
            "copy": "从专业评估开始，理解肌肤真正需要，再用温和护理找回自然透亮。",
            "style": "pop",
            "ratio": "16:9",
        }
        preview = self.script_to_video.smart_montage_plan_response(request)
        request["plan_digest"] = preview["plan_digest"]
        body = self.script_to_video.prepare_script_to_video_payload(request, "fang")
        self.assertEqual(body["pipeline"], "smart_montage")
        self.assertEqual(body["mode"], "smart_montage")
        self.assertEqual(body["style"], "pop")
        self.assertNotIn("plan", body)
        self.assertEqual(body["material_generate_count"], body["smart_plan"]["scene_count"])
        self.assertEqual(len(body["material_plan"]), body["material_generate_count"])
        self.assertTrue(all(item["source"] == "generate" for item in body["material_plan"]))
        self.assertTrue(all(item["file"] is None for item in body["material_plan"]))
        self.assertEqual(body["plan_digest"], preview["plan_digest"])

    def test_smart_montage_rejects_missing_stale_or_injected_plan_digest(self):
        request = {
            "pipeline": "smart_montage",
            "copy": "专业评估看见真实需求，温和护理让状态自然稳定。",
            "style": "clinic",
            "ratio": "9:16",
        }
        with self.assertRaisesRegex(
            self.script_to_video.SmartMontageRequestError, "缺少已确认",
        ) as missing:
            self.script_to_video.prepare_script_to_video_payload(request, "fang")
        self.assertEqual("plan_digest_required", missing.exception.code)

        with self.assertRaises(self.script_to_video.SmartMontageRequestError) as stale:
            self.script_to_video.prepare_script_to_video_payload(
                {**request, "plan_digest": "0" * 64}, "fang",
            )
        self.assertEqual(409, stale.exception.status)
        self.assertEqual("plan_digest_mismatch", stale.exception.code)

        preview = self.script_to_video.smart_montage_plan_response(request)
        with self.assertRaises(self.script_to_video.SmartMontageRequestError) as injected:
            self.script_to_video.prepare_script_to_video_payload({
                **request,
                "plan_digest": preview["plan_digest"],
                "plan": {"scenes": [{"headline": "被篡改"}]},
            }, "fang")
        self.assertEqual("invalid_request", injected.exception.code)

    def test_smart_plan_http_contract_is_authenticated_and_aggregate(self):
        class Handler:
            path = "/api/gen/script_to_video/plan"

            def __init__(self):
                self.sent = None

            def _token(self): return "token"
            def _json_body_strict(self):
                return {
                    "copy": "专业护理理解真实需求，让肌肤回到自然透亮的状态。",
                    "styles": ["luxe", "pop"],
                    "ratio": "16:9",
                }
            def _send(self, status, payload): self.sent = (status, payload)
            def _method_not_allowed(self): self.sent = (405, {})

        handler = Handler()
        handled = self.script_to_video.dispatch_http(
            handler, "POST", lambda token: {"username": "fang"}, lambda user: False,
        )
        self.assertTrue(handled)
        self.assertEqual(handler.sent[0], 200)
        self.assertEqual(
            [item["style"] for item in handler.sent[1]["plan"]["styles"]],
            ["luxe", "pop"],
        )
        digests = handler.sent[1]["plan_digests"]
        self.assertEqual({"luxe", "pop"}, set(digests))
        self.assertTrue(all(len(value) == 64 for value in digests.values()))
        self.assertEqual(
            digests,
            {
                item["style"]: item["plan_digest"]
                for item in handler.sent[1]["plan"]["styles"]
            },
        )

    def test_smart_voiceover_preserves_the_complete_confirmed_copy(self):
        copy = ("完整朗读每一段用户文案。" * 30)[:320]
        plan = {
            "copy": copy,
            "duration_seconds": 90,
            "scenes": [
                {"supporting_copy": "第一幕护理评估" * 40},
                {"supporting_copy": "第二幕定制方案" * 40},
                {"supporting_copy": "第三幕长期管理" * 40},
            ],
        }
        narration = self.script_to_video._smart_voiceover_text(plan)
        self.assertEqual(narration, copy)

    def test_retime_smart_plan_keeps_contiguous_exact_timeline(self):
        plan = {
            "duration_seconds": 10,
            "scenes": [
                {"start_seconds": 0, "duration_seconds": 3},
                {"start_seconds": 3, "duration_seconds": 3},
                {"start_seconds": 6, "duration_seconds": 4},
            ],
        }
        retimed = self.script_to_video._retime_smart_plan(plan, 9.2)
        self.assertEqual(retimed["duration_seconds"], 10.0)
        cursor = 0.0
        for scene in retimed["scenes"]:
            self.assertEqual(scene["start_seconds"], cursor)
            cursor = round(cursor + scene["duration_seconds"], 3)
        self.assertEqual(cursor, retimed["duration_seconds"])
        with self.assertRaisesRegex(ValueError, "超过已确认方案"):
            self.script_to_video._retime_smart_plan(plan, 9.95)

    def test_overlong_voiceover_is_fitted_without_changing_confirmed_plan(self):
        with tempfile.TemporaryDirectory() as raw:
            old_out = self.script_to_video.OUT_DIR
            self.script_to_video.OUT_DIR = Path(raw)
            source = Path(raw) / "audio" / "voice.mp3"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"voice")

            def fake_run(command, **_kwargs):
                Path(command[-1]).write_bytes(b"fitted")
                return subprocess.CompletedProcess(command, 0, b"", b"")

            try:
                with mock.patch.object(self.script_to_video.subprocess, "run", side_effect=fake_run) as run, \
                     mock.patch.object(self.script_to_video, "_probe_media_duration", return_value=9.3):
                    rel, path, duration = self.script_to_video._fit_voiceover_to_plan(
                        "audio/voice.mp3", source, 12.0, 10.0,
                    )
                self.assertTrue(rel.startswith("audio/aud_fit_"))
                self.assertEqual(path, Path(raw) / rel)
                self.assertEqual(duration, 9.3)
                self.assertFalse(source.exists())
                command = run.call_args.args[0]
                self.assertIn("-filter:a", command)
                self.assertIn("atempo=", command[command.index("-filter:a") + 1])
            finally:
                self.script_to_video.OUT_DIR = old_out

    def test_smart_material_generation_retries_duplicate_hash(self):
        with tempfile.TemporaryDirectory() as raw:
            old_out = self.script_to_video.OUT_DIR
            self.script_to_video.OUT_DIR = Path(raw)
            calls = []

            def fake_gen_image(payload):
                calls.append(payload)
                index = len(calls)
                name = "image-%d.png" % index
                (Path(raw) / name).write_bytes(b"same" if index < 3 else b"different")
                return {"file": name}

            plan = {
                "ratio": "16:9",
                "scenes": [
                    {"image_prompt": "第一幕"},
                    {"image_prompt": "第二幕"},
                ],
            }
            try:
                with mock.patch("content_domains.image.gen_image", side_effect=fake_gen_image):
                    materials = self.script_to_video._smart_material_images(plan)
                self.assertEqual(len(calls), 3)
                self.assertEqual(len({item["sha256"] for item in materials}), 2)
                self.assertFalse((Path(raw) / "image-2.png").exists())
                self.assertTrue(all(call["count"] == 1 for call in calls))
            finally:
                self.script_to_video.OUT_DIR = old_out

    def test_smart_material_generation_honors_the_total_job_deadline(self):
        plan = {"ratio": "16:9", "scenes": [{"image_prompt": "第一幕"}]}
        with mock.patch.object(
            self.script_to_video.time, "monotonic", return_value=101.0,
        ), mock.patch("content_domains.image.gen_image") as generate:
            with self.assertRaisesRegex(TimeoutError, "两小时总时限"):
                self.script_to_video._smart_material_images(plan, deadline=100.0)
        generate.assert_not_called()

    def test_smart_phase_only_propagates_required_asset_persistence_failure(self):
        with mock.patch.object(
            self.video, "update_video_asset_phase",
            side_effect=RuntimeError("asset database unavailable"),
        ):
            self.assertFalse(
                self.script_to_video._smart_phase(88, "rendering")
            )
            with self.assertRaisesRegex(
                RuntimeError, "asset database unavailable",
            ):
                self.script_to_video._smart_phase(
                    88, "completed", strict=True, status="done",
                )

    def test_smart_pipeline_generates_voice_every_scene_and_verified_mp4(self):
        request = {
            "pipeline": "smart_montage",
            "copy": "专业评估看见真实需求，温和护理改善肤质，持续管理让状态自然稳定。",
            "style": "clinic",
            "ratio": "9:16",
        }
        request["plan_digest"] = self.script_to_video.smart_montage_plan_response(
            request,
        )["plan_digest"]
        plan = self.script_to_video.prepare_script_to_video_payload(
            request, "fang",
        )["smart_plan"]
        with tempfile.TemporaryDirectory() as raw:
            old_out = self.script_to_video.OUT_DIR
            self.script_to_video.OUT_DIR = Path(raw)
            image_calls = []

            def fake_audio(payload):
                (Path(raw) / "voice.mp3").write_bytes(b"voice")
                return {"file": "voice.mp3", "duration_ms": 8200}

            def fake_image(payload):
                image_calls.append(payload)
                name = "scene-%d.png" % len(image_calls)
                (Path(raw) / name).write_bytes(("image-%d" % len(image_calls)).encode())
                return {"file": name}

            def fake_render(render_plan, materials, output_path, **kwargs):
                Path(output_path).write_bytes(b"mp4")
                return {
                    "template_id": "smart-montage-v1",
                    "template_version": "1.0.1",
                    "output": {"duration_ms": int(render_plan["duration_seconds"] * 1000)},
                }

            try:
                with mock.patch("content_domains.audio.gen_audio", side_effect=fake_audio) as audio, \
                     mock.patch("content_domains.image.gen_image", side_effect=fake_image), \
                     mock.patch("content_domains.script_video_render.render", side_effect=fake_render) as render, \
                     mock.patch.object(self.video, "public_url", return_value="/private/final.mp4") as public_url, \
                     mock.patch.object(self.script_to_video, "_smart_phase") as phase:
                    result = self.script_to_video.gen_script_to_video({
                        "_username": "fang",
                        "_job_id": 88,
                        "pipeline": "smart_montage",
                        "smart_plan": plan,
                    })
                audio.assert_called_once()
                self.assertEqual(len(image_calls), plan["scene_count"])
                self.assertEqual(render.call_args.kwargs["voiceover"].name, "voice.mp3")
                self.assertGreaterEqual(render.call_args.kwargs["timeout"], 1)
                self.assertLessEqual(
                    render.call_args.kwargs["timeout"],
                    self.script_to_video.SMART_MONTAGE_MAX_RUNTIME,
                )
                public_url.assert_called_once()
                self.assertTrue(public_url.call_args.kwargs["private"])
                self.assertEqual(result["pipeline"], "smart_montage")
                self.assertEqual(result["material_generated_count"], plan["scene_count"])
                self.assertEqual(result["material_reused_count"], 0)
                self.assertEqual(len({item["sha256"] for item in result["materials"]}), plan["scene_count"])
                self.assertTrue((Path(raw) / result["video_file"]).is_file())
                self.assertEqual(phase.call_args_list[-1].args[1], "completed")
                self.assertIs(phase.call_args_list[-1].kwargs["strict"], True)
            finally:
                self.script_to_video.OUT_DIR = old_out

    def test_smart_pipeline_cleans_outputs_when_required_asset_save_fails(self):
        request = {
            "pipeline": "smart_montage",
            "copy": "先看清需求，再给出明确方案。",
            "style": "clinic",
            "ratio": "16:9",
        }
        request["plan_digest"] = self.script_to_video.smart_montage_plan_response(
            request,
        )["plan_digest"]
        plan = self.script_to_video.prepare_script_to_video_payload(
            request, "fang",
        )["smart_plan"]
        with tempfile.TemporaryDirectory() as raw:
            old_out = self.script_to_video.OUT_DIR
            self.script_to_video.OUT_DIR = Path(raw)
            voice = Path(raw) / "voice.mp3"
            image = Path(raw) / "scene.png"

            def fake_audio(_payload):
                voice.write_bytes(b"voice")
                return {"file": "voice.mp3", "duration_ms": 3200}

            def fake_materials(_plan, deadline=None):
                image.write_bytes(b"image")
                return [{
                    "scene_index": 0,
                    "prompt": "测试画面",
                    "source": "generate",
                    "file": "scene.png",
                    "sha256": "demo",
                }]

            def fake_render(_plan, _materials, output_path, **_kwargs):
                Path(output_path).write_bytes(b"mp4")
                return {"output": {"duration_ms": 3200}}

            def phase(_job_id, name, **_fields):
                if name == "completed":
                    raise RuntimeError("asset save failed")

            try:
                with mock.patch(
                    "content_domains.audio.gen_audio", side_effect=fake_audio,
                ), mock.patch.object(
                    self.script_to_video, "_smart_material_images",
                    side_effect=fake_materials,
                ), mock.patch(
                    "content_domains.script_video_render.render",
                    side_effect=fake_render,
                ), mock.patch.object(
                    self.video, "public_url", return_value="/private/final.mp4",
                ), mock.patch.object(
                    self.script_to_video, "_smart_phase", side_effect=phase,
                ):
                    with self.assertRaisesRegex(RuntimeError, "asset save failed"):
                        self.script_to_video.gen_script_to_video({
                            "_username": "fang",
                            "_job_id": 89,
                            "pipeline": "smart_montage",
                            "smart_plan": plan,
                        })
                self.assertFalse(voice.exists())
                self.assertFalse(image.exists())
                self.assertFalse(any((Path(raw) / "video").glob("*.mp4")))
            finally:
                self.script_to_video.OUT_DIR = old_out

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

    def test_photo_motion_randomly_uses_only_gentle_ken_burns_effects(self):
        for motion in self.script_to_video.PHOTO_MOTIONS:
            with self.subTest(motion=motion), \
                 mock.patch.object(self.script_to_video.random, "choice", return_value=motion):
                effect = self.script_to_video._photo_motion_filter(1080, 1920)
            self.assertIn("zoompan=", effect)
            self.assertIn("d=1:s=1080x1920:fps=25", effect)
            self.assertNotIn("rotate", effect)

    def test_material_composition_applies_motion_before_overlay(self):
        material = {"scene_index": 0, "prompt": "产品特写", "source": "asset", "file": "image/a.png"}
        with mock.patch.object(self.video, "_resolve_out_file", return_value=Path("/tmp/avatar.mp4")), \
             mock.patch.object(self.video, "_probe_video_duration", return_value=10), \
             mock.patch.object(self.video, "_probe_video_size", return_value=(1080, 1920)), \
             mock.patch.object(self.script_to_video, "_photo_motion_filter", return_value="zoompan=test") as motion, \
             mock.patch.object(self.script_to_video.subprocess, "run") as run, \
             mock.patch.object(self.video, "_faststart_video_file", side_effect=lambda value: value):
            self.script_to_video._compose_materials(
                "video/avatar.mp4", [{"dur": "10s", "line": "台词"}], [material],
            )

        motion.assert_called_once_with(1080, 1920)
        filter_complex = run.call_args.args[0][run.call_args.args[0].index("-filter_complex") + 1]
        self.assertIn("setsar=1,zoompan=test[mat0]", filter_complex)

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

    def test_material_image_520_retries_only_current_image(self):
        plan = [
            {"scene_index": 0, "prompt": "第一张", "source": "generate", "file": None},
            {"scene_index": 1, "prompt": "第二张", "source": "generate", "file": None},
        ]
        transient = RuntimeError("HTTP Error 520")
        transient.code = 520
        generated = [
            {"file": "image/first.png"},
            transient,
            {"file": "image/second.png"},
        ]
        with mock.patch("content_domains.image.gen_image", side_effect=generated) as gen_image, \
             mock.patch.object(self.script_to_video, "_safe_existing_image", return_value=True), \
             mock.patch.object(self.script_to_video.time, "sleep") as sleep:
            materials = self.script_to_video._material_images(plan)

        self.assertEqual(gen_image.call_count, 3)
        sleep.assert_called_once_with(self.script_to_video.MATERIAL_IMAGE_RETRY_DELAY)
        self.assertEqual(
            [item["file"] for item in materials],
            ["image/first.png", "image/second.png"],
        )

    def test_material_image_non_520_is_not_retried(self):
        plan = [{"scene_index": 0, "prompt": "第一张", "source": "generate", "file": None}]
        failure = RuntimeError("HTTP Error 500")
        failure.code = 500
        with mock.patch("content_domains.image.gen_image", side_effect=failure) as gen_image, \
             mock.patch.object(self.script_to_video.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "500"):
                self.script_to_video._material_images(plan)

        gen_image.assert_called_once()
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
