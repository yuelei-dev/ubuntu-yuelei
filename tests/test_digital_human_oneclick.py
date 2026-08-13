import importlib
import base64
import hashlib
import json
import shutil
import sqlite3
import subprocess
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
        self.consent_db = self.root / "digital-human-consent.db"
        connection = sqlite3.connect(self.db)
        connection.execute("""CREATE TABLE jobs(
            id INTEGER PRIMARY KEY, username TEXT, kind TEXT,
            status TEXT, payload TEXT, result TEXT
        )""")
        for job_id in range(1, 4):
            rel = "videos/%d.mp4" % job_id
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"video")
            connection.execute(
                "INSERT INTO jobs VALUES(?,?,?,?,?,?)",
                (job_id, "yuelei", "video", "done", "{}", json.dumps({"video_file": rel})),
            )
        for job_id in range(11, 17):
            rel = "images/%d.png" % job_id
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"image")
            connection.execute(
                "INSERT INTO jobs VALUES(?,?,?,?,?,?)",
                (job_id, "yuelei", "image", "done", "{}", json.dumps({"file": rel})),
            )
        connection.commit()
        connection.close()
        self.patches = [
            mock.patch.object(self.domain, "OUT_DIR", self.root),
            mock.patch.object(self.domain, "jdb", self._connection),
            mock.patch.object(self.domain, "CONSENT_DB", self.consent_db),
        ]
        for patcher in self.patches:
            patcher.start()

    def _connection(self):
        connection = sqlite3.connect(self.db)
        connection.row_factory = sqlite3.Row
        return connection

    def _consent_connection(self):
        connection = sqlite3.connect(self.consent_db)
        connection.row_factory = sqlite3.Row
        return connection

    def _consent_record(self, plan_digest, **overrides):
        record = {
            "id": "dhc_" + "1" * 32,
            "username": "yuelei",
            "run_id": "dh-run-test-001",
            "purpose": self.domain.CONSENT_PURPOSE,
            "plan_digest": plan_digest,
        }
        record.update(overrides)
        return record

    def _bind_child_jobs(self, record):
        connection = sqlite3.connect(self.db)
        try:
            for job_id in range(1, 4):
                payload = {
                    "digital_human_pipeline": self.domain.CONSENT_PURPOSE,
                    "digital_human_stage": "talking",
                    "digital_human_consent_id": record["id"],
                    "digital_human_run_id": record["run_id"],
                    "digital_human_plan_digest": record["plan_digest"],
                }
                connection.execute(
                    "UPDATE jobs SET payload=? WHERE id=?",
                    (json.dumps(payload), job_id),
                )
            for job_id in range(11, 17):
                payload = {
                    "digital_human_pipeline": self.domain.CONSENT_PURPOSE,
                    "digital_human_stage": "material",
                    "digital_human_consent_id": record["id"],
                    "digital_human_run_id": record["run_id"],
                    "digital_human_plan_digest": record["plan_digest"],
                }
                connection.execute(
                    "UPDATE jobs SET payload=? WHERE id=?",
                    (json.dumps(payload), job_id),
                )
            connection.commit()
        finally:
            connection.close()

    def _compose_request(self, script, planned, record):
        return {
            "pipeline": self.domain.PIPELINE,
            "script": script,
            "plan_digest": planned["plan_digest"],
            "video_job_ids": [1, 2, 3],
            "material_job_ids": [11, 12, 13, 14, 15, 16],
            "digital_human_pipeline": self.domain.CONSENT_PURPOSE,
            "digital_human_stage": "compose",
            "digital_human_run_id": record["run_id"],
            "digital_human_plan_digest": record["plan_digest"],
            "digital_human_consent_id": record["id"],
        }

    def _consent_payload(self, **overrides):
        payload = {
            "confirmed": True,
            "consent_version": self.domain.CONSENT_VERSION,
            "purpose": self.domain.CONSENT_PURPOSE,
            "run_id": "dh-run-test-001",
            "plan_digest": "a" * 64,
            "photo_sha256": hashlib.sha256(b"portrait").hexdigest(),
            "voice_mode": "existing",
            "voice_ref": "vip_ready_voice",
            "voice_sha256": "",
        }
        payload.update(overrides)
        return payload

    def test_consent_is_persisted_without_raw_media_and_is_idempotent(self):
        first = self.domain.create_consent(
            self._consent_payload(), "yuelei", "test-signing-secret", now=1000,
            db_factory=self._consent_connection,
        )
        second = self.domain.create_consent(
            self._consent_payload(), "yuelei", "test-signing-secret", now=1010,
            db_factory=self._consent_connection,
        )
        self.assertEqual(first["consent_token"], second["consent_token"])
        connection = self._consent_connection()
        try:
            row = dict(connection.execute("SELECT * FROM digital_human_consents").fetchone())
        finally:
            connection.close()
        self.assertEqual(row["username"], "yuelei")
        self.assertEqual(row["photo_sha256"], hashlib.sha256(b"portrait").hexdigest())
        self.assertNotIn("portrait", json.dumps(row))
        self.assertNotIn(first["consent_token"], json.dumps(row))

    def test_consent_rejects_unconfirmed_and_changed_binding(self):
        with self.assertRaisesRegex(self.domain.DigitalHumanRequestError, "确认") as missing:
            self.domain.create_consent(
                self._consent_payload(confirmed=False), "yuelei", "secret",
                db_factory=self._consent_connection,
            )
        self.assertEqual(missing.exception.code, "consent_required")
        self.domain.create_consent(
            self._consent_payload(), "yuelei", "secret",
            db_factory=self._consent_connection,
        )
        with self.assertRaisesRegex(self.domain.DigitalHumanRequestError, "变化") as changed:
            self.domain.create_consent(
                self._consent_payload(photo_sha256="b" * 64), "yuelei", "secret",
                db_factory=self._consent_connection,
            )
        self.assertEqual(changed.exception.code, "consent_binding_conflict")

    def test_consent_service_requires_a_signing_secret_without_creating_db(self):
        with self.assertRaises(self.domain.DigitalHumanRequestError) as unavailable:
            self.domain.create_consent(
                self._consent_payload(run_id="dh-run-no-secret-001"),
                "yuelei", "",
            )
        self.assertEqual("consent_service_unavailable", unavailable.exception.code)
        self.assertFalse(self.consent_db.exists())

    def test_gesture_and_talking_submissions_require_matching_consent(self):
        consent = self.domain.create_consent(
            self._consent_payload(), "yuelei", "secret", now=int(self.domain.time.time()),
            db_factory=self._consent_connection,
        )
        common = {
            "digital_human_pipeline": self.domain.CONSENT_PURPOSE,
            "digital_human_run_id": consent["run_id"],
            "digital_human_plan_digest": "a" * 64,
            "digital_human_consent_token": consent["consent_token"],
        }
        gesture = dict(common, digital_human_stage="gesture", prompt="safe",
                       reference_images=[base64.b64encode(b"portrait").decode("ascii")])
        with mock.patch.object(self.domain, "cdb", self._consent_connection):
            checked = self.domain.verify_child_submission(gesture, "yuelei", "image")
            self.assertEqual(checked["digital_human_consent_id"], consent["consent_id"])
            self.assertNotIn("digital_human_consent_token", checked)
            with self.assertRaisesRegex(self.domain.DigitalHumanRequestError, "照片"):
                self.domain.verify_child_submission(
                    dict(gesture, reference_images=[base64.b64encode(b"other").decode("ascii")]),
                    "yuelei", "image",
                )
            talking = dict(common, digital_human_stage="talking", voice="vip_ready_voice")
            self.assertIn(
                "digital_human_consent_id",
                self.domain.verify_child_submission(talking, "yuelei", "video"),
            )
            with self.assertRaisesRegex(self.domain.DigitalHumanRequestError, "声音"):
                self.domain.verify_child_submission(
                    dict(talking, voice="vip_wrong"), "yuelei", "video",
                )

    def test_clone_consent_binds_audio_hash_and_slot(self):
        sample = b"voice-sample"
        consent = self.domain.create_consent(
            self._consent_payload(
                run_id="dh-run-clone-001", voice_mode="clone", voice_ref="slot_123",
                voice_sha256=hashlib.sha256(sample).hexdigest(),
            ), "yuelei", "secret", now=int(self.domain.time.time()),
            db_factory=self._consent_connection,
        )
        body = {
            "digital_human_pipeline": self.domain.CONSENT_PURPOSE,
            "digital_human_stage": "voice_clone",
            "digital_human_run_id": consent["run_id"],
            "digital_human_plan_digest": "a" * 64,
            "digital_human_consent_token": consent["consent_token"],
            "slot_id": "slot_123", "audio": base64.b64encode(sample).decode("ascii"),
        }
        with mock.patch.object(self.domain, "cdb", self._consent_connection):
            checked = self.domain.verify_clone_submission(body, "yuelei")
            self.assertEqual(checked["digital_human_consent_id"], consent["consent_id"])
            with self.assertRaisesRegex(self.domain.DigitalHumanRequestError, "声音样本"):
                self.domain.verify_clone_submission(
                    dict(body, audio=base64.b64encode(b"other").decode("ascii")), "yuelei",
                )
            with self.assertRaisesRegex(self.domain.DigitalHumanRequestError, "字段不完整"):
                self.domain.verify_clone_submission({
                    "digital_human_consent_token": consent["consent_token"],
                    "slot_id": "slot_123", "audio": body["audio"],
                }, "yuelei")

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
        self.assertTrue(all("眼神稳定直视镜头" in item["gesture_prompt"] for item in first["segments"]))
        self.assertTrue(all("嘴唇自然闭合" in item["gesture_prompt"] for item in first["segments"]))
        self.assertEqual([item["role"] for item in first["segments"]], ["hook", "explain", "cta"])
        self.assertTrue(all(item["source_policy"] == "customer_then_feishu_then_ai" for item in first["materials"]))
        self.assertTrue(all(item["material_query"] for item in first["materials"]))

    def test_prepare_freezes_only_owned_completed_child_jobs(self):
        script = "第一段说明背景。第二段解释方案。第三段展示结果。第四段补充细节。第五段强调价值。第六段邀请行动。"
        planned = self.domain.plan(script)
        record = self._consent_record(planned["plan_digest"])
        self._bind_child_jobs(record)
        payload = self.domain.prepare_compose_payload(
            self._compose_request(script, planned, record),
            "yuelei", consent_record=record,
        )
        self.assertEqual(payload["video_files"], ["videos/1.mp4", "videos/2.mp4", "videos/3.mp4"])
        self.assertEqual(len(payload["material_files"]), 6)
        self.assertEqual(payload["digital_human_consent_id"], record["id"])
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
        record = self._consent_record(planned["plan_digest"])
        self._bind_child_jobs(record)
        with self.assertRaisesRegex(self.domain.DigitalHumanRequestError, "不属于当前账号"):
            self.domain.prepare_compose_payload(
                self._compose_request(script, planned, record),
                "yuelei", consent_record=record,
            )

    def test_prepare_rejects_old_consent_and_cross_run_or_plan_children(self):
        script = "第一段说明背景。第二段解释方案。第三段展示结果。第四段补充细节。第五段强调价值。第六段邀请行动。"
        planned = self.domain.plan(script)
        current = self._consent_record(planned["plan_digest"])
        request = self._compose_request(script, planned, current)
        cases = (
            ("digital_human_consent_id", "dhc_" + "2" * 32),
            ("digital_human_run_id", "dh-run-old-002"),
            ("digital_human_plan_digest", "b" * 64),
        )
        for field, value in cases:
            with self.subTest(field=field):
                self._bind_child_jobs(current)
                connection = sqlite3.connect(self.db)
                payload = json.loads(connection.execute(
                    "SELECT payload FROM jobs WHERE id=1"
                ).fetchone()[0])
                payload[field] = value
                connection.execute(
                    "UPDATE jobs SET payload=? WHERE id=1", (json.dumps(payload),),
                )
                connection.commit()
                connection.close()
                with self.assertRaises(self.domain.DigitalHumanRequestError) as rejected:
                    self.domain.prepare_compose_payload(
                        request, "yuelei", consent_record=current,
                    )
                self.assertEqual("child_consent_binding_mismatch", rejected.exception.code)

    def test_prepare_rejects_missing_or_corrupt_child_binding(self):
        script = "第一段说明背景。第二段解释方案。第三段展示结果。第四段补充细节。第五段强调价值。第六段邀请行动。"
        planned = self.domain.plan(script)
        record = self._consent_record(planned["plan_digest"])
        request = self._compose_request(script, planned, record)
        for payload_text in ("{}", "{broken-json"):
            with self.subTest(payload=payload_text):
                self._bind_child_jobs(record)
                connection = sqlite3.connect(self.db)
                connection.execute(
                    "UPDATE jobs SET payload=? WHERE id=11", (payload_text,),
                )
                connection.commit()
                connection.close()
                with self.assertRaises(self.domain.DigitalHumanRequestError) as rejected:
                    self.domain.prepare_compose_payload(
                        request, "yuelei", consent_record=record,
                    )
                self.assertIn(rejected.exception.code, {
                    "child_consent_binding_invalid",
                    "child_consent_binding_mismatch",
                })

    def test_compose_binding_rejection_precedes_charge_and_job_creation(self):
        script = "第一段说明背景。第二段解释方案。第三段展示结果。第四段补充细节。第五段强调价值。第六段邀请行动。"
        planned = self.domain.plan(script)
        consent = self.domain.create_consent(
            self._consent_payload(plan_digest=planned["plan_digest"]),
            "yuelei", "secret", now=int(self.domain.time.time()),
            db_factory=self._consent_connection,
        )
        record = self._consent_record(
            planned["plan_digest"], id=consent["consent_id"],
        )
        self._bind_child_jobs(record)
        connection = sqlite3.connect(self.db)
        payload = json.loads(connection.execute(
            "SELECT payload FROM jobs WHERE id=1"
        ).fetchone()[0])
        payload["digital_human_consent_id"] = "dhc_" + "9" * 32
        connection.execute(
            "UPDATE jobs SET payload=? WHERE id=1", (json.dumps(payload),),
        )
        before_jobs = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        connection.commit()
        connection.close()

        request = self._compose_request(script, planned, record)
        request["digital_human_consent_token"] = consent["consent_token"]

        class Handler:
            path = "/api/gen/script_to_video"
            headers = {}

            def __init__(self):
                self.sent = None

            def _token(self):
                return "token"

            def _json_body_strict(self):
                return dict(request)

            def _send(self, status, payload):
                self.sent = (status, payload)

        handler = Handler()
        cost_of = mock.Mock(return_value=0)
        points_domain = SimpleNamespace(cost_of=cost_of)
        core_module = importlib.import_module("content_domains.core")
        with mock.patch.object(self.domain, "cdb", self._consent_connection), \
             mock.patch.object(self.domain, "jdb", self._connection), \
             mock.patch.dict(core_module.HANDLERS, {"script_to_video": lambda _payload: {}}), \
             mock.patch("content_domains.core._domains", return_value=(SimpleNamespace(), points_domain, SimpleNamespace())), \
             mock.patch("content_domains.core.verify", return_value={"username": "yuelei", "points": 100}), \
             mock.patch("content_domains.core._must_change_password", return_value=False), \
             mock.patch("content_domains.core.feature_flags.require_enabled"), \
             mock.patch("content_domains.core.miniprogram_security.check_payload"), \
             mock.patch("content_domains.jobs_store.create_job_after_charge") as create_job:
            core_module.H.do_POST(handler)
        self.assertEqual(409, handler.sent[0])
        self.assertEqual("child_consent_binding_mismatch", handler.sent[1]["code"])
        cost_of.assert_not_called()
        create_job.assert_not_called()
        connection = sqlite3.connect(self.db)
        try:
            self.assertEqual(before_jobs, connection.execute(
                "SELECT COUNT(*) FROM jobs"
            ).fetchone()[0])
        finally:
            connection.close()

        core = (Path(__file__).resolve().parents[1] / "server" / "content_domains" / "core.py").read_text(encoding="utf-8")
        paid = core[core.index("if kind is not None:"):]
        self.assertLess(
            paid.index("prepare_script_to_video_payload("),
            paid.index("cost = points_domain.cost_of(kind, body)"),
        )
        self.assertLess(
            paid.index("prepare_script_to_video_payload("),
            paid.index("jobs_store.create_job_after_charge("),
        )

    def test_security_outage_precedes_charge_and_job_creation(self):
        script = "第一段说明背景。第二段解释方案。第三段展示结果。第四段补充细节。第五段强调价值。第六段邀请行动。"
        planned = self.domain.plan(script)
        consent = self.domain.create_consent(
            self._consent_payload(plan_digest=planned["plan_digest"]),
            "yuelei", "secret", now=int(self.domain.time.time()),
            db_factory=self._consent_connection,
        )
        record = self._consent_record(
            planned["plan_digest"], id=consent["consent_id"],
        )
        self._bind_child_jobs(record)
        request = self._compose_request(script, planned, record)
        request["digital_human_consent_token"] = consent["consent_token"]
        connection = sqlite3.connect(self.db)
        before_jobs = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        connection.close()

        class Handler:
            path = "/api/gen/script_to_video"
            headers = {}

            def __init__(self):
                self.sent = None

            def _token(self):
                return "token"

            def _json_body_strict(self):
                return dict(request)

            def _send(self, status, payload):
                self.sent = (status, payload)

        handler = Handler()
        cost_of = mock.Mock(return_value=0)
        points_domain = SimpleNamespace(cost_of=cost_of)
        core_module = importlib.import_module("content_domains.core")
        outage = core_module.miniprogram_security.SecurityUnavailable(
            "内容安全服务令牌暂时不可用，请稍后重试", "token",
        )
        with mock.patch.object(self.domain, "cdb", self._consent_connection), \
             mock.patch.object(self.domain, "jdb", self._connection), \
             mock.patch.dict(core_module.HANDLERS, {"script_to_video": lambda _payload: {}}), \
             mock.patch("content_domains.core._domains", return_value=(SimpleNamespace(), points_domain, SimpleNamespace())), \
             mock.patch("content_domains.core.verify", return_value={"username": "yuelei", "points": 100}), \
             mock.patch("content_domains.core._must_change_password", return_value=False), \
             mock.patch("content_domains.core.feature_flags.require_enabled"), \
             mock.patch("content_domains.core.miniprogram_security.check_payload", side_effect=outage), \
             mock.patch("content_domains.jobs_store.create_job_after_charge") as create_job:
            core_module.H.do_POST(handler)

        self.assertEqual(503, handler.sent[0])
        self.assertEqual("content_security_token_unavailable", handler.sent[1]["code"])
        cost_of.assert_not_called()
        create_job.assert_not_called()
        connection = sqlite3.connect(self.db)
        try:
            self.assertEqual(before_jobs, connection.execute(
                "SELECT COUNT(*) FROM jobs"
            ).fetchone()[0])
        finally:
            connection.close()

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

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "requires ffmpeg and ffprobe")
    def test_three_presenters_and_six_materials_render_a_verified_final_video(self):
        """Run the complete local compositor with real media, not placeholder bytes."""
        video_dir = self.root / "videos"
        image_dir = self.root / "images"
        video_dir.mkdir(exist_ok=True)
        image_dir.mkdir(exist_ok=True)
        for index, color in enumerate(("#8B5CF6", "#0EA5E9", "#F97316"), 1):
            subprocess.run([
                "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=%s:s=360x640:r=30:d=0.45" % color,
                "-f", "lavfi", "-i", "sine=frequency=%d:sample_rate=48000:duration=0.45" % (320 + index * 80),
                "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                str(video_dir / ("%d.mp4" % index)),
            ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for index, color in enumerate(("red", "orange", "yellow", "green", "blue", "purple"), 11):
            subprocess.run([
                "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=%s:s=360x640" % color,
                "-frames:v", "1", str(image_dir / ("%d.png" % index)),
            ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        out_dir = self.root / "rendered"
        out_dir.mkdir()

        class VideoDomain:
            VIDEO_OUT_DIR = out_dir

            @staticmethod
            def _resolve_out_file(rel):
                path = self.root / rel
                return path if path.is_file() else None

            @staticmethod
            def _probe_video_size(path):
                output = subprocess.check_output([
                    "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                    "stream=width,height", "-of", "csv=p=0:s=x", str(path),
                ], text=True).strip()
                return tuple(map(int, output.split("x")))

            @staticmethod
            def _probe_video_duration(rel):
                path = self.root / rel
                return float(subprocess.check_output([
                    "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                    "default=nw=1:nk=1", str(path),
                ], text=True).strip())

            @staticmethod
            def burn_subtitle(rel, **_kwargs):
                source = self.root / rel
                target = out_dir / "verified-final.mp4"
                shutil.copy2(source, target)
                return target.relative_to(self.root).as_posix()

        planned = self.domain.plan("开场说明问题。中段解释解决方案和关键价值。结尾邀请用户采取行动。")
        payload = {
            "_job_id": 230, "copy": planned["copy"], "segments": planned["segments"],
            "video_job_ids": [1, 2, 3], "material_job_ids": [11, 12, 13, 14, 15, 16],
            "video_files": ["videos/1.mp4", "videos/2.mp4", "videos/3.mp4"],
            "material_files": ["images/%d.png" % index for index in range(11, 17)],
        }
        package = importlib.import_module("content_domains")
        with mock.patch.object(package, "video", VideoDomain, create=True), mock.patch.dict(
            sys.modules, {"content_domains.video": VideoDomain},
        ):
            result = self.domain.compose(payload)
        self.assertEqual(result["child_jobs"], {"videos": [1, 2, 3], "materials": [11, 12, 13, 14, 15, 16]})
        self.assertEqual((result["width"], result["height"]), (1080, 1920))
        self.assertTrue(result["verification"]["audio_stream"])
        self.assertTrue((self.root / result["video_file"]).is_file())


class DigitalHumanOneClickUiTests(unittest.TestCase):
    def test_page_exposes_required_inputs_and_real_pipeline_calls(self):
        page = (Path(__file__).resolve().parents[1] / "site" / "workbench" / "digital-human-oneclick.html").read_text(encoding="utf-8")
        for marker in (
            'id="photo"', 'id="voice"', 'id="script"',
            'id="customerMaterials"', 'id="customerMaterialList"',
            "digital-human-material-state.js?v=1",
            "digital-human-voice-state.js?v=1",
            "digital-human-setup-state.js?v=3",
            "digital-human-submit.js?v=2",
            "/api/gen/digital-human-oneclick/plan", "/api/gen/audio/clone-vip",
            "/api/gen/script_to_video/material-upload",
            "reference_images:[photoData]", "motion:profile.motion||'high'", "speed:Number(profile.speed||1)", "pitch:Number(profile.pitch||0)", "volume:Number(profile.volume||0)", "subtitle:false",
            "body.reference_upload_ids=[customerUploads[index].upload_id]",
            "DigitalHumanMaterialState.normalize(state.customerUploads)",
            "DigitalHumanMaterialState.canChange(state.phase,customerMaterialBusy)",
            "DigitalHumanMaterialState.canAnalyze(state.phase,customerMaterialBusy)",
            "DigitalHumanMaterialState.canStart(state.phase,customerMaterialBusy,!!plan)",
            "DigitalHumanMaterialState.restoreStartButton($('start'),state.phase)",
            "DigitalHumanSetupState.runJobs({items:items",
            "epoch:epoch,currentEpoch:function(){return generationEpoch;}",
            "return generateImages(epoch)",
            "digital_human_oneclick_compose", "video_job_ids", "material_job_ids",
            "/api/gen/digital-human-oneclick/consent",
            "digital_human_consent_token",
            "photo_sha256", "voice_sha256", "consent_version",
        ):
            self.assertIn(marker, page)
        self.assertIn("合成本身 0 点", page)
        self.assertIn("Whisper 字幕", page)
        self.assertIn("分析并预览方案", page)
        self.assertIn("确认方案并生成", page)
        self.assertIn("客户素材 → 飞书授权真实素材 → AI 补缺", page)
        self.assertIn("不上传也可以继续", page)
        self.assertIn('id="voiceSource"', page)
        self.assertIn("已有声音无需再次复刻", page)
        self.assertIn("DigitalHumanVoiceState.resolveLoaded(before,data.items||[])", page)
        self.assertIn("DigitalHumanVoiceState.loadFailed(before,error.message)", page)
        self.assertIn("if(!clone&&state.voiceKey){setStep('voice','done','已复用')", page)
        self.assertIn("重新上传样音并复刻", page)
        self.assertIn('id="restartSetup"', page)
        self.assertIn("放弃上次任务并重新设置", page)
        self.assertIn("重新生成可能再次扣点", page)
        self.assertIn("声音来源和样音已随当前任务锁定", page)
        self.assertIn("DigitalHumanSetupState.applyControls(setupNodes(),phase||state.phase)", page)
        self.assertIn("DigitalHumanSetupState.restart(state,window.confirm", page)
        self.assertIn("DigitalHumanSubmit.withSecurityRetry", page)
        self.assertIn("DigitalHumanSubmit.withCapacityRetry", page)
        self.assertIn("},3).then(function(batch)", page)
        self.assertIn("DigitalHumanSubmit.describe(error)", page)
        self.assertIn("安全检查重试 '+attempt+'/2", page)
        self.assertIn("{gesture:'gestures',material:'materials',video:'talking'}", page)
        self.assertIn("error.code==='voice_clone_in_progress'", page)
        self.assertIn("尚未创建任务、未扣点", (Path(__file__).resolve().parents[1] / "site" / "workbench" / "digital-human-submit.js").read_text(encoding="utf-8"))
        self.assertIn('data-step-error="gestures"', page)
        self.assertIn("scrollIntoView", page)
        self.assertIn("提交后系统将记录本次授权时间及素材校验值", page)
        domain = (Path(__file__).resolve().parents[1] / "server" / "content_domains" / "digital_human_oneclick.py").read_text(encoding="utf-8")
        self.assertIn('cleaned["digital_human_consent_id"]', domain)
        self.assertNotIn("选择剪辑风格", page)

    def test_primary_actions_are_visible_before_long_material_fields(self):
        page = (Path(__file__).resolve().parents[1] / "site" / "workbench" / "digital-human-oneclick.html").read_text(encoding="utf-8")
        self.assertLess(page.index('id="analyze"'), page.index('id="photo"'))
        self.assertLess(page.index('id="start"'), page.index('id="script"'))
        self.assertIn(".action-dock{position:sticky", page)
        self.assertIn("资料填好后，先分析并预览三段方案", page)

    def test_video_flush_workspace_remains_vertically_scrollable(self):
        page = (Path(__file__).resolve().parents[1] / "site" / "workbench" / "digital-human-oneclick.html").read_text(encoding="utf-8")
        self.assertIn('body .hq-main-scroll-flush{overflow-x:hidden;overflow-y:auto}', page)
        self.assertIn('.hq-content[data-active="video"]{min-height:max-content}', page)

    def test_consent_enforcement_runs_before_security_validation_and_charge(self):
        root = Path(__file__).resolve().parents[1]
        core = (root / "server" / "content_domains" / "core.py").read_text(
            encoding="utf-8"
        )
        paid = core[core.index("if kind is not None:"):]
        consent_at = paid.index("verify_child_submission")
        security_at = paid.index("miniprogram_security.check_payload(body)")
        charge_at = paid.index("points_domain.cost_of(kind, body)")
        self.assertLess(consent_at, security_at)
        self.assertLess(security_at, charge_at)
        clone = core[
            core.index('if p == "/api/gen/audio/clone-vip"'):
            core.index('if p == "/api/gen/video/avatar-name"')
        ]
        self.assertLess(
            clone.index("verify_clone_submission"),
            clone.index("validate_clone_vip_payload"),
        )
        entry = (root / "server" / "content_api.py").read_text(encoding="utf-8")
        self.assertIn("digital_human_oneclick.init_db()", entry)

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
