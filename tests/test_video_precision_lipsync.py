# -*- coding: utf-8 -*-

import importlib
import json
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing, nullcontext
from types import SimpleNamespace
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
video = importlib.import_module("content_domains.video")
core = importlib.import_module("content_domains.core")
digital_human_oneclick = importlib.import_module("content_domains.digital_human_oneclick")
startup_recovery = importlib.import_module("content_domains.startup_recovery")


class VideoPrecisionLipsyncTests(unittest.TestCase):
    def test_nginx_accepts_the_declared_100mb_upload_boundary(self):
        for relative in (
            "server/nginx-huangquechuanmei.conf",
            "deploy/nginx-huangquechuanmei.conf",
        ):
            source = (ROOT / relative).read_text(encoding="utf-8")
            start = source.index("location = /api/gen/video/lipsync-import")
            end = source.index("\n    }", start)
            location = source[start:end]
            self.assertIn("client_max_body_size 100m;", location)
            self.assertIn("limit_conn hq_cli_upload_conn 2;", location)
            self.assertIn("proxy_set_header X-HQ-Internal-Token \"\";", location)

    def test_voice_sample_extracts_bounded_audio_and_removes_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "owned.mp4"
            source.write_bytes(b"video")

            def run(command, **_kwargs):
                if command[0] == "ffmpeg":
                    pathlib.Path(command[-1]).write_bytes(b"ID3" + b"v" * 600)
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                return SimpleNamespace(returncode=0, stdout="12.345\n", stderr="")

            with mock.patch.object(video, "AUDIO_OUT_DIR", root), \
                 mock.patch.object(video, "_owned_video_asset", return_value={
                     "id": 17, "mode": "lipsync_source",
                     "video_file": "video/owned.mp4",
                 }), \
                 mock.patch.object(video, "_resolve_out_file", return_value=source), \
                 mock.patch.object(video, "_user_owns_output_file", return_value=True), \
                 mock.patch.object(video.subprocess, "run", side_effect=run):
                sample = video.extract_lipsync_voice_sample("yuelei", 17)

            self.assertEqual(17, sample["video_asset_id"])
            self.assertEqual("mp3", sample["audio_format"])
            self.assertEqual(12.345, sample["duration"])
            self.assertGreater(len(video.base64.b64decode(sample["audio"])), 256)
            self.assertEqual([], list(root.glob(".lipsync-voice-sample-*.mp3")))

    def test_voice_sample_rejects_non_lipsync_or_silent_source(self):
        with mock.patch.object(
                video, "_owned_video_asset",
                side_effect=ValueError("真人源视频不存在、未完成或不属于当前账号"),
        ) as owned:
            with self.assertRaisesRegex(ValueError, "不属于当前账号"):
                video.extract_lipsync_voice_sample("other-user", 17)
            owned.assert_called_once_with("other-user", 17)
        with mock.patch.object(video, "_owned_video_asset", return_value={
                "id": 18, "mode": "text", "video_file": "video/other.mp4"}):
            with self.assertRaisesRegex(ValueError, "真人口播源视频"):
                video.extract_lipsync_voice_sample("yuelei", 18)
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "silent.mp4"
            source.write_bytes(b"video")
            with mock.patch.object(video, "AUDIO_OUT_DIR", root), \
                 mock.patch.object(video, "_owned_video_asset", return_value={
                     "id": 19, "mode": "lipsync_source",
                     "video_file": "video/silent.mp4",
                 }), \
                 mock.patch.object(video, "_resolve_out_file", return_value=source), \
                 mock.patch.object(video, "_user_owns_output_file", return_value=True), \
                 mock.patch.object(video.subprocess, "run", return_value=SimpleNamespace(
                     returncode=1, stdout="", stderr="no audio")):
                with self.assertRaisesRegex(ValueError, "没有可用人声"):
                    video.extract_lipsync_voice_sample("yuelei", 19)
            self.assertEqual([], list(root.glob(".lipsync-voice-sample-*.mp3")))

    def test_voice_sample_api_requires_auth_and_explicit_consent(self):
        slot = {"slot_id": "slot-1", "status": "active", "voice_name": None}
        audio_domain = SimpleNamespace(
            list_user_audio_voice_slots=mock.Mock(return_value=[slot]),
        )
        domain = SimpleNamespace(
            extract_lipsync_voice_sample=mock.Mock(return_value={
                "video_asset_id": 17, "video_sha256": "a" * 64,
                "audio": "dm9pY2U=", "audio_format": "mp3",
                "sha256": "b" * 64, "duration": 1.0,
            })
        )
        consent_create = mock.Mock(return_value={
            "consent_token": "dhvc_signed", "clone_attempt_id": "attempt-1",
        })

        class Handler:
            path = "/api/gen/video/lipsync-voice-sample"
            headers = {}
            def __init__(self, body):
                self.body = body
                self.sent = None
            def _token(self): return "token"
            def _json_body_strict(self): return dict(self.body)
            def _send(self, status, payload):
                self.sent = (status, payload)
                return self.sent

        common = (
            mock.patch.object(core, "_domains", return_value=(audio_domain, mock.Mock(), domain)),
            mock.patch.object(core.cli_gateway, "handle_image_upload", return_value=False),
            mock.patch.object(core.cli_gateway, "handle_quote", return_value=False),
            mock.patch.object(core, "_dispatch_short_drama", return_value=False),
            mock.patch.object(core, "_must_change_password", return_value=False),
            mock.patch.object(core.feature_flags, "require_enabled"),
            mock.patch.object(
                digital_human_oneclick, "create_unified_video_consent",
                consent_create,
            ),
        )
        for patcher in common: patcher.start()
        try:
            with mock.patch.object(core, "verify", return_value=None):
                unauthenticated = Handler({"video_asset_id": 17, "consent_confirmed": True})
                core.H.do_POST(unauthenticated)
            with mock.patch.object(core, "verify", return_value={"username": "yuelei"}):
                unconfirmed = Handler({"video_asset_id": 17, "consent_confirmed": False})
                core.H.do_POST(unconfirmed)
                incomplete = Handler({"video_asset_id": 17, "consent_confirmed": True})
                core.H.do_POST(incomplete)
                confirmed = Handler({
                    "video_asset_id": 17, "slot_id": "slot-1",
                    "script": "本次真人视频完整口播文案",
                    "run_id": "dhv-precision-test-001",
                    "consent_confirmed": True,
                    "consent_version": digital_human_oneclick.UNIFIED_VIDEO_CONSENT_VERSION,
                    "purpose": digital_human_oneclick.UNIFIED_VIDEO_CONSENT_PURPOSE,
                    "overwrite_confirmed": False, "overwrite_voice_name": "",
                })
                core.H.do_POST(confirmed)
        finally:
            for patcher in reversed(common): patcher.stop()

        self.assertEqual(401, unauthenticated.sent[0])
        self.assertEqual(403, unconfirmed.sent[0])
        self.assertEqual(403, incomplete.sent[0])
        self.assertEqual(200, confirmed.sent[0])
        domain.extract_lipsync_voice_sample.assert_called_once_with("yuelei", 17)
        consent_create.assert_called_once()

    def test_validation_resolves_owned_assets_and_forces_precision(self):
        payload = {
            "mode": "lipsync", "video_asset_id": 7, "audio_asset_id": 8,
            "text": "新的口播文案", "lipsync_mode": "precision",
            "dynamic_duration": False, "ratio": "9:16", "resolution": "1080p",
        }
        with mock.patch.object(video, "_owned_video_asset", return_value={
                "video_file": "video/owned.mp4"}), \
             mock.patch.object(video, "_owned_audio_asset", return_value={
                "file": "audio/owned.mp3"}), \
             mock.patch.object(video, "_normalize_audio_file_ref", side_effect=lambda value, username=None: value):
            cleaned = video.validate_video_payload(payload, "fang")
        self.assertEqual("video/owned.mp4", cleaned["source_video_file"])
        self.assertEqual("audio/owned.mp3", cleaned["audio_file"])
        self.assertEqual("precision", cleaned["lipsync_mode"])
        self.assertTrue(cleaned["dynamic_duration"])
        self.assertNotIn("image_data", cleaned)

    def test_validation_rejects_non_precision_mode_before_paid_submission(self):
        payload = {
            "mode": "lipsync", "video_asset_id": 7, "audio_asset_id": 8,
            "lipsync_mode": "speed",
        }
        with mock.patch.object(video, "_owned_video_asset", return_value={
                "video_file": "video/owned.mp4"}), \
             mock.patch.object(video, "_owned_audio_asset", return_value={
                "file": "audio/owned.mp3"}), \
             mock.patch.object(video, "_normalize_audio_file_ref", side_effect=lambda value, username=None: value):
            with self.assertRaisesRegex(ValueError, "仅支持 HeyGen Precision"):
                video.validate_video_payload(payload, "fang")

    def test_api_create_uses_official_v3_precision_contract(self):
        captured = {}

        def request(method, path, body=None, headers=None, timeout=0, direct=False):
            captured.update({
                "method": method, "path": path, "body": json.loads(body),
                "headers": headers, "direct": direct,
            })
            return {"data": {"lipsync_id": "lip_123"}}

        with mock.patch.object(video, "_HEYGEN_ALLOW_API_WALLET", True), \
             mock.patch.object(video, "_heygen_request_json", side_effect=request):
            lipsync_id = video._heygen_create_lipsync(
                "video_asset", "audio_asset", direct=True, route="api_wallet")
        self.assertEqual("lip_123", lipsync_id)
        self.assertEqual(("POST", "/lipsyncs"), (captured["method"], captured["path"]))
        self.assertEqual({"type": "asset_id", "asset_id": "video_asset"}, captured["body"]["video"])
        self.assertEqual({"type": "asset_id", "asset_id": "audio_asset"}, captured["body"]["audio"])
        self.assertEqual("precision", captured["body"]["mode"])
        self.assertTrue(captured["body"]["enable_dynamic_duration"])
        self.assertTrue(captured["body"]["keep_the_same_format"])
        self.assertEqual("cfr", captured["body"]["fps_mode"])
        self.assertTrue(captured["direct"])

    def test_mcp_create_maps_dynamic_duration_to_camel_case(self):
        with mock.patch.object(video, "_heygen_mcp_enabled", return_value=True), \
             mock.patch.object(video, "_heygen_mcp_call", return_value={
                "data": {"lipsyncId": "lip_mcp"}}) as call:
            self.assertEqual("lip_mcp", video._heygen_create_lipsync(
                "video_asset", "audio_asset", route="mcp_oauth"))
        arguments = call.call_args.args[1]
        self.assertEqual("precision", arguments["mode"])
        self.assertTrue(arguments["enableDynamicDuration"])
        self.assertTrue(arguments["keepTheSameFormat"])
        self.assertEqual("cfr", arguments["fpsMode"])

    def test_poll_returns_completed_precision_output(self):
        with mock.patch.object(video, "_heygen_request_json", return_value={
                "data": {"status": "completed", "video_url": "https://example.test/final.mp4"}}):
            result = video._heygen_poll_lipsync("lip_123", direct=True, deadline_s=5)
        self.assertEqual("https://example.test/final.mp4", result["video_url"])

    def test_post_submit_failure_never_creates_a_second_paid_lipsync(self):
        with tempfile.TemporaryDirectory() as directory:
            source = pathlib.Path(directory) / "source.mp4"
            audio = pathlib.Path(directory) / "audio.mp3"
            source.write_bytes(b"source")
            audio.write_bytes(b"audio")
            create = mock.Mock(return_value="lip_paid")
            with mock.patch.object(video, "_resolve_out_file", side_effect=lambda rel: {
                    "video/source.mp4": source, "audio/voice.mp3": audio,
                    }.get(str(rel))), \
                 mock.patch.object(video, "_ensure_heygen_audio_mp3", return_value=audio), \
                 mock.patch.object(video, "_heygen_require_paid_route", return_value="api_wallet"), \
                 mock.patch.object(video, "_HEYGEN_DIRECT", True), \
                 mock.patch.object(video, "HEYGEN_API_KEY", "test-key"), \
                 mock.patch.object(video, "_heygen_upload_asset", side_effect=["video_asset", "audio_asset"]), \
                 mock.patch.object(video, "_heygen_retry_net", side_effect=lambda fn, _label: fn()), \
                 mock.patch.object(video, "heygen_slot", side_effect=lambda _label: nullcontext()), \
                 mock.patch.object(video, "_heygen_retry_429", side_effect=lambda fn, _label: fn()), \
                 mock.patch.object(video, "_heygen_create_lipsync", create), \
                 mock.patch.object(video, "_heygen_poll_lipsync", side_effect=TimeoutError("poll timeout")):
                with self.assertRaisesRegex(video.HeyGenBilledError, "lip_paid"):
                    video.generate_heygen_precision_lipsync(
                        "video/source.mp4", "audio/voice.mp3")
            create.assert_called_once_with(
                "video_asset", "audio_asset", direct=True, route="api_wallet",
                dynamic_duration=True,
            )

    def test_upload_failure_stops_before_the_paid_create(self):
        with tempfile.TemporaryDirectory() as directory:
            source = pathlib.Path(directory) / "source.mp4"
            audio = pathlib.Path(directory) / "audio.mp3"
            source.write_bytes(b"source")
            audio.write_bytes(b"audio")
            create = mock.Mock()
            with mock.patch.object(video, "_resolve_out_file", side_effect=lambda rel: {
                    "video/source.mp4": source, "audio/voice.mp3": audio,
                    }.get(str(rel))), \
                 mock.patch.object(video, "_ensure_heygen_audio_mp3", return_value=audio), \
                 mock.patch.object(video, "_heygen_require_paid_route", return_value="api_wallet"), \
                 mock.patch.object(video, "_HEYGEN_DIRECT", True), \
                 mock.patch.object(video, "HEYGEN_API_KEY", "test-key"), \
                 mock.patch.object(video, "_heygen_upload_asset", side_effect=video.HeyGenNetworkError("upload failed")), \
                 mock.patch.object(video, "_heygen_retry_net", side_effect=lambda fn, _label: fn()), \
                 mock.patch.object(video, "_heygen_create_lipsync", create):
                with self.assertRaises(video.HeyGenNetworkError):
                    video.generate_heygen_precision_lipsync(
                        "video/source.mp4", "audio/voice.mp3")
            create.assert_not_called()

    def test_precision_cost_is_per_complete_audio_second(self):
        body = {"mode": "lipsync", "audio_file": "audio/owned.mp3"}
        with mock.patch.object(video, "_talking_estimate_seconds", return_value=12.2), \
             mock.patch.object(video.pricing, "get_price", return_value=6):
            self.assertEqual(78, video.video_cost(body))
            self.assertEqual(78, video.talking_actual_cost({
                "mode": "lipsync", "duration": 12.2,
            }))
        self.assertEqual(6, body["_lipsync_second_points"])


class PrecisionPaidRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.jobs = self.root / "jobs.db"
        self.assets = self.root / "assets.db"
        self.source = self.root / "source.mp4"
        self.audio = self.root / "audio.mp3"
        self.source.write_bytes(b"source")
        self.audio.write_bytes(b"audio")
        with closing(sqlite3.connect(self.jobs)) as connection:
            connection.execute(
                """CREATE TABLE jobs(
                    id INTEGER PRIMARY KEY, username TEXT, cost INTEGER,
                    kind TEXT, status TEXT, owner TEXT, payload TEXT,
                    error TEXT, updated_at INTEGER)"""
            )
            connection.execute(
                "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    41, "tester", 60, "video", "running", "content",
                    json.dumps({
                        "mode": "lipsync",
                        "source_video_file": "video/source.mp4",
                        "audio_file": "audio/voice.mp3",
                    }), None, 1,
                ),
            )
            connection.commit()
        with closing(sqlite3.connect(self.assets)) as connection:
            connection.execute(
                """CREATE TABLE video_assets(
                    id INTEGER PRIMARY KEY, job_id INTEGER UNIQUE,
                    username TEXT NOT NULL, mode TEXT, image_file TEXT,
                    audio_file TEXT, reference_video_file TEXT,
                    video_file TEXT, video_url TEXT, text TEXT, voice_key TEXT,
                    resolution TEXT, ratio TEXT, motion TEXT, phase TEXT,
                    image_asset_id TEXT, audio_asset_id TEXT,
                    reference_asset_id TEXT, provider_video_id TEXT,
                    provider_key_id TEXT, provider_avatar_id TEXT,
                    provider_avatar_group_id TEXT, source_video_url TEXT,
                    background_file TEXT, tryon_mode TEXT, model TEXT,
                    status TEXT NOT NULL, error TEXT, created_at INTEGER,
                    updated_at INTEGER)"""
            )
            connection.commit()

    def tearDown(self):
        self.tmp.cleanup()

    def job_db(self):
        connection = sqlite3.connect(self.jobs)
        connection.row_factory = sqlite3.Row
        return connection

    def asset_db(self):
        connection = sqlite3.connect(self.assets)
        connection.row_factory = sqlite3.Row
        return connection

    def payload(self):
        return {
            "_job_id": 41, "_username": "tester", "mode": "lipsync",
            "source_video_file": "video/source.mp4",
            "audio_file": "audio/voice.mp3", "resolution": "1080p",
            "ratio": "9:16",
        }

    def provider_patches(self, *, create, poll):
        return (
            mock.patch.object(video, "jdb", side_effect=self.job_db),
            mock.patch.object(video, "adb", side_effect=self.asset_db),
            mock.patch.object(video, "_resolve_out_file", side_effect=lambda value: {
                "video/source.mp4": self.source,
                "audio/voice.mp3": self.audio,
            }.get(str(value))),
            mock.patch.object(video, "_normalize_audio_file_ref",
                              side_effect=lambda value, username=None: value),
            mock.patch.object(video, "preflight_heygen_audio_file",
                              return_value={"path": self.audio}),
            mock.patch.object(video, "_ensure_heygen_audio_mp3",
                              return_value=self.audio),
            mock.patch.object(video, "_heygen_require_paid_route",
                              return_value="api_wallet"),
            mock.patch.object(video, "_HEYGEN_DIRECT", True),
            mock.patch.object(video, "HEYGEN_API_KEY", "test-key"),
            mock.patch.object(video, "_heygen_upload_asset",
                              side_effect=["video_asset", "audio_asset"]),
            mock.patch.object(video, "_heygen_retry_net",
                              side_effect=lambda function, _label: function()),
            mock.patch.object(video, "heygen_slot",
                              side_effect=lambda _label: nullcontext()),
            mock.patch.object(video, "_heygen_retry_429",
                              side_effect=lambda function, _label: function()),
            mock.patch.object(video, "_heygen_create_lipsync", create),
            mock.patch.object(video, "_heygen_poll_lipsync", poll),
            mock.patch.object(video, "_download_video_file_direct",
                              return_value="video/final.mp4"),
            mock.patch.object(video, "_extract_first_frame_cover",
                              return_value=None),
            mock.patch.object(video, "public_url", return_value="/private/final"),
        )

    def _run_with(self, patches, callback):
        with patches[0]:
            with patches[1]:
                with patches[2]:
                    with patches[3]:
                        with patches[4]:
                            with patches[5]:
                                with patches[6]:
                                    with patches[7]:
                                        with patches[8]:
                                            with patches[9]:
                                                with patches[10]:
                                                    with patches[11]:
                                                        with patches[12]:
                                                            with patches[13]:
                                                                with patches[14]:
                                                                    with patches[15]:
                                                                        with patches[16]:
                                                                            return callback()

    def test_unknown_post_outcome_is_held_and_replay_never_creates_again(self):
        create = mock.Mock(side_effect=TimeoutError("POST outcome unknown"))
        poll = mock.Mock()
        patches = self.provider_patches(create=create, poll=poll)

        def exercise():
            with self.assertRaises(TimeoutError):
                video.gen_video(self.payload())
            self.assertTrue(video.recover_precision_lipsync_paid_job(
                41, "worker interrupted"))
            with self.assertRaisesRegex(
                    video.HeyGenBilledError, "禁止自动重发"):
                video.gen_video(self.payload())
            state = video.get_precision_lipsync_recovery_state(41)
            self.assertEqual("precision_recovery_required", state["phase"])
            self.assertNotIn("provider_video_id", state)

        self._run_with(patches, exercise)
        self.assertEqual(1, create.call_count)
        poll.assert_not_called()

    def test_post_return_before_id_persistence_requires_manual_recovery(self):
        create = mock.Mock(return_value="lip_unpersisted")
        poll = mock.Mock()
        patches = self.provider_patches(create=create, poll=poll)
        persist = video._persist_precision_lipsync_state

        def crash_before_id(job_id, phase, **fields):
            if phase == "precision_submitted":
                raise KeyboardInterrupt("process stopped before id commit")
            return persist(job_id, phase, **fields)

        def exercise():
            with mock.patch.object(
                    video, "_persist_precision_lipsync_state",
                    side_effect=crash_before_id):
                with self.assertRaisesRegex(
                        video.HeyGenBilledError, "lip_unpersisted"):
                    video.gen_video(self.payload())
            self.assertTrue(video.recover_precision_lipsync_paid_job(
                41, "post returned before id commit"))
            state = video.get_precision_lipsync_recovery_state(41)
            self.assertEqual("precision_recovery_required", state["phase"])
            self.assertNotIn("provider_video_id", state)
            with self.assertRaisesRegex(
                    video.HeyGenBilledError, "禁止自动重发"):
                video.gen_video(self.payload())

        self._run_with(patches, exercise)
        self.assertEqual(1, create.call_count)
        poll.assert_not_called()

    def test_persisted_id_poll_timeout_and_idempotent_replay_create_once(self):
        create = mock.Mock(return_value="lip_paid_once")
        poll = mock.Mock(side_effect=[
            TimeoutError("poll timeout"),
            {"video_url": "https://example.test/final.mp4", "duration": 10},
        ])
        patches = self.provider_patches(create=create, poll=poll)

        def exercise():
            with self.assertRaises(video.HeyGenBilledError):
                video.gen_video(self.payload())
            state = video.get_precision_lipsync_recovery_state(41)
            self.assertEqual("lip_paid_once", state["provider_video_id"])
            self.assertTrue(video.recover_precision_lipsync_paid_job(
                41, "poll timeout", lambda job_id: startup_recovery.requeue_running_job(
                    self.job_db, job_id)))
            with closing(sqlite3.connect(self.jobs)) as connection:
                self.assertEqual(
                    "pending", connection.execute(
                        "SELECT status FROM jobs WHERE id=41"
                    ).fetchone()[0],
                )
                connection.execute(
                    "UPDATE jobs SET status='running' WHERE id=41"
                )
                connection.commit()
            result = video.gen_video(self.payload())
            self.assertEqual("lip_paid_once", result["provider_video_id"])

        self._run_with(patches, exercise)
        self.assertEqual(1, create.call_count)
        self.assertEqual(2, poll.call_count)

    def test_process_restart_requeues_persisted_id_without_refund(self):
        with closing(sqlite3.connect(self.jobs)) as connection:
            payload = json.loads(connection.execute(
                "SELECT payload FROM jobs WHERE id=41"
            ).fetchone()[0])
            payload[video.PRECISION_RECOVERY_KEY] = {
                "phase": "precision_submitted",
                "provider": "api_wallet",
                "provider_transport": "direct",
                "provider_video_id": "lip_restart",
            }
            connection.execute(
                "UPDATE jobs SET payload=? WHERE id=41",
                (json.dumps(payload),),
            )
            connection.commit()
        terminal, refund, failed = mock.Mock(), mock.Mock(), mock.Mock()
        with mock.patch.object(video, "jdb", side_effect=self.job_db):
            handled = startup_recovery.reclaim_orphaned_running(
                jdb=self.job_db, service_owner="content",
                domains=lambda: (None, None, video),
                set_terminal=terminal, refund_once=refund,
                mark_video_asset_failed=failed,
                requeue_job=lambda job_id: startup_recovery.requeue_running_job(
                    self.job_db, job_id),
                logger=lambda *_args, **_kwargs: None,
            )
        self.assertEqual(1, handled)
        terminal.assert_not_called()
        refund.assert_not_called()
        failed.assert_not_called()
        with closing(sqlite3.connect(self.jobs)) as connection:
            self.assertEqual(
                "pending", connection.execute(
                    "SELECT status FROM jobs WHERE id=41"
                ).fetchone()[0],
            )

    def test_reaper_classification_never_refunds_precision_recovery(self):
        class StopLoop(Exception):
            pass

        class Rows:
            def execute(self, *_args):
                return self

            def fetchall(self):
                return [{
                    "id": 41, "username": "tester", "cost": 60,
                    "kind": "video", "payload": json.dumps({"mode": "lipsync"}),
                    "updated_at": 1,
                }]

            def close(self):
                return None

        domain = mock.Mock()
        domain.retry_pending_seedance_cleanups.return_value = None
        domain.recover_precision_lipsync_paid_job.return_value = True
        points = mock.Mock()
        fail = mock.Mock()
        with mock.patch.object(core, "jdb", return_value=Rows()), \
             mock.patch.object(core, "_domains", return_value=(None, points, domain)), \
             mock.patch.object(core, "_fail_job_and_schedule_refund", fail), \
             mock.patch.object(core.time, "time", return_value=5000), \
             mock.patch.object(core.time, "sleep", side_effect=StopLoop):
            with self.assertRaises(StopLoop):
                core.reaper()
        domain.recover_precision_lipsync_paid_job.assert_called_once()
        fail.assert_not_called()

    def test_run_job_requeues_known_precision_id_without_refund(self):
        class JobConnection:
            def execute(self, *_args):
                return self

            def fetchone(self):
                return {
                    "id": 41, "username": "tester", "cost": 60,
                    "kind": "video", "status": "pending",
                    "payload": json.dumps({"mode": "lipsync"}),
                }

            def close(self):
                return None

        domain = mock.Mock()
        domain.HeyGenProviderFailed = video.HeyGenProviderFailed
        domain.recover_precision_lipsync_paid_job.return_value = True
        fail = mock.Mock()
        handler = mock.Mock(side_effect=video.HeyGenBilledError("poll timeout"))
        with mock.patch.object(core, "jdb", return_value=JobConnection()), \
             mock.patch.object(core.jobs_store, "claim_running", return_value=True), \
             mock.patch.object(core, "_user_running_talking_count", return_value=0), \
             mock.patch.object(core, "_start_job_heartbeat", return_value=lambda: None), \
             mock.patch.object(core, "_domains", return_value=(None, mock.Mock(), domain)), \
             mock.patch.object(core, "_fail_job_and_schedule_refund", fail), \
             mock.patch.dict(core.HANDLERS, {"video": handler}):
            core.run_job(41)
        handler.assert_called_once()
        domain.recover_precision_lipsync_paid_job.assert_called_once()
        fail.assert_not_called()


if __name__ == "__main__":
    unittest.main()
