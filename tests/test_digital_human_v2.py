import base64
import hashlib
import importlib
import io
import json
import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock


PNG_2X2 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEklEQVR4nGMU0bBhYGBgYgADAAWiAHylyrQdAAAAAElFTkSuQmCC"
)


class DigitalHumanV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server_dir = str(Path(__file__).resolve().parents[1] / "server")
        if server_dir not in sys.path:
            sys.path.insert(0, server_dir)
        cls.domain = importlib.import_module("content_domains.digital_human_v2")
        cls.legacy = importlib.import_module("content_domains.digital_human_oneclick")
        cls.points = importlib.import_module("content_domains.points")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.jobs_db = self.root / "jobs.db"
        self.consent_db = self.root / "consents.db"
        connection = sqlite3.connect(self.jobs_db)
        connection.execute("""CREATE TABLE jobs(
            id INTEGER PRIMARY KEY, username TEXT, kind TEXT, status TEXT,
            payload TEXT, result TEXT, deleted INTEGER DEFAULT 0
        )""")
        connection.commit()
        connection.close()
        self.video = types.ModuleType("content_domains.video")
        self.video.subtitle_runtime_preflight = mock.Mock(return_value={"ok": True})
        self.patches = [
            mock.patch.object(self.domain, "OUT_DIR", self.root),
            mock.patch.object(self.domain, "jdb", self._jobs_connection),
            mock.patch.object(self.legacy, "OUT_DIR", self.root),
            mock.patch.object(self.legacy, "CONSENT_DB", self.consent_db),
            mock.patch.object(self.legacy, "cdb", self._consent_connection),
            mock.patch.dict(sys.modules, {"content_domains.video": self.video}),
            mock.patch.object(sys.modules["content_domains"], "video", self.video, create=True),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        self.tmp.cleanup()

    def _jobs_connection(self):
        connection = sqlite3.connect(self.jobs_db)
        connection.row_factory = sqlite3.Row
        return connection

    def _consent_connection(self):
        connection = sqlite3.connect(self.consent_db)
        connection.row_factory = sqlite3.Row
        return connection

    def _consent(self, script, gestures=2):
        plan = self.domain.timeline.plan_text(script, gestures)
        payload = {
            "confirmed": True,
            "consent_version": self.domain.CONSENT_VERSION,
            "purpose": self.domain.CONSENT_PURPOSE,
            "run_id": "dh-v2-run-test-001",
            "plan_digest": plan["plan_digest"],
            "script": script,
            "gesture_count": gestures,
            "photo_sha256": hashlib.sha256(PNG_2X2).hexdigest(),
            "voice_mode": "existing",
            "voice_ref": "voice-owned-1",
            "voice_sha256": "",
            "narration_mode": "text",
        }
        consent = self.domain.create_consent(
            payload, "yuelei", "test-signing-secret", db_factory=self._consent_connection,
        )
        return plan, consent

    def _metadata(self, plan, consent, stage, index):
        return {
            "digital_human_pipeline": self.domain.CONSENT_PURPOSE,
            "digital_human_stage": stage,
            "digital_human_run_id": consent["run_id"],
            "digital_human_plan_digest": plan["plan_digest"],
            "digital_human_consent_token": consent["consent_token"],
            "digital_human_script": plan["copy"],
            "digital_human_gesture_count": plan["gesture_count"],
            "digital_human_item_index": index,
        }

    def test_consent_is_bound_to_duration_driven_plan(self):
        plan, consent = self._consent("普通人使用人工智能时，先把目标讲清楚，再选择合适工具。" * 8)
        self.assertEqual(consent["purpose"], self.domain.CONSENT_PURPOSE)
        self.assertGreater(plan["segment_count"], 1)
        with self.assertRaises(self.domain.DigitalHumanRequestError) as caught:
            self.domain.create_consent({
                "confirmed": True,
                "consent_version": self.domain.CONSENT_VERSION,
                "purpose": self.domain.CONSENT_PURPOSE,
                "run_id": "dh-v2-run-test-002",
                "plan_digest": "0" * 64,
                "script": plan["copy"],
                "gesture_count": plan["gesture_count"],
                "photo_sha256": hashlib.sha256(PNG_2X2).hexdigest(),
                "voice_mode": "existing", "voice_ref": "voice-owned-1",
                "voice_sha256": "", "narration_mode": "text",
            }, "yuelei", "test-signing-secret", db_factory=self._consent_connection)
        self.assertEqual(caught.exception.code, "consent_plan_mismatch")

    def test_gesture_submission_ignores_forged_provider_and_prompt(self):
        plan, consent = self._consent("这是一段用于验证数字人手势安全绑定的完整口播文案。")
        payload = self._metadata(plan, consent, "gesture", 0)
        payload.update({
            "provider": "openai", "model": "forged", "quality": "hd",
            "prompt": "forged", "reference_images": [base64.b64encode(PNG_2X2).decode("ascii")],
        })
        cleaned, record = self.domain.verify_child_submission_with_record(
            payload, "yuelei", "image",
        )
        self.assertEqual(cleaned["provider"], "banana")
        self.assertEqual(cleaned["model"], "nb2")
        self.assertEqual(cleaned["quality"], "std")
        self.assertNotEqual(cleaned["prompt"], "forged")
        self.assertEqual(record["purpose"], self.domain.CONSENT_PURPOSE)

    def test_talking_submission_cycles_gesture_and_preserves_one_voice(self):
        script = "普通人学习人工智能，不用先背很多术语，从一个真实问题开始就可以。" * 8
        plan, consent = self._consent(script, gestures=2)
        segment = plan["segments"][1]
        gesture_index = segment["gesture_index"]
        image_rel = "images/gesture.png"
        image_path = self.root / image_rel
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image_path.write_bytes(PNG_2X2)
        gesture_payload = self._metadata(plan, consent, "gesture", gesture_index)
        gesture_payload.pop("digital_human_consent_token")
        gesture_payload["digital_human_consent_id"] = consent["consent_id"]
        connection = sqlite3.connect(self.jobs_db)
        connection.execute(
            "INSERT INTO jobs(id,username,kind,status,payload,result) VALUES(?,?,?,?,?,?)",
            (21, "yuelei", "image", "done", json.dumps(gesture_payload), json.dumps({"file": image_rel})),
        )
        connection.commit()
        connection.close()
        talking = self._metadata(plan, consent, "talking", 1)
        talking.update({"voice": "voice-owned-1", "gesture_job_id": 21, "text": "forged"})
        cleaned, _record = self.domain.verify_child_submission_with_record(
            talking, "yuelei", "video",
        )
        self.assertEqual(cleaned["text"], segment["text"])
        self.assertEqual(cleaned["voice"], "voice-owned-1")
        self.assertTrue(cleaned["image_data"].startswith("data:image/png;base64,"))

    def test_full_audio_plan_and_talking_use_exact_owned_slice(self):
        raw_audio = b"real-complete-audio-for-binding"
        claimed = hashlib.sha256(raw_audio).hexdigest()

        def create_slice(command, **_kwargs):
            Path(command[-1]).write_bytes(("slice:" + command[4]).encode("utf-8"))

        transcript = [
            {"start": 0.0, "end": 24.0, "text": "这是录音驱动的第一段完整口播。"},
            {"start": 24.0, "end": 42.0, "text": "这是录音驱动的第二段完整口播。"},
        ]
        with mock.patch.object(self.domain, "_probe_audio_duration", return_value=42.0), \
                mock.patch.object(self.domain, "_transcribe_audio", return_value=transcript), \
                mock.patch.object(self.legacy, "_run", side_effect=create_slice):
            uploaded = self.domain.audio_upload_response(
                io.BytesIO(raw_audio), len(raw_audio), "yuelei", "dh-v2-run-audio-001",
                "audio/mpeg", claimed,
            )
        plan = self.domain.plan_response({
            "narration_mode": "audio", "audio_upload_id": uploaded["audio_upload_id"],
            "gesture_count": 1,
        }, "yuelei")["plan"]
        self.assertEqual(plan["narration_mode"], "audio")
        self.assertEqual(plan["segment_count"], 2)
        self.assertEqual(plan["presenter_windows"], [[0.0, 3.0], [24.0, 27.0], [39.0, 42.0]])

        consent = self.domain.create_consent({
            "confirmed": True, "consent_version": self.domain.CONSENT_VERSION,
            "purpose": self.domain.CONSENT_PURPOSE, "run_id": "dh-v2-run-audio-001",
            "plan_digest": plan["plan_digest"], "script": "", "gesture_count": 1,
            "photo_sha256": hashlib.sha256(PNG_2X2).hexdigest(),
            "voice_mode": "audio", "voice_ref": "", "voice_sha256": "",
            "narration_mode": "audio", "audio_upload_id": uploaded["audio_upload_id"],
        }, "yuelei", "test-signing-secret", db_factory=self._consent_connection)
        image_rel = "images/audio-gesture.png"
        image_path = self.root / image_rel
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image_path.write_bytes(PNG_2X2)
        gesture_payload = {
            "digital_human_pipeline": self.domain.CONSENT_PURPOSE,
            "digital_human_stage": "gesture", "digital_human_consent_id": consent["consent_id"],
            "digital_human_run_id": consent["run_id"],
            "digital_human_plan_digest": plan["plan_digest"],
            "digital_human_item_index": 0,
        }
        with closing(self._jobs_connection()) as connection:
            connection.execute(
                "INSERT INTO jobs(id,username,kind,status,payload,result) VALUES(?,?,?,?,?,?)",
                (31, "yuelei", "image", "done", json.dumps(gesture_payload),
                 json.dumps({"file": image_rel})),
            )
            connection.commit()
        talking = {
            "digital_human_pipeline": self.domain.CONSENT_PURPOSE,
            "digital_human_stage": "talking", "digital_human_run_id": consent["run_id"],
            "digital_human_plan_digest": plan["plan_digest"],
            "digital_human_consent_token": consent["consent_token"],
            "digital_human_script": plan["copy"], "digital_human_gesture_count": 1,
            "digital_human_item_index": 0, "digital_human_narration_mode": "audio",
            "digital_human_audio_upload_id": uploaded["audio_upload_id"],
            "gesture_job_id": 31, "mode": "audio", "audio_data": "forged",
        }
        expected_slice = self.domain._load_audio_asset(
            uploaded["audio_upload_id"], "yuelei",
        )["slices"][0]
        cleaned, _record = self.domain.verify_child_submission_with_record(
            talking, "yuelei", "video",
        )
        self.assertEqual(cleaned["mode"], "audio")
        self.assertEqual(cleaned["text"], plan["segments"][0]["text"])
        self.assertNotEqual(cleaned["audio_data"], "forged")
        decoded = base64.b64decode(cleaned["audio_data"].split(",", 1)[1])
        self.assertEqual(hashlib.sha256(decoded).hexdigest(), expected_slice["sha256"])

    def test_short_audio_boundaries_keep_material_contract(self):
        for duration, expected_count in ((6.0, 0), (6.05, 0), (6.06, 1)):
            asset = {
                "asset_id": "dhau_short_boundary",
                "source_sha256": "b" * 64,
                "duration": duration,
                "transcript": "最短合法录音",
                "slices": [{
                    "start": 0.0, "end": duration, "duration": duration,
                    "text": "最短合法录音", "sha256": "a" * 64,
                }],
            }
            with self.subTest(duration=duration):
                plan = self.domain._audio_plan(asset, 1)
                self.assertEqual(plan["expected_duration"], duration)
                self.assertEqual(plan["material_count"], expected_count)

    def test_material_resolver_uses_feishu_then_public_web_then_ai(self):
        plan, consent = self._consent("公开素材应当先查飞书，再查公开网络，最后才使用人工智能补图。" * 3)
        payload = self._metadata(plan, consent, "material_resolve", 0)
        calls = []

        def feishu(_query, _preferred):
            calls.append("feishu")
            return None

        def public_web(_query, _preferred):
            calls.append("public_web")
            return PNG_2X2, "image/png", "public_web"

        with mock.patch.object(self.domain, "_feishu_material", side_effect=feishu), \
                mock.patch.object(self.domain, "_wikimedia_material", side_effect=public_web), \
                mock.patch.object(self.domain, "_store_material_asset", return_value={
                    "asset_id": "dhm_" + "1" * 32, "media_type": "image",
                }):
            result = self.domain.resolve_material_response(payload, "yuelei")
        self.assertEqual(calls, ["feishu", "public_web"])
        self.assertEqual(result["source"], "public_web")
        self.assertFalse(result.get("ai_fallback", False))

        calls.clear()
        with mock.patch.object(self.domain, "_feishu_material", side_effect=feishu), \
                mock.patch.object(self.domain, "_wikimedia_material", side_effect=lambda *_: calls.append("public_web")):
            result = self.domain.resolve_material_response(payload, "yuelei")
        self.assertEqual(calls, ["feishu", "public_web"])
        self.assertTrue(result["ai_fallback"])
        self.assertEqual(result["source"], "ai")

        calls.clear()
        with mock.patch.object(self.domain, "_feishu_material", side_effect=feishu), \
                mock.patch.object(self.domain, "_wikimedia_material", side_effect=public_web), \
                mock.patch.object(self.domain, "_store_material_asset", side_effect=ValueError("decode failed")):
            result = self.domain.resolve_material_response(payload, "yuelei")
        self.assertEqual(calls, ["feishu", "public_web"])
        self.assertTrue(result["ai_fallback"])
        self.assertTrue(result["retryable_sources"])

    def test_local_v2_compose_is_zero_cost(self):
        body = {"pipeline": self.domain.PIPELINE, "material_count": 7}
        self.assertEqual(self.points.cost_of("script_to_video", body), 0)
        self.assertEqual(body["cost_breakdown"]["material_reused_count"], 7)


if __name__ == "__main__":
    unittest.main()
