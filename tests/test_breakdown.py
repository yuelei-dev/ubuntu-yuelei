import importlib
import errno
import io
import json
import os
import socket
import ssl
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path


_ROUTING_ENV = (
    "BREAKDOWN_MODEL",
    "BREAKDOWN_FALLBACK_MODEL",
    "REVERSE_ZHIPU_BASE",
    "REVERSE_ZHIPU_KEY",
    "BREAKDOWN_ZHIPU_TIMEOUT",
)


class BreakdownTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server_dir = str(Path(__file__).resolve().parents[1] / "server")
        if server_dir not in sys.path:
            sys.path.insert(0, server_dir)
        cls.breakdown = importlib.import_module("content_domains.breakdown")

    def setUp(self):
        self.orig_heartbeat = self.breakdown._heartbeat
        self.orig_extract_frames = self.breakdown._extract_frames
        self.orig_pair_reverse_frames = getattr(self.breakdown, "_pair_reverse_frames", None)
        self.orig_chat_multimodal = self.breakdown._chat_multimodal
        self.orig_tempfile = self.breakdown.tempfile.NamedTemporaryFile
        self.orig_tikhub = sys.modules.get("tikhub")
        self.orig_routing_env = {key: os.environ.get(key) for key in _ROUTING_ENV}
        self.had_zhipu_post = hasattr(self.breakdown, "_post_zhipu")
        self.orig_zhipu_post = getattr(self.breakdown, "_post_zhipu", None)
        self.had_openai_post = hasattr(self.breakdown, "_post_openai_fallback")
        self.orig_openai_post = getattr(self.breakdown, "_post_openai_fallback", None)
        self.orig_egress_post_json = self.breakdown.egress.post_json
        self.orig_egress_direct = self.breakdown.egress._DIRECT
        self.orig_openai_key = self.breakdown.OPENAI_KEY

    def tearDown(self):
        self.breakdown._heartbeat = self.orig_heartbeat
        self.breakdown._extract_frames = self.orig_extract_frames
        if self.orig_pair_reverse_frames is not None:
            self.breakdown._pair_reverse_frames = self.orig_pair_reverse_frames
        self.breakdown._chat_multimodal = self.orig_chat_multimodal
        self.breakdown.tempfile.NamedTemporaryFile = self.orig_tempfile
        if self.orig_tikhub is None:
            sys.modules.pop("tikhub", None)
        else:
            sys.modules["tikhub"] = self.orig_tikhub
        for key, value in self.orig_routing_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if self.had_zhipu_post:
            self.breakdown._post_zhipu = self.orig_zhipu_post
        elif hasattr(self.breakdown, "_post_zhipu"):
            delattr(self.breakdown, "_post_zhipu")
        if self.had_openai_post:
            self.breakdown._post_openai_fallback = self.orig_openai_post
        elif hasattr(self.breakdown, "_post_openai_fallback"):
            delattr(self.breakdown, "_post_openai_fallback")
        self.breakdown.egress.post_json = self.orig_egress_post_json
        self.breakdown.egress._DIRECT = self.orig_egress_direct
        self.breakdown.OPENAI_KEY = self.orig_openai_key

    def _frame_file(self, directory):
        path = Path(directory) / "frame.jpg"
        path.write_bytes(b"jpeg-test-data")
        return str(path)

    def _detailed_reverse_objects(self, count=4):
        result = []
        for index in range(1, count + 1):
            result.append({
                "subject": "第%d段白衣人物居中，服装、朝向和姿态清晰一致" % index,
                "scene": "傍晚海滩、近处浪花、远处海平线和低空云层",
                "action": "人物迈步进入，抬臂转身，改变视线，最后减速收束",
                "camera": "中景平视横向跟随，随后缓慢推进并在结尾拉远",
                "lighting": "夕阳逆光形成暖色轮廓，沙面保留柔和高光",
                "sound": "保留海浪环境声和舒缓背景音乐",
                "continuity": "承接人物朝向与动作落点，保持光线和运镜连续",
            })
        return result

    def test_post_zhipu_uses_official_endpoint_key_json_and_timeout(self):
        captured = {}
        os.environ["REVERSE_ZHIPU_BASE"] = "https://open.bigmodel.cn/api/paas/v4/"
        os.environ["BREAKDOWN_ZHIPU_TIMEOUT"] = "123"

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"choices":[]}'

        class FakeOpener:
            def open(self, request, timeout):
                captured["request"] = request
                captured["timeout"] = timeout
                return FakeResponse()

        self.breakdown.egress._DIRECT = FakeOpener()
        body = {"model": "glm-4v-plus", "messages": []}

        result = self.breakdown._post_zhipu(body, "zhipu-test-key")

        request = captured["request"]
        self.assertEqual(request.full_url, "https://open.bigmodel.cn/api/paas/v4/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer zhipu-test-key")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(json.loads(request.data), body)
        self.assertEqual(captured["timeout"], 123)
        self.assertEqual(result, {"choices": []})

    def test_post_zhipu_logs_rejected_response_body(self):
        os.environ["REVERSE_ZHIPU_BASE"] = "https://open.bigmodel.cn/api/paas/v4"

        class FakeOpener:
            def open(self, request, timeout):
                raise urllib.error.HTTPError(
                    request.full_url, 400, "Bad Request", {},
                    io.BytesIO(b'{"error":{"message":"invalid image"}}'),
                )

        self.breakdown.egress._DIRECT = FakeOpener()
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(urllib.error.HTTPError):
            self.breakdown._post_zhipu(
                {"model": "glm-4v-plus", "messages": []}, "zhipu-test-key"
            )

        self.assertIn("status=400", output.getvalue())
        self.assertIn("invalid image", output.getvalue())

    def test_post_openai_fallback_uses_openai_endpoint_key_and_body(self):
        from content_domains.image import OPENAI_OFFICIAL_BASE

        captured = {}

        def fake_post_json(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return {"choices": [{"message": {"content": "GPT 结果"}}]}

        self.breakdown.egress.post_json = fake_post_json
        body = {"model": "gpt-4o", "messages": []}

        result = self.breakdown._post_openai_fallback(body)

        args = captured["args"]
        self.assertEqual(args[0], OPENAI_OFFICIAL_BASE)
        self.assertEqual(args[1], self.breakdown.OPENAI_BASE)
        self.assertEqual(args[2], "/v1/chat/completions")
        self.assertEqual(json.loads(args[3]), body)
        self.assertEqual(args[4]["Authorization"], "Bearer " + self.breakdown.OPENAI_KEY)
        self.assertEqual(result["choices"][0]["message"]["content"], "GPT 结果")

    def test_chat_multimodal_uses_zhipu_primary_without_openai(self):
        captured = {}
        os.environ["BREAKDOWN_MODEL"] = "glm-4v-plus"
        os.environ["REVERSE_ZHIPU_KEY"] = "zhipu-test-key"

        def fake_zhipu(body, api_key):
            captured["zhipu_body"] = body
            captured["zhipu_key"] = api_key
            return {"choices": [{"message": {"content": "智谱结果"}}]}

        self.breakdown._post_zhipu = fake_zhipu
        self.breakdown._post_openai_fallback = lambda body: captured.setdefault("openai_called", True)
        self.breakdown.egress.post_json = lambda *args, **kwargs: self.fail("legacy OpenAI path called")

        with tempfile.TemporaryDirectory() as directory:
            result = self.breakdown._chat_multimodal(
                "system", "user", [self._frame_file(directory)]
            )

        self.assertEqual(result, "智谱结果")
        self.assertEqual(captured["zhipu_body"]["model"], "glm-4v-plus")
        self.assertEqual(captured["zhipu_key"], "zhipu-test-key")
        self.assertNotIn("openai_called", captured)

    def test_chat_multimodal_can_force_openai_for_format_recovery(self):
        captured = {}
        os.environ["BREAKDOWN_FALLBACK_MODEL"] = "gpt-4o"
        os.environ["REVERSE_ZHIPU_KEY"] = "zhipu-test-key"
        self.breakdown.OPENAI_KEY = "openai-test-key"

        self.breakdown._post_zhipu = lambda body, api_key: self.fail("Zhipu should be skipped")

        def fake_openai(body):
            captured["body"] = body
            return {"choices": [{"message": {"content": "GPT 修复结果"}}]}

        self.breakdown._post_openai_fallback = fake_openai
        with tempfile.TemporaryDirectory() as directory:
            result = self.breakdown._chat_multimodal(
                "system", "user", [self._frame_file(directory)],
                temp=0.1, max_tokens=1600, provider="openai",
            )

        self.assertEqual(result, "GPT 修复结果")
        self.assertEqual(captured["body"]["model"], "gpt-4o")
        self.assertEqual(captured["body"]["temperature"], 0.1)
        self.assertEqual(captured["body"]["max_tokens"], 1600)

    def test_chat_multimodal_falls_back_to_gpt_on_pre_delivery_failure(self):
        captured = {}
        os.environ["BREAKDOWN_MODEL"] = "glm-4v-plus"
        os.environ["BREAKDOWN_FALLBACK_MODEL"] = "gpt-4o"
        os.environ["REVERSE_ZHIPU_KEY"] = "zhipu-test-key"

        def fake_zhipu(body, api_key):
            raise urllib.error.URLError(socket.gaierror(-2, "name resolution failed"))

        def fake_openai(body):
            captured["openai_body"] = body
            return {"choices": [{"message": {"content": "GPT 结果"}}]}

        self.breakdown._post_zhipu = fake_zhipu
        self.breakdown._post_openai_fallback = fake_openai
        self.breakdown.egress.post_json = lambda *args, **kwargs: self.fail("legacy OpenAI path called")

        with tempfile.TemporaryDirectory() as directory:
            result = self.breakdown._chat_multimodal(
                "system", "user", [self._frame_file(directory)]
            )

        self.assertEqual(result, "GPT 结果")
        self.assertEqual(captured["openai_body"]["model"], "gpt-4o")

    def test_chat_multimodal_falls_back_for_all_pre_delivery_failure_types(self):
        os.environ["REVERSE_ZHIPU_KEY"] = "zhipu-test-key"
        failures = (
            urllib.error.URLError(socket.gaierror(-2, "dns")),
            urllib.error.URLError(ConnectionRefusedError("refused")),
            urllib.error.URLError(OSError(errno.EHOSTUNREACH, "unreachable")),
            urllib.error.URLError(ssl.SSLError("handshake")),
        )

        for failure in failures:
            with self.subTest(failure=repr(failure)), tempfile.TemporaryDirectory() as directory:
                called = []

                def fake_zhipu(body, api_key, error=failure):
                    raise error

                self.breakdown._post_zhipu = fake_zhipu
                self.breakdown._post_openai_fallback = lambda body: (
                    called.append(body) or {"choices": [{"message": {"content": "GPT 结果"}}]}
                )

                result = self.breakdown._chat_multimodal(
                    "system", "user", [self._frame_file(directory)]
                )

                self.assertEqual(result, "GPT 结果")
                self.assertEqual(len(called), 1)

    def test_chat_multimodal_falls_back_after_zhipu_rejects_request(self):
        os.environ["REVERSE_ZHIPU_KEY"] = "zhipu-test-key"
        failures = (
            urllib.error.HTTPError("https://open.bigmodel.cn", 400, "Bad Request", {}, None),
            urllib.error.HTTPError("https://open.bigmodel.cn", 404, "Not Found", {}, None),
            urllib.error.HTTPError("https://open.bigmodel.cn", 429, "Too Many Requests", {}, None),
        )

        for failure in failures:
            with self.subTest(status=failure.code), tempfile.TemporaryDirectory() as directory:
                called = []
                self.breakdown._post_zhipu = (
                    lambda body, api_key, error=failure: (_ for _ in ()).throw(error)
                )
                self.breakdown._post_openai_fallback = lambda body: (
                    called.append(body) or {"choices": [{"message": {"content": "GPT 结果"}}]}
                )

                result = self.breakdown._chat_multimodal(
                    "system", "user", [self._frame_file(directory)]
                )

                self.assertEqual(result, "GPT 结果")
                self.assertEqual(len(called), 1)

    def test_chat_multimodal_missing_zhipu_key_fails_without_openai(self):
        os.environ.pop("REVERSE_ZHIPU_KEY", None)
        called = []
        self.breakdown._post_openai_fallback = lambda body: called.append(body)

        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(
            RuntimeError, "REVERSE_ZHIPU_KEY"
        ):
            self.breakdown._chat_multimodal(
                "system", "user", [self._frame_file(directory)]
            )

        self.assertEqual(called, [])

    def test_chat_multimodal_does_not_fallback_after_possible_delivery(self):
        os.environ["REVERSE_ZHIPU_KEY"] = "zhipu-test-key"
        failures = (
            urllib.error.HTTPError("https://open.bigmodel.cn", 500, "Server Error", {}, None),
            TimeoutError("timed out"),
            urllib.error.URLError(ConnectionResetError("reset")),
        )

        for failure in failures:
            with self.subTest(failure=repr(failure)), tempfile.TemporaryDirectory() as directory:
                called = []

                def fake_zhipu(body, api_key, error=failure):
                    raise error

                self.breakdown._post_zhipu = fake_zhipu
                self.breakdown._post_openai_fallback = lambda body: called.append(body)
                self.breakdown.egress.post_json = lambda *args, **kwargs: self.fail("legacy OpenAI path called")

                with self.assertRaises(type(failure)) as raised:
                    self.breakdown._chat_multimodal(
                        "system", "user", [self._frame_file(directory)]
                    )

                self.assertIs(raised.exception, failure)
                self.assertEqual(called, [])

    def test_chat_multimodal_propagates_openai_fallback_failure(self):
        os.environ["REVERSE_ZHIPU_KEY"] = "zhipu-test-key"

        def fake_zhipu(body, api_key):
            raise urllib.error.URLError(socket.gaierror(-2, "name resolution failed"))

        def fake_openai(body):
            raise RuntimeError("openai failed")

        self.breakdown._post_zhipu = fake_zhipu
        self.breakdown._post_openai_fallback = fake_openai
        self.breakdown.egress.post_json = lambda *args, **kwargs: self.fail("legacy OpenAI path called")

        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(
            RuntimeError, "openai failed"
        ):
            self.breakdown._chat_multimodal(
                "system", "user", [self._frame_file(directory)]
            )

    def test_chat_multimodal_logs_openai_fallback_failure(self):
        os.environ["REVERSE_ZHIPU_KEY"] = "zhipu-test-key"
        self.breakdown._post_zhipu = lambda body, api_key: (_ for _ in ()).throw(
            urllib.error.URLError(socket.gaierror(-2, "dns"))
        )
        self.breakdown._post_openai_fallback = lambda body: (_ for _ in ()).throw(
            RuntimeError("openai failed")
        )

        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(output), self.assertRaises(
            RuntimeError
        ):
            self.breakdown._chat_multimodal(
                "system", "user", [self._frame_file(directory)]
            )

        self.assertIn("openai fallback failure: RuntimeError", output.getvalue())

    def test_chat_multimodal_does_not_log_success_for_empty_zhipu_response(self):
        os.environ["REVERSE_ZHIPU_KEY"] = "zhipu-test-key"
        self.breakdown._post_zhipu = lambda body, api_key: {"choices": []}

        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(output), self.assertRaisesRegex(
            RuntimeError, "empty content"
        ):
            self.breakdown._chat_multimodal(
                "system", "user", [self._frame_file(directory)]
            )

        self.assertNotIn("zhipu success", output.getvalue())

    def _install_fake_env(self, raw_json, transcript=None):
        calls = {}

        class FakeTikHub:
            @staticmethod
            def detail(platform, item_id, note_type=None):
                calls["detail"] = (platform, item_id, note_type)
                return {
                    "play_url": "https://example.test/demo.mp4",
                    "duration": 18,
                    "title": "团购探店案例",
                }

            @staticmethod
            def download_to_file(play_url, deadline, filename):
                calls["download"] = (play_url, filename)

            @staticmethod
            def transcript(det, video_path=None):
                calls["transcript"] = (det.get("title"), video_path)
                return transcript if transcript is not None else [{"start": 0, "end": 3, "text": "先看门头"}]

        self.breakdown._heartbeat = lambda job_id, phase: calls.setdefault("phases", []).append(phase)
        self.breakdown._extract_frames = lambda video_path, count, duration, scale_width=512, min_frames=None: (
            "fake-frame-dir",
            ["frame_1.jpg", "frame_2.jpg"],
        )

        def fake_chat_multimodal(sysmsg, usermsg, frames, temp=0.7, **kwargs):
            calls["sysmsg"] = sysmsg
            calls["usermsg"] = usermsg
            calls["frames"] = list(frames)
            calls["chat_kwargs"] = kwargs
            return raw_json

        self.breakdown._chat_multimodal = fake_chat_multimodal
        self.breakdown.tempfile.NamedTemporaryFile = lambda suffix="", delete=False: type("Tmp", (), {"name": "fake-video.mp4"})()
        sys.modules["tikhub"] = FakeTikHub
        return calls

    def test_do_breakdown_returns_analysis_and_requests_it_in_prompt(self):
        calls = self._install_fake_env(
            '{"scenes":[{"dur":"3s","scene":"门店门头","line":"今天带你看一家店"}],"analysis":"这是一条团购探店口播视频"}'
        )

        result = self.breakdown._do_breakdown(
            {"_job_id": 11},
            {"platform": "douyin", "id": "abc123"},
            "https://example.test/post/1",
        )

        self.assertEqual(result["type"], "breakdown")
        self.assertEqual(result["source_platform"], "douyin")
        self.assertEqual(result["analysis"], "这是一条团购探店口播视频")
        self.assertEqual(result["scenes"][0]["scene"], "门店门头")
        self.assertFalse(result["asr_failed"])
        self.assertIn('"analysis"', calls["usermsg"])
        self.assertIn("同时输出一份视频内容综合分析", calls["sysmsg"])
        self.assertIn("60-100 字描述一个可直接拍摄或生成的完整镜头", calls["usermsg"])
        self.assertEqual(calls["frames"], ["frame_1.jpg", "frame_2.jpg"])
        self.assertEqual(calls["phases"], ["downloading", "extracting_frames", "transcribing", "analyzing"])

    def test_do_breakdown_defaults_analysis_to_empty_string(self):
        self._install_fake_env(
            '{"scenes":[{"dur":"4s","scene":"产品特写","line":"重点看这个细节"}]}'
        )

        result = self.breakdown._do_breakdown(
            {"_job_id": 12},
            {"platform": "xiaohongshu", "id": "note-9", "note_type": "video"},
            "https://example.test/post/2",
        )

        self.assertEqual(result["analysis"], "")
        self.assertEqual(result["source_title"], "团购探店案例")
        self.assertEqual(result["duration"], 18)
        self.assertEqual(len(result["scenes"]), 1)

    def test_scenes_prompt_requires_rich_detail(self):
        """分镜 prompt 必须要求 60-100 字及主体、动作、场景、镜头、光影细节。"""
        import inspect
        src = inspect.getsource(self.breakdown._breakdown_scenes_from_frames)
        self.assertIn("60-100字", src)
        self.assertIn("4-6 个分镜", src)
        self.assertIn("六类细节中的五类", src)
        self.assertIn("动作起点、过程、结果", src)
        self.assertIn("表情、视线和身体姿态", src)
        self.assertIn("前中后景关系", src)
        self.assertIn("推进/跟随/摇移", src)
        self.assertIn("光线方向、明暗层次", src)
        self.assertIn("max_tokens=3200", src)
        self.assertIn("每个 scene 50-80 字", src)
        self.assertIn("max_tokens=2400", src)
        self.assertIn('provider="openai"', src)
        self.assertIn("固定输出 4 个分镜", src)
        self.assertNotIn("10字内", src)

    def test_reverse_prompt_requires_structured_action_detail(self):
        """反推 prompt 必须要求时间轴、六层结构、人物/镜头动作及 500-800 字。"""
        import inspect
        src = inspect.getsource(self.breakdown._reverse_prompt_from_frames)
        self.assertIn("500-800 字", src)
        self.assertNotIn("150-300 字", src)
        self.assertIn("六个层次", src)
        self.assertIn("动作与时序", src)
        self.assertIn("表情、视线、手势、肢体姿态、走位", src)
        self.assertIn("跟随、推进、拉远、摇移或转场", src)
        self.assertIn("起始—发展—结束", src)
        self.assertIn("镜头至少 5 项（景别、视角、构图和整体运镜风格）", src)
        self.assertIn("时间轴由程序根据真实视频时长生成", src)
        self.assertIn("你不要输出、计算或修改任何时间", src)
        self.assertIn("segments 必须恰好包含", src)
        self.assertIn("严格依据关键帧还原镜头出现顺序", src)
        self.assertIn("不要泛化成另一条“同风格原创”视频", src)
        self.assertIn("不新增人物、道具、镜头或无关情节", src)

    def test_reverse_prompt_requires_minimum_detail_counts(self):
        import inspect
        src = inspect.getsource(self.breakdown._reverse_prompt_from_frames)
        self.assertIn("主体至少 5 项", src)
        self.assertIn("场景至少 5 项", src)
        self.assertIn("动作与时序至少 8 项", src)
        self.assertIn("镜头至少 5 项", src)
        self.assertIn("光线与色调至少 4 项", src)
        self.assertIn("节奏与情绪钩子至少 3 项", src)
        self.assertIn("左侧早于右侧", src)
        self.assertIn("图片顺序代表时间推进", src)

    def test_chat_multimodal_supports_reverse_output_and_image_options(self):
        import inspect
        signature = str(inspect.signature(self.breakdown._chat_multimodal))
        self.assertIn("max_tokens=None", signature)
        self.assertIn("image_detail='low'", signature)
        captured = []
        os.environ["REVERSE_ZHIPU_KEY"] = "zhipu-test-key"

        def fake_zhipu(body, api_key):
            captured.append(body)
            return {"choices": [{"message": {"content": "结果"}}]}

        self.breakdown._post_zhipu = fake_zhipu
        with tempfile.TemporaryDirectory() as directory:
            frame = self._frame_file(directory)
            self.breakdown._chat_multimodal(
                "system", "user", [frame], max_tokens=1800, image_detail=None
            )
            self.breakdown._chat_multimodal("system", "user", [frame])

        reverse_body, default_body = captured
        self.assertEqual(reverse_body["max_tokens"], 1800)
        reverse_image = reverse_body["messages"][1]["content"][1]["image_url"]
        self.assertNotIn("detail", reverse_image)
        self.assertNotIn("max_tokens", default_body)
        default_image = default_body["messages"][1]["content"][1]["image_url"]
        self.assertEqual(default_image["detail"], "low")

    def test_do_breakdown_reverse_prompt_returns_prompt_and_keeps_asr_flag(self):
        import os, tempfile
        thumb = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        thumb.write(bytes.fromhex(
            "ffd8ffe000104a46494600010101006000600000ffdb004300080606070605080707070909080a0c"
            "140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c20242e2720222c231c1c2837292c3031343434"
            "1f27393d38323c2e333432ffdb0043010909090c0b0c180d0d1832211c21323232323232323232"
            "323232323232323232323232323232323232323232323232323232323232323232323232323232"
            "ffc00011080001000103012200021101031101ffc4001400010000000000000000000000000000"
            "0008ffc40014100100000000000000000000000000000000ffda0008010100013f10c9b0a3c4ff"
            "d9"
        ))
        thumb.close()
        calls = self._install_fake_env(
            json.dumps(
                {"segments": self._detailed_reverse_objects()},
                ensure_ascii=False,
            ),
            transcript=None,
        )
        self.breakdown._extract_frames = lambda video_path, count, duration, scale_width=512, min_frames=None: (
            "fake-frame-dir",
            [thumb.name] * 8,
        )
        self.breakdown._pair_reverse_frames = lambda frame_dir, frames: list(frames)[:4]

        try:
            result = self.breakdown._do_breakdown(
                {"_job_id": 14, "mode": "reverse_prompt"},
                {"platform": "douyin", "id": "rev-1"},
                "https://example.test/post/reverse",
                "reverse_prompt",
            )
        finally:
            os.unlink(thumb.name)

        self.assertEqual(result["type"], "breakdown_reverse")
        self.assertEqual(result["source_platform"], "douyin")
        self.assertIn("白衣人物居中", result["prompt"])
        self.assertEqual(result["frame_count"], 8)
        self.assertEqual(len(result["frame_thumbnails"]), 4)
        self.assertTrue(result["frame_thumbnails"][0].startswith("data:image/jpeg;base64,"))
        self.assertFalse(result["asr_failed"])
        self.assertIn("反推出一条可直接用于视频模型生成同款视频的中文执行提示词", calls["usermsg"])
        self.assertIn("严格只输出用户指定结构的 JSON 对象", calls["sysmsg"])
        self.assertEqual(calls["phases"], ["downloading", "extracting_frames", "transcribing", "analyzing"])

    def test_breakdown_scenes_retries_once_when_parse_fails(self):
        calls = []
        original_parse = self.breakdown._parse_breakdown_json
        try:
            def fake_chat_multimodal(sysmsg, usermsg, frames, temp=0.7, **kwargs):
                calls.append((sysmsg, usermsg, list(frames), temp, kwargs))
                return (
                    'first' if len(calls) == 1
                    else '{"scenes":[{"dur":"3s","scene":"产品展示","line":""}],"analysis":"ok"}'
                )

            seen = {"count": 0}
            def fake_parse(raw):
                seen["count"] += 1
                if seen["count"] == 1:
                    raise ValueError("拆解结果解析失败，请重试")
                return original_parse(raw)

            self.breakdown._chat_multimodal = fake_chat_multimodal
            self.breakdown._parse_breakdown_json = fake_parse
            result = self.breakdown._breakdown_scenes_from_frames("标题", 18, "douyin", "文案", ["f1.jpg"])
        finally:
            self.breakdown._parse_breakdown_json = original_parse

        self.assertEqual(result["analysis"], "ok")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][4]["max_tokens"], 3200)
        self.assertEqual(calls[1][4]["max_tokens"], 2400)
        self.assertIn("上一次输出未形成完整 JSON", calls[1][1])

    def test_breakdown_reverse_prompt_calls_model_once(self):
        calls = []
        def fake_chat_multimodal(sysmsg, usermsg, frames, temp=0.7, **kwargs):
            calls.append((sysmsg, usermsg, list(frames), temp, kwargs))
            return ''

        self.breakdown._chat_multimodal = fake_chat_multimodal
        with self.assertRaisesRegex(ValueError, "反推结果解析失败，请重试"):
            self.breakdown._reverse_prompt_from_frames("标题", 18, "douyin", "文案", ["f1.jpg"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][3], 0.1)
        self.assertEqual(calls[0][4], {"max_tokens": 2400, "image_detail": None})

    def test_reverse_prompt_timeline_is_code_generated_and_gap_free(self):
        calls = []

        def fake_chat_multimodal(sysmsg, usermsg, frames, temp=0.7, **kwargs):
            calls.append((usermsg, temp, kwargs))
            return json.dumps(
                {"segments": self._detailed_reverse_objects()},
                ensure_ascii=False,
            )

        self.breakdown._chat_multimodal = fake_chat_multimodal
        prompt = self.breakdown._reverse_prompt_from_frames(
            "海边舞蹈", 11.434, "douyin", "", ["f1.jpg"]
        )

        lines = prompt.splitlines()
        self.assertEqual(len(lines), 4)
        self.assertTrue(lines[0].startswith("[00:00-00:02.858]"))
        self.assertTrue(lines[-1].startswith("[00:08.575-00:11.434]"))
        self.assertNotIn("00:11.434-00:11.434", prompt)
        self.assertIn("不要输出、计算或修改任何时间", calls[0][0])
        self.assertEqual(calls[0][1], 0.1)
        self.assertEqual(calls[0][2], {"max_tokens": 2400, "image_detail": None})

    def test_reverse_prompt_composes_structured_segment_fields(self):
        structured = {
            "segments": [
                {
                    "subject": "白衣人物位于画面左侧",
                    "scene": "傍晚海滩与远处海平线",
                    "action": "人物向右迈步并抬起双臂",
                    "camera": "中景横向跟随",
                    "lighting": "夕阳逆光形成暖色轮廓",
                    "sound": "舒缓背景音乐",
                    "continuity": "承接入场动作并转向下一段旋转",
                }
            ]
        }
        segments = self.breakdown._parse_reverse_segments(
            json.dumps(structured, ensure_ascii=False), 1
        )
        self.assertEqual(len(segments), 1)
        for label in ("主体：", "场景：", "动作：", "镜头：", "光影：", "声音：", "衔接："):
            self.assertIn(label, segments[0])

    def test_reverse_prompt_requests_structured_fields_and_larger_budget(self):
        calls = []

        def fake_chat_multimodal(sysmsg, usermsg, frames, temp=0.7, **kwargs):
            calls.append((usermsg, kwargs))
            return json.dumps(
                {"segments": self._detailed_reverse_objects()},
                ensure_ascii=False,
            )

        self.breakdown._chat_multimodal = fake_chat_multimodal
        self.breakdown._reverse_prompt_from_frames(
            "标题", 18, "douyin", "", ["f1.jpg"]
        )
        self.assertEqual(calls[0][1]["max_tokens"], 2400)
        self.assertIn("每段目标 125-180 个中文字符", calls[0][0])
        for field in ("subject", "scene", "action", "camera", "lighting", "sound", "continuity"):
            self.assertIn(field, calls[0][0])

    def test_reverse_prompt_scales_segment_length_for_short_video(self):
        calls = []

        def fake_chat_multimodal(sysmsg, usermsg, frames, **kwargs):
            calls.append(usermsg)
            segment = self._detailed_reverse_objects(1)[0]
            segment = {key: value * 4 for key, value in segment.items()}
            return json.dumps({"segments": [segment]}, ensure_ascii=False)

        self.breakdown._chat_multimodal = fake_chat_multimodal
        self.breakdown._reverse_prompt_from_frames(
            "短视频", 2.5, "douyin", "", ["f1.jpg"]
        )
        self.assertIn("每段目标 500-720 个中文字符", calls[0])

    def test_reverse_transcript_filters_implausible_density_and_repetition(self):
        self.assertTrue(
            self.breakdown._reverse_transcript_is_abnormal("内容" * 100, 11.434)
        )
        self.assertTrue(
            self.breakdown._reverse_transcript_is_abnormal("重复句子" * 30, 60)
        )
        self.assertFalse(
            self.breakdown._reverse_transcript_is_abnormal(
                "人物在海边缓慢走动，随后转身看向镜头。", 12
            )
        )

    def test_reverse_prompt_rejects_wrong_segment_count_without_retry(self):
        calls = []

        def fake_chat_multimodal(*args, **kwargs):
            calls.append(kwargs)
            return '{"segments":["只有一段"]}'

        self.breakdown._chat_multimodal = fake_chat_multimodal
        with self.assertRaisesRegex(ValueError, "段数错误"):
            self.breakdown._reverse_prompt_from_frames(
                "标题", 11.434, "douyin", "", ["f1.jpg"]
            )
        self.assertEqual(len(calls), 1)

    def test_reverse_prompt_recovers_plain_text_without_second_model_call(self):
        plain = "\n".join(
            self.breakdown._compose_reverse_segment(item)
            for item in self._detailed_reverse_objects()
        )
        calls = []

        def fake_chat_multimodal(*args, **kwargs):
            calls.append(kwargs)
            return plain

        self.breakdown._chat_multimodal = fake_chat_multimodal
        prompt = self.breakdown._reverse_prompt_from_frames(
            "标题", 11.434, "douyin", "", ["f1.jpg"]
        )
        self.assertEqual(len(prompt.splitlines()), 4)
        self.assertTrue(prompt.splitlines()[-1].startswith("[00:08.575-00:11.434]"))
        self.assertEqual(len(calls), 1)

    def test_reverse_prompt_rejects_placeholder_segments(self):
        raw = json.dumps({
            "segments": [
                "第一段画面描述",
                "第二段画面描述",
                "第三段画面描述",
                "第四段画面描述",
            ]
        }, ensure_ascii=False)
        with self.assertRaisesRegex(ValueError, "内容不完整"):
            self.breakdown._parse_reverse_segments(raw, 4)

    def test_reverse_prompt_rejects_long_structured_json_with_wrong_count(self):
        one_long_segment = self._detailed_reverse_objects(1)[0]
        one_long_segment = {
            key: value * 4 for key, value in one_long_segment.items()
        }
        raw = json.dumps({"segments": [one_long_segment]}, ensure_ascii=False)
        with self.assertRaisesRegex(ValueError, "需要4段，实际1段"):
            self.breakdown._parse_reverse_segments(raw, 4)

    def test_reverse_prompt_rejects_missing_structured_field(self):
        segments = self._detailed_reverse_objects()
        segments[0].pop("camera")
        raw = json.dumps({"segments": segments}, ensure_ascii=False)
        with self.assertRaisesRegex(ValueError, "第1段缺少字段：camera"):
            self.breakdown._parse_reverse_segments(raw, 4)

    def test_reverse_prompt_rejects_short_structured_output(self):
        calls = []

        def fake_chat_multimodal(*args, **kwargs):
            calls.append(kwargs)
            short = {
                key: "画面细节" for key, _label in
                self.breakdown._REVERSE_SEGMENT_FIELDS
            }
            return json.dumps({"segments": [short] * 4}, ensure_ascii=False)

        self.breakdown._chat_multimodal = fake_chat_multimodal
        with self.assertRaisesRegex(ValueError, "第1段过于简略"):
            self.breakdown._reverse_prompt_from_frames(
                "标题", 11.434, "douyin", "", ["f1.jpg"]
            )
        self.assertEqual(len(calls), 1)

    def test_duration_normalization_preserves_milliseconds(self):
        self.assertEqual(
            self.breakdown._normalize_duration_seconds(11434),
            11.434,
        )
        self.assertEqual(
            self.breakdown._normalize_duration_seconds(18.32),
            18.32,
        )

    def test_clean_reverse_prompt_does_not_truncate_long_output(self):
        raw = "画" * 850
        self.assertEqual(self.breakdown._clean_reverse_prompt(raw), raw)

    def test_reverse_mode_extracts_eight_high_resolution_frames_and_pairs_them(self):
        calls = self._install_fake_env(
            json.dumps(
                {"segments": self._detailed_reverse_objects()},
                ensure_ascii=False,
            ),
            transcript=[],
        )

        def fake_extract(path, count, duration, scale_width=512, min_frames=None):
            calls["extract_args"] = (count, scale_width, min_frames)
            return "frames-dir", ["f%d.jpg" % i for i in range(1, 9)]

        def fake_pair(frame_dir, frames):
            calls["pair_args"] = (frame_dir, list(frames))
            return ["p1.jpg", "p2.jpg", "p3.jpg", "p4.jpg"]

        self.breakdown._extract_frames = fake_extract
        had_pair = hasattr(self.breakdown, "_pair_reverse_frames")
        original_pair = getattr(self.breakdown, "_pair_reverse_frames", None)
        self.breakdown._pair_reverse_frames = fake_pair
        def fake_chat(sysmsg, usermsg, frames, **kwargs):
            calls["frames"] = list(frames)
            return json.dumps(
                {"segments": self._detailed_reverse_objects()},
                ensure_ascii=False,
            )
        self.breakdown._chat_multimodal = fake_chat
        try:
            result = self.breakdown._do_breakdown(
                {"_job_id": 80, "mode": "reverse_prompt"},
                {"platform": "douyin", "id": "detail-depth"},
                "https://example.test/detail-depth",
                "reverse_prompt",
            )
        finally:
            if had_pair:
                self.breakdown._pair_reverse_frames = original_pair
            else:
                delattr(self.breakdown, "_pair_reverse_frames")

        self.assertEqual(calls["extract_args"], (8, 1024, 8))
        self.assertEqual(calls["pair_args"][1], ["f%d.jpg" % i for i in range(1, 9)])
        self.assertEqual(calls["frames"], ["p1.jpg", "p2.jpg", "p3.jpg", "p4.jpg"])
        self.assertEqual(result["frame_count"], 8)

    def test_pair_reverse_frames_preserves_time_order(self):
        pair_frames = getattr(self.breakdown, "_pair_reverse_frames", None)
        self.assertIsNotNone(pair_frames, "_pair_reverse_frames is missing")
        original_run = self.breakdown.subprocess.run
        commands = []

        def fake_run(command, **kwargs):
            commands.append(command)
            return type("Completed", (), {"returncode": 0})()

        self.breakdown.subprocess.run = fake_run
        try:
            outputs = pair_frames("frames-dir", ["f%d.jpg" % i for i in range(1, 9)])
        finally:
            self.breakdown.subprocess.run = original_run

        self.assertEqual(len(outputs), 4)
        self.assertEqual([(cmd[cmd.index("-i") + 1], cmd[cmd.index("-i", cmd.index("-i") + 1) + 1]) for cmd in commands], [
            ("f1.jpg", "f2.jpg"), ("f3.jpg", "f4.jpg"),
            ("f5.jpg", "f6.jpg"), ("f7.jpg", "f8.jpg"),
        ])
        self.assertTrue(all("hstack=inputs=2" in cmd for cmd in commands))

    def test_pair_reverse_frames_rejects_fewer_than_eight(self):
        pair_frames = getattr(self.breakdown, "_pair_reverse_frames", None)
        self.assertIsNotNone(pair_frames, "_pair_reverse_frames is missing")
        with self.assertRaisesRegex(ValueError, "反推高清帧不足 8 张"):
            pair_frames("frames-dir", ["f%d.jpg" % i for i in range(1, 8)])

    def test_gen_breakdown_rejects_unknown_mode(self):
        with self.assertRaisesRegex(ValueError, "mode 仅支持 scenes / reverse_prompt"):
            self.breakdown.gen_breakdown({"url": "https://example.test/v/1", "mode": "mystery"})

    def test_parse_breakdown_json_accepts_fenced_json(self):
        result = self.breakdown._parse_breakdown_json(
            '```json\n{"scenes":[{"dur":"3s","scene":"门头","line":"欢迎来到门店"}],"analysis":"先钩子再转化"}\n```'
        )

        self.assertEqual(result["analysis"], "先钩子再转化")
        self.assertEqual(result["scenes"][0]["scene"], "门头")

    def test_parse_breakdown_json_accepts_complete_json_without_closing_fence(self):
        result = self.breakdown._parse_breakdown_json(
            '```json\n{"scenes":[{"dur":"3s","scene":"门店外景","line":""}],"analysis":"完整对象"}'
        )

        self.assertEqual(result["analysis"], "完整对象")
        self.assertEqual(result["scenes"][0]["scene"], "门店外景")

    def test_parse_breakdown_json_accepts_wrapped_prose(self):
        result = self.breakdown._parse_breakdown_json(
            '下面是拆解结果，请直接取 JSON：\n```json\n{"scenes":[{"dur":"5s","scene":"产品特写","line":"先看成分"}],"analysis":"中段突出卖点"}\n```\n请查收。'
        )

        self.assertEqual(result["analysis"], "中段突出卖点")
        self.assertEqual(result["scenes"][0]["line"], "先看成分")

    def test_parse_breakdown_json_ignores_trailing_braces_in_prose(self):
        result = self.breakdown._parse_breakdown_json(
            '{"scenes":[{"dur":"4s","scene":"护理镜头","line":"重点看手法"}],"analysis":"结尾给行动指令"}\n备注：字段 {analysis} 已生成。'
        )

        self.assertEqual(result["analysis"], "结尾给行动指令")
        self.assertEqual(result["scenes"][0]["dur"], "4s")

    def test_parse_breakdown_json_raises_same_error_for_invalid_output(self):
        with self.assertRaisesRegex(ValueError, "拆解结果解析失败，请重试"):
            self.breakdown._parse_breakdown_json("not json at all")

    def test_parse_breakdown_json_raises_same_error_for_invalid_output(self):
        with self.assertRaisesRegex(ValueError, "拆解结果解析失败，请重试"):
            self.breakdown._parse_breakdown_json("not json at all")

    def test_do_breakdown_records_asr_failure(self):
        calls = {}

        class FakeTikHub:
            @staticmethod
            def detail(platform, item_id, note_type=None):
                calls["detail"] = (platform, item_id, note_type)
                return {
                    "play_url": "https://example.test/demo.mp4",
                    "duration": 18,
                    "title": "团购探店案例",
                }

            @staticmethod
            def download_to_file(play_url, deadline, filename):
                calls["download"] = (play_url, filename)

            @staticmethod
            def transcript(det, video_path=None):
                calls["transcript"] = (det.get("title"), video_path)
                raise RuntimeError("ASR service unavailable")

        self.breakdown._heartbeat = lambda job_id, phase: calls.setdefault("phases", []).append(phase)
        self.breakdown._extract_frames = lambda video_path, count, duration, scale_width=512: (
            "fake-frame-dir",
            ["frame_1.jpg", "frame_2.jpg"],
        )

        def fake_chat_multimodal(sysmsg, usermsg, frames, temp=0.7, **kwargs):
            calls["sysmsg"] = sysmsg
            calls["usermsg"] = usermsg
            calls["frames"] = list(frames)
            return '{"scenes":[{"dur":"3s","scene":"门头","line":"欢迎光临"}],"analysis":"探店视频"}'

        self.breakdown._chat_multimodal = fake_chat_multimodal
        self.breakdown.tempfile.NamedTemporaryFile = lambda suffix="", delete=False: type("Tmp", (), {"name": "fake-video.mp4"})()
        sys.modules["tikhub"] = FakeTikHub

        result = self.breakdown._do_breakdown(
            {"_job_id": 13},
            {"platform": "douyin", "id": "abc456"},
            "https://example.test/post/3",
        )

        self.assertTrue(result["asr_failed"])
        self.assertIn("无人物口播或转写不可用", calls["usermsg"])
        self.assertEqual(calls["phases"], ["downloading", "extracting_frames", "transcribing", "analyzing"])

    def test_short_transcript_treated_as_no_speech(self):
        """转写文本过短（<8字，≈纯音乐/歌舞）按无口播处理，prompt 要求 line 返回空串"""
        calls = self._install_fake_env(
            '{"scenes":[{"dur":"3s","scene":"舞者起舞","line":""}],"analysis":"歌舞视频"}',
            transcript=[{"start": 0, "end": 3, "text": "嗯啊"}],
        )

        result = self.breakdown._do_breakdown(
            {"_job_id": 60},
            {"platform": "douyin", "id": "music-video"},
            "https://example.test/post/music",
        )

        self.assertFalse(result["asr_failed"])
        self.assertIn("无人物口播或转写不可用", calls["usermsg"])
        self.assertIn("歌词、听写乱码", calls["usermsg"])

    def test_scenes_prompt_allows_empty_line_for_music_videos(self):
        import inspect
        src = inspect.getsource(self.breakdown._breakdown_scenes_from_frames)
        self.assertIn("所有 line 输出空串", src)

    def test_heartbeat_uses_prefixed_key_to_avoid_collision(self):
        import inspect
        src = inspect.getsource(self.breakdown._heartbeat)
        self.assertIn('"_hb_phase"', src)
        self.assertNotIn('"phase"', src)

    def test_iter_json_objects_skips_oversized_input(self):
        big = "x" * 50001
        result = list(self.breakdown._iter_json_objects(big))
        self.assertEqual(result, [])

    def test_iter_json_objects_handles_normal_input(self):
        result = list(self.breakdown._iter_json_objects('{"a":1} extra {"b":2}'))
        self.assertEqual(len(result), 2)
        self.assertIn('{"a":1}', result)
        self.assertIn('{"b":2}', result)

    def test_extract_frames_clamps_count_to_range(self):
        import inspect
        src = inspect.getsource(self.breakdown._extract_frames)
        self.assertIn("max(2, min(count, 12))", src)

    def test_reverse_extract_falls_back_when_scene_detection_returns_six_frames(self):
        import inspect
        import shutil
        signature = str(inspect.signature(self.breakdown._extract_frames))
        self.assertIn("min_frames=None", signature)
        original_run = self.breakdown.subprocess.run
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            output_pattern = command[-1]
            frame_total = 6 if len(calls) == 1 else 8
            for index in range(1, frame_total + 1):
                Path(output_pattern.replace("%d", str(index))).write_bytes(b"jpeg")
            return type("Completed", (), {"returncode": 0})()

        self.breakdown.subprocess.run = fake_run
        frame_dir = None
        try:
            frame_dir, frames = self.breakdown._extract_frames(
                "video.mp4", 8, 30, scale_width=1024, min_frames=8
            )
        finally:
            self.breakdown.subprocess.run = original_run

        try:
            self.assertEqual(len(calls), 2)
            self.assertEqual(len(frames), 8)
            self.assertIn("fps=", calls[1][calls[1].index("-vf") + 1])
        finally:
            if frame_dir:
                shutil.rmtree(frame_dir, ignore_errors=True)

    def test_gen_breakdown_single_url_still_works(self):
        calls = self._install_fake_env(
            '{"scenes":[{"dur":"3s","scene":"门头","line":"欢迎"}],"analysis":"ok"}'
        )
        sys.modules["tikhub"].parse_link = lambda url: {"platform": "douyin", "id": "abc123"}

        result = self.breakdown.gen_breakdown({"url": "https://example.test/v/1", "_job_id": 20})

        self.assertEqual(result["type"], "breakdown")
        self.assertEqual(result["source_platform"], "douyin")

    def test_gen_breakdown_batch_urls_returns_combined_results(self):
        calls = {}

        class FakeTikHub:
            @staticmethod
            def parse_link(url):
                return {"platform": "douyin", "id": "abc" + url[-1]}
            @staticmethod
            def detail(platform, item_id, note_type=None):
                return {
                    "play_url": "https://example.test/demo.mp4",
                    "duration": 18,
                    "title": "测试视频",
                }
            @staticmethod
            def download_to_file(play_url, deadline, filename):
                pass
            @staticmethod
            def transcript(det, video_path=None):
                return [{"start": 0, "end": 3, "text": "测试文案"}]

        self.breakdown._heartbeat = lambda job_id, phase: None
        self.breakdown._extract_frames = lambda video_path, count, duration, scale_width=512: ("d", ["f1.jpg", "f2.jpg"])
        self.breakdown._chat_multimodal = lambda sysmsg, usermsg, frames, temp=0.7, **kwargs: '{"scenes":[{"dur":"3s","scene":"画面","line":"口播"}],"analysis":"分析"}'
        self.breakdown.tempfile.NamedTemporaryFile = lambda suffix="", delete=False: type("Tmp", (), {"name": "f.mp4"})()
        sys.modules["tikhub"] = FakeTikHub

        result = self.breakdown.gen_breakdown({"urls": ["https://example.test/v/1", "https://example.test/v/2"], "_job_id": 21})

        self.assertEqual(result["type"], "breakdown_batch")
        self.assertEqual(result["total"], 2)
        self.assertEqual(len(result["results"]), 2)
        self.assertEqual(len(result["errors"]), 0)

    def test_gen_breakdown_batch_rejects_more_than_5(self):
        with self.assertRaisesRegex(ValueError, "最多 5 条"):
            self.breakdown.gen_breakdown({"urls": ["http://a.test/1"] * 6, "_job_id": 22})

    def test_do_breakdown_includes_frame_thumbnails(self):
        import tempfile
        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        tmp.write(bytes.fromhex(
            "ffd8ffe000104a46494600010101006000600000ffdb004300080606070605080707070909080a0c"
            "140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c20242e2720222c231c1c2837292c3031343434"
            "1f27393d38323c2e333432ffdb0043010909090c0b0c180d0d1832211c21323232323232323232"
            "323232323232323232323232323232323232323232323232323232323232323232323232323232"
            "ffc00011080001000103012200021101031101ffc4001400010000000000000000000000000000"
            "0008ffc40014100100000000000000000000000000000000ffda0008010100013f10c9b0a3c4ff"
            "d9"
        ))
        tmp.close()

        calls = self._install_fake_env(
            '{"scenes":[{"dur":"3s","scene":"门头","line":"欢迎"}],"analysis":"ok"}'
        )
        self.breakdown._extract_frames = lambda video_path, count, duration, scale_width=512: (
            "fake-frame-dir",
            [tmp.name],
        )

        result = self.breakdown._do_breakdown(
            {"_job_id": 30},
            {"platform": "douyin", "id": "thumb-test"},
            "https://example.test/post/thumb",
        )

        self.assertIn("frame_thumbnails", result)
        self.assertEqual(len(result["frame_thumbnails"]), 1)
        self.assertTrue(result["frame_thumbnails"][0].startswith("data:image/jpeg;base64,"))
        import os; os.unlink(tmp.name)

    def test_do_breakdown_retries_once_on_parse_failure(self):
        self._install_fake_env('{"scenes":[]}')
        responses = [
            "这不是 JSON，完全无法解析",
            '{"scenes":[{"dur":"3s","scene":"门头","line":"欢迎光临"}],"analysis":"ok"}',
        ]
        calls = {"n": 0}

        def flaky_chat(sysmsg, usermsg, frames, temp=0.7, **kwargs):
            r = responses[calls["n"]]
            calls["n"] += 1
            return r

        self.breakdown._chat_multimodal = flaky_chat

        result = self.breakdown._do_breakdown(
            {"_job_id": 40},
            {"platform": "douyin", "id": "retry-ok"},
            "https://example.test/post/retry",
        )

        self.assertEqual(calls["n"], 2)
        self.assertEqual(result["scenes"][0]["scene"], "门头")

    def test_do_breakdown_retries_once_on_empty_scenes(self):
        self._install_fake_env('{"scenes":[]}')
        responses = [
            '{"scenes":[],"analysis":"没有识别出分镜"}',
            '{"scenes":[{"dur":"3s","scene":"产品特写","line":""}],"analysis":"ok"}',
        ]
        calls = {"n": 0}

        def empty_then_valid(sysmsg, usermsg, frames, temp=0.7, **kwargs):
            response = responses[calls["n"]]
            calls["n"] += 1
            return response

        self.breakdown._chat_multimodal = empty_then_valid

        result = self.breakdown._do_breakdown(
            {"_job_id": 42},
            {"platform": "douyin", "id": "empty-retry-ok"},
            "https://example.test/post/empty-retry",
        )

        self.assertEqual(calls["n"], 2)
        self.assertEqual(result["scenes"][0]["scene"], "产品特写")

    def test_do_breakdown_uses_openai_after_two_empty_scene_results(self):
        self._install_fake_env('{"scenes":[]}')
        calls = []

        def empty_then_fallback(sysmsg, usermsg, frames, temp=0.7, **kwargs):
            calls.append(kwargs)
            if kwargs.get("provider") == "openai":
                return '{"scenes":[{"dur":"3s","scene":"锦鲤池全景","line":""}],"analysis":"恢复成功"}'
            return '{"scenes":[{"dur":"3s","scene":"  ","line":""}],"analysis":"empty"}'

        self.breakdown._chat_multimodal = empty_then_fallback

        result = self.breakdown._do_breakdown(
            {"_job_id": 43},
            {"platform": "douyin", "id": "empty-retry-fail"},
            "https://example.test/post/empty-retry-fail",
        )

        self.assertEqual(result["scenes"][0]["scene"], "锦鲤池全景")
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[-1]["provider"], "openai")

    def test_do_breakdown_raises_after_zhipu_and_openai_parse_failures(self):
        self._install_fake_env('{"scenes":[]}')
        calls = []

        def invalid_everywhere(sysmsg, usermsg, frames, temp=0.7, **kwargs):
            calls.append(kwargs)
            return "not json at all"

        self.breakdown._chat_multimodal = invalid_everywhere

        with self.assertRaisesRegex(ValueError, "拆解结果解析失败，请重试"):
            self.breakdown._do_breakdown(
                {"_job_id": 41},
                {"platform": "douyin", "id": "retry-fail"},
                "https://example.test/post/retry-fail",
            )
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[-1]["provider"], "openai")

    def test_scene_validation_rejects_prompt_placeholders(self):
        for result in (
            {"scenes": [{"dur": "3s", "scene": "画面描述(20-40字)", "line": ""}]},
            {"scenes": [{"dur": "3s", "scene": "人物站在门口", "line": "口播台词"}]},
            {"scenes": [{"dur": "3s", "scene": "具体画面", "line": "对应口播或空串"}]},
        ):
            with self.subTest(result=result):
                with self.assertRaisesRegex(ValueError, "模板占位内容"):
                    self.breakdown._validate_scene_breakdown(result)

    def test_do_breakdown_normalizes_millisecond_duration(self):
        """tikhub 返回毫秒时长（18320），结果必须统一成秒且保留小数精度。"""
        self._install_fake_env(
            '{"scenes":[{"dur":"3s","scene":"门头","line":"欢迎"}],"analysis":"ok"}'
        )
        sys.modules["tikhub"].detail = lambda platform, item_id, note_type=None: {
            "play_url": "https://example.test/demo.mp4",
            "duration": 18320,
            "title": "毫秒时长视频",
        }

        result = self.breakdown._do_breakdown(
            {"_job_id": 50},
            {"platform": "douyin", "id": "ms-duration"},
            "https://example.test/post/ms",
        )

        self.assertEqual(result["duration"], 18.32)

    def test_run_job_settles_batch_breakdown_refund(self):
        """run_job 必须对批量拆解结果结算退点（结算本体在 points.settle_breakdown_batch）"""
        core_src = (Path(__file__).resolve().parents[1] / "server/content_domains/core.py").read_text(encoding="utf-8")
        self.assertIn("settle_breakdown_batch", core_src)

if __name__ == "__main__":
    unittest.main()
