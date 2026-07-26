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
        sections = {key: label + "细节" for key, label in self.processor._SECTION_ORDER}
        sections.update({
            "core_subject": "透明耳机盒与无线耳机",
            "subject_evidence": "闭合盒特写；开盖特写；佩戴耳机特写",
            "timeline": "盒子闭合；盒盖打开；手指取出耳机；耳机靠近耳朵；完成佩戴；进入使用场景",
        })
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
        self.assertIn("七个字段合计写 500-800 个中文字符", prompt)
        self.assertIn("subject 写 70-100 字，至少 5 项可见细节", prompt)
        self.assertIn("scene 写 70-100 字，至少 5 项场景细节", prompt)
        self.assertIn("composition 写 70-100 字，至少 5 项镜头细节", prompt)
        self.assertIn("action 写 150-200 字，至少 8 个连续动作节点", prompt)
        self.assertIn("lighting 写 55-80 字，至少 4 项光影细节", prompt)
        self.assertIn("style 写 55-80 字，至少 4 项风格细节", prompt)
        self.assertIn("parameters 写 55-80 字，至少 6 项可执行参数", prompt)
        self.assertEqual(captured["kwargs"]["max_tokens"], 1800)
        self.assertNotIn("口播转写：", prompt)

    def test_video_result_exposes_verified_subject_and_timeline(self):
        sections = {key: label + "细节" for key, label in self.processor._SECTION_ORDER}
        sections.update({
            "core_subject": "透明耳机盒与无线耳机",
            "subject_evidence": "闭合盒特写；开盖特写；佩戴耳机特写",
            "timeline": "闭合；开盖；取出；靠近耳朵；佩戴；使用",
        })
        response = __import__("json").dumps(sections, ensure_ascii=False)
        breakdown = importlib.import_module("content_domains.breakdown")
        with mock.patch.object(breakdown, "_chat_multimodal", return_value=response):
            result, prompt = self.processor._structured_prompt(
                "video", "demo.mp4", 15, "", ["overview.jpg", "pair.jpg"])
        self.assertIn("核心主体：透明耳机盒与无线耳机", result["subject"])
        self.assertIn("识别依据：闭合盒特写；开盖特写；佩戴耳机特写", result["subject"])
        self.assertIn("完整时间线：闭合；开盖；取出；靠近耳朵；佩戴；使用", result["action"])
        self.assertIn(result["subject"], prompt)
        self.assertIn(result["action"], prompt)

    def test_video_result_rejects_missing_core_subject_evidence(self):
        sections = {key: label + "细节" for key, label in self.processor._SECTION_ORDER}
        response = __import__("json").dumps(sections, ensure_ascii=False)
        breakdown = importlib.import_module("content_domains.breakdown")
        with mock.patch.object(breakdown, "_chat_multimodal", return_value=response):
            with self.assertRaisesRegex(ValueError, "缺少核心主体判断"):
                self.processor._structured_prompt(
                    "video", "demo.mp4", 15, "", ["overview.jpg", "pair.jpg"])

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
        sections = {key: label + "细节" for key, label in self.processor._SECTION_ORDER}
        sections.update({
            "core_subject": "产品",
            "subject_evidence": "时间点一；时间点二；时间点三",
            "timeline": "节点一；节点二；节点三；节点四；节点五；节点六",
        })
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
        self.assertIn("max_tokens=1800", self.processor)
        self.assertIn('id="bdReverseCopyBtn"', self.ui)
        self.assertIn('id="bdRemakeBtn"', self.ui)
        self.assertIn("正在读取音频语义（仅辅助）", self.ui)
        self.assertNotIn("正在转写视频口播", self.ui)

    def test_standard_paid_job_and_refund_flow_is_reused(self):
        self.assertIn("jobs_store.create_paid_job", (ROOT / "server/content_domains/local_reverse_upload.py").read_text(encoding="utf-8"))
        self.assertIn("reject_pending_job", (ROOT / "server/content_domains/local_reverse_upload.py").read_text(encoding="utf-8"))
        self.assertIn('if p == "/api/gen/breakdown/local-upload"', self.core)
        self.assertIn('"source_type": r.get("source_type")', self.assets)
        self.assertIn('"sections": r.get("sections")', self.assets)

    def test_nginx_streams_two_hundred_megabyte_uploads(self):
        self.assertIn("location = /api/gen/breakdown/local-upload", self.nginx)
        self.assertIn("proxy_request_buffering off", self.nginx)
        self.assertIn("client_max_body_size 200m", self.nginx)


if __name__ == "__main__":
    unittest.main()
