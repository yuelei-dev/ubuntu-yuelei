import io
import importlib
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]


class LocalReverseUploadUnitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server = str(ROOT / "server")
        if server not in sys.path:
            sys.path.insert(0, server)
        cls.upload = importlib.import_module("content_domains.local_reverse_upload")

    def test_limits_and_fixed_cost(self):
        self.assertEqual(self.upload.IMAGE_LIMIT, 20 * 1024 * 1024)
        self.assertEqual(self.upload.VIDEO_LIMIT, 200 * 1024 * 1024)
        self.assertEqual(self.upload.VIDEO_DURATION_LIMIT, 120.0)
        self.assertEqual(self.upload.UPLOAD_COST, 20)

    def test_image_magic_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            jpg = pathlib.Path(folder) / "image.jpg"
            png = pathlib.Path(folder) / "image.png"
            jpg.write_bytes(b"\xff\xd8\xff\xe0demo")
            png.write_bytes(b"\x89PNG\r\n\x1a\ndemo")
            self.assertEqual(self.upload._image_type(jpg), "image/jpeg")
            self.assertEqual(self.upload._image_type(png), "image/png")

    def test_stream_rejects_incomplete_file(self):
        handler = type("Handler", (), {"rfile": io.BytesIO(b"abc")})()
        with tempfile.TemporaryDirectory() as folder:
            target = pathlib.Path(folder) / "upload.bin"
            with self.assertRaisesRegex(ValueError, "文件读取不完整"):
                self.upload._stream_body(handler, target, 5)

    def test_video_duration_rejects_over_two_minutes(self):
        result = type("Result", (), {"stdout": b'{"format":{"duration":"120.1"}}'})()
        with mock.patch.object(self.upload.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(ValueError, "最长支持 2 分钟"):
                self.upload._video_duration("demo.mp4")


class LocalReverseProcessorUnitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server = str(ROOT / "server")
        if server not in sys.path:
            sys.path.insert(0, server)
        cls.processor = importlib.import_module("content_domains.local_reverse_processor")

    def _valid_video_data(self):
        return {
            "core_subject": "透明保护壳中的白色无线耳机",
            "subject_evidence": ["开场展示闭合耳机盒", "中段手指取出耳机", "结尾人物完成佩戴"],
            "timeline": ["耳机盒静置", "盒盖打开", "手指靠近耳机", "取出单只耳机",
                         "耳机移向人物耳部", "人物完成佩戴并进入使用场景"],
            "subject": "透明磨砂保护壳包裹白色无线耳机盒，白色入耳式耳机为核心产品，年轻人物作为操作与佩戴演示者；盒体边缘圆润，开盖结构清楚，耳机表面具有细腻高光，人物手部和耳部始终服务于产品展示。",
            "scene": "开场为冷灰色产品展示台，中段保持干净摄影背景，后段切入人物日常使用环境；桌面道具位于前景，耳机盒稳定处于中景视觉中心，背景陈设轻微虚化，空间层次随使用场景转换但不喧宾夺主。",
            "composition": "先以居中特写突出闭合盒体，再切正面近景展示开盖结构，随后使用手部微距，最终转为人物耳部特写；镜头焦点跟随耳机移动，产品在关键画面保持中心或三分线位置，转场维持方向和尺度连续。",
            "action": "盒体先保持闭合静置，镜头缓慢推近；盒盖随后向上打开，手指从画面上方进入并靠近耳机，拇指与食指捏住单只耳机并平稳取出；镜头跟随耳机离开盒体、横向移向人物耳部，人物略微侧头并抬手承接，完成佩戴后放下手臂，视线转向前方，最后以耳部近景和稳定使用状态收束。",
            "lighting": "冷灰背景使用顶部大面积柔光，产品边缘形成清晰轮廓高光，透明外壳保留通透反射；人物段改用柔和侧光塑造面部层次，白色材质控制曝光，背景亮度低于主体。",
            "style": "写实商业产品广告风格，画面克制简洁，金属、塑料与透明材质清晰，主色调保持冷白和浅灰；剪辑节奏由静态展示逐步转入生活化使用，整体呈现精致、可信且功能导向的成片观感。",
            "parameters": "竖屏九比十六，四K清晰度，每秒三十帧，中长焦微距与浅景深，快门保持动作清楚；使用稳定推近、横向跟随和焦点转移，控制时长约十五秒，保持耳机盒、耳机外观及人物手部方向前后一致。",
        }

    def test_local_image_result_is_structured_and_upload_is_deleted(self):
        original_out = self.processor.OUT_DIR
        with tempfile.TemporaryDirectory() as folder:
            self.processor.OUT_DIR = pathlib.Path(folder)
            upload_dir = pathlib.Path(folder) / "reverse_uploads"
            upload_dir.mkdir()
            image = upload_dir / "input.jpg"
            image.write_bytes(b"\xff\xd8\xff\xe0demo")
            sections = {key: label + "细节" for key, label in self.processor._SECTION_ORDER}
            payload = {"local_media_path": str(image), "local_media_type": "image",
                       "source_title": "input.jpg", "duration": 0}
            try:
                with mock.patch.object(self.processor, "_structured_prompt",
                                       return_value=(sections, "完整提示词")):
                    result = self.processor.gen_local_reverse(payload)
            finally:
                self.processor.OUT_DIR = original_out
            self.assertEqual(result["sections"], sections)
            self.assertEqual(result["prompt"], "完整提示词")
            self.assertFalse(image.exists())

    def test_video_prompt_prioritizes_visual_motion_over_transcript(self):
        captured = {}
        sections = self._valid_video_data()
        response = __import__("json").dumps(sections, ensure_ascii=False)

        def fake_chat(system, user, frames, **kwargs):
            captured["system"] = system
            captured["user"] = user
            captured["kwargs"] = kwargs
            return response

        breakdown = importlib.import_module("content_domains.breakdown")
        with mock.patch.object(breakdown, "_chat_multimodal", side_effect=fake_chat):
            self.processor._structured_prompt(
                "video", "demo.mp4", 30, "现在购买产品立减一百元", ["frame.jpg"])

        prompt = captured["user"]
        self.assertIn("视频生成提示词反推", prompt)
        self.assertIn("可见画面、人物动作、场景变化和镜头运动为第一依据", prompt)
        self.assertIn("不得把转写原句、营销话术、旁白或台词写入任何字段", prompt)
        self.assertIn("音频与画面冲突时以画面为准", prompt)
        self.assertIn("第一张图片是 8 个时间点组成的 4×2 总览图", prompt)
        self.assertIn("产品必须是 core_subject 和 subject 的第一主体", prompt)
        self.assertIn('"subject_evidence" 写至少 3 个不同时间点的可见证据', prompt)
        self.assertIn('"timeline" 按总览图顺序写至少 6 个连续节点', prompt)
        self.assertIn("时间值只保留 1 位小数（0.1 秒精度）", prompt)
        self.assertIn("七个字段合计写 500-800 个中文字符", prompt)
        self.assertIn("subject 写 70-100 字，至少 5 项可见细节", prompt)
        self.assertIn("scene 写 70-100 字，至少 5 项场景细节", prompt)
        self.assertIn("composition 写 70-100 字，至少 5 项镜头细节", prompt)
        self.assertIn("action 写 150-200 字，至少 8 个连续动作节点", prompt)
        self.assertIn("lighting 写 55-80 字，至少 4 项光影细节", prompt)
        self.assertIn("style 写 55-80 字，至少 4 项风格细节", prompt)
        self.assertIn("parameters 写 55-80 字，至少 6 项可执行参数", prompt)
        self.assertEqual(captured["kwargs"]["max_tokens"], 2400)
        self.assertEqual(captured["kwargs"]["temp"], 0.1)
        self.assertNotIn("口播转写：", prompt)
        self.assertNotIn('"scene":"场景细节"', prompt)
        self.assertIn("不要输出示例、字段说明或占位词", prompt)

    def test_video_result_exposes_verified_subject_and_timeline(self):
        sections = self._valid_video_data()
        response = __import__("json").dumps(sections, ensure_ascii=False)
        breakdown = importlib.import_module("content_domains.breakdown")
        with mock.patch.object(breakdown, "_chat_multimodal", return_value=response):
            result, prompt = self.processor._structured_prompt(
                "video", "demo.mp4", 15, "", ["overview.jpg", "pair.jpg"])
        self.assertIn("核心主体：透明保护壳中的白色无线耳机", result["subject"])
        self.assertIn("识别依据：开场展示闭合耳机盒；中段手指取出耳机；结尾人物完成佩戴", result["subject"])
        self.assertIn("完整时间线：耳机盒静置；盒盖打开；手指靠近耳机；取出单只耳机", result["action"])
        self.assertNotIn("['", result["subject"])
        self.assertNotIn("['", result["action"])
        self.assertIn(result["subject"], prompt)
        self.assertIn(result["action"], prompt)

    def test_video_result_rejects_missing_core_subject_evidence(self):
        sections = self._valid_video_data()
        for key in ("core_subject", "subject_evidence", "timeline"):
            sections.pop(key)
        response = __import__("json").dumps(sections, ensure_ascii=False)
        breakdown = importlib.import_module("content_domains.breakdown")
        with mock.patch.object(breakdown, "_chat_multimodal", return_value=response):
            with self.assertRaisesRegex(ValueError, "缺少核心主体判断"):
                self.processor._structured_prompt(
                    "video", "demo.mp4", 15, "", ["overview.jpg", "pair.jpg"])

    def test_video_result_rejects_copied_schema_placeholders(self):
        sections = self._valid_video_data()
        sections["scene"] = "场景细节"
        response = __import__("json").dumps(sections, ensure_ascii=False)
        breakdown = importlib.import_module("content_domains.breakdown")
        with mock.patch.object(breakdown, "_chat_multimodal", return_value=response):
            with self.assertRaisesRegex(ValueError, "场景过于简略"):
                self.processor._structured_prompt(
                    "video", "demo.mp4", 15, "", ["overview.jpg", "pair.jpg"])

    def test_video_result_rejects_placeholder_embedded_in_other_text(self):
        sections = self._valid_video_data()
        sections["scene"] += "；场景细节"
        response = __import__("json").dumps(sections, ensure_ascii=False)
        breakdown = importlib.import_module("content_domains.breakdown")
        with mock.patch.object(breakdown, "_chat_multimodal", return_value=response):
            with self.assertRaisesRegex(ValueError, "场景过于简略"):
                self.processor._structured_prompt(
                    "video", "demo.mp4", 15, "", ["overview.jpg", "pair.jpg"])

    def test_video_result_rejects_short_non_placeholder_content(self):
        sections = self._valid_video_data()
        sections["composition"] = "产品居中，固定镜头"
        response = __import__("json").dumps(sections, ensure_ascii=False)
        breakdown = importlib.import_module("content_domains.breakdown")
        with mock.patch.object(breakdown, "_chat_multimodal", return_value=response):
            with self.assertRaisesRegex(ValueError, "构图过于简略"):
                self.processor._structured_prompt(
                    "video", "demo.mp4", 15, "", ["overview.jpg", "pair.jpg"])

    def test_video_result_rejects_long_but_undetailed_content(self):
        sections = self._valid_video_data()
        sections["subject"] = "画面始终展示同一个可见主体" * 8
        response = __import__("json").dumps(sections, ensure_ascii=False)
        breakdown = importlib.import_module("content_domains.breakdown")
        with mock.patch.object(breakdown, "_chat_multimodal", return_value=response):
            with self.assertRaisesRegex(ValueError, "主体细节不足"):
                self.processor._structured_prompt(
                    "video", "demo.mp4", 15, "", ["overview.jpg", "pair.jpg"])

    def test_detail_items_accepts_chinese_sentence_punctuation(self):
        evidence = "开场展示闭合耳机盒。中段手指取出耳机！结尾人物完成佩戴？"
        timeline = "盒体静置。盒盖打开。手指靠近。取出耳机。移向耳部。完成佩戴。"
        self.assertEqual(len(self.processor._detail_items(evidence)), 3)
        self.assertEqual(len(self.processor._detail_items(timeline)), 6)

    def test_video_result_requires_five_hundred_detail_chars(self):
        sections = self._valid_video_data()
        def detailed_value(prefix, item_count, minimum):
            value = "；".join("%s%d" % (prefix, index)
                             for index in range(item_count))
            return value + "甲" * max(0, minimum - len(value))
        sections.update({
            "subject": detailed_value("主体特征", 5, 50),
            "scene": detailed_value("场景关系", 5, 50),
            "composition": detailed_value("镜头构图", 5, 50),
            "action": detailed_value("连续动作节点", 8, 120),
            "lighting": detailed_value("光影变化", 4, 40),
            "style": detailed_value("风格质感", 4, 40),
            "parameters": detailed_value("画幅设置", 6, 40),
        })
        response = __import__("json").dumps(sections, ensure_ascii=False)
        breakdown = importlib.import_module("content_domains.breakdown")
        with mock.patch.object(breakdown, "_chat_multimodal", return_value=response):
            with self.assertRaisesRegex(ValueError, "未达到详细度要求"):
                self.processor._structured_prompt(
                    "video", "demo.mp4", 15, "", ["overview.jpg", "pair.jpg"])

    def test_abnormal_transcript_filter(self):
        self.assertTrue(self.processor._transcript_is_abnormal("内容" * 100, 15))
        self.assertTrue(self.processor._transcript_is_abnormal("重复句子" * 30, 60))
        self.assertFalse(self.processor._transcript_is_abnormal(
            "人物拿起耳机盒并完成佩戴。", 15))

    def test_local_reverse_duration_uses_tenth_second_precision(self):
        self.assertEqual(self.processor._duration_tenth(15.093), 15.1)
        self.assertEqual(self.processor._duration_tenth("2.86"), 2.9)
        self.assertEqual(self.processor._duration_tenth(None), 0.0)

    def test_video_detail_count_contract_matches_prompt(self):
        self.assertEqual(self.processor._VIDEO_SECTION_MIN_ITEMS, {
            "subject": 5, "scene": 5, "composition": 5, "action": 8,
            "lighting": 4, "style": 4, "parameters": 6,
        })

    def test_reverse_overview_uses_all_eight_frames_in_time_order(self):
        frames = ["frame_%d.jpg" % i for i in range(1, 9)]
        with mock.patch.object(self.processor.subprocess, "run") as run:
            output = self.processor._reverse_overview("tmp", frames)
        command = run.call_args.args[0]
        self.assertEqual([command[command.index(frame)] for frame in frames], frames)
        self.assertIn("xstack=inputs=8", " ".join(command))
        self.assertTrue(output.endswith("reverse_overview.jpg"))

    def test_image_prompt_does_not_inherit_video_length_requirements(self):
        captured = {}
        sections = {key: label + "细节" for key, label in self.processor._SECTION_ORDER}
        response = __import__("json").dumps(sections, ensure_ascii=False)

        def fake_chat(system, user, frames, **kwargs):
            captured["user"] = user
            return response

        breakdown = importlib.import_module("content_domains.breakdown")
        with mock.patch.object(breakdown, "_chat_multimodal", side_effect=fake_chat):
            self.processor._structured_prompt(
                "image", "demo.jpg", 0, "", ["frame.jpg"])

        self.assertNotIn("七个字段合计写 500-800 个中文字符", captured["user"])

    def test_transcript_reference_is_capped_to_avoid_dominating_frames(self):
        captured = {}
        sections = self._valid_video_data()
        response = __import__("json").dumps(sections, ensure_ascii=False)

        def fake_chat(system, user, frames, **kwargs):
            captured["user"] = user
            return response

        breakdown = importlib.import_module("content_domains.breakdown")
        transcript = "甲" * 800
        with mock.patch.object(breakdown, "_chat_multimodal", side_effect=fake_chat):
            self.processor._structured_prompt(
                "video", "demo.mp4", 120, transcript, ["frame.jpg"])

        self.assertIn("甲" * 600, captured["user"])
        self.assertNotIn("甲" * 601, captured["user"])


class LocalReverseFeatureSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ui = (ROOT / "site/workbench/script.html").read_text(encoding="utf-8")
        cls.processor = (ROOT / "server/content_domains/local_reverse_processor.py").read_text(encoding="utf-8")
        cls.core = (ROOT / "server/content_domains/core.py").read_text(encoding="utf-8")
        cls.nginx = (ROOT / "deploy/nginx-huangquechuanmei.conf").read_text(encoding="utf-8")
        cls.assets = (ROOT / "server/content_domains/assets_store.py").read_text(encoding="utf-8")

    def test_ui_has_separate_image_and_video_entries(self):
        for marker in ('id="bdLocalImage"', 'id="bdLocalVideo"',
                       'id="bdImageReverse"', 'id="bdVideoReverse"'):
            self.assertIn(marker, self.ui)
        self.assertIn("图片不能超过 20MB", self.ui)
        self.assertIn("视频不能超过 200MB", self.ui)
        self.assertIn("视频最长支持 2 分钟", self.ui)

    def test_ui_checks_points_before_raw_upload(self):
        self.assertIn("function _localPointsCheck()", self.ui)
        self.assertIn("durationCheck.then(_localPointsCheck).then(function()", self.ui)
        self.assertIn("/api/gen/breakdown/local-upload?media_type=", self.ui)
        self.assertIn("d&&d.user&&d.user.points", self.ui)
        self.assertNotIn("Number(d.points||0)", self.ui)
        self.assertLess(self.ui.index("function _localBusy"), self.ui.index("if(bdGen)"))
        self.assertIn("headers:{'Content-Type':mime", self.ui)

    def test_structured_sections_and_actions_are_present(self):
        for label in ("主体", "场景", "构图", "动作", "光影", "风格", "参数"):
            self.assertIn(label, self.processor)
        self.assertIn("max_tokens=2400", self.processor)
        self.assertIn('id="bdReverseCopyBtn"', self.ui)
        self.assertIn('id="bdRemakeBtn"', self.ui)
        self.assertIn("正在读取音频语义（仅辅助）", self.ui)
        self.assertNotIn("正在转写视频口播", self.ui)

    def test_standard_paid_job_and_refund_flow_is_reused(self):
        upload = (ROOT / "server/content_domains/local_reverse_upload.py").read_text(encoding="utf-8")
        self.assertNotIn("jobs_store.create_paid_job", upload)
        self.assertIn("points_domain.deduct_points", upload)
        self.assertIn("INSERT INTO jobs", upload)
        self.assertIn("INSERT INTO breakdown_uploads", upload)
        self.assertIn("reject_pending_job", upload)
        self.assertIn('if p == "/api/gen/breakdown/local-upload"', self.core)
        self.assertIn('"source_type": r.get("source_type")', self.assets)
        self.assertIn('"sections": r.get("sections")', self.assets)

    def test_nginx_streams_two_hundred_megabyte_uploads(self):
        self.assertIn("location = /api/gen/breakdown/local-upload", self.nginx)
        self.assertIn("proxy_request_buffering off", self.nginx)
        self.assertIn("client_max_body_size 200m", self.nginx)


if __name__ == "__main__":
    unittest.main()
