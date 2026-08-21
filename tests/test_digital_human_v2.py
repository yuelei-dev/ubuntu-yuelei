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

    def _consent(self, script, portrait=PNG_2X2):
        plan = self.domain.timeline.plan_text(script)
        payload = {
            "confirmed": True,
            "consent_version": self.domain.CONSENT_VERSION,
            "purpose": self.domain.CONSENT_PURPOSE,
            "run_id": "dh-v2-run-test-001",
            "plan_digest": plan["plan_digest"],
            "script": script,
            "photo_sha256": hashlib.sha256(portrait).hexdigest(),
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
                "photo_sha256": hashlib.sha256(PNG_2X2).hexdigest(),
                "voice_mode": "existing", "voice_ref": "voice-owned-1",
                "voice_sha256": "", "narration_mode": "text",
            }, "yuelei", "test-signing-secret", db_factory=self._consent_connection)
        self.assertEqual(caught.exception.code, "consent_plan_mismatch")

    def test_v2_voice_clone_routes_through_legacy_entrypoint_and_keeps_bindings(self):
        sample = b"authorized-v2-voice-sample"
        script = "这是用于验证新版数字人声音复刻授权绑定的完整口播文案。"
        plan = self.domain.timeline.plan_text(script)
        consent = self.domain.create_consent({
            "confirmed": True,
            "consent_version": self.domain.CONSENT_VERSION,
            "purpose": self.domain.CONSENT_PURPOSE,
            "run_id": "dh-v2-run-clone-001",
            "plan_digest": plan["plan_digest"],
            "script": script,
            "photo_sha256": hashlib.sha256(PNG_2X2).hexdigest(),
            "voice_mode": "clone",
            "voice_ref": "slot-v2-owned-1",
            "voice_sha256": hashlib.sha256(sample).hexdigest(),
            "narration_mode": "text",
        }, "yuelei", "test-signing-secret", db_factory=self._consent_connection)
        body = {
            "digital_human_pipeline": self.domain.CONSENT_PURPOSE,
            "digital_human_stage": "voice_clone",
            "digital_human_run_id": consent["run_id"],
            "digital_human_plan_digest": plan["plan_digest"],
            "digital_human_consent_token": consent["consent_token"],
            "digital_human_script": plan["copy"],
            "digital_human_narration_mode": "text",
            "slot_id": "slot-v2-owned-1",
            "audio": base64.b64encode(sample).decode("ascii"),
        }

        cleaned = self.legacy.verify_clone_submission(body, "yuelei")

        self.assertEqual(cleaned["digital_human_consent_id"], consent["consent_id"])
        self.assertNotIn("digital_human_consent_token", cleaned)
        for changed, expected_code in (
            ({"slot_id": "slot-other"}, "consent_voice_mismatch"),
            ({"audio": base64.b64encode(b"other").decode("ascii")}, "consent_voice_mismatch"),
            ({"digital_human_script": script + "篡改"}, "consent_plan_mismatch"),
        ):
            with self.subTest(changed=changed), self.assertRaises(
                    self.domain.DigitalHumanRequestError) as caught:
                self.legacy.verify_clone_submission(dict(body, **changed), "yuelei")
            self.assertEqual(caught.exception.code, expected_code)


    def test_clone_vip_requires_matching_v2_idempotency_before_provider_work(self):
        core = importlib.import_module("content_domains.core")
        base = {
            "digital_human_pipeline": self.domain.CONSENT_PURPOSE,
            "slot_id": "slot_123", "audio": "dm9pY2U=", "audio_format": "mp3",
        }

        class Handler:
            path = "/api/gen/audio/clone-vip"
            def __init__(self, body, headers):
                self.body, self.headers, self.sent = body, headers, None
            def _token(self): return "token"
            def _json_body_strict(self): return dict(self.body)
            def _send(self, status, payload): self.sent = (status, payload); return self.sent

        audio = types.SimpleNamespace(
            CloneVipValidationError=type("CloneVipValidationError", (ValueError,), {}),
            CloneAttemptError=type("CloneAttemptError", (ValueError,), {}),
            validate_clone_vip_payload=mock.Mock(), mark_clone_training=mock.Mock(),
            clone_vip_voice_background=mock.Mock(),
        )
        verifier = mock.Mock()
        cases = (
            (dict(base), {}, "必须提供 Idempotency-Key"),
            (dict(base, clone_attempt_id="dh-v2-other"),
             {"Idempotency-Key": "dh-v2-header"}, "必须与 Idempotency-Key 一致"),
        )
        for body, headers, detail in cases:
            with self.subTest(detail=detail), \
                 mock.patch("content_domains.core._domains", return_value=(audio, types.SimpleNamespace(), types.SimpleNamespace())), \
                 mock.patch("content_domains.core._dispatch_short_drama", return_value=False), \
                 mock.patch("content_domains.core.verify", return_value={"username": "yuelei"}), \
                 mock.patch("content_domains.core._must_change_password", return_value=False), \
                 mock.patch("content_domains.core.feature_flags.require_enabled"), \
                 mock.patch.object(self.legacy, "verify_clone_submission", verifier), \
                 mock.patch("content_domains.core.threading.Thread") as thread:
                handler = Handler(body, headers); core.H.do_POST(handler)
                self.assertEqual(400, handler.sent[0])
                self.assertIn(detail, handler.sent[1]["detail"])
                thread.assert_not_called()
        verifier.assert_not_called()
        audio.validate_clone_vip_payload.assert_not_called()
        audio.mark_clone_training.assert_not_called()

    def test_clone_vip_replays_v2_idempotency_without_restarting_provider(self):
        core = importlib.import_module("content_domains.core")
        db_path = self.root / "clone-v2-idempotency.db"
        def connection():
            db = sqlite3.connect(db_path)
            db.row_factory = sqlite3.Row
            return db
        key = "dh-v2-voice-clone-stable-001"
        request = {
            "digital_human_pipeline": self.domain.CONSENT_PURPOSE,
            "slot_id": "slot_123", "audio": "dm9pY2U=", "audio_format": "mp3",
            "clone_attempt_id": key,
        }

        class Handler:
            path = "/api/gen/audio/clone-vip"
            headers = {"Idempotency-Key": key}
            def __init__(self): self.sent = None
            def _token(self): return "token"
            def _json_body_strict(self): return dict(request)
            def _send(self, status, payload): self.sent = (status, payload); return self.sent

        validated = dict(request, digital_human_consent_id="dhc_" + "4" * 32)
        audio = types.SimpleNamespace(
            CloneVipValidationError=type("CloneVipValidationError", (ValueError,), {}),
            CloneAttemptError=type("CloneAttemptError", (ValueError,), {}),
            validate_clone_vip_payload=mock.Mock(return_value=validated),
            mark_clone_training=mock.Mock(return_value={"status": "training", "voice_key": "vip_slot_123"}),
            mark_clone_attempt_running=mock.Mock(return_value=True),
            clone_vip_voice_background=mock.Mock(),
        )
        started = []
        class Thread:
            def __init__(self, target, args, daemon): self.target, self.args = target, args
            def start(self): started.append(self.args)

        with mock.patch("content_domains.core._domains", return_value=(audio, types.SimpleNamespace(), types.SimpleNamespace())), \
             mock.patch("content_domains.core._dispatch_short_drama", return_value=False), \
             mock.patch("content_domains.core.verify", return_value={"username": "yuelei"}), \
             mock.patch("content_domains.core._must_change_password", return_value=False), \
             mock.patch("content_domains.core.feature_flags.require_enabled"), \
             mock.patch.object(self.legacy, "verify_clone_submission", return_value=validated), \
             mock.patch.object(core, "jdb", connection), \
             mock.patch("content_domains.core.threading.Thread", Thread):
            first = Handler(); core.H.do_POST(first)
            replay = Handler(); core.H.do_POST(replay)

        self.assertEqual(200, first.sent[0])
        self.assertEqual(first.sent, replay.sent)
        self.assertEqual(1, audio.mark_clone_training.call_count)
        self.assertEqual(1, len(started))

    def test_clone_vip_v2_provider_training_recovery_never_restarts_provider(self):
        core = importlib.import_module("content_domains.core")
        db_path = self.root / "clone-v2-provider-training.db"
        def connection():
            db = sqlite3.connect(db_path)
            db.row_factory = sqlite3.Row
            return db
        key = "dh-v2-voice-clone-provider-training-001"
        request = {
            "digital_human_pipeline": self.domain.CONSENT_PURPOSE,
            "slot_id": "slot_123", "audio": "dm9pY2U=", "audio_format": "mp3",
            "clone_attempt_id": key,
        }
        verified = dict(request, digital_human_consent_id="dhc_" + "5" * 32)
        core.submission_idempotency.begin(
            connection, "yuelei", "/api/gen/audio/clone-vip", key, verified,
        )

        class Handler:
            path = "/api/gen/audio/clone-vip"
            headers = {"Idempotency-Key": key}
            def __init__(self): self.sent = None
            def _token(self): return "token"
            def _json_body_strict(self): return dict(request)
            def _send(self, status, payload): self.sent = (status, payload); return self.sent

        audio = types.SimpleNamespace(
            CloneVipValidationError=type("CloneVipValidationError", (ValueError,), {}),
            CloneAttemptError=type("CloneAttemptError", (ValueError,), {}),
            clone_attempt_snapshot=mock.Mock(return_value={
                "action": "provider_training", "attempt_id": key, "age": 3600,
            }),
            check_clone_status=mock.Mock(return_value={
                "status": "training", "attempt_id": key,
            }),
            validate_clone_vip_payload=mock.Mock(), mark_clone_training=mock.Mock(),
            mark_clone_attempt_running=mock.Mock(), clone_vip_voice_background=mock.Mock(),
        )
        with mock.patch("content_domains.core._domains", return_value=(audio, types.SimpleNamespace(), types.SimpleNamespace())), \
             mock.patch("content_domains.core._dispatch_short_drama", return_value=False), \
             mock.patch("content_domains.core.verify", return_value={"username": "yuelei"}), \
             mock.patch("content_domains.core._must_change_password", return_value=False), \
             mock.patch("content_domains.core.feature_flags.require_enabled"), \
             mock.patch.object(self.legacy, "verify_clone_submission", return_value=verified), \
             mock.patch.object(core, "jdb", connection), \
             mock.patch("content_domains.core.threading.Thread") as thread:
            handler = Handler(); core.H.do_POST(handler)

        self.assertEqual((409, "idempotency_in_progress"),
                         (handler.sent[0], handler.sent[1]["code"]))
        audio.check_clone_status.assert_called_once_with("yuelei", "slot_123", key)
        audio.mark_clone_training.assert_not_called()
        thread.assert_not_called()
    def test_removed_gesture_stage_is_rejected_before_paid_work(self):
        plan, consent = self._consent("这是一段用于验证旧手势步骤已经删除的完整口播文案。")
        payload = self._metadata(plan, consent, "gesture", 0)
        payload["reference_images"] = [base64.b64encode(PNG_2X2).decode("ascii")]
        with self.assertRaises(self.domain.DigitalHumanRequestError):
            self.domain.verify_child_submission_with_record(payload, "yuelei", "image")

    def test_material_submission_forces_seedream_standard_route(self):
        script = "普通人学习人工智能时，应先明确问题，再选择与内容匹配的工具。" * 8
        plan, consent = self._consent(script)
        self.assertGreater(plan["material_count"], 0)
        reference = base64.b64encode(PNG_2X2).decode("ascii")
        material = self._metadata(plan, consent, "material", 0)
        material.update({
            "provider": "banana", "model": "nb2", "variant": "pro",
            "quality": "hd", "count": 2, "ratio": "1:1",
            "prompt": "forged prompt", "images": ["forged"],
            "reference_images": [reference],
        })

        cleaned, _record = self.domain.verify_child_submission_with_record(
            material, "yuelei", "image",
        )

        self.assertEqual("seedream", cleaned["provider"])
        self.assertEqual("std", cleaned["variant"])
        self.assertEqual("std", cleaned["quality"])
        self.assertEqual(1, cleaned["count"])
        self.assertEqual("9:16", cleaned["ratio"])
        self.assertEqual([reference], cleaned["reference_images"])
        self.assertNotIn("images", cleaned)
        self.assertNotIn("model", cleaned)
        self.assertEqual(
            self.points.pricing.get_price("image.seedream.std.std"),
            self.points.cost_of("image", cleaned),
        )

    def test_talking_submission_uses_authorized_portrait_and_preserves_one_voice(self):
        script = "普通人学习人工智能，不用先背很多术语，从一个真实问题开始就可以。" * 8
        plan, consent = self._consent(script)
        segment = plan["segments"][1]
        talking = self._metadata(plan, consent, "talking", 1)
        talking.update({
            "voice": "voice-owned-1", "text": "forged",
            "reference_images": [
                "data:image/png;base64," + base64.b64encode(PNG_2X2).decode("ascii")
            ],
        })
        cleaned, _record = self.domain.verify_child_submission_with_record(
            talking, "yuelei", "video",
        )
        self.assertEqual(cleaned["text"], segment["text"])
        self.assertEqual(cleaned["voice"], "voice-owned-1")
        self.assertTrue(cleaned["image_data"].startswith("data:image/jpeg;base64,"))
        self.assertNotIn("reference_images", cleaned)

        swapped = dict(talking, reference_images=[base64.b64encode(b"other").decode("ascii")])
        with self.assertRaises(self.domain.DigitalHumanRequestError) as caught:
            self.domain.verify_child_submission_with_record(swapped, "yuelei", "video")
        self.assertEqual(caught.exception.code, "consent_photo_mismatch")

    def test_webp_portrait_is_canonicalized_before_heygen_submission(self):
        from PIL import Image
        buffer = io.BytesIO()
        Image.new("RGB", (32, 48), (25, 60, 90)).save(buffer, format="WEBP")
        portrait = buffer.getvalue()
        plan, consent = self._consent(
            "这是一段用于验证 WebP 人物照片可以直接驱动数字人口播的完整文案。",
            portrait=portrait,
        )
        talking = self._metadata(plan, consent, "talking", 0)
        talking.update({
            "voice": "voice-owned-1",
            "reference_images": [
                "data:image/webp;base64," + base64.b64encode(portrait).decode("ascii")
            ],
        })

        cleaned, _record = self.domain.verify_child_submission_with_record(
            talking, "yuelei", "video",
        )

        self.assertTrue(cleaned["image_data"].startswith("data:image/jpeg;base64,"))
        canonical = base64.b64decode(cleaned["image_data"].split(",", 1)[1])
        self.assertTrue(canonical.startswith(b"\xff\xd8\xff"))

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
        }, "yuelei")["plan"]
        self.assertEqual(plan["narration_mode"], "audio")
        self.assertEqual(plan["segment_count"], 2)
        self.assertEqual(plan["presenter_windows"], [[0.0, 3.0], [24.0, 27.0], [39.0, 42.0]])

        consent = self.domain.create_consent({
            "confirmed": True, "consent_version": self.domain.CONSENT_VERSION,
            "purpose": self.domain.CONSENT_PURPOSE, "run_id": "dh-v2-run-audio-001",
            "plan_digest": plan["plan_digest"], "script": "",
            "photo_sha256": hashlib.sha256(PNG_2X2).hexdigest(),
            "voice_mode": "audio", "voice_ref": "", "voice_sha256": "",
            "narration_mode": "audio", "audio_upload_id": uploaded["audio_upload_id"],
        }, "yuelei", "test-signing-secret", db_factory=self._consent_connection)
        talking = {
            "digital_human_pipeline": self.domain.CONSENT_PURPOSE,
            "digital_human_stage": "talking", "digital_human_run_id": consent["run_id"],
            "digital_human_plan_digest": plan["plan_digest"],
            "digital_human_consent_token": consent["consent_token"],
            "digital_human_script": plan["copy"],
            "digital_human_item_index": 0, "digital_human_narration_mode": "audio",
            "digital_human_audio_upload_id": uploaded["audio_upload_id"],
            "reference_images": [base64.b64encode(PNG_2X2).decode("ascii")],
            "mode": "audio", "audio_data": "forged",
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
                plan = self.domain._audio_plan(asset)
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
