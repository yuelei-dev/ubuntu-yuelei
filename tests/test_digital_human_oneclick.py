import importlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


class DigitalHumanOneClickTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server_dir = str(Path(__file__).resolve().parents[1] / "server")
        if server_dir not in sys.path:
            sys.path.insert(0, server_dir)
        cls.domain = importlib.import_module("content_domains.digital_human_oneclick")
        cls.points = importlib.import_module("content_domains.points")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "jobs.db"
        connection = sqlite3.connect(self.db)
        connection.execute("""CREATE TABLE jobs(
            id INTEGER PRIMARY KEY, username TEXT, kind TEXT,
            status TEXT, result TEXT
        )""")
        for job_id in range(1, 4):
            rel = "videos/%d.mp4" % job_id
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"video")
            connection.execute(
                "INSERT INTO jobs VALUES(?,?,?,?,?)",
                (job_id, "yuelei", "video", "done", json.dumps({"video_file": rel})),
            )
        for job_id in range(11, 17):
            rel = "images/%d.png" % job_id
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"image")
            connection.execute(
                "INSERT INTO jobs VALUES(?,?,?,?,?)",
                (job_id, "yuelei", "image", "done", json.dumps({"file": rel})),
            )
        connection.commit()
        connection.close()
        self.patches = [
            mock.patch.object(self.domain, "OUT_DIR", self.root),
            mock.patch.object(self.domain, "jdb", self._connection),
        ]
        for patcher in self.patches:
            patcher.start()

    def _connection(self):
        connection = sqlite3.connect(self.db)
        connection.row_factory = sqlite3.Row
        return connection

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        self.tmp.cleanup()

    def test_plan_is_deterministic_and_has_three_segments_six_materials(self):
        script = "第一部分介绍问题。第二部分说明方案。第三部分给出结果。第四部分补充证据。第五部分强调价值。第六部分发出行动邀请。"
        first = self.domain.plan(script)
        second = self.domain.plan(script)
        self.assertEqual(first, second)
        self.assertEqual(len(first["segments"]), 3)
        self.assertEqual(len(first["materials"]), 6)
        self.assertEqual("".join(item["text"] for item in first["segments"]), script)
        self.assertEqual(len({item["gesture_prompt"] for item in first["segments"]}), 3)
        self.assertEqual([item["role"] for item in first["segments"]], ["hook", "explain", "cta"])
        self.assertTrue(all(item["source_policy"] == "customer_then_feishu_then_ai" for item in first["materials"]))
        self.assertTrue(all(item["material_query"] for item in first["materials"]))

    def test_prepare_freezes_only_owned_completed_child_jobs(self):
        script = "第一段说明背景。第二段解释方案。第三段展示结果。第四段补充细节。第五段强调价值。第六段邀请行动。"
        planned = self.domain.plan(script)
        payload = self.domain.prepare_compose_payload({
            "pipeline": self.domain.PIPELINE,
            "script": script,
            "plan_digest": planned["plan_digest"],
            "video_job_ids": [1, 2, 3],
            "material_job_ids": [11, 12, 13, 14, 15, 16],
        }, "yuelei")
        self.assertEqual(payload["video_files"], ["videos/1.mp4", "videos/2.mp4", "videos/3.mp4"])
        self.assertEqual(len(payload["material_files"]), 6)
        self.assertNotIn("_script_to_video_state", payload)

    def test_prepare_rejects_digest_drift_and_foreign_job(self):
        script = "第一段说明背景。第二段解释方案。第三段展示结果。第四段补充细节。第五段强调价值。第六段邀请行动。"
        with self.assertRaisesRegex(self.domain.DigitalHumanRequestError, "方案已变化"):
            self.domain.prepare_compose_payload({
                "pipeline": self.domain.PIPELINE, "script": script,
                "plan_digest": "0" * 64, "video_job_ids": [1, 2, 3],
                "material_job_ids": [11, 12, 13, 14, 15, 16],
            }, "yuelei")
        connection = sqlite3.connect(self.db)
        connection.execute("UPDATE jobs SET username='other' WHERE id=3")
        connection.commit()
        connection.close()
        planned = self.domain.plan(script)
        with self.assertRaisesRegex(self.domain.DigitalHumanRequestError, "不属于当前账号"):
            self.domain.prepare_compose_payload({
                "pipeline": self.domain.PIPELINE, "script": script,
                "plan_digest": planned["plan_digest"], "video_job_ids": [1, 2, 3],
                "material_job_ids": [11, 12, 13, 14, 15, 16],
            }, "yuelei")

    def test_local_compose_has_zero_additional_points(self):
        body = {"pipeline": self.domain.PIPELINE}
        self.assertEqual(self.points.cost_of("script_to_video", body), 0)
        self.assertEqual(body["cost_breakdown"]["local_compose"], 0)

    def test_compose_matches_vertical_delivery_rate_and_labels_ai_materials(self):
        source = Path(self.domain.__file__).read_text(encoding="utf-8")
        self.assertIn("fps=30", source)
        self.assertIn("r=30", source)
        self.assertNotIn("fps=25", source)
        self.assertIn("CONCEPT / AI FILL", source)

    def test_final_verification_requires_audio_sync_and_rejects_long_black_frame(self):
        media = self.root / "final.mp4"
        media.write_bytes(b"video")
        video_domain = SimpleNamespace(
            _resolve_out_file=lambda rel: media,
            _probe_video_size=lambda path: (1080, 1920),
            _probe_video_duration=lambda rel: 12.0,
        )
        audio = SimpleNamespace(returncode=0, stdout=b"audio\n", stderr=b"")
        clear = SimpleNamespace(returncode=0, stdout=b"", stderr=b"no black frames")
        with mock.patch.object(self.domain.subprocess, "run", side_effect=[audio, clear]):
            result = self.domain._verify_final_video(video_domain, "final.mp4", 12.1)
        self.assertEqual(result[1:], (1080, 1920, 12.0))
        black = SimpleNamespace(returncode=0, stdout=b"", stderr=b"black_duration:11.2")
        with mock.patch.object(self.domain.subprocess, "run", side_effect=[audio, black]):
            with self.assertRaisesRegex(RuntimeError, "持续黑帧"):
                self.domain._verify_final_video(video_domain, "final.mp4", 12.0)


