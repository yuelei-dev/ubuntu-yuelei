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
from unittest.mock import patch
from unittest import mock


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
        self.orig_reverse_frame_pair_ssim = self.breakdown._reverse_frame_pair_ssim
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
        self.breakdown._reverse_frame_pair_ssim = self.orig_reverse_frame_pair_ssim
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
        variants = [
            (
                "迈步进入画面，抬起右臂转身，视线转向镜头，最后侧身停住",
                "中景平视横向跟随，人物转身时缓慢推进，动作结束后停稳",
            ),
            (
                "双手从腰侧抬到肩部，向左跨步并连续旋转，最后背向镜头",
                "全景低机位固定起拍，旋转阶段顺时针环绕，结尾轻微拉远",
            ),
            (
                "双手从胸前下移后快速前行，头部转向远处，随后张开双臂减速",
                "近景捕捉手部动作，再切中景侧向跟拍，并向海平线摇移",
            ),
            (
                "沿浪线后退两步，身体前倾触碰水面，起身回望并挥手收束",
                "俯拍交代浪花位置，下降到平视跟随，挥手时缓慢向后拉远",
            ),
        ]
        result = []
        for index in range(1, count + 1):
            action, camera = variants[(index - 1) % len(variants)]
            item = {
                "subject": "第%d段白衣人物居中，服装、朝向和姿态清晰一致" % index,
                "scene": "傍晚海滩、近处浪花、远处海平线和低空云层",
                "action": action,
                "camera": camera,
                "lighting": "夕阳逆光形成暖色轮廓，沙面保留柔和高光",
                "sound": "",
                "continuity": "",
                "evidence_frames": {
                    "subject": [1, 2],
                    "scene": [1, 2],
                    "action": [1, 2],
                    "camera": [1, 2],
                    "lighting": [1, 2],
                },
                "continuity_evidence_frames": [],
            }
            item.update(self._generation_ready_structure(
                index=index,
                action=action,
                camera=camera,
            ))
            result.append(item)
        return result

    def _generation_ready_structure(
        self, index=1, action="抬起右手", camera="平视中景", static=False
    ):
        observed = lambda value, frames=(1, 2): {
            "status": "observed",
            "value": value,
            "evidence_frames": list(frames),
        }
        not_applicable = lambda: {
            "status": "not_applicable",
            "value": "",
            "evidence_frames": [],
        }
        start = "双手位于身体两侧"
        end = "右手抬至肩部上方"
        motion_type = "static" if static else "dynamic"
        if static:
            start = end = "主体位置和形态保持一致"
        return {
            "shot_boundary": {
                "type": "continuous",
                "evidence_frames": [1, 2],
            },
            "shots": [
                {
                    "frame": 1,
                    "subject": "白衣人物位于画面中央",
                    "scene": "海滩、浪花和海平线",
                    "camera": camera,
                    "lighting": "暖色逆光",
                    "style": "写实电影质感",
                },
                {
                    "frame": 2,
                    "subject": "白衣人物仍位于画面中央",
                    "scene": "海滩、浪花和海平线",
                    "camera": camera,
                    "lighting": "暖色逆光",
                    "style": "写实电影质感",
                },
            ],
            "generation": {
                "subject": {
                    "identity": observed("人物"),
                    "appearance": observed("黑色长发、白色长衣"),
                    "wardrobe": observed("白色长衣"),
                    "position_scale": observed("画面中央，约占画面高度二分之一"),
                },
                "action": {
                    "motion_type": observed(motion_type),
                    "start": observed(start, (1,)),
                    "process": observed(action),
                    "end": observed(end, (2,)),
                    "direction_speed": observed("手臂向上移动，速度无法精确确认"),
                    "associated_object": not_applicable(),
                },
                "scene": {
                    "foreground": observed("近处浅色浪花"),
                    "midground": observed("人物和湿润沙面"),
                    "background": observed("海平线与低空云层"),
                    "spatial_relationship": observed("人物位于浪花前方、海平线下方"),
                },
                "camera": {
                    "shot_size": observed("中景"),
                    "camera_position": observed("与人物胸部近似等高"),
                    "viewing_angle": observed("平视"),
                    "composition": observed("主体居中，海平线位于画面上部"),
                    "movement": observed(camera),
                },
                "lighting": {
                    "direction_brightness": observed("逆光，主体轮廓较亮"),
                    "color_tone": observed("暖橙色高光与冷色天空"),
                },
                "style": {
                    "visual_style": observed("写实电影画面"),
                    "texture": observed("柔和高光、细腻沙面"),
                },
                "rhythm": {
                    "pacing": observed("本段持续约四秒，动作连续"),
                },
                "continuity": {
                    "retained": not_applicable(),
                    "changed": not_applicable(),
                },
            },
        }

    def _generation_entry(self, index=1, **structure_overrides):
        item = {
            "sound": "",
            "continuity": "",
            "continuity_evidence_frames": [],
        }
        structure = self._generation_ready_structure(index=index)
        structure.update(structure_overrides)
        item.update(structure)
        return self.breakdown._parse_reverse_segment_evidence(
            json.dumps({"segments": [item]}, ensure_ascii=False)
        )

    def _hard_cut_entry(self):
        item = self._generation_ready_structure(index=1)
        item["shot_boundary"] = {
            "type": "hard_cut",
            "evidence_frames": [1, 2],
        }
        item["shots"] = [
            {
                "frame": 1,
                "subject": "无人物，石桥占据画面中央",
                "scene": "夜间古镇建筑和河面",
                "camera": "高机位大远景",
                "lighting": "暖色灯笼点亮冷色夜景",
                "style": "写实夜景",
            },
            {
                "frame": 2,
                "subject": "粉色连帽上衣女性位于画面中央",
                "scene": "夜间木质栏杆和灯笼",
                "camera": "平视中景",
                "lighting": "暖色灯笼照亮人物",
                "style": "写实电影夜景",
            },
        ]
        observed = lambda value, frames=(1, 2): {
            "status": "observed",
            "value": value,
            "evidence_frames": list(frames),
        }
        item["generation"]["subject"] = {
            "identity": observed("镜头A为石桥，镜头B为人物"),
            "appearance": observed("石桥与粉色上衣黑色长发女性"),
            "wardrobe": observed("镜头B女性穿粉色连帽上衣", (2,)),
            "position_scale": observed("镜头A桥为大远景，镜头B人物为中景"),
        }
        item["generation"]["scene"] = {
            "foreground": observed("镜头B木质栏杆", (2,)),
            "midground": observed("镜头A石桥、镜头B女性"),
            "background": observed("古镇建筑、夜空与灯笼"),
            "spatial_relationship": observed("首帧为桥梁远景，尾帧硬切至人物中景"),
        }
        item["generation"]["camera"] = {
            "shot_size": observed("大远景硬切至中景"),
            "camera_position": observed("高机位硬切至人物等高机位"),
            "viewing_angle": observed("俯视硬切至平视"),
            "composition": observed("桥居中硬切至人物居中"),
            "movement": observed("镜头切换，无法证明连续运镜"),
        }
        for key in item["generation"]["action"]:
            item["generation"]["action"][key] = {
                "status": "not_applicable",
                "value": "",
                "evidence_frames": [],
            }
        item.update({
            "sound": "",
            "continuity": "",
            "continuity_evidence_frames": [],
        })
        return self.breakdown._parse_reverse_segment_evidence(
            json.dumps({"segments": [item]}, ensure_ascii=False)
        )

    def _reverse_entry(self, **overrides):
        fields = {
            "subject": "白衣人物位于画面中",
            "scene": "树林背景",
            "action": "抬起右手",
            "camera": "",
            "lighting": "",
            "sound": "",
            "continuity": "",
        }
        fields.update(overrides)
        return {
            "text": self.breakdown._compose_reverse_segment(fields),
            "fields": fields,
            "evidence_frames": {
                key: [1, 2]
                for key in (
                    "subject", "scene", "action", "camera", "lighting"
                )
                if fields.get(key)
            },
        }

    def _global_continuity(self):
        facts = {
            "subject_identity": "同一名白衣人物，黑色长发",
            "wardrobe": "白色长衣，未见服装切换",
            "recurring_scene_objects": "海滩、浪花与远处海平线",
            "scene_style": "户外海边场景",
            "camera_style": "以平视中景为主",
            "lighting_style": "暖色逆光，轮廓高光",
        }
        evidence = {
            key: [1, 3, 5, 7]
            for key in facts
        }
        return {
            "facts": facts,
            "evidence_frames": evidence,
            "frame_count": 8,
            "segment_count": 4,
        }

    def _global_continuity_response(self):
        data = self._global_continuity()
        return json.dumps({
            "global_facts": data["facts"],
            "evidence_frames": data["evidence_frames"],
        }, ensure_ascii=False)

    def test_download_breakdown_video_retries_empty_and_truncated_cdn(self):
        calls = []

        class FakeTikHub:
            @staticmethod
            def download_to_file(url, deadline, destination, max_bytes=None):
                calls.append((url, deadline, destination, max_bytes))
                if url.endswith("empty.mp4"):
                    return 0
                if url.endswith("truncated.mp4"):
                    raise ConnectionError(
                        "下载响应截断：Content-Length=100，实际=20"
                    )
                return 20

        detail = {
            "play_url": "https://cdn.test/empty.mp4",
            "play_urls": [
                "https://cdn.test/empty.mp4",
                "https://cdn.test/truncated.mp4",
                "https://cdn.test/backup.mp4",
            ],
        }
        result = self.breakdown._download_breakdown_video(
            FakeTikHub,
            {"platform": "douyin", "id": "123"},
            detail,
            "video.mp4",
        )

        self.assertEqual([call[0] for call in calls], [
            "https://cdn.test/empty.mp4",
            "https://cdn.test/truncated.mp4",
            "https://cdn.test/backup.mp4",
        ])
        self.assertEqual(len({call[1] for call in calls}), 1)
        self.assertEqual(calls[-1][3], self.breakdown.BREAKDOWN_MAX_DOWNLOAD_BYTES)
        self.assertEqual(result["play_url"], "https://cdn.test/backup.mp4")

    def test_download_breakdown_video_refreshes_once_and_honors_total_budget(self):
        calls = {"download": [], "detail": []}

        class FakeTikHub:
            @staticmethod
            def detail(platform, item_id, note_type=None, fresh=False):
                calls["detail"].append((platform, item_id, note_type, fresh))
                return {
                    "play_url": "https://cdn.test/refreshed.mp4",
                    "duration": 12,
                }

            @staticmethod
            def download_to_file(url, deadline, destination, max_bytes=None):
                calls["download"].append((url, deadline))
                if url.endswith("expired.mp4"):
                    raise TimeoutError("expired")
                return 20

        result = self.breakdown._download_breakdown_video(
            FakeTikHub,
            {"platform": "douyin", "id": "123", "note_type": "video"},
            {"play_url": "https://cdn.test/expired.mp4"},
            "video.mp4",
        )

        self.assertEqual([call[0] for call in calls["download"]], [
            "https://cdn.test/expired.mp4",
            "https://cdn.test/refreshed.mp4",
        ])
        self.assertEqual(len({call[1] for call in calls["download"]}), 1)
        self.assertEqual(calls["detail"], [("douyin", "123", "video", True)])
        self.assertEqual(result["play_url"], "https://cdn.test/refreshed.mp4")

        exhausted_calls = {"download": [], "detail": []}

        class ExhaustedTikHub:
            @staticmethod
            def detail(platform, item_id, note_type=None, fresh=False):
                exhausted_calls["detail"].append(fresh)
                return {"play_url": "https://cdn.test/refreshed.mp4"}

            @staticmethod
            def download_to_file(url, deadline, destination, max_bytes=None):
                exhausted_calls["download"].append((url, deadline))
                raise TimeoutError("first CDN consumed the total budget")

        with patch.object(
            self.breakdown.time, "time", side_effect=[100.0, 100.0, 100.0, 281.0]
        ), self.assertRaisesRegex(TimeoutError, "alternate URLs"):
            self.breakdown._download_breakdown_video(
                ExhaustedTikHub,
                {"platform": "douyin", "id": "123", "note_type": "video"},
                {
                    "play_url": "https://cdn.test/first.mp4",
                    "play_urls": [
                        "https://cdn.test/first.mp4",
                        "https://cdn.test/must-not-start.mp4",
                    ],
                },
                "video.mp4",
            )

        self.assertEqual(
            [call[0] for call in exhausted_calls["download"]],
            ["https://cdn.test/first.mp4"],
        )
        self.assertEqual(
            exhausted_calls["download"][0][1],
            100.0 + self.breakdown.BREAKDOWN_DOWNLOAD_BUDGET,
        )
        self.assertEqual(exhausted_calls["detail"], [])

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
            def download_to_file(play_url, deadline, filename, max_bytes=None):
                calls["download"] = (play_url, filename)
                return 1

            @staticmethod
            def transcript(det, video_path=None):
                calls["transcript"] = (det.get("title"), video_path)
                return transcript if transcript is not None else [{"start": 0, "end": 3, "text": "先看门头"}]

        self.breakdown._heartbeat = lambda job_id, phase: calls.setdefault("phases", []).append(phase)
        self.breakdown._extract_frames = lambda video_path, count, duration, scale_width=512, min_frames=None, uniform=False: (
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

    def _gemini_reverse_result(self, duration=18.0, count=4):
        shots = []
        labels = (
            "A" * 24,
            "B" * 24,
            "C" * 24,
            "D" * 24,
        )
        segment_duration = float(duration) / count
        for index in range(count):
            start = round(index * segment_duration, 1)
            end = round(duration if index == count - 1 else (index + 1) * segment_duration, 1)
            facts = {}
            evidence = {}
            for key in self.breakdown._GEMINI_FACT_FIELDS:
                if key in self.breakdown._GEMINI_OPTIONAL_FACT_FIELDS:
                    facts[key] = "not_applicable"
                    evidence[key] = []
                else:
                    facts[key] = "%s-%s" % (labels[index], key)
                    evidence[key] = [round(start + 0.1, 1)]
            evidence["action_end"] = [round(end - 0.1, 1)]
            shots.append({
                "start_seconds": start,
                "end_seconds": end,
                "cut_from_previous": index > 0,
                "facts": facts,
                "evidence_seconds": evidence,
                "generation_advice": {
                    "aspect_ratio": "16:9",
                    "fps": "24",
                    "camera_control": "preserve observed camera motion",
                    "negative_prompt": "no extra subjects",
                },
            })
        return self.breakdown._parse_gemini_reverse_result(
            json.dumps({"shots": shots}), duration,
        )

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
        """反推必须按段绑定证据，明确禁止字数填充和无证据推断。"""
        import inspect
        src = inspect.getsource(self.breakdown._reverse_segment_messages)
        self.assertNotIn("500-800", src)
        self.assertNotIn("至少 5 项", src)
        self.assertIn("只属于当前时间段", src)
        self.assertIn("逐帧比较主体位置、手臂手腕、身体姿态", src)
        self.assertIn("只有相邻帧确实近乎一致时", src)
        self.assertIn("一律省略", src)
        self.assertIn("没有证据就留空", src)
        self.assertIn("不设最低字数", src)
        self.assertIn("宁可短也不能编造", src)
        self.assertIn("背对镜头或面部被遮挡时，不得描述表情", src)
        self.assertIn("未观察到明显动作变化", src)
        self.assertIn("不得根据画面写“未观察到声音”", src)
        self.assertIn("字幕或屏幕文字只有在本段图片清晰可读时", src)
        self.assertIn("面向树根", src)
        self.assertIn("主体不一定是人物", src)
        self.assertIn("不得因画面无人而把主体留空", src)
        self.assertIn("静态非人物画面也必须形成可生成提示词", src)
        self.assertIn("抽象画面", src)
        self.assertIn("纯色背景", src)
        self.assertIn("不能把主要实体只塞进 scene", src)
        self.assertIn("主体保持静止，未观察到位置或形态变化", src)
        self.assertIn("generation每个槽位", src)
        self.assertIn("unknown不会冒充生成就绪", src)
        self.assertIn("不得让同一次回答中的多个字段互相自证", src)
        self.assertIn("意图词无法由两个端点帧证明", src)

    def test_reverse_prompt_requires_segment_scoped_original_frames(self):
        import inspect
        src = inspect.getsource(self.breakdown._reverse_prompt_from_frames)
        self.assertIn("_reverse_model_frame_groups", src)
        self.assertIn("_reverse_chat_multimodal", src)
        self.assertIn("frame_group", src)
        self.assertIn("max_tokens=2000 if strict_generation else 900", src)
        self.assertNotIn("allow_duplicates", src)
        retry_src = inspect.getsource(self.breakdown._reverse_segment_messages)
        self.assertIn("不要沿用任何历史草稿", retry_src)

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
        self.breakdown._extract_frames = lambda video_path, count, duration, scale_width=512, min_frames=None, uniform=False: (
            "fake-frame-dir",
            [thumb.name] * 8,
        )
        try:
            with patch.object(
                self.breakdown,
                "_gemini_reverse_prompt_from_media",
                return_value=self._gemini_reverse_result(),
            ) as gemini:
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
        self.assertIn("subject:", result["prompt"])
        self.assertIn("generation advice:", result["prompt"])
        self.assertEqual(result["frame_count"], 8)
        self.assertEqual(len(result["frame_thumbnails"]), 8)
        self.assertTrue(result["frame_thumbnails"][0].startswith("data:image/jpeg;base64,"))
        self.assertFalse(result["asr_failed"])
        gemini.assert_called_once()
        self.assertEqual(gemini.call_args.args[1], "video/mp4")
        self.assertIsNotNone(gemini.call_args.kwargs["deadline"])
        self.assertTrue(callable(gemini.call_args.kwargs["heartbeat"]))
        self.assertEqual(
            result["reference_frame_strategy"],
            "explicit_indices_one_per_segment",
        )
        self.assertEqual(result["reference_thumbnail_indices"], [1, 2, 3, 4])
        self.assertEqual(result["audit_thumbnail_indices"], [5, 6, 7, 8])
        self.assertEqual(
            [item["source_frame_index"] for item in result["frame_manifest"]],
            [2, 4, 6, 8, 1, 3, 5, 7],
        )
        self.assertEqual(result["quality_score"]["total"], 100)
        self.assertEqual(result["quality_contract"]["target_score"], 80)
        self.assertEqual(len(result["segment_evidence"]), 4)
        self.assertEqual(
            result["segment_evidence"][0][
                "local_evidence_frames"
            ]["action"],
            [1, 2],
        )
        self.assertEqual(
            result["segment_evidence"][1][
                "source_evidence_frames"
            ]["action"],
            [3, 4],
        )
        self.assertEqual(
            result["sections"]["reverse_audit"]["frame_manifest"],
            result["frame_manifest"],
        )
        self.assertEqual(
            result["sections"]["reverse_audit"]["audit_thumbnail_indices"],
            [5, 6, 7, 8],
        )
        audit_sources = {
            item["source_frame_index"]
            for item in result["frame_manifest"]
        }
        self.assertEqual(audit_sources, set(range(1, 9)))
        assets_store = importlib.import_module("content_domains.assets_store")
        persisted_meta = assets_store._project("breakdown", result)[3]
        self.assertEqual(len(persisted_meta["frame_thumbnails"]), 8)
        self.assertEqual(
            persisted_meta["sections"]["reverse_audit"]["frame_manifest"],
            result["frame_manifest"],
        )
        self.assertEqual(result["global_continuity"]["model_calls"], 0)
        self.assertEqual(result["global_continuity"]["image_count"], 0)
        self.assertEqual(result["analysis_call_budget"], {
            "analysis_deadline_seconds": 540,
            "max_images_per_request": 0,
            "max_video_inputs_per_request": 1,
            "global_model_calls": 0,
            "normal_logical_calls": 1,
            "worst_logical_calls": 2,
            "normal_physical_http_attempts": 1,
            "same_provider_physical_attempts_per_logical": 2,
            "worst_physical_http_attempts": 4,
            "provider": "google",
            "model": "gemini-3.1-pro-preview",
            "http_4xx_retry": False,
            "cross_provider_fallback": False,
        })
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

    def test_breakdown_reverse_prompt_retries_only_failed_segment(self):
        calls = []
        def fake_chat_multimodal(sysmsg, usermsg, frames, temp=0.7, **kwargs):
            calls.append((sysmsg, usermsg, list(frames), temp, kwargs))
            return ''

        self.breakdown._chat_multimodal = fake_chat_multimodal
        with self.assertRaisesRegex(ValueError, "拆解结果解析失败，请重试"):
            self.breakdown._reverse_prompt_from_frames(
                "标题", 18, "douyin", "文案",
                ["f%d.jpg" % index for index in range(1, 9)],
            )
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][2], ["f1.jpg", "f2.jpg"])
        self.assertEqual(calls[1][2], ["f1.jpg", "f2.jpg"])
        self.assertEqual(calls[0][3], 0.1)
        self.assertEqual(calls[0][4], {
            "max_tokens": 900,
            "image_detail": None,
            "provider": "zhipu",
            "model": "glm-4v-plus",
            "allow_provider_fallback": False,
        })
        self.assertIn("不要沿用任何历史草稿", calls[1][1])

    def test_reverse_prompt_timeline_is_code_generated_and_gap_free(self):
        calls = []
        objects = self._detailed_reverse_objects()

        def fake_chat_multimodal(sysmsg, usermsg, frames, temp=0.7, **kwargs):
            calls.append((usermsg, list(frames), temp, kwargs))
            return json.dumps(
                {"segments": [objects[len(calls) - 1]]},
                ensure_ascii=False,
            )

        self.breakdown._chat_multimodal = fake_chat_multimodal
        prompt = self.breakdown._reverse_prompt_from_frames(
            "海边舞蹈", 11.434, "douyin", "",
            ["f%d.jpg" % index for index in range(1, 9)],
        )

        lines = prompt.splitlines()
        self.assertEqual(len(lines), 4)
        self.assertTrue(lines[0].startswith("[00:00.0-00:02.9]"))
        self.assertTrue(lines[-1].startswith("[00:08.6-00:11.4]"))
        self.assertNotIn("00:11.4-00:11.4", prompt)
        self.assertEqual(len(calls), 4)
        self.assertIn("当前时间段：第1/4段", calls[0][0])
        self.assertEqual(calls[0][1], ["f1.jpg", "f2.jpg"])
        self.assertEqual(calls[0][2], 0.1)
        self.assertEqual(calls[0][3], {
            "max_tokens": 900,
            "image_detail": None,
            "provider": "zhipu",
            "model": "glm-4v-plus",
            "allow_provider_fallback": False,
        })

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

    def test_reverse_prompt_requests_structured_fields_without_length_target(self):
        calls = []
        objects = self._detailed_reverse_objects()

        def fake_chat_multimodal(sysmsg, usermsg, frames, temp=0.7, **kwargs):
            calls.append((usermsg, kwargs))
            return json.dumps(
                {"segments": [objects[len(calls) - 1]]},
                ensure_ascii=False,
            )

        self.breakdown._chat_multimodal = fake_chat_multimodal
        self.breakdown._reverse_prompt_from_frames(
            "标题", 18, "douyin", "",
            ["f%d.jpg" % index for index in range(1, 9)],
        )
        self.assertEqual(len(calls), 4)
        self.assertEqual(calls[0][1]["max_tokens"], 900)
        self.assertIn("不设最低字数", calls[0][0])
        self.assertNotIn("每段目标", calls[0][0])
        for field in ("subject", "scene", "action", "camera", "lighting", "sound", "continuity"):
            self.assertIn(field, calls[0][0])

    def test_reverse_prompt_accepts_concise_segment_for_short_video(self):
        calls = []

        def fake_chat_multimodal(sysmsg, usermsg, frames, **kwargs):
            calls.append(usermsg)
            segment = {
                "subject": "白衣人物",
                "scene": "树林",
                "action": "抬起右手",
                "camera": "",
                "lighting": "",
                "sound": "",
                "continuity": "",
                "evidence_frames": {
                    "subject": [1, 2],
                    "scene": [1, 2],
                    "action": [1, 2],
                },
            }
            return json.dumps({"segments": [segment]}, ensure_ascii=False)

        self.breakdown._chat_multimodal = fake_chat_multimodal
        prompt = self.breakdown._reverse_prompt_from_frames(
            "短视频", 2.5, "douyin", "", ["f1.jpg", "f2.jpg"]
        )
        self.assertIn("动作：抬起右手", prompt)
        self.assertEqual(len(calls), 1)
        self.assertIn("不设最低字数", calls[0])

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

    def test_reverse_prompt_rejects_wrong_single_segment_count_after_retry(self):
        calls = []

        def fake_chat_multimodal(*args, **kwargs):
            calls.append(kwargs)
            return '{"segments":[]}'

        self.breakdown._chat_multimodal = fake_chat_multimodal
        with self.assertRaisesRegex(ValueError, "段数错误"):
            self.breakdown._reverse_prompt_from_frames(
                "标题", 11.434, "douyin", "",
                ["f%d.jpg" % index for index in range(1, 9)],
            )
        self.assertEqual(len(calls), 2)

    def test_reverse_prompt_rejects_unstructured_plain_text_after_retry(self):
        plain = self.breakdown._compose_reverse_segment(
            self._detailed_reverse_objects(1)[0]
        )
        calls = []

        def fake_chat_multimodal(*args, **kwargs):
            calls.append(kwargs)
            return plain

        self.breakdown._chat_multimodal = fake_chat_multimodal
        with self.assertRaisesRegex(ValueError, "拆解结果解析失败"):
            self.breakdown._reverse_prompt_from_frames(
                "标题", 11.434, "douyin", "",
                ["f%d.jpg" % index for index in range(1, 9)],
            )
        self.assertEqual(len(calls), 2)

    def test_reverse_prompt_rejects_json_string_without_evidence_fields(self):
        calls = []
        self.breakdown._chat_multimodal = lambda *args, **kwargs: (
            calls.append(args) or
            '{"segments":["只有一段笼统画面描述"]}'
        )
        with self.assertRaisesRegex(ValueError, "必须是结构化对象"):
            self.breakdown._reverse_prompt_from_frames(
                "标题", 2.5, "douyin", "",
                ["f1.jpg", "f2.jpg"],
            )
        self.assertEqual(len(calls), 2)

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
            key: (value * 4 if isinstance(value, str) else value)
            for key, value in one_long_segment.items()
        }
        raw = json.dumps({"segments": [one_long_segment]}, ensure_ascii=False)
        with self.assertRaisesRegex(ValueError, "需要4段，实际1段"):
            self.breakdown._parse_reverse_segments(raw, 4)

    def test_reverse_prompt_allows_unsupported_fields_to_be_omitted(self):
        segments = self._detailed_reverse_objects()
        segments[0].pop("camera")
        raw = json.dumps({"segments": segments}, ensure_ascii=False)
        parsed = self.breakdown._parse_reverse_segments(raw, 4)
        self.assertEqual(len(parsed), 4)
        self.assertNotIn("镜头：", parsed[0])

    def test_reverse_prompt_accepts_short_structured_output(self):
        calls = []
        variants = ["抬右手", "双臂举起", "侧身转头", "绕树伸手"]

        def fake_chat_multimodal(*args, **kwargs):
            calls.append(kwargs)
            short = {
                "subject": "白衣人物",
                "scene": "树林",
                "action": variants[len(calls) - 1],
                "camera": "",
                "lighting": "",
                "sound": "",
                "continuity": "",
                "evidence_frames": {
                    "subject": [1, 2],
                    "scene": [1, 2],
                    "action": [1, 2],
                },
            }
            return json.dumps({"segments": [short]}, ensure_ascii=False)

        self.breakdown._chat_multimodal = fake_chat_multimodal
        prompt = self.breakdown._reverse_prompt_from_frames(
            "标题", 11.434, "douyin", "",
            ["f%d.jpg" % index for index in range(1, 9)],
        )
        self.assertEqual(len(calls), 4)
        self.assertEqual(len(prompt.splitlines()), 4)

    def test_reverse_prompt_rejects_identical_detailed_segments(self):
        segment = self.breakdown._compose_reverse_segment(
            self._detailed_reverse_objects(1)[0]
        )
        with self.assertRaisesRegex(ValueError, "第2段与第1段内容重复"):
            self.breakdown._validate_reverse_prompt_lengths([segment] * 4)

    def test_reverse_prompt_rejects_near_duplicate_detailed_segments(self):
        objects = self._detailed_reverse_objects()
        objects[1] = dict(objects[0])
        objects[1]["action"] = objects[1]["action"].replace(
            "迈步进入画面", "缓慢进入画面"
        )
        segments = [
            self.breakdown._compose_reverse_segment(item)
            for item in objects
        ]
        with self.assertRaisesRegex(ValueError, "第2段与第1段内容重复"):
            self.breakdown._validate_reverse_prompt_lengths(segments)

    def test_regression_thresholds_cover_recorded_3248_metrics(self):
        self.assertGreaterEqual(
            0.878, self.breakdown._REVERSE_DUPLICATE_SEQUENCE_THRESHOLD
        )
        self.assertGreaterEqual(
            0.7351, self.breakdown._REVERSE_DUPLICATE_SHINGLE_THRESHOLD
        )

    def test_regression_3248_reanalyzes_only_duplicate_segment_from_its_frames(self):
        """任务3248：第1/2段相似度87.8%时只重看第2段原始帧。"""
        segment_1 = {
            "subject": "白色长裙女性坐在林间木桩上",
            "scene": "树林前景，远处可见积雪山体",
            "action": "身体朝向镜头，右臂向前上方抬起，手腕略向外翻",
            "camera": "中景平视，人物位于画面中央",
            "lighting": "自然日光照亮白色裙装",
            "sound": "",
            "continuity": "",
        }
        duplicate_2 = dict(segment_1)
        duplicate_2["action"] = (
            "身体朝向镜头，右臂继续向前上方抬起，手腕略向外翻"
        )
        corrected_2 = {
            "subject": "同一白色长裙女性仍坐在木桩上",
            "scene": "树林与远处雪山仍在背景中",
            "action": "上身角度发生偏转，抬起的手臂位置降低，手腕和手掌朝向改变",
            "camera": "中景构图，人物身体轮廓相对前一段发生位移",
            "lighting": "自然日光，裙装高光位置随姿态改变",
            "sound": "",
            "continuity": "",
        }
        segment_3 = {
            "subject": "白衣女性坐在室内床面",
            "scene": "卧室床铺与浅色墙面",
            "action": "双臂举过头顶，身体保持坐姿",
            "camera": "室内中景正面构图",
            "lighting": "柔和室内光",
            "sound": "",
            "continuity": "",
        }
        segment_4 = {
            "subject": "白衣女性位于粗大树干旁并被部分遮挡",
            "scene": "户外树林与近景树干",
            "action": "一侧手臂向外弯曲，身体从树干侧面露出",
            "camera": "树干占据近景，人物位于侧后方",
            "lighting": "林间自然光",
            "sound": "",
            "continuity": "",
        }
        for segment in (
            segment_1, duplicate_2, corrected_2, segment_3, segment_4
        ):
            segment["evidence_frames"] = {
                "subject": [1, 2],
                "scene": [1, 2],
                "action": [1, 2],
                "camera": [1, 2],
                "lighting": [1, 2],
            }
        self.assertGreaterEqual(
            self.breakdown._reverse_text_similarity(
                self.breakdown._compose_reverse_segment(segment_1),
                self.breakdown._compose_reverse_segment(duplicate_2),
            ),
            0.80,
        )
        responses = [
            segment_1, duplicate_2, corrected_2, segment_3, segment_4,
        ]
        calls = []

        def fake_chat(sysmsg, usermsg, frames, **kwargs):
            calls.append((usermsg, list(frames), kwargs))
            return json.dumps(
                {"segments": [responses[len(calls) - 1]]},
                ensure_ascii=False,
            )

        self.breakdown._chat_multimodal = fake_chat
        prompt = self.breakdown._reverse_prompt_from_frames(
            "任务3248", 15.267, "douyin",
            "[0s-4s] 林间画面\n[8s-12s] 室内画面",
            ["task3248-frame-%d.jpg" % index for index in range(1, 9)],
        )

        self.assertEqual(len(calls), 5)
        self.assertEqual(
            [call[1] for call in calls],
            [
                ["task3248-frame-1.jpg", "task3248-frame-2.jpg"],
                ["task3248-frame-3.jpg", "task3248-frame-4.jpg"],
                ["task3248-frame-3.jpg", "task3248-frame-4.jpg"],
                ["task3248-frame-5.jpg", "task3248-frame-6.jpg"],
                ["task3248-frame-7.jpg", "task3248-frame-8.jpg"],
            ],
        )
        self.assertIn("上身角度发生偏转", prompt)
        self.assertIn("双臂举过头顶", prompt)
        self.assertIn("树干旁并被部分遮挡", prompt)
        self.assertEqual(
            [line.split("] ", 1)[0] + "]" for line in prompt.splitlines()],
            [
                "[00:00.0-00:03.8]",
                "[00:03.8-00:07.6]",
                "[00:07.6-00:11.5]",
                "[00:11.5-00:15.3]",
            ],
        )
        self.assertNotIn(duplicate_2["action"], calls[2][0])
        self.assertIn("不要沿用任何历史草稿", calls[2][0])

    def test_regression_3248_rejects_motion_and_no_change_contradiction(self):
        entry = self._reverse_entry(
            scene="室内床铺",
            action="人物在床上坐起，但未观察到明显动作变化",
        )
        with self.assertRaisesRegex(ValueError, "动作与“无变化”自相矛盾"):
            self.breakdown._validate_reverse_segment_evidence(
                entry, [], ["bed-1.jpg", "bed-2.jpg"], 3
            )

    def test_regression_3248_rejects_hidden_face_expression(self):
        entry = self._reverse_entry(
            subject="白衣人物背对镜头",
            action="人物保持背向，表情平静并带有微笑",
        )
        with self.assertRaisesRegex(ValueError, "不可见的背面表情"):
            self.breakdown._validate_reverse_segment_evidence(
                entry, [], ["back-1.jpg", "back-2.jpg"], 1
            )

    def test_regression_3248_rejects_subjective_visual_inferences(self):
        for phrase in ("似乎在感受风", "阳光明媚", "绿草如茵"):
            with self.subTest(phrase=phrase):
                entry = self._reverse_entry(scene="树林背景，" + phrase)
                with self.assertRaisesRegex(ValueError, "无证据主观推断"):
                    self.breakdown._validate_reverse_segment_evidence(
                        entry, [], ["view-1.jpg", "view-2.jpg"], 1
                    )

    def test_regression_3248_rejects_sound_claim_inferred_from_image(self):
        entry = self._reverse_entry(sound="未观察到声音")
        with self.assertRaisesRegex(ValueError, "从画面推断声音"):
            self.breakdown._validate_reverse_segment_evidence(
                entry, [], ["silent-1.jpg", "silent-2.jpg"], 1,
                transcript="[0s-2s] 实际可辨识口播",
            )

    def test_regression_3248_rejects_unreliable_tree_root_orientation(self):
        entry = self._reverse_entry(action="人物面向树根并抬起右手")
        with self.assertRaisesRegex(ValueError, "无可靠证据方位"):
            self.breakdown._validate_reverse_segment_evidence(
                entry, [], ["tree-1.jpg", "tree-2.jpg"], 4
            )

    def test_regression_3248_subtitle_requires_quoted_visible_text(self):
        unsupported = self._reverse_entry(scene="画面字幕提示人物来到树林")
        with self.assertRaisesRegex(ValueError, "字幕缺少可核验逐字内容"):
            self.breakdown._validate_reverse_segment_evidence(
                unsupported, [], ["caption-1.jpg", "caption-2.jpg"], 1
            )

        supported = self._reverse_entry(scene="画面字幕“来到树林”清晰可读")
        self.assertIs(
            self.breakdown._validate_reverse_segment_evidence(
                supported, [], ["caption-1.jpg", "caption-2.jpg"], 1
            ),
            supported,
        )

    def test_regression_5708_identical_segments_fail_after_frame_only_retry(self):
        """任务5708：不同画面生成相同正文时，重试也绝不放行。"""
        repeated = {
            "subject": "一名女性站在画面中央",
            "scene": "户外背景位于人物身后",
            "action": "人物面向镜头并抬起右手",
            "camera": "中景固定构图",
            "lighting": "自然光",
            "sound": "",
            "continuity": "",
            "evidence_frames": {
                "subject": [1, 2],
                "scene": [1, 2],
                "action": [1, 2],
                "camera": [1, 2],
                "lighting": [1, 2],
            },
        }
        calls = []

        def fake_chat(sysmsg, usermsg, frames, **kwargs):
            calls.append((usermsg, list(frames), kwargs))
            return json.dumps({"segments": [repeated]}, ensure_ascii=False)

        self.breakdown._chat_multimodal = fake_chat
        with self.assertRaisesRegex(ValueError, "第2段与第1段内容重复"):
            self.breakdown._reverse_prompt_from_frames(
                "任务5708", 33.209, "douyin", "",
                ["task5708-frame-%d.jpg" % index for index in range(1, 9)],
            )

        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0][1], [
            "task5708-frame-1.jpg", "task5708-frame-2.jpg",
        ])
        self.assertEqual(calls[1][1], [
            "task5708-frame-3.jpg", "task5708-frame-4.jpg",
        ])
        self.assertEqual(calls[2][1], calls[1][1])
        self.assertNotIn(repeated["action"], calls[2][0])
        self.assertIn("不要沿用任何历史草稿", calls[2][0])

    def test_reverse_static_claim_requires_static_frame_evidence(self):
        segment = {
            "subject": "白衣人物",
            "scene": "树林",
            "action": "人物静止不动，姿态无变化",
            "camera": "",
            "lighting": "",
            "sound": "",
            "continuity": "",
            "evidence_frames": {
                "subject": [1, 2],
                "scene": [1, 2],
                "action": [1, 2],
            },
        }
        calls = []
        self.breakdown._chat_multimodal = lambda *args, **kwargs: (
            calls.append(args) or
            json.dumps({"segments": [segment]}, ensure_ascii=False)
        )
        with mock.patch.object(
            self.breakdown, "_frames_are_effectively_static", return_value=False
        ):
            with self.assertRaisesRegex(ValueError, "无静止画面证据"):
                self.breakdown._reverse_prompt_from_frames(
                    "动态画面", 2.5, "douyin", "",
                    ["moving-1.jpg", "moving-2.jpg"],
                )
        self.assertEqual(len(calls), 2)

    def test_reverse_static_claim_is_allowed_when_frames_are_static(self):
        segment = {
            "subject": "白衣人物",
            "scene": "树林",
            "action": "人物静止不动，姿态无变化",
            "camera": "",
            "lighting": "",
            "sound": "",
            "continuity": "",
            "evidence_frames": {
                "subject": [1, 2],
                "scene": [1, 2],
                "action": [1, 2],
            },
        }
        self.breakdown._chat_multimodal = lambda *args, **kwargs: json.dumps(
            {"segments": [segment]}, ensure_ascii=False
        )
        with mock.patch.object(
            self.breakdown, "_frames_are_effectively_static", return_value=True
        ):
            prompt = self.breakdown._reverse_prompt_from_frames(
                "静止画面", 2.5, "douyin", "",
                ["static-1.jpg", "static-2.jpg"],
            )
        self.assertIn("人物静止不动", prompt)

    def test_reverse_static_non_person_is_generation_ready_with_static_evidence(self):
        segment = {
            "subject": "白色矩形位于纯色画面中央",
            "scene": "蓝色纯色背景包围中央矩形",
            "action": "主体保持静止，未观察到位置或形态变化",
            "camera": "固定正面构图",
            "lighting": "画面亮度均匀，未见阴影变化",
            "sound": "",
            "continuity": "",
            "evidence_frames": {
                "subject": [1, 2],
                "scene": [1, 2],
                "action": [1, 2],
                "camera": [1, 2],
                "lighting": [1, 2],
            },
        }
        self.breakdown._chat_multimodal = lambda *args, **kwargs: json.dumps(
            {"segments": [segment]}, ensure_ascii=False
        )
        with mock.patch.object(
            self.breakdown, "_frames_are_effectively_static", return_value=True
        ) as static_check:
            prompt = self.breakdown._reverse_prompt_from_frames(
                "静态几何图形",
                2.5,
                "local",
                "",
                ["rectangle-first.jpg", "rectangle-last.jpg"],
            )
        static_check.assert_called_once_with(
            ["rectangle-first.jpg", "rectangle-last.jpg"]
        )
        self.assertIn("主体：白色矩形位于纯色画面中央", prompt)
        self.assertIn("动作：主体保持静止，未观察到位置或形态变化", prompt)

    def test_reverse_static_non_person_claim_still_requires_ssim(self):
        segment = {
            "subject": "白色矩形位于画面中央",
            "scene": "蓝色纯色背景",
            "action": "主体保持静止，未观察到位置或形态变化",
            "camera": "",
            "lighting": "",
            "sound": "",
            "continuity": "",
            "evidence_frames": {
                "subject": [1, 2],
                "scene": [1, 2],
                "action": [1, 2],
            },
        }
        self.breakdown._chat_multimodal = lambda *args, **kwargs: json.dumps(
            {"segments": [segment]}, ensure_ascii=False
        )
        with mock.patch.object(
            self.breakdown, "_frames_are_effectively_static", return_value=False
        ):
            with self.assertRaisesRegex(ValueError, "无静止画面证据"):
                self.breakdown._reverse_prompt_from_frames(
                    "动态矩形",
                    2.5,
                    "local",
                    "",
                    ["rectangle-first.jpg", "rectangle-last.jpg"],
                )

    def test_reverse_no_action_with_static_frames_calls_ssim_once(self):
        entry = {
            "text": "主体：白色矩形；场景：蓝色背景；动作：无动作。",
            "fields": {
                "subject": "白色矩形",
                "scene": "蓝色背景",
                "action": "无动作",
                "camera": "",
                "lighting": "",
                "sound": "",
                "continuity": "",
            },
            "evidence_frames": {
                "subject": [1, 2],
                "scene": [1, 2],
                "action": [1, 2],
            },
            "continuity_evidence_frames": [],
        }
        frames = ["static-first.jpg", "static-last.jpg"]
        with mock.patch.object(
            self.breakdown, "_frames_are_effectively_static", return_value=True
        ) as static_check:
            self.breakdown._validate_reverse_segment_evidence(
                entry,
                [],
                frames,
                1,
                require_frame_evidence=True,
            )
        static_check.assert_called_once_with(frames)

    def test_reverse_no_action_with_motion_frames_calls_ssim_once_and_fails(self):
        entry = {
            "text": "主体：白色矩形；场景：蓝色背景；动作：无动作。",
            "fields": {
                "subject": "白色矩形",
                "scene": "蓝色背景",
                "action": "无动作",
                "camera": "",
                "lighting": "",
                "sound": "",
                "continuity": "",
            },
            "evidence_frames": {
                "subject": [1, 2],
                "scene": [1, 2],
                "action": [1, 2],
            },
            "continuity_evidence_frames": [],
        }
        frames = ["motion-first.jpg", "motion-last.jpg"]
        with mock.patch.object(
            self.breakdown, "_frames_are_effectively_static", return_value=False
        ) as static_check:
            with self.assertRaisesRegex(ValueError, "无静止画面证据"):
                self.breakdown._validate_reverse_segment_evidence(
                    entry,
                    [],
                    frames,
                    1,
                    require_frame_evidence=True,
                )
        static_check.assert_called_once_with(frames)

    def test_reverse_explicit_no_action_synonyms_claim_static(self):
        for action in (
            "无动作",
            "没有动作",
            "未见动作",
            "未观察到动作",
            "未发生动作",
            "但仍无动作",
        ):
            with self.subTest(action=action):
                self.assertTrue(
                    self.breakdown._reverse_segment_claims_static(
                        {"fields": {"action": action}}
                    )
                )

    def test_reverse_no_action_prefix_inside_speed_change_is_not_static(self):
        entry = {
            "text": (
                "主体：黑色圆形；场景：浅色背景；"
                "动作：未观察到动作速度变化，圆形持续从左到右移动。"
            ),
            "fields": {
                "subject": "黑色圆形",
                "scene": "浅色背景",
                "action": "未观察到动作速度变化，圆形持续从左到右移动",
                "camera": "",
                "lighting": "",
                "sound": "",
                "continuity": "",
            },
            "evidence_frames": {
                "subject": [1, 2],
                "scene": [1, 2],
                "action": [1, 2],
            },
            "continuity_evidence_frames": [],
        }
        frames = ["motion-first.jpg", "motion-last.jpg"]
        self.assertFalse(self.breakdown._reverse_segment_claims_static(entry))
        with mock.patch.object(
            self.breakdown, "_frames_are_effectively_static"
        ) as static_check:
            self.breakdown._validate_reverse_segment_evidence(
                entry,
                [],
                frames,
                1,
                require_frame_evidence=True,
            )
        static_check.assert_not_called()

    def test_reverse_complete_static_clause_with_real_motion_is_contradictory(self):
        actions = (
            "没有明显动作变化，但人物从坐姿起身",
            "没有明显动作变化，但物体继续移动",
            "圆形持续移动，但仍无动作",
        )
        for action in actions:
            with self.subTest(action=action):
                entry = {
                    "text": "主体：可见主体；场景：室内；动作：%s。" % action,
                    "fields": {
                        "subject": "可见主体",
                        "scene": "室内",
                        "action": action,
                        "camera": "",
                        "lighting": "",
                        "sound": "",
                        "continuity": "",
                    },
                    "evidence_frames": {
                        "subject": [1, 2],
                        "scene": [1, 2],
                        "action": [1, 2],
                    },
                    "continuity_evidence_frames": [],
                }
                self.assertFalse(
                    self.breakdown._reverse_segment_claims_static(entry)
                )
                with mock.patch.object(
                    self.breakdown, "_frames_are_effectively_static"
                ) as static_check:
                    with self.assertRaisesRegex(ValueError, "自相矛盾"):
                        self.breakdown._validate_reverse_segment_evidence(
                            entry,
                            [],
                            ["motion-first.jpg", "motion-last.jpg"],
                            1,
                            require_frame_evidence=True,
                        )
                static_check.assert_not_called()

    def test_reverse_pure_abstract_frame_uses_evidence_backed_subject(self):
        segment = {
            "subject": "抽象画面，红色色块填满整个画面",
            "scene": "无独立前景或背景物体",
            "action": "主体保持静止，未观察到位置或形态变化",
            "camera": "固定满幅构图",
            "lighting": "红色区域亮度均匀",
            "sound": "",
            "continuity": "",
            "evidence_frames": {
                "subject": [1, 2],
                "scene": [1, 2],
                "action": [1, 2],
                "camera": [1, 2],
                "lighting": [1, 2],
            },
        }
        self.breakdown._chat_multimodal = lambda *args, **kwargs: json.dumps(
            {"segments": [segment]}, ensure_ascii=False
        )
        with mock.patch.object(
            self.breakdown, "_frames_are_effectively_static", return_value=True
        ):
            prompt = self.breakdown._reverse_prompt_from_frames(
                "纯色画面",
                2.5,
                "local",
                "",
                ["abstract-first.jpg", "abstract-last.jpg"],
            )
        self.assertIn("主体：抽象画面，红色色块填满整个画面", prompt)

    def test_reverse_non_person_shape_describes_visible_change_without_static_claim(self):
        segment = {
            "subject": "黑色圆形位于浅色画面左侧",
            "scene": "浅色纯色背景",
            "action": "黑色圆形从左侧移到右侧，同时直径增大",
            "camera": "固定满幅构图",
            "lighting": "画面亮度均匀",
            "sound": "",
            "continuity": "",
            "evidence_frames": {
                "subject": [1, 2],
                "scene": [1, 2],
                "action": [1, 2],
                "camera": [1, 2],
                "lighting": [1, 2],
            },
        }
        self.breakdown._chat_multimodal = lambda *args, **kwargs: json.dumps(
            {"segments": [segment]}, ensure_ascii=False
        )
        with mock.patch.object(
            self.breakdown, "_frames_are_effectively_static"
        ) as static_check:
            prompt = self.breakdown._reverse_prompt_from_frames(
                "变化图形",
                2.5,
                "local",
                "",
                ["shape-first.jpg", "shape-last.jpg"],
            )
        static_check.assert_not_called()
        self.assertIn("从左侧移到右侧", prompt)

    def test_reverse_unchanged_position_with_color_change_is_not_static(self):
        segment = {
            "subject": "画面中央的矩形首帧为白色，尾帧为红色",
            "scene": "蓝色纯色背景",
            "action": "矩形位置保持不变，颜色由白色逐渐变为红色",
            "camera": "固定满幅构图",
            "lighting": "背景亮度保持均匀",
            "sound": "",
            "continuity": "",
            "evidence_frames": {
                "subject": [1, 2],
                "scene": [1, 2],
                "action": [1, 2],
                "camera": [1, 2],
                "lighting": [1, 2],
            },
        }
        self.breakdown._chat_multimodal = lambda *args, **kwargs: json.dumps(
            {"segments": [segment]}, ensure_ascii=False
        )
        with mock.patch.object(
            self.breakdown, "_frames_are_effectively_static"
        ) as static_check:
            prompt = self.breakdown._reverse_prompt_from_frames(
                "矩形变色",
                2.5,
                "local",
                "",
                ["color-first.jpg", "color-last.jpg"],
            )
        static_check.assert_not_called()
        self.assertIn("位置保持不变，颜色由白色逐渐变为红色", prompt)

    def test_reverse_unchanged_shape_with_motion_is_not_static(self):
        segment = {
            "subject": "黑色圆形出现在浅色画面中",
            "scene": "浅色纯色背景",
            "action": "圆形形状保持不变，从左侧移动到右侧",
            "camera": "固定满幅构图",
            "lighting": "画面亮度均匀",
            "sound": "",
            "continuity": "",
            "evidence_frames": {
                "subject": [1, 2],
                "scene": [1, 2],
                "action": [1, 2],
                "camera": [1, 2],
                "lighting": [1, 2],
            },
        }
        self.breakdown._chat_multimodal = lambda *args, **kwargs: json.dumps(
            {"segments": [segment]}, ensure_ascii=False
        )
        with mock.patch.object(
            self.breakdown, "_frames_are_effectively_static"
        ) as static_check:
            prompt = self.breakdown._reverse_prompt_from_frames(
                "圆形移动",
                2.5,
                "local",
                "",
                ["motion-first.jpg", "motion-last.jpg"],
            )
        static_check.assert_not_called()
        self.assertIn("形状保持不变，从左侧移动到右侧", prompt)

    def test_reverse_non_person_object_remains_the_subject(self):
        segment = {
            "subject": "透明玻璃瓶位于木桌中央，瓶身贴有白色标签",
            "scene": "浅色木桌与灰色背景构成简洁室内静物场景",
            "action": "玻璃瓶从竖直状态向画面右侧倾斜",
            "camera": "固定正面中近景构图",
            "lighting": "左侧柔光在瓶身形成可见高光",
            "sound": "",
            "continuity": "",
            "evidence_frames": {
                "subject": [1, 2],
                "scene": [1, 2],
                "action": [1, 2],
                "camera": [1, 2],
                "lighting": [1, 2],
            },
        }
        self.breakdown._chat_multimodal = lambda *args, **kwargs: json.dumps(
            {"segments": [segment]}, ensure_ascii=False
        )
        with mock.patch.object(
            self.breakdown, "_frames_are_effectively_static"
        ) as static_check:
            prompt = self.breakdown._reverse_prompt_from_frames(
                "玻璃瓶静物",
                2.5,
                "local",
                "",
                ["bottle-first.jpg", "bottle-last.jpg"],
            )
        static_check.assert_not_called()
        self.assertIn("主体：透明玻璃瓶位于木桌中央", prompt)
        self.assertIn("动作：玻璃瓶从竖直状态向画面右侧倾斜", prompt)

    def test_reverse_retry_carries_missing_field_error_not_old_draft(self):
        missing = {
            "subject": "",
            "scene": "Blue background with a white rectangle",
            "action": "",
            "camera": "Static",
            "lighting": "Uniform",
            "sound": "",
            "continuity": "",
        }
        corrected = {
            "subject": "白色矩形位于画面中央",
            "scene": "蓝色纯色背景",
            "action": "主体保持静止，未观察到位置或形态变化",
            "camera": "固定满幅构图",
            "lighting": "画面亮度均匀",
            "sound": "",
            "continuity": "",
            "evidence_frames": {
                "subject": [1, 2],
                "scene": [1, 2],
                "action": [1, 2],
                "camera": [1, 2],
                "lighting": [1, 2],
            },
        }
        calls = []
        responses = [missing, corrected]

        def fake_chat(_system, user, frames, **kwargs):
            calls.append((user, list(frames), kwargs))
            return json.dumps(
                {"segments": [responses[len(calls) - 1]]},
                ensure_ascii=False,
            )

        self.breakdown._chat_multimodal = fake_chat
        with mock.patch.object(
            self.breakdown, "_frames_are_effectively_static", return_value=True
        ):
            prompt = self.breakdown._reverse_prompt_from_frames(
                "静态白色矩形",
                2.5,
                "local",
                "",
                ["rectangle-first.jpg", "rectangle-last.jpg"],
            )
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1], calls[1][1])
        self.assertIn("上一轮校验错误", calls[1][0])
        self.assertIn("subject（主体）", calls[1][0])
        self.assertIn("action（动作）", calls[1][0])
        self.assertIn("禁止返回全空JSON", calls[1][0])
        self.assertNotIn("Blue background with a white rectangle", calls[1][0])
        self.assertNotIn("camera=Static", calls[1][0])
        self.assertIn("主体：白色矩形位于画面中央", prompt)

    def test_regression_real_glm_static_rectangle_responses_still_fail(self):
        first_real_response = {
            "subject": "",
            "scene": "Blue background with a white rectangle",
            "action": "",
            "camera": "Static",
            "lighting": "Uniform",
            "sound": "",
            "continuity": "",
        }
        blank = {
            "subject": "",
            "scene": "",
            "action": "",
            "camera": "",
            "lighting": "",
            "sound": "",
            "continuity": "",
        }
        calls = []
        responses = [first_real_response, blank]

        def fake_chat(_system, user, frames, **kwargs):
            calls.append((user, list(frames), kwargs))
            return json.dumps(
                {"segments": [responses[len(calls) - 1]]},
                ensure_ascii=False,
            )

        self.breakdown._chat_multimodal = fake_chat
        with self.assertRaisesRegex(
            ValueError, "本段为空.*subject、scene、action"
        ):
            self.breakdown._reverse_prompt_from_frames(
                "静态空画面",
                2.5,
                "local",
                "",
                ["blank-first.jpg", "blank-last.jpg"],
            )
        self.assertEqual(len(calls), 2)
        self.assertIn("subject（主体）", calls[1][0])
        self.assertIn("action（动作）", calls[1][0])
        self.assertNotIn(
            "Blue background with a white rectangle", calls[1][0]
        )

    def test_reverse_segment_transcript_keeps_only_overlapping_asr(self):
        transcript = (
            "[0s-3.8s] 第一段口播\n"
            "[3.8s-7.6s] 第二段口播\n"
            "[7.6s-11.5s] 第三段口播\n"
            "[11.5s-15.3s] 第四段口播"
        )
        self.assertEqual(
            self.breakdown._segment_transcript(transcript, 3.8, 7.6),
            "第二段口播",
        )

    def test_reverse_rejects_sound_without_segment_asr_evidence(self):
        segment = {
            "subject": "白衣人物",
            "scene": "树林",
            "action": "抬起右手",
            "camera": "",
            "lighting": "",
            "sound": "舒缓背景音乐",
            "continuity": "",
            "evidence_frames": {
                "subject": [1, 2],
                "scene": [1, 2],
                "action": [1, 2],
            },
        }
        calls = []
        self.breakdown._chat_multimodal = lambda *args, **kwargs: (
            calls.append(args) or
            json.dumps({"segments": [segment]}, ensure_ascii=False)
        )
        with self.assertRaisesRegex(ValueError, "声音缺少本段ASR证据"):
            self.breakdown._reverse_prompt_from_frames(
                "无口播画面", 2.5, "douyin", "",
                ["frame-1.jpg", "frame-2.jpg"],
            )
        self.assertEqual(len(calls), 2)

    def test_reverse_rejects_subject_action_mechanical_copy(self):
        entry = {
            "text": "主体：白衣人物坐在树旁抬起右手；动作：白衣人物坐在树旁抬起右手。",
            "fields": {
                "subject": "白衣人物坐在树旁抬起右手",
                "scene": "树林背景",
                "action": "白衣人物坐在树旁抬起右手",
            },
        }
        with self.assertRaisesRegex(ValueError, "主体与动作机械重复"):
            self.breakdown._validate_reverse_segment_evidence(
                entry, [], ["f1.jpg", "f2.jpg"], 1
            )

    def test_reverse_timeline_uses_tenth_second_precision(self):
        self.assertEqual(
            self.breakdown._fixed_reverse_ranges(11.434),
            [
                "[00:00.0-00:02.9]",
                "[00:02.9-00:05.7]",
                "[00:05.7-00:08.6]",
                "[00:08.6-00:11.4]",
            ],
        )

    def test_reverse_visual_semantic_rubric_is_auditable_hundred_points(self):
        contract = self.breakdown._reverse_quality_contract()
        self.assertEqual(contract["definition"], "visual_semantic_not_pixel")
        self.assertEqual(
            contract["score_scope"],
            "reverse_prompt_source_fidelity_and_generation_readiness",
        )
        self.assertEqual(contract["target_score"], 80)
        self.assertTrue(contract["requires_reference_guidance"])
        self.assertEqual(
            set(contract["components"]),
            {
                "source_evidence_coverage",
                "generation_readiness",
                "factual_consistency",
            },
        )
        self.assertFalse(contract["generated_video_similarity_claim"])
        self.assertEqual(
            contract["critical_failures"],
            [
                "hard_cut_merged_as_action",
                "unsupported_fact",
                "subject_scene_action_error",
            ],
        )

        entries = [
            self._reverse_entry(
                subject="白衣人物位于画面中部",
                scene="海滩、浪花与海平线",
                action="人物从左向右迈步",
                camera="平视中景跟随",
                lighting="暖色逆光",
            )
            for _index in range(4)
        ]
        score = self.breakdown._score_reverse_generation_coverage(
            entries,
            self._global_continuity(),
            self.breakdown._reverse_segment_windows(15.267),
        )
        self.assertEqual(score["total"], 100)
        self.assertEqual(score["parts"], {
            "subject": 30,
            "action_timing": 25,
            "scene_composition": 20,
            "camera_duration": 15,
            "lighting_style": 10,
        })

    def test_task_3258_hard_cut_cannot_be_merged_into_one_action(self):
        continuous = self._generation_entry()
        with self.assertRaisesRegex(ValueError, "存在硬切.*不能合并"):
            self.breakdown._validate_reverse_segment_evidence(
                continuous,
                [],
                ["bridge.jpg", "woman.jpg"],
                1,
                require_frame_evidence=True,
                require_generation_readiness=True,
                pair_ssim=0.20,
            )

        hard_cut = self._hard_cut_entry()
        validated = self.breakdown._validate_reverse_segment_evidence(
            hard_cut,
            [],
            ["bridge.jpg", "woman.jpg"],
            1,
            require_frame_evidence=True,
            require_generation_readiness=True,
            pair_ssim=0.20,
        )
        self.assertEqual(
            validated["validation_summary"]["shot_boundary"], "hard_cut"
        )
        prompt = self.breakdown._assemble_reverse_prompt(
            [validated], self.breakdown._reverse_segment_windows(2.5)
        )
        self.assertIn("镜头A", prompt)
        self.assertIn("硬切至镜头B", prompt)
        self.assertIn("石桥", prompt)
        self.assertIn("粉色连帽上衣女性", prompt)
        self.assertNotIn("桥上有人行走", prompt)

    def test_task_3258_rejects_scarf_guess_and_unsupported_wardrobe_action(self):
        scarf = self._generation_entry()
        scarf["generation"]["action"]["associated_object"] = {
            "status": "observed",
            "value": "围巾",
            "evidence_frames": [1, 2],
        }
        with self.assertRaisesRegex(ValueError, "同一次模型响应中的重复描述不能自证事实"):
            self.breakdown._validate_reverse_segment_evidence(
                scarf,
                [],
                ["first.jpg", "last.jpg"],
                1,
                require_frame_evidence=True,
                require_generation_readiness=True,
                pair_ssim=0.80,
            )

        self_consistent_scarf = self._generation_entry()
        self_consistent_scarf["generation"]["subject"]["appearance"]["value"] = (
            "黑色长发女性，颈部有粉色围巾"
        )
        self_consistent_scarf["generation"]["subject"]["wardrobe"]["value"] = (
            "粉色连帽上衣和粉色围巾"
        )
        self_consistent_scarf["generation"]["action"]["associated_object"] = {
            "status": "observed",
            "value": "粉色围巾",
            "evidence_frames": [1, 2],
        }
        for shot in self_consistent_scarf["shots"]:
            shot["subject"] = "粉色连帽上衣女性佩戴粉色围巾"
        with self.assertRaisesRegex(ValueError, "重复描述不能自证事实"):
            self.breakdown._validate_reverse_segment_evidence(
                self_consistent_scarf,
                [],
                ["first.jpg", "last.jpg"],
                1,
                require_frame_evidence=True,
                require_generation_readiness=True,
                pair_ssim=0.80,
            )

        wardrobe_story = self._generation_entry()
        wardrobe_story["generation"]["action"]["process"] = {
            "status": "observed",
            "value": "女性正在整理粉色卫衣",
            "evidence_frames": [1, 2],
        }
        with self.assertRaisesRegex(ValueError, "不得把手部变化臆写为整理卫衣"):
            self.breakdown._validate_reverse_segment_evidence(
                wardrobe_story,
                [],
                ["first.jpg", "last.jpg"],
                1,
                require_frame_evidence=True,
                require_generation_readiness=True,
                pair_ssim=0.80,
            )

    def test_generation_readiness_uses_eighty_percent_without_counting_not_applicable(self):
        below_target = self._generation_entry()
        for path in (
            "camera.camera_position",
            "camera.movement",
            "camera.viewing_angle",
            "lighting.color_tone",
            "style.texture",
        ):
            group, key = path.split(".")
            below_target["generation"][group][key] = {
                "status": "unknown",
                "value": "unknown",
                "evidence_frames": [],
            }
        with self.assertRaisesRegex(ValueError, "至少需要80%"):
            self.breakdown._validate_reverse_segment_evidence(
                below_target,
                [],
                ["first.jpg", "last.jpg"],
                1,
                require_frame_evidence=True,
                require_generation_readiness=True,
                pair_ssim=0.80,
            )

        ready = self._generation_entry()
        for path in (
            "camera.camera_position",
            "camera.movement",
            "style.texture",
        ):
            group, key = path.split(".")
            ready["generation"][group][key] = {
                "status": "unknown",
                "value": "unknown",
                "evidence_frames": [],
            }
        validated = self.breakdown._validate_reverse_segment_evidence(
            ready,
            [],
            ["first.jpg", "last.jpg"],
            1,
            require_frame_evidence=True,
            require_generation_readiness=True,
            pair_ssim=0.80,
        )
        self.assertGreaterEqual(
            validated["validation_summary"]["generation_readiness"], 80
        )
        self.assertEqual(
            ready["generation"]["camera"]["movement"]["value"], "unknown"
        )

    def test_dynamic_and_static_generation_contracts_use_first_last_and_ssim(self):
        dynamic = self._generation_entry()
        validated = self.breakdown._validate_reverse_segment_evidence(
            dynamic,
            [],
            ["first.jpg", "last.jpg"],
            1,
            require_frame_evidence=True,
            require_generation_readiness=True,
            pair_ssim=0.80,
        )
        self.assertEqual(
            validated["generation"]["action"]["start"]["evidence_frames"], [1]
        )
        self.assertEqual(
            validated["generation"]["action"]["end"]["evidence_frames"], [2]
        )

        static_item = self._generation_ready_structure(static=True)
        static_entry = self.breakdown._parse_reverse_segment_evidence(
            json.dumps({"segments": [static_item]}, ensure_ascii=False)
        )
        self.breakdown._reverse_frame_pair_ssim = lambda _left, _right: 0.998
        self.breakdown._validate_reverse_segment_evidence(
            static_entry,
            [],
            ["first.jpg", "last.jpg"],
            1,
            require_frame_evidence=True,
            require_generation_readiness=True,
            pair_ssim=0.998,
        )
        self.breakdown._reverse_frame_pair_ssim = lambda _left, _right: 0.70
        with self.assertRaisesRegex(ValueError, "无静止画面证据"):
            self.breakdown._validate_reverse_segment_evidence(
                static_entry,
                [],
                ["first.jpg", "last.jpg"],
                1,
                require_frame_evidence=True,
                require_generation_readiness=True,
                pair_ssim=0.70,
            )

    def test_structured_quality_scores_three_components_not_nonempty_fields(self):
        entries = []
        processes = [
            "右臂从腰侧向上抬至头部旁",
            "人物从画面左侧横向移动到右侧",
            "双手从面部前方下降到胸前",
            "身体顺时针旋转并展开裙摆",
        ]
        for index in range(1, 5):
            entry = self._generation_entry(index=index)
            entry["generation"]["action"]["process"]["value"] = (
                processes[index - 1]
            )
            self.breakdown._validate_reverse_segment_evidence(
                entry,
                entries,
                ["first.jpg", "last.jpg"],
                index,
                require_frame_evidence=True,
                require_generation_readiness=True,
                pair_ssim=0.80,
            )
            entries.append(entry)
        score = self.breakdown._score_reverse_generation_coverage(
            entries,
            self._global_continuity(),
            self.breakdown._reverse_segment_windows(15.093),
        )
        self.assertEqual(score["components"], {
            "source_evidence_coverage": 100,
            "generation_readiness": 100,
            "factual_consistency": 100,
        })
        self.assertFalse(score["generated_video_similarity_claim"])
        self.assertEqual(len(score["segment_scores"]), 4)

    def test_normalized_attribute_continuity_does_not_require_full_sentence_match(self):
        first = self._generation_entry(index=1)
        second = self._generation_entry(index=2)
        first["generation"]["subject"]["appearance"]["value"] = "一名黑色长发女性"
        second["generation"]["subject"]["appearance"]["value"] = "黑色长发的女性"
        first["generation"]["subject"]["wardrobe"]["value"] = "粉色连帽上衣"
        second["generation"]["subject"]["wardrobe"]["value"] = "粉色连帽衫"
        continuity = self.breakdown._reverse_global_facts_from_segments(
            [first, second], [[1, 2], [3, 4]], 4
        )
        self.assertEqual(
            continuity["aggregation"],
            "deterministic_normalized_validated_attribute_intersection",
        )
        self.assertIn(
            "黑色长发女性", continuity["facts"]["subject_identity"]
        )
        self.assertIn("粉色连帽", continuity["facts"]["wardrobe"])
        self.assertEqual(
            set(continuity["evidence_frames"]["wardrobe"]), {1, 2, 3, 4}
        )

    def test_normalized_continuity_rejects_negation_color_and_garment_conflicts(self):
        cases = (
            ("佩戴围巾", "未佩戴围巾"),
            ("粉色连帽上衣", "白色连帽衫"),
            ("粉色连帽上衣", "粉色长裙"),
        )
        for first_value, second_value in cases:
            with self.subTest(first=first_value, second=second_value):
                first = self._generation_entry(index=1)
                second = self._generation_entry(index=2)
                first["generation"]["subject"]["wardrobe"]["value"] = first_value
                second["generation"]["subject"]["wardrobe"]["value"] = second_value
                continuity = self.breakdown._reverse_global_facts_from_segments(
                    [first, second], [[1, 2], [3, 4]], 4
                )
                self.assertEqual(continuity["facts"]["wardrobe"], "")
                self.assertEqual(
                    [item["text"] for item in continuity["changes"]["wardrobe"]],
                    [first_value, second_value],
                )

    def test_factual_consistency_score_records_deterministic_checks(self):
        entry = self._generation_entry()
        validated = self.breakdown._validate_reverse_segment_evidence(
            entry,
            [],
            ["first.jpg", "last.jpg"],
            1,
            require_frame_evidence=True,
            require_generation_readiness=True,
            pair_ssim=0.80,
        )
        summary = validated["validation_summary"]
        self.assertEqual(summary["factual_consistency"], 100)
        self.assertEqual(
            set(summary["factual_consistency_checks"]),
            {
                "shot_boundary_matches_pair_evidence",
                "shot_states_have_local_frame_evidence",
                "observed_slots_have_local_frame_evidence",
                "action_start_end_match_first_last_frames",
                "static_claim_requires_ssim",
                "no_ambiguous_accessory_self_corroboration",
                "no_interpretive_action_from_sparse_frames",
            },
        )

    def test_attempt_audit_persists_hash_attempt_and_validation_without_raw(self):
        invalid = self._generation_ready_structure()
        for key in ("camera_position", "movement", "viewing_angle"):
            invalid["generation"]["camera"][key] = {
                "status": "unknown",
                "value": "unknown",
                "evidence_frames": [],
            }
        invalid["generation"]["lighting"]["color_tone"] = {
            "status": "unknown",
            "value": "unknown",
            "evidence_frames": [],
        }
        invalid["generation"]["style"]["texture"] = {
            "status": "unknown",
            "value": "unknown",
            "evidence_frames": [],
        }
        valid = self._generation_ready_structure()
        responses = [invalid, valid]
        calls = []

        def fake_chat(_system, _user, frames, **kwargs):
            calls.append((list(frames), kwargs))
            return json.dumps(
                {"segments": [responses[len(calls) - 1]]},
                ensure_ascii=False,
            )

        self.breakdown._chat_multimodal = fake_chat
        self.breakdown._reverse_frame_pair_ssim = lambda _left, _right: 0.80
        details = self.breakdown._reverse_prompt_from_frames(
            "审计样本",
            2.5,
            "local",
            "",
            ["first.jpg", "last.jpg"],
            return_details=True,
        )
        audit = details["entries"][0]["attempt_audit"]
        self.assertEqual(
            [item["validation"] for item in audit], ["rejected", "accepted"]
        )
        self.assertEqual([item["attempt"] for item in audit], [1, 2])
        self.assertTrue(all(len(item["response_sha256"]) == 64 for item in audit))
        self.assertNotIn("raw", json.dumps(audit))
        bundle = self.breakdown._reverse_frame_bundle(
            ["first.jpg", "last.jpg"], 1
        )
        details["prompt"] = self.breakdown._assemble_reverse_prompt(
            details["entries"], details["windows"]
        )
        manifest = self.breakdown._reverse_segment_evidence_manifest(
            details["entries"],
            details["windows"],
            bundle["segment_source_indices"],
            bundle["segment_model_source_indices"],
        )
        self.assertEqual(manifest[0]["attempt_audit"], audit)
        self.assertIn("generation_structure", manifest[0])
        self.assertIn("validation_summary", manifest[0])
        self.assertEqual(
            manifest[0]["generation_suggestions"]["scope"],
            "recommendation_not_observed_source_fact",
        )
        self.assertEqual(
            manifest[0]["source_parameters"]["scope"],
            "measured_source_fact",
        )

    def test_strict_one_to_four_segments_keep_two_images_glm_only_and_no_fallback(self):
        for duration, expected_segments in (
            (2.5, 1),
            (5.0, 2),
            (8.0, 3),
            (11.0, 4),
        ):
            with self.subTest(duration=duration):
                objects = self._detailed_reverse_objects(expected_segments)
                calls = []

                def fake_chat(_system, _user, frames, **kwargs):
                    calls.append((list(frames), kwargs))
                    return json.dumps(
                        {"segments": [objects[len(calls) - 1]]},
                        ensure_ascii=False,
                    )

                self.breakdown._chat_multimodal = fake_chat
                self.breakdown._reverse_frame_pair_ssim = (
                    lambda _left, _right: 0.80
                )
                details = self.breakdown._reverse_prompt_from_frames(
                    "严格分段合同",
                    duration,
                    "local",
                    "",
                    ["frame-%d.jpg" % index for index in range(1, 9)],
                    return_details=True,
                )
                self.assertEqual(len(details["entries"]), expected_segments)
                self.assertEqual(len(calls), expected_segments)
                self.assertTrue(all(len(call[0]) == 2 for call in calls))
                self.assertTrue(all(
                    call[1]["model"] == "glm-4v-plus"
                    and call[1]["provider"] == "zhipu"
                    and call[1]["allow_provider_fallback"] is False
                    and call[1]["max_tokens"] == 2000
                    for call in calls
                ))

    def test_task_3258_old_flat_response_cannot_score_or_escape_on_retry(self):
        old_segment = {
            "subject": "桥",
            "scene": "夜晚古镇，有灯笼和建筑",
            "action": "桥上有人行走",
            "camera": "俯视角度",
            "lighting": "灯笼光线",
            "sound": "",
            "continuity": "",
            "evidence_frames": {
                "subject": [1, 2],
                "scene": [1, 2],
                "action": [1, 2],
                "camera": [1, 2],
                "lighting": [1, 2],
            },
        }
        calls = []

        def fake_chat(_system, user, frames, **kwargs):
            calls.append((user, list(frames), kwargs))
            return json.dumps(
                {"segments": [old_segment]}, ensure_ascii=False
            )

        self.breakdown._chat_multimodal = fake_chat
        self.breakdown._reverse_frame_pair_ssim = (
            lambda _left, _right: 0.20
        )
        with self.assertRaisesRegex(ValueError, "缺少可生成的 generation"):
            self.breakdown._reverse_prompt_from_frames(
                "3258",
                2.5,
                "local",
                "",
                ["bridge.jpg", "woman.jpg"],
                return_details=True,
            )
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(call[1] == ["bridge.jpg", "woman.jpg"] for call in calls))
        self.assertIn("必须标记hard_cut", calls[0][0])
        self.assertIn("不要沿用任何历史草稿", calls[1][0])

    def test_reverse_global_facts_require_original_frame_evidence(self):
        parsed = self.breakdown._parse_reverse_global_facts(
            self._global_continuity_response(), 8, 4
        )
        self.assertEqual(
            parsed["facts"]["wardrobe"],
            "白色长衣，未见服装切换",
        )
        self.assertEqual(parsed["evidence_frames"]["wardrobe"], [1, 3, 5, 7])

        invalid = json.loads(self._global_continuity_response())
        invalid["evidence_frames"]["wardrobe"] = []
        with self.assertRaisesRegex(ValueError, "缺少原始帧编号"):
            self.breakdown._parse_reverse_global_facts(
                json.dumps(invalid, ensure_ascii=False), 8, 4
            )

        single_segment = json.loads(self._global_continuity_response())
        single_segment["evidence_frames"]["wardrobe"] = [1, 2]
        with self.assertRaisesRegex(ValueError, "至少两个不同时间段"):
            self.breakdown._parse_reverse_global_facts(
                json.dumps(single_segment, ensure_ascii=False), 8, 4
            )

    def test_reverse_segment_isolated_request_forbids_cross_segment_draft(self):
        _system, user = self.breakdown._reverse_segment_messages(
            "标题", 15.267, "douyin", "", 2, 4,
            "[00:03.8-00:07.6]",
            retry=True,
        )
        self.assertIn("隔离分段取证", user)
        self.assertIn("跨段连续性将在全部分段通过校验后由代码确定性归纳", user)
        self.assertIn("不要沿用任何历史草稿", user)
        self.assertNotIn("上一轮输出", user)
        self.assertIn('"evidence_frames"', user)
        self.assertIn("dynamic必须给出不同的首尾状态", user)
        self.assertIn("continuity槽位在隔离分析阶段标记not_applicable", user)
        self.assertNotIn("同一名白衣人物，黑色长发", user)
        self.assertTrue(_system)
        entry = self._reverse_entry()
        entry["continuity_evidence_frames"] = [1, 3]
        with self.assertRaisesRegex(ValueError, "不能在隔离分段分析中"):
            self.breakdown._validate_reverse_segment_evidence(
                entry,
                [],
                ["segment-first.jpg", "segment-last.jpg"],
                1,
                require_frame_evidence=True,
            )

    def test_reverse_production_segment_requires_auditable_frame_indices(self):
        raw = json.dumps({
            "segments": [{
                "subject": "白衣人物位于画面中央",
                "scene": "海滩与远处海平线",
                "action": "人物从左向右迈步",
                "camera": "平视中景",
                "lighting": "暖色逆光",
                "sound": "",
                "continuity": "",
                "evidence_frames": {
                    "subject": [1],
                    "scene": [1],
                    "action": [1],
                    "camera": [1],
                    "lighting": [1],
                },
            }],
        }, ensure_ascii=False)
        entry = self.breakdown._parse_reverse_segment_evidence(raw)
        with self.assertRaisesRegex(ValueError, "动作时序必须同时引用"):
            self.breakdown._validate_reverse_segment_evidence(
                entry,
                [],
                ["segment-first.jpg", "segment-last.jpg"],
                1,
                require_frame_evidence=True,
            )

    def test_reverse_rejects_hollow_critical_generation_fields(self):
        entry = self._reverse_entry(scene="场景细节")
        with self.assertRaisesRegex(ValueError, "空洞占位内容"):
            self.breakdown._validate_reverse_segment_evidence(
                entry,
                [],
                ["segment-first.jpg", "segment-last.jpg"],
                1,
                require_frame_evidence=True,
            )

    def test_reverse_sound_must_match_current_segment_asr_text(self):
        mismatch = self._reverse_entry(sound="激昂摇滚乐")
        with self.assertRaisesRegex(ValueError, "声音与本段ASR内容不匹配"):
            self.breakdown._validate_reverse_segment_evidence(
                mismatch,
                [],
                ["segment-first.jpg", "segment-last.jpg"],
                1,
                transcript="欢迎光临",
            )

        matching = self._reverse_entry(sound="人物口播“欢迎光临”")
        validated = self.breakdown._validate_reverse_segment_evidence(
            matching,
            [],
            ["segment-first.jpg", "segment-last.jpg"],
            1,
            transcript="欢迎光临本店",
        )
        self.assertIs(validated, matching)
        embellished = self._reverse_entry(
            sound="人物说“欢迎光临”，同时伴随激昂摇滚乐"
        )
        with self.assertRaisesRegex(ValueError, "声音与本段ASR内容不匹配"):
            self.breakdown._validate_reverse_segment_evidence(
                embellished,
                [],
                ["segment-first.jpg", "segment-last.jpg"],
                1,
                transcript="欢迎光临",
            )

    def test_reverse_rejects_fixed_continuity_even_when_bodies_differ(self):
        entry = self._reverse_entry(
            subject="第二段黑衣人物位于画面右侧",
            scene="室内桌面和台灯",
            action="人物从右向左放下杯子",
            continuity="与上一段保持一致",
        )
        entry["continuity_evidence_frames"] = [1, 3]
        with self.assertRaisesRegex(ValueError, "使用固定衔接文字"):
            self.breakdown._validate_reverse_segment_evidence(
                entry,
                [self._reverse_entry()],
                ["segment-first.jpg", "segment-last.jpg"],
                2,
                require_frame_evidence=True,
                global_continuity=self._global_continuity(),
            )

    def test_reverse_continuity_requires_current_and_adjacent_segment_evidence(self):
        entry = self._reverse_entry(
            continuity="人物从室外切换到室内，服装未变"
        )
        entry["continuity_evidence_frames"] = [1]
        with self.assertRaisesRegex(ValueError, "本段和相邻时间段"):
            self.breakdown._validate_reverse_segment_evidence(
                entry,
                [],
                ["segment-first.jpg", "segment-last.jpg"],
                2,
                require_frame_evidence=True,
                global_continuity=self._global_continuity(),
            )

    def test_reverse_analysis_budget_stays_inside_reaper_grace(self):
        core = importlib.import_module("content_domains.core")
        self.assertEqual(core.KIND_GRACE["breakdown"], 600)
        self.assertLessEqual(self.breakdown.BREAKDOWN_ANALYSIS_BUDGET, 540)
        self.assertLess(
            self.breakdown.BREAKDOWN_ANALYSIS_BUDGET,
            core.KIND_GRACE["breakdown"],
        )
        self.assertEqual(
            self.breakdown._reverse_analysis_call_budget(4),
            {
                "analysis_deadline_seconds": 540,
                "max_images_per_request": 0,
                "max_video_inputs_per_request": 1,
                "global_model_calls": 0,
                "normal_logical_calls": 1,
                "worst_logical_calls": 2,
                "normal_physical_http_attempts": 1,
                "same_provider_physical_attempts_per_logical": 2,
                "worst_physical_http_attempts": 4,
                "provider": "google",
                "model": "gemini-3.1-pro-preview",
                "http_4xx_retry": False,
                "cross_provider_fallback": False,
            },
        )

    def test_reverse_provider_contract_rejects_more_than_two_images(self):
        with patch.object(
            self.breakdown, "_chat_multimodal"
        ) as chat:
            with self.assertRaisesRegex(ValueError, "最多只能携带2张图片"):
                self.breakdown._reverse_chat_multimodal(
                    "system",
                    "user",
                    ["frame-%d.jpg" % index for index in range(1, 9)],
                )
        chat.assert_not_called()

    def test_reverse_provider_contract_never_falls_back_to_openai(self):
        os.environ["REVERSE_ZHIPU_KEY"] = "zhipu-test-key"
        failures = [
            urllib.error.HTTPError(
                "https://zhipu.test", 400, "bad request", {}, io.BytesIO(b"{}")
            ),
            urllib.error.HTTPError(
                "https://zhipu.test", 503, "unavailable", {}, io.BytesIO(b"{}")
            ),
            urllib.error.URLError("network down"),
            TimeoutError("timed out"),
        ]
        for failure in failures:
            with self.subTest(error=type(failure).__name__), patch.object(
                self.breakdown, "_post_zhipu", side_effect=failure
            ), patch.object(
                self.breakdown, "_post_openai_fallback"
            ) as fallback:
                with self.assertRaises(type(failure)):
                    self.breakdown._reverse_chat_multimodal(
                        "system", "user", []
                    )
                fallback.assert_not_called()

    def test_reverse_zhipu_1210_is_one_http_attempt_without_fallback(self):
        os.environ["REVERSE_ZHIPU_KEY"] = "zhipu-test-key"
        error = urllib.error.HTTPError(
            "https://open.bigmodel.cn/api/paas/v4/chat/completions",
            400,
            "bad request",
            {},
            io.BytesIO(
                b'{"error":{"code":"1210","message":"image count exceeded"}}'
            ),
        )
        with patch.object(
            self.breakdown.egress,
            "post_json_idempotent",
            side_effect=error,
        ) as post, patch.object(
            self.breakdown, "_post_openai_fallback"
        ) as fallback:
            with self.assertRaisesRegex(ValueError, "1210") as raised:
                self.breakdown._reverse_chat_multimodal(
                    "system", "user", []
                )
        self.assertNotIsInstance(raised.exception, TimeoutError)
        self.assertEqual(post.call_count, 1)
        fallback.assert_not_called()

    def test_general_multimodal_1210_keeps_existing_openai_fallback(self):
        os.environ["REVERSE_ZHIPU_KEY"] = "zhipu-test-key"
        self.breakdown.OPENAI_KEY = "openai-test-key"
        error = urllib.error.HTTPError(
            "https://open.bigmodel.cn/api/paas/v4/chat/completions",
            400,
            "bad request",
            {},
            io.BytesIO(b"{}"),
        )
        error.breakdown_response_detail = (
            '{"error":{"code":"1210","message":"image count exceeded"}}'
        )
        with patch.object(
            self.breakdown, "_post_zhipu", side_effect=error
        ), patch.object(
            self.breakdown,
            "_post_openai_fallback",
            return_value={
                "choices": [{"message": {"content": "fallback result"}}]
            },
        ) as fallback:
            result = self.breakdown._chat_multimodal(
                "system",
                "user",
                [],
                allow_provider_fallback=True,
            )
        self.assertEqual(result, "fallback result")
        self.assertEqual(fallback.call_count, 1)

    def test_reverse_requests_are_fixed_to_glm_4v_plus(self):
        captured = []
        os.environ["REVERSE_ZHIPU_KEY"] = "zhipu-test-key"

        def fake_zhipu(body, api_key):
            captured.append((body, api_key))
            return {"choices": [{"message": {"content": "ok"}}]}

        with patch.object(
            self.breakdown, "_post_zhipu", side_effect=fake_zhipu
        ), patch.object(
            self.breakdown, "_post_openai_fallback"
        ) as fallback:
            self.assertEqual(
                self.breakdown._reverse_chat_multimodal(
                    "system", "user", []
                ),
                "ok",
            )
        self.assertEqual(captured[0][0]["model"], "glm-4v-plus")
        fallback.assert_not_called()

    def test_reverse_model_physical_retry_refreshes_heartbeat(self):
        events = []
        captured = {}

        def fake_post(*args, **kwargs):
            captured.update(kwargs)
            events.append("physical_attempt_1")
            kwargs["log"]("first physical attempt failed")
            events.append("physical_attempt_2")
            return {"choices": [{"message": {"content": "ok"}}]}

        with patch.object(
            self.breakdown.egress, "post_json_idempotent", fake_post
        ), patch.object(
            self.breakdown.time,
            "monotonic",
            side_effect=[100.0, 101.0, 102.0],
        ):
            response = self.breakdown._post_zhipu(
                {"model": "glm-4v-plus"},
                "key",
                deadline=640.0,
                heartbeat=lambda: events.append("heartbeat"),
            )

        self.assertEqual(response["choices"][0]["message"]["content"], "ok")
        self.assertEqual(events, [
            "heartbeat",
            "physical_attempt_1",
            "heartbeat",
            "physical_attempt_2",
        ])
        self.assertEqual(captured["max_attempts"], 2)
        self.assertLessEqual(captured["timeout"] * 2, 539)

    def test_post_zhipu_accepts_deployed_egress_without_timeout_parameter(self):
        calls = []

        def deployed_post(
            official_base, fallback_base, path, data, headers, log=None,
            max_attempts=2,
        ):
            calls.append({
                "official_base": official_base,
                "fallback_base": fallback_base,
                "path": path,
                "max_attempts": max_attempts,
            })
            return {"choices": [{"message": {"content": "ok"}}]}

        with patch.object(
            self.breakdown.egress, "post_json_idempotent", deployed_post,
        ), patch.object(
            self.breakdown.time, "monotonic", side_effect=[100.0, 101.0],
        ):
            response = self.breakdown._post_zhipu(
                {"model": "glm-4v-plus"},
                "key",
                deadline=640.0,
                heartbeat=lambda: None,
            )

        self.assertEqual(response["choices"][0]["message"]["content"], "ok")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["path"], "/chat/completions")
        self.assertEqual(calls[0]["max_attempts"], 2)

    def test_reverse_model_normal_attempt_refreshes_heartbeat(self):
        heartbeats = []
        with patch.object(
            self.breakdown.egress,
            "post_json_idempotent",
            return_value={"choices": [{"message": {"content": "ok"}}]},
        ), patch.object(
            self.breakdown.time,
            "monotonic",
            side_effect=[100.0, 101.0],
        ):
            self.breakdown._post_zhipu(
                {"model": "glm-4v-plus"},
                "key",
                deadline=640.0,
                heartbeat=lambda: heartbeats.append("beat"),
            )
        self.assertEqual(heartbeats, ["beat"])

    def test_reverse_analysis_deadline_stops_before_new_request(self):
        called = []
        with patch.object(
            self.breakdown.egress,
            "post_json_idempotent",
            lambda *args, **kwargs: called.append(True),
        ), patch.object(
            self.breakdown.time, "monotonic", return_value=701.0
        ):
            with self.assertRaisesRegex(TimeoutError, "总时间预算"):
                self.breakdown._post_zhipu(
                    {"model": "glm-4v-plus"},
                    "key",
                    deadline=700.0,
                    heartbeat=lambda: called.append("heartbeat"),
                )
        self.assertEqual(called, [])

    def test_reverse_audit_fields_use_existing_persisted_asset_columns(self):
        root = Path(__file__).resolve().parents[1]
        assets = (
            root / "server" / "content_domains" / "assets_store.py"
        ).read_text(encoding="utf-8")
        self.assertIn('"sections": r.get("sections")', assets)
        self.assertIn('"frame_thumbnails": r.get("frame_thumbnails")', assets)

    def test_reverse_reference_frames_cover_every_segment_in_order(self):
        self.assertEqual(
            self.breakdown._reverse_reference_frames(
                ["frame-%d.jpg" % index for index in range(1, 9)], 4
            ),
            ["frame-2.jpg", "frame-4.jpg", "frame-6.jpg", "frame-8.jpg"],
        )
        bundle = self.breakdown._reverse_frame_bundle(
            ["frame-%d.jpg" % index for index in range(1, 9)], 4
        )
        self.assertEqual(bundle["frames"], [
            "frame-2.jpg", "frame-4.jpg", "frame-6.jpg", "frame-8.jpg",
            "frame-1.jpg", "frame-3.jpg", "frame-5.jpg", "frame-7.jpg",
        ])
        self.assertEqual(
            {item["source_frame_index"] for item in bundle["manifest"]},
            set(range(1, 9)),
        )

    def test_reverse_model_frame_groups_use_two_images_for_one_to_four_segments(self):
        frames = ["frame-%d.jpg" % index for index in range(1, 9)]
        cases = (
            (1, [["frame-1.jpg", "frame-8.jpg"]], [[1, 8]]),
            (
                2,
                [
                    ["frame-1.jpg", "frame-4.jpg"],
                    ["frame-5.jpg", "frame-8.jpg"],
                ],
                [[1, 4], [5, 8]],
            ),
            (
                3,
                [
                    ["frame-1.jpg", "frame-3.jpg"],
                    ["frame-4.jpg", "frame-5.jpg"],
                    ["frame-6.jpg", "frame-8.jpg"],
                ],
                [[1, 3], [4, 5], [6, 8]],
            ),
            (
                4,
                [
                    ["frame-1.jpg", "frame-2.jpg"],
                    ["frame-3.jpg", "frame-4.jpg"],
                    ["frame-5.jpg", "frame-6.jpg"],
                    ["frame-7.jpg", "frame-8.jpg"],
                ],
                [[1, 2], [3, 4], [5, 6], [7, 8]],
            ),
        )
        for segment_count, expected_frames, expected_sources in cases:
            with self.subTest(segment_count=segment_count):
                self.assertEqual(
                    self.breakdown._reverse_model_frame_groups(
                        frames, segment_count
                    ),
                    expected_frames,
                )
                self.assertEqual(
                    self.breakdown._reverse_frame_bundle(
                        frames, segment_count
                    )["segment_model_source_indices"],
                    expected_sources,
                )
        entries = [
            self._reverse_entry(action="第%d段动作" % index)
            for index in range(1, 4)
        ]
        evidence = self.breakdown._reverse_segment_evidence_manifest(
            entries,
            self.breakdown._reverse_segment_windows(8.0),
            [[1, 2, 3], [4, 5], [6, 7, 8]],
            [[1, 3], [4, 5], [6, 8]],
        )
        self.assertEqual(
            [
                item["source_evidence_frames"]["action"]
                for item in evidence
            ],
            [[1, 3], [4, 5], [6, 8]],
        )
        self.assertEqual(
            evidence[2]["segment_source_frames"],
            [6, 7, 8],
        )

    def test_reverse_global_continuity_is_zero_image_deterministic_aggregation(self):
        entries = [
            self._reverse_entry(
                subject="同一白衣人物位于画面中央",
                scene="树林与石阶",
                action="人物抬起右手",
                camera="平视中景",
                lighting="柔和自然光",
            ),
            self._reverse_entry(
                subject="同一白衣人物位于画面中央",
                scene="室内床铺",
                action="人物坐到床边",
                camera="平视中景",
                lighting="柔和自然光",
            ),
            self._reverse_entry(
                subject="同一白衣人物位于画面中央",
                scene="树干近景",
                action="人物从树后走出",
                camera="平视中景",
                lighting="柔和自然光",
            ),
        ]
        with patch.object(
            self.breakdown, "_chat_multimodal"
        ) as chat:
            result = self.breakdown._reverse_global_facts_from_segments(
                entries,
                [[1, 3], [4, 5], [6, 8]],
                8,
            )
        chat.assert_not_called()
        self.assertEqual(result["model_calls"], 0)
        self.assertEqual(result["image_count"], 0)
        self.assertEqual(result["segment_count"], 3)
        self.assertEqual(
            result["facts"]["subject_identity"],
            "同一白衣人物位于画面中央",
        )
        self.assertEqual(
            result["evidence_frames"]["subject_identity"],
            [1, 3, 4, 5, 6, 8],
        )
        self.assertEqual(
            [item["text"] for item in result["changes"]["action"]],
            ["人物抬起右手", "人物坐到床边", "人物从树后走出"],
        )
        self.assertEqual(
            result["changes"]["scene"][1]["evidence_frames"],
            [4, 5],
        )

    def test_reverse_one_to_four_segments_make_only_two_image_requests(self):
        frames = ["frame-%d.jpg" % index for index in range(1, 9)]
        cases = (
            (2.0, [[frames[0], frames[7]]]),
            (5.0, [[frames[0], frames[3]], [frames[4], frames[7]]]),
            (
                8.0,
                [
                    [frames[0], frames[2]],
                    [frames[3], frames[4]],
                    [frames[5], frames[7]],
                ],
            ),
            (
                11.0,
                [
                    [frames[0], frames[1]],
                    [frames[2], frames[3]],
                    [frames[4], frames[5]],
                    [frames[6], frames[7]],
                ],
            ),
        )
        for duration, expected in cases:
            calls = []
            objects = self._detailed_reverse_objects(len(expected))

            def fake_chat(_system, _user, request_frames, **kwargs):
                calls.append((list(request_frames), kwargs))
                return json.dumps(
                    {"segments": [objects[len(calls) - 1]]},
                    ensure_ascii=False,
                )

            with self.subTest(duration=duration), patch.object(
                self.breakdown, "_chat_multimodal", side_effect=fake_chat
            ):
                self.breakdown._reverse_prompt_from_frames(
                    "短视频", duration, "douyin", "", frames
                )
            self.assertEqual(
                [request_frames for request_frames, _kwargs in calls],
                expected,
            )
            self.assertTrue(all(
                len(request_frames) <= 2
                and kwargs["model"] == "glm-4v-plus"
                and kwargs["allow_provider_fallback"] is False
                for request_frames, kwargs in calls
            ))

    def test_reverse_short_video_reference_indices_survive_persistence(self):
        assets_store = importlib.import_module("content_domains.assets_store")
        frames = ["frame-%d" % index for index in range(1, 9)]
        cases = (
            (2.0, [8]),
            (5.0, [4, 8]),
            (8.0, [3, 5, 8]),
            (11.0, [2, 4, 6, 8]),
        )
        for duration, expected_sources in cases:
            with self.subTest(duration=duration):
                segment_count = len(
                    self.breakdown._reverse_segment_windows(duration)
                )
                bundle = self.breakdown._reverse_frame_bundle(
                    frames, segment_count
                )
                thumbnails = [
                    "thumb-source-%d" % item["source_frame_index"]
                    for item in bundle["manifest"]
                ]
                result = {
                    "type": "breakdown_reverse",
                    "prompt": "prompt",
                    "frame_thumbnails": thumbnails,
                    "sections": {
                        "reverse_audit": {
                            "reference_thumbnail_indices": bundle[
                                "reference_thumbnail_indices"
                            ],
                            "audit_thumbnail_indices": bundle[
                                "audit_thumbnail_indices"
                            ],
                            "frame_manifest": bundle["manifest"],
                        },
                    },
                }
                persisted = assets_store._project(
                    "breakdown", result
                )[3]
                audit = persisted["sections"]["reverse_audit"]
                selected = [
                    persisted["frame_thumbnails"][index - 1]
                    for index in audit["reference_thumbnail_indices"]
                ]
                self.assertEqual(
                    selected,
                    [
                        "thumb-source-%d" % source
                        for source in expected_sources
                    ],
                )
                self.assertEqual(
                    len(audit["reference_thumbnail_indices"]),
                    segment_count,
                )
                self.assertTrue(
                    set(audit["reference_thumbnail_indices"]).isdisjoint(
                        audit["audit_thumbnail_indices"]
                    )
                )

    def test_reverse_short_video_continuity_uses_actual_segment_count(self):
        single = {
            "facts": {
                key: "" for key, _label
                in self.breakdown._REVERSE_GLOBAL_FACT_FIELDS
            },
            "evidence_frames": {
                key: [] for key, _label
                in self.breakdown._REVERSE_GLOBAL_FACT_FIELDS
            },
            "frame_count": 8,
            "segment_count": 1,
        }
        one_segment_entry = self._reverse_entry(
            continuity="人物在本段保持白色服装"
        )
        one_segment_entry["continuity_evidence_frames"] = [1, 8]
        with self.assertRaisesRegex(ValueError, "单段视频"):
            self.breakdown._validate_reverse_segment_evidence(
                one_segment_entry,
                [],
                ["segment-first.jpg", "segment-last.jpg"],
                1,
                require_frame_evidence=True,
                global_continuity=single,
            )

        two_segments = self._global_continuity()
        two_segments["segment_count"] = 2
        two_segments["evidence_frames"] = {
            key: [1, 5] for key in two_segments["facts"]
        }
        second_segment_entry = self._reverse_entry(
            continuity="白色服装在两个相邻时间段均可见"
        )
        second_segment_entry["continuity_evidence_frames"] = [1, 5]
        self.assertIs(
            self.breakdown._validate_reverse_segment_evidence(
                second_segment_entry,
                [],
                ["segment-first.jpg", "segment-last.jpg"],
                2,
                require_frame_evidence=True,
                global_continuity=two_segments,
            ),
            second_segment_entry,
        )

        self.assertEqual(
            [
                self.breakdown._reverse_source_frame_segment(
                    frame, 8, 3
                )
                for frame in range(1, 9)
            ],
            [1, 1, 1, 2, 2, 3, 3, 3],
        )
        three_segments = self._global_continuity()
        three_segments["segment_count"] = 3
        three_segments["evidence_frames"] = {
            key: [4, 6] for key in three_segments["facts"]
        }
        third_segment_entry = self._reverse_entry(
            continuity="人物服装在第二、第三时间段均可见"
        )
        third_segment_entry["continuity_evidence_frames"] = [4, 6]
        self.assertIs(
            self.breakdown._validate_reverse_segment_evidence(
                third_segment_entry,
                [],
                ["segment-first.jpg", "segment-last.jpg"],
                3,
                require_frame_evidence=True,
                global_continuity=three_segments,
            ),
            third_segment_entry,
        )

        cross_boundary = json.loads(self._global_continuity_response())
        cross_boundary["evidence_frames"] = {
            key: [4, 6] for key in cross_boundary["global_facts"]
        }
        parsed = self.breakdown._parse_reverse_global_facts(
            json.dumps(cross_boundary, ensure_ascii=False), 8, 3
        )
        self.assertEqual(
            parsed["evidence_frames"]["subject_identity"], [4, 6]
        )
        same_segment = dict(cross_boundary)
        same_segment["evidence_frames"] = {
            key: [6, 8] for key in same_segment["global_facts"]
        }
        with self.assertRaisesRegex(ValueError, "至少两个不同时间段"):
            self.breakdown._parse_reverse_global_facts(
                json.dumps(same_segment, ensure_ascii=False), 8, 3
            )

    def test_reverse_one_segment_skips_cross_segment_global_model_call(self):
        self.breakdown._chat_multimodal = mock.Mock(
            side_effect=AssertionError("single segment must not call global VLM")
        )
        continuity = self.breakdown._reverse_global_facts_from_segments(
            [self._reverse_entry(
                camera="中景固定机位",
                lighting="中性自然光",
            )],
            [[1, 8]],
            8,
        )
        self.assertEqual(continuity["segment_count"], 1)
        self.assertFalse(any(continuity["facts"].values()))
        self.assertEqual(continuity["model_calls"], 0)
        self.assertEqual(continuity["image_count"], 0)
        entry = self._reverse_entry(
            camera="中景固定机位",
            lighting="中性自然光",
        )
        score = self.breakdown._score_reverse_generation_coverage(
            [entry],
            continuity,
            self.breakdown._reverse_segment_windows(2.0),
        )
        self.assertEqual(score["total"], 100)

    def test_reverse_downstream_generation_field_mapping_is_explicit(self):
        root = Path(__file__).resolve().parents[1]
        ui = (root / "site" / "workbench" / "script.html").read_text(
            encoding="utf-8"
        )
        video = (root / "server" / "content_domains" / "video.py").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "var reverseRefs=reverseReferenceImages(lastBreakdownReverse)",
            ui,
        )
        self.assertNotIn(
            "frame_thumbnails)||[]).slice(0,4)",
            ui,
        )
        self.assertIn(
            "reverse_audit||{}).reference_thumbnail_indices",
            ui,
        )
        self.assertIn("channel:'micro',prompt:seedancePrompt,reference_images:reverseRefs,duration:choice.duration", ui)
        self.assertIn("endpoint:'/api/gen/xiaole_video'", ui)
        self.assertIn("cine_mode:'open', avatar_ids:[choice.avatarId], prompt:avatarPrompt", ui)
        self.assertIn("endpoint:'/api/gen/cinematic'", ui)
        self.assertIn('"micro": "seedance-2.0-fast"', video)
        self.assertIn('input_d["mode"] = "image_to_video"', video)
        self.assertIn('input_d["duration_seconds"] = 10', video)
        self.assertIn('"prompt": prompt or MOTION_PROMPT', video)

    def test_reverse_segment_scoped_acceptance_contract(self):
        calls = []
        objects = self._detailed_reverse_objects()

        def fake_chat_multimodal(sysmsg, usermsg, frames, temp=0.7, **kwargs):
            calls.append((sysmsg, usermsg, list(frames), temp, kwargs))
            return json.dumps(
                {"segments": [objects[len(calls) - 1]]},
                ensure_ascii=False,
            )

        self.breakdown._chat_multimodal = fake_chat_multimodal
        prompt = self.breakdown._reverse_prompt_from_frames(
            "海边人物动作", 15.093, "local", "", [
                "frame-%d.jpg" % index for index in range(1, 9)
            ],
        )

        self.assertEqual(len(calls), 4)
        self.assertEqual(
            [call[2] for call in calls],
            [
                ["frame-1.jpg", "frame-2.jpg"],
                ["frame-3.jpg", "frame-4.jpg"],
                ["frame-5.jpg", "frame-6.jpg"],
                ["frame-7.jpg", "frame-8.jpg"],
            ],
        )
        self.assertTrue(all(call[3] == 0.1 for call in calls))
        self.assertTrue(all(
            call[4] == {
                "max_tokens": 900,
                "image_detail": None,
                "provider": "zhipu",
                "model": "glm-4v-plus",
                "allow_provider_fallback": False,
            }
            for call in calls
        ))
        self.assertIn("不能从图片直接确认", calls[0][1])
        self.assertIn("不得复制其他时间段", calls[0][0])

        lines = prompt.splitlines()
        self.assertEqual(len(lines), 4)
        self.assertTrue(lines[0].startswith("[00:00.0-00:03.8]"))
        self.assertTrue(lines[-1].startswith("[00:11.3-00:15.1]"))
        bodies = [line.split("] ", 1)[1] for line in lines]
        self.assertEqual(len(set(bodies)), 4)
        for body in bodies:
            for label in (
                "主体：", "场景：", "动作：", "构图与镜头：",
                "光影色彩：", "风格材质：", "节奏：",
            ):
                self.assertIn(label, body)
            self.assertNotIn("衔接：", body)
            self.assertNotIn("声音：", body)

    def test_reverse_segment_contract_handles_ten_concurrent_jobs(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor

        objects = self._detailed_reverse_objects()
        lock = threading.Lock()
        call_count = [0]

        def fake_chat_multimodal(sysmsg, usermsg, frames, *args, **kwargs):
            frame_number = int(
                os.path.basename(frames[0]).split("-")[-1].split(".")[0]
            )
            item = objects[(frame_number - 1) // 2]
            with lock:
                call_count[0] += 1
            return json.dumps({"segments": [item]}, ensure_ascii=False)

        self.breakdown._chat_multimodal = fake_chat_multimodal

        def reverse_one(index):
            return self.breakdown._reverse_prompt_from_frames(
                "并发任务%d" % index,
                15.093,
                "local",
                "",
                ["frame-%d.jpg" % number for number in range(1, 9)],
            )

        with ThreadPoolExecutor(max_workers=10) as executor:
            prompts = list(executor.map(reverse_one, range(10)))

        self.assertEqual(call_count[0], 40)
        self.assertEqual(len(prompts), 10)
        self.assertTrue(all(len(prompt.splitlines()) == 4 for prompt in prompts))
        self.assertTrue(all(
            prompt.splitlines()[-1].startswith("[00:11.3-00:15.1]")
            for prompt in prompts
        ))

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

    def test_reverse_mode_extracts_eight_uniform_frames_and_groups_by_segment(self):
        calls = self._install_fake_env(
            json.dumps(
                {"segments": self._detailed_reverse_objects()},
                ensure_ascii=False,
            ),
            transcript=[],
        )

        def fake_extract(
            path, count, duration, scale_width=512, min_frames=None,
            uniform=False,
        ):
            calls["extract_args"] = (count, scale_width, min_frames, uniform)
            return "frames-dir", ["f%d.jpg" % i for i in range(1, 9)]

        def fake_pair(frame_dir, frames):
            self.fail("reverse content path must use original frames, not pair images")

        self.breakdown._extract_frames = fake_extract
        original_thumbnails = self.breakdown._frame_thumbnails
        self.breakdown._frame_thumbnails = lambda paths, limit=4: [
            "data:image/jpeg;base64,frame-%d" % index
            for index, _path in enumerate(list(paths)[:limit], 1)
        ]
        had_pair = hasattr(self.breakdown, "_pair_reverse_frames")
        original_pair = getattr(self.breakdown, "_pair_reverse_frames", None)
        self.breakdown._pair_reverse_frames = fake_pair
        try:
            with patch.object(
                self.breakdown,
                "_gemini_reverse_prompt_from_media",
                return_value=self._gemini_reverse_result(),
            ) as gemini:
                result = self.breakdown._do_breakdown(
                    {"_job_id": 80, "mode": "reverse_prompt"},
                    {"platform": "douyin", "id": "detail-depth"},
                    "https://example.test/detail-depth",
                    "reverse_prompt",
                )
        finally:
            self.breakdown._frame_thumbnails = original_thumbnails
            if had_pair:
                self.breakdown._pair_reverse_frames = original_pair
            else:
                delattr(self.breakdown, "_pair_reverse_frames")

        self.assertEqual(calls["extract_args"], (8, 1024, 8, True))
        gemini.assert_called_once()
        self.assertEqual(gemini.call_args.args[1], "video/mp4")
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
            def download_to_file(play_url, deadline, filename, max_bytes=None):
                calls["download"] = (play_url, filename)
                return 1

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
            def download_to_file(play_url, deadline, filename, max_bytes=None):
                return 1
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