class DigitalHumanOneClickUiTests(unittest.TestCase):
    def test_page_exposes_required_inputs_and_real_pipeline_calls(self):
        page = (Path(__file__).resolve().parents[1] / "site" / "workbench" / "digital-human-oneclick.html").read_text(encoding="utf-8")
        for marker in (
            'id="photo"', 'id="voice"', 'id="script"',
            "/api/gen/digital-human-oneclick/plan", "/api/gen/audio/clone-vip",
            "reference_images:[photoData]", "motion:profile.motion||'high'", "speed:Number(profile.speed||1)", "pitch:Number(profile.pitch||0)", "volume:Number(profile.volume||0)", "subtitle:false",
            "digital_human_oneclick_compose", "video_job_ids", "material_job_ids",
        ):
            self.assertIn(marker, page)
        self.assertIn("合成本身 0 点", page)
        self.assertIn("Whisper 字幕", page)
        self.assertIn("分析并预览方案", page)
        self.assertIn("确认方案并生成", page)
        self.assertIn("客户素材 → 飞书授权真实素材 → AI 补缺", page)
        self.assertNotIn("选择剪辑风格", page)

    def test_plan_contains_distinct_delivery_profiles(self):
        domain = importlib.import_module("content_domains.digital_human_oneclick")
        payload = domain.plan("开场先抓住注意力。接着把方案讲清楚。最后给出行动号召。")
        profiles = [item["speech_profile"] for item in payload["segments"]]
        self.assertEqual([item["delivery"] for item in profiles], ["energetic_hook", "clear_explain", "confident_cta"])
        self.assertNotEqual(profiles[0]["speed"], profiles[1]["speed"])


    def test_video_page_replaces_the_old_talking_tab_instead_of_adding_a_header_link(self):
        page = (Path(__file__).resolve().parents[1] / "site" / "workbench" / "video.html").read_text(encoding="utf-8")
        self.assertIn('<a class="function-tab on" href="digital-human-oneclick.html"', page)
        self.assertEqual(page.count('href="digital-human-oneclick.html"'), 1)
        self.assertNotIn('data-function="talking">数字人口播', page)


if __name__ == "__main__":
    unittest.main()
