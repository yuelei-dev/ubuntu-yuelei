import base64
import io
import json
import os
import subprocess
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PIL import Image


if os.name == "nt":
    sys.modules.setdefault("fcntl", SimpleNamespace(
        LOCK_EX=1, LOCK_NB=2, LOCK_UN=8, flock=lambda *_args: None,
    ))

SERVER = Path(__file__).resolve().parents[1] / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from content_domains import core, script_to_video, video  # noqa: E402


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class ScriptToVideoMaterialLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp.name)
        self.out = self.root / "out"
        self.out.mkdir()
        self.old = {
            "job_db": core.JOB_DB,
            "audio_db": core.AUDIO_DB,
            "core_out": core.OUT_DIR,
            "script_out": script_to_video.OUT_DIR,
            "video_out": video.OUT_DIR,
            "video_video_out": video.VIDEO_OUT_DIR,
            "avatar": script_to_video._get_first_avatar,
            "gen_video": video.gen_video,
            "mcp_credentials": video._HEYGEN_MCP_CREDENTIALS,
            "allow_api_wallet": video._HEYGEN_ALLOW_API_WALLET,
            "heygen_api_key": video.HEYGEN_API_KEY,
        }
        core.JOB_DB = str(self.root / "jobs.db")
        core.AUDIO_DB = str(self.root / "audio.db")
        core.OUT_DIR = self.out
        script_to_video.OUT_DIR = self.out
        video.OUT_DIR = self.out
        video.VIDEO_OUT_DIR = self.out / "video"
        video.VIDEO_OUT_DIR.mkdir()
        avatar = self.out / "image/avatar.png"
        avatar.parent.mkdir(parents=True)
        avatar.write_bytes(PNG)
        script_to_video._get_first_avatar = lambda _username: {
            "id": 1, "image_file": "image/avatar.png",
        }
        video._HEYGEN_MCP_CREDENTIALS = ""
        video._HEYGEN_ALLOW_API_WALLET = True
        video.HEYGEN_API_KEY = "test-api-wallet"
        self._init_databases()

    def tearDown(self):
        core.JOB_DB = self.old["job_db"]
        core.AUDIO_DB = self.old["audio_db"]
        core.OUT_DIR = self.old["core_out"]
        script_to_video.OUT_DIR = self.old["script_out"]
        video.OUT_DIR = self.old["video_out"]
        video.VIDEO_OUT_DIR = self.old["video_video_out"]
        script_to_video._get_first_avatar = self.old["avatar"]
        video.gen_video = self.old["gen_video"]
        video._HEYGEN_MCP_CREDENTIALS = self.old["mcp_credentials"]
        video._HEYGEN_ALLOW_API_WALLET = self.old["allow_api_wallet"]
        video.HEYGEN_API_KEY = self.old["heygen_api_key"]
        self.temp.cleanup()

    def _init_databases(self):
        with sqlite3.connect(core.JOB_DB) as conn:
            conn.execute("""CREATE TABLE jobs(
                id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, username TEXT,
                cost INTEGER DEFAULT 20, status TEXT DEFAULT 'pending',
                payload TEXT, result TEXT, error TEXT, created_at INTEGER DEFAULT 1,
                updated_at INTEGER DEFAULT 1, owner TEXT DEFAULT 'content',
                refunded INTEGER DEFAULT 0)""")
        with sqlite3.connect(core.AUDIO_DB) as conn:
            conn.execute("""CREATE TABLE video_assets(
                job_id INTEGER PRIMARY KEY, phase TEXT, status TEXT, error TEXT,
                updated_at INTEGER, provider_video_id TEXT, image_asset_id TEXT,
                audio_asset_id TEXT, video_file TEXT, source_video_url TEXT)""")

    def _image(self, rel, data=PNG):
        path = self.out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def _audio(self, rel, container=None):
        path = self.out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        command = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=0.25",
            "-vn", "-ac", "1", "-ar", "24000",
        ]
        if container == "mp3":
            command.extend(["-c:a", "libmp3lame", "-f", "mp3"])
        elif container == "wav":
            command.extend(["-c:a", "pcm_s16le", "-f", "wav"])
        elif container == "m4a":
            command.extend(["-c:a", "aac", "-f", "mp4"])
        command.append(str(path))
        subprocess.run(
            command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        return path

    @staticmethod
    def _encoded_image(fmt, size=(8, 8), color=(34, 85, 136)):
        output = io.BytesIO()
        Image.new("RGB", size, color).save(output, format=fmt)
        return output.getvalue()

    def test_avatar_preflight_uses_true_bytes_for_jpeg_png_webp_and_suffix_mismatch(self):
        cases = [
            ("avatar-as-png.png", "JPEG", "image/jpeg"),
            ("avatar-as-jpg.jpg", "PNG", "image/png"),
            ("avatar-as-jpeg.jpeg", "WEBP", "image/webp"),
        ]
        for name, fmt, expected_mime in cases:
            with self.subTest(name=name, fmt=fmt):
                self._image("image/" + name, self._encoded_image(fmt))
                checked = video.preflight_heygen_image_file(
                    "image/" + name, "avatar",
                )
                self.assertEqual(expected_mime, checked["mime"])
                self.assertEqual(self.out / "image" / name, checked["path"])
        canonical = self._image("image/canonical.jpg", self._encoded_image("JPEG"))
        self.assertEqual(canonical, video._ensure_heygen_image_jpg(canonical))

    def test_transient_avatar_read_error_is_not_misreported_as_bad_image_content(self):
        avatar = self._image("image/read-error.jpg", self._encoded_image("JPEG"))
        original = Path.read_bytes

        def denied(path):
            if Path(path) == avatar:
                raise OSError("temporary read failure")
            return original(path)

        with mock.patch.object(Path, "read_bytes", denied):
            with self.assertRaises(video.HeyGenMediaInputError) as rejected:
                video.preflight_heygen_image_file("image/read-error.jpg", "avatar")
        self.assertEqual("avatar_unreadable", rejected.exception.code)
        self.assertNotIn("read-error.jpg", str(rejected.exception))

    def test_missing_generated_tts_audio_is_classified_before_provider_upload(self):
        avatar = self._image("image/audio-check.jpg", self._encoded_image("JPEG"))
        with mock.patch.object(video, "HEYGEN_API_KEY", "configured"), \
             mock.patch.object(video, "get_video_avatar", return_value={
                 "id": 1, "image_file": avatar.relative_to(self.out).as_posix(),
             }), \
             mock.patch.object(video, "gen_audio", return_value={
                 "file": "audio/missing.mp3", "url": "/missing.mp3",
             }), \
             mock.patch.object(video, "_upload_heygen_image_asset") as image_upload, \
             mock.patch.object(video, "_heygen_create_video") as create:
            with self.assertRaises(video.HeyGenMediaInputError) as rejected:
                video.gen_video({
                    "_username": "alice", "_job_id": 1,
                    "mode": "text", "avatar_id": "1",
                    "text": "介绍产品", "voice": "voice-1",
                })
        self.assertEqual("tts_audio_missing", rejected.exception.code)
        self.assertEqual("tts_audio", rejected.exception.category)
        image_upload.assert_not_called()
        create.assert_not_called()

    def test_nonempty_garbage_and_truncated_mp3_stop_before_any_provider_call(self):
        self._image("image/audio-avatar.jpg", self._encoded_image("JPEG"))
        valid = self._audio("audio/complete.mp3", "mp3").read_bytes()
        cases = {
            "audio/garbage.mp3": b"not-an-mp3-but-nonempty",
            "audio/truncated.mp3": valid[:64],
        }
        for rel, contents in cases.items():
            with self.subTest(rel=rel):
                path = self.out / rel
                path.write_bytes(contents)
                with mock.patch.object(video, "_upload_heygen_image_asset") as image_upload, \
                     mock.patch.object(video, "_heygen_upload_asset") as audio_upload, \
                     mock.patch.object(video, "_heygen_create_video") as create:
                    with self.assertRaises(video.HeyGenMediaInputError) as rejected:
                        video.generate_heygen_video_recoverable(
                            "image/audio-avatar.jpg", rel, "720p", "9:16", "medium",
                            {"state": {}},
                        )
                self.assertEqual("tts_audio_content_invalid", rejected.exception.code)
                self.assertEqual("tts_audio", rejected.exception.category)
                self.assertNotIn(path.name, str(rejected.exception))
                image_upload.assert_not_called()
                audio_upload.assert_not_called()
                create.assert_not_called()

    def test_valid_mp3_wav_and_m4a_are_fully_decoded_and_canonicalized(self):
        for container in ("mp3", "wav", "m4a"):
            with self.subTest(container=container):
                source = self._audio("audio/valid.%s" % container, container)
                checked = video.preflight_heygen_audio_file(
                    source.relative_to(self.out).as_posix(),
                )
                self.assertEqual(source, checked)
                canonical = video._ensure_heygen_audio_mp3(source)
                info = video._preflight_heygen_audio_path(canonical)
                self.assertEqual("mp3", info["codec_name"])
                self.assertIn("mp3", info["format_names"])

    def test_mp3_suffix_cannot_disguise_wav_bytes(self):
        wav = self._audio("audio/source.wav", "wav")
        disguised = self.out / "audio/disguised.mp3"
        disguised.write_bytes(wav.read_bytes())
        canonical = video._ensure_heygen_audio_mp3(disguised)
        self.assertNotEqual(disguised, canonical)
        self.assertEqual("mp3", video._preflight_heygen_audio_path(canonical)["codec_name"])

    def test_transient_audio_read_failure_is_sanitized_and_stops_provider(self):
        self._image("image/read-error-avatar.jpg", self._encoded_image("JPEG"))
        audio = self._audio("audio/read-error.mp3", "mp3")
        original = Path.open

        def denied(path, *args, **kwargs):
            if Path(path) == audio:
                raise OSError("temporary read failure with private path")
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, "open", denied), \
             mock.patch.object(video, "_upload_heygen_image_asset") as image_upload, \
             mock.patch.object(video, "_heygen_upload_asset") as audio_upload, \
             mock.patch.object(video, "_heygen_create_video") as create:
            with self.assertRaises(video.HeyGenMediaInputError) as rejected:
                video.generate_heygen_video_recoverable(
                    "image/read-error-avatar.jpg", "audio/read-error.mp3",
                    "720p", "9:16", "medium", {"state": {}},
                )
        self.assertEqual("tts_audio_unreadable", rejected.exception.code)
        self.assertNotIn("read-error.mp3", str(rejected.exception))
        image_upload.assert_not_called()
        audio_upload.assert_not_called()
        create.assert_not_called()

    def test_structured_avatar_ref_never_falls_back_to_same_named_out_dir_file(self):
        self._image("avatar.jpg", self._encoded_image("JPEG"))
        with self.assertRaises(video.HeyGenMediaInputError) as missing:
            video.preflight_heygen_image_file("image/avatar.jpg", "avatar")
        self.assertEqual("avatar_missing", missing.exception.code)
        # Bare historical rows keep the old basename lookup contract.
        checked = video.preflight_heygen_image_file("avatar.jpg", "avatar")
        self.assertEqual(self.out / "avatar.jpg", checked["path"])

    def test_corrupt_avatar_is_rejected_before_material_generation_tts_or_heygen(self):
        broken = self._image("image/broken.jpg", b"\xff\xd8\xfftruncated")
        script_to_video._get_first_avatar = lambda _username: {
            "id": 1, "image_file": broken.relative_to(self.out).as_posix(),
        }
        job_id = self._job({"material_plan": self._plan(None, "generate")}, status="running")
        with mock.patch("content_domains.image.gen_image") as image_create, \
             mock.patch.object(video, "gen_audio") as tts, \
             mock.patch.object(video, "_heygen_create_video") as provider_create:
            with self.assertRaises(video.HeyGenMediaInputError) as rejected:
                script_to_video.gen_script_to_video({
                    "_username": "alice", "_job_id": job_id,
                    "scenes": [{"line": "介绍产品"}],
                    "material_plan": self._plan(None, "generate"),
                })
        self.assertEqual("avatar_content_invalid", rejected.exception.code)
        self.assertNotIn("broken.jpg", str(rejected.exception))
        image_create.assert_not_called()
        tts.assert_not_called()
        provider_create.assert_not_called()
        state = script_to_video.get_recovery_state(job_id)
        self.assertEqual({
            "stage": "media_preflight",
            "category": "avatar",
            "code": "avatar_content_invalid",
        }, state["input_error"])

    def test_prepare_rejects_invalid_avatar_before_charge_contract_or_asset_lookup(self):
        self._image("image/empty.jpg", b"")
        script_to_video._get_first_avatar = lambda _username: {
            "id": 1, "image_file": "image/empty.jpg",
        }
        with mock.patch.object(script_to_video, "_match_image_asset") as asset_lookup:
            with self.assertRaises(video.HeyGenMediaInputError) as rejected:
                script_to_video.prepare_script_to_video_payload({
                    "style": "口播",
                    "scenes": [{"line": "介绍产品", "scene": "产品特写"}],
                }, "alice")
        self.assertEqual("avatar_empty", rejected.exception.code)
        asset_lookup.assert_not_called()

    def test_generated_material_corruption_is_classified_without_avatar_false_positive(self):
        generated = self._image("image/generated.jpg", b"\xff\xd8\xfftruncated")
        plan = self._plan(None, "generate")
        job_id = self._job({"material_plan": plan}, status="running")
        with mock.patch("content_domains.image.gen_image", return_value={
                "file": generated.relative_to(self.out).as_posix()}), \
             mock.patch.object(video, "gen_audio") as tts, \
             mock.patch.object(video, "_heygen_create_video") as provider_create:
            with self.assertRaises(script_to_video.ScriptToVideoMediaInputError):
                script_to_video.gen_script_to_video({
                    "_username": "alice", "_job_id": job_id,
                    "scenes": [{"line": "介绍产品"}], "material_plan": plan,
                })
        tts.assert_not_called()
        provider_create.assert_not_called()
        state = script_to_video.get_recovery_state(job_id)
        self.assertEqual("material", state["input_error"]["category"])
        self.assertEqual("material_invalid", state["input_error"]["code"])

    def test_valid_jpeg_avatar_and_ready_material_reach_provider_submitting_once(self):
        avatar = self._image(
            "image/avatar-valid.jpg",
            self._encoded_image("JPEG", size=(128, 150)),
        )
        script_to_video._get_first_avatar = lambda _username: {
            "id": 1, "image_file": avatar.relative_to(self.out).as_posix(),
        }
        generated = self._image("image/generated-valid.png", self._encoded_image("PNG"))
        self._audio("audio/speech.mp3", "mp3")
        base = self.out / "video/base.mp4"
        base.write_bytes(b"video")
        plan = self._plan(None, "generate")
        job_id = self._job({"material_plan": plan}, status="running")
        creates = []

        def create_once(*_args, **_kwargs):
            self.assertEqual(
                "provider_submitting",
                script_to_video.get_recovery_state(job_id)["phase"],
            )
            creates.append(1)
            return "provider-1"

        with mock.patch("content_domains.image.gen_image", return_value={
                "file": generated.relative_to(self.out).as_posix()}), \
             mock.patch.object(video, "HEYGEN_API_KEY", "configured"), \
             mock.patch.object(video, "_HEYGEN_DIRECT", False), \
             mock.patch.object(video, "get_video_avatar", return_value={
                 "id": 1, "image_file": avatar.relative_to(self.out).as_posix(),
             }), \
             mock.patch.object(video, "gen_audio", return_value={
                 "file": "audio/speech.mp3", "url": "/speech.mp3",
             }), \
             mock.patch.object(video, "_upload_heygen_image_asset", return_value="image-asset"), \
             mock.patch.object(video, "_heygen_upload_asset", return_value="audio-asset"), \
             mock.patch.object(video, "_heygen_create_video", side_effect=create_once), \
             mock.patch.object(video, "_heygen_poll_video", return_value={
                 "video_url": "https://example.invalid/base.mp4",
             }), \
             mock.patch.object(video, "_download_video_file", return_value="video/base.mp4"), \
             mock.patch.object(video, "_extract_first_frame_cover", return_value=None), \
             mock.patch.object(script_to_video, "_compose_materials", return_value="video/base.mp4"):
            result = script_to_video.gen_script_to_video({
                "_username": "alice", "_job_id": job_id,
                "scenes": [{"line": "介绍产品"}], "material_plan": plan,
                "subtitle": False,
            })
        self.assertEqual([1], creates)
        self.assertEqual("done", script_to_video.get_recovery_state(job_id)["phase"])
        self.assertEqual("talking_with_materials", result["pipeline"])

    def _job(self, payload, username="alice", status="pending", result=None, kind="script_to_video"):
        with sqlite3.connect(core.JOB_DB) as conn:
            cur = conn.execute(
                "INSERT INTO jobs(kind,username,status,payload,result) VALUES(?,?,?,?,?)",
                (kind, username, status, json.dumps(payload, ensure_ascii=False),
                 json.dumps(result, ensure_ascii=False) if result is not None else None),
            )
            job_id = cur.lastrowid
        if kind == "script_to_video":
            with sqlite3.connect(core.AUDIO_DB) as conn:
                conn.execute(
                    "INSERT INTO video_assets(job_id,phase,status,updated_at) VALUES(?,?,?,1)",
                    (job_id, "queued", "running"),
                )
        return job_id

    def _owned_asset(self, username="alice", rel="image/source.png"):
        self._image(rel)
        self._job({}, username=username, status="done", kind="image", result={
            "file": rel, "prompt": "产品",
        })
        return rel

    @staticmethod
    def _plan(rel=None, source="asset"):
        return [{
            "scene_index": 0, "prompt": "产品特写", "source": source,
            "file": rel,
        }]

    def test_reused_asset_is_frozen_before_enqueue_and_survives_source_delete(self):
        rel = self._owned_asset()
        job_id = self._job({"material_plan": self._plan(rel)})
        state = script_to_video.freeze_reused_materials_for_job(job_id, "alice")
        frozen = self.out / state["materials"][0]["file"]
        self.assertTrue(frozen.is_file())
        (self.out / rel).unlink()
        with sqlite3.connect(core.JOB_DB) as conn:
            conn.execute("UPDATE jobs SET status='running' WHERE id=?", (job_id,))
        materials = script_to_video._prepare_frozen_materials(
            job_id, "alice", self._plan(rel),
        )
        self.assertEqual(materials[0]["file"], state["materials"][0]["file"])

    def test_missing_zero_byte_and_cross_user_asset_never_reach_provider(self):
        cases = []
        cases.append(("missing", self._plan("image/missing.png")))
        self._image("image/zero.png", b"")
        cases.append(("zero", self._plan("image/zero.png")))
        foreign = self._owned_asset("bob", "image/bob.png")
        cases.append(("cross-user", self._plan(foreign)))
        for label, plan in cases:
            with self.subTest(label=label):
                job_id = self._job({"material_plan": plan}, status="running")
                with mock.patch.object(video, "gen_video") as provider:
                    with self.assertRaises((PermissionError, RuntimeError)):
                        script_to_video.gen_script_to_video({
                            "_username": "alice", "_job_id": job_id,
                            "scenes": [{"line": "介绍产品"}],
                            "material_plan": plan,
                        })
                provider.assert_not_called()

    def test_unreadable_asset_is_rejected_before_provider(self):
        rel = self._owned_asset()
        job_id = self._job({"material_plan": self._plan(rel)}, status="running")
        original = Path.open

        def denied(path, *args, **kwargs):
            if Path(path) == (self.out / rel):
                raise PermissionError("denied")
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, "open", denied), mock.patch.object(video, "gen_video") as provider:
            with self.assertRaises(PermissionError):
                script_to_video.gen_script_to_video({
                    "_username": "alice", "_job_id": job_id,
                    "scenes": [{"line": "介绍产品"}], "material_plan": self._plan(rel),
                })
        provider.assert_not_called()

    def test_generated_material_is_ready_before_provider_create(self):
        generated = self._image("image/generated.png")
        plan = self._plan(None, "generate")
        job_id = self._job({"material_plan": plan}, status="running")

        def fake_provider(payload, provider_lifecycle=None):
            state = script_to_video.get_recovery_state(job_id)
            self.assertEqual(state["phase"], "materials_ready")
            self.assertTrue((self.out / state["materials"][0]["file"]).is_file())
            base = self._image("video/base.mp4", b"video")
            return {"video_file": base.relative_to(self.out).as_posix(), "video_url": "/base"}

        with mock.patch("content_domains.image.gen_image", return_value={
                "file": generated.relative_to(self.out).as_posix()}), \
             mock.patch.object(video, "gen_video", side_effect=fake_provider), \
             mock.patch.object(script_to_video, "_provider_file_exists", return_value=False), \
             mock.patch.object(script_to_video, "_compose_materials", return_value="video/base.mp4"):
            result = script_to_video.gen_script_to_video({
                "_username": "alice", "_job_id": job_id,
                "scenes": [{"line": "介绍产品"}], "material_plan": plan,
                "subtitle": False,
            })
        self.assertEqual(result["pipeline"], "talking_with_materials")

    def test_ten_concurrent_freezes_share_one_server_derived_copy(self):
        rel = self._owned_asset()
        job_id = self._job({"material_plan": self._plan(rel)})
        results = []
        errors = []

        def run():
            try:
                results.append(script_to_video.freeze_reused_materials_for_job(job_id, "alice"))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 10)
        self.assertEqual(len({item["materials"][0]["file"] for item in results}), 1)

    def test_provider_acceptance_is_persisted_and_restart_does_not_post_twice(self):
        state = {}
        creates = []
        self._image("image/a.png")
        self._audio("audio/a.mp3", "mp3")

        def submitted(data):
            state.update(data)

        lifecycle = {"state": {}, "on_submitting": lambda data: state.update(data),
                     "on_submitted": submitted, "on_completed": lambda _data: None}
        with mock.patch.object(video, "_HEYGEN_DIRECT", False), \
             mock.patch.object(video, "_upload_heygen_image_asset", return_value="img"), \
             mock.patch.object(video, "_heygen_upload_asset", return_value="aud"), \
             mock.patch.object(video, "_heygen_create_video", side_effect=lambda *a, **k: creates.append(1) or "vid-1"), \
             mock.patch.object(video, "_heygen_poll_video", side_effect=RuntimeError("restart")):
            with self.assertRaises(video.HeyGenBilledError):
                video.generate_heygen_video_recoverable(
                    "image/a.png", "audio/a.mp3", "720p", "9:16", "medium", lifecycle,
                )
        self.assertEqual(state["provider_video_id"], "vid-1")
        resume = {"state": state, "on_completed": lambda _data: None}
        with mock.patch.object(video, "_heygen_create_video") as create, \
             mock.patch.object(video, "_heygen_poll_video", return_value={"video_url": "https://example/video"}), \
             mock.patch.object(video, "_download_video_file", return_value="video/base.mp4"), \
             mock.patch.object(video, "_extract_first_frame_cover", return_value=None):
            video.generate_heygen_video_recoverable(
                "unused", "unused", "720p", "9:16", "medium", resume,
            )
        create.assert_not_called()
        self.assertEqual(len(creates), 1)

    def test_provider_create_uses_shared_slot_and_only_429_retry_wrapper(self):
        events = []
        self._image("image/a.png")
        self._audio("audio/a.mp3", "mp3")

        class Slot:
            def __enter__(self):
                events.append("slot-enter")

            def __exit__(self, *_args):
                events.append("slot-exit")

        def retry_429(fn, label):
            events.append(("retry-429", label))
            return fn()

        def create(*_args, **_kwargs):
            self.assertIn("slot-enter", events)
            self.assertNotIn("slot-exit", events)
            events.append("create")
            return "vid-1"

        lifecycle = {
            "state": {},
            "on_submitting": lambda _data: events.append("submitting"),
            "on_submitted": lambda _data: events.append("submitted"),
            "on_completed": lambda _data: events.append("completed"),
        }
        with mock.patch.object(video, "_HEYGEN_DIRECT", False), \
             mock.patch.object(video, "_upload_heygen_image_asset", return_value="img"), \
             mock.patch.object(video, "_heygen_upload_asset", return_value="aud"), \
             mock.patch.object(video, "heygen_slot", return_value=Slot()), \
             mock.patch.object(video, "_heygen_retry_429", side_effect=retry_429) as retry, \
             mock.patch.object(video, "_heygen_create_video", side_effect=create) as create_call, \
             mock.patch.object(video, "_heygen_poll_video", return_value={"video_url": "https://example/video"}), \
             mock.patch.object(video, "_download_video_file", return_value="video/base.mp4"), \
             mock.patch.object(video, "_extract_first_frame_cover", return_value=None):
            result = video.generate_heygen_video_recoverable(
                "image/a.png", "audio/a.mp3", "720p", "9:16", "medium", lifecycle,
            )

        self.assertEqual(result["video_id"], "vid-1")
        create_call.assert_called_once()
        retry.assert_called_once()
        self.assertLess(events.index("slot-enter"), events.index("create"))
        self.assertLess(events.index("create"), events.index("slot-exit"))

    def test_definitive_provider_rejection_clears_ambiguous_phase_without_fallback(self):
        self._image("image/a.png")
        self._audio("audio/a.mp3", "mp3")
        events = []
        lifecycle = {
            "state": {},
            "on_submitting": lambda data: events.append(("submitting", data)),
            "on_rejected": lambda data: events.append(("rejected", data)),
        }
        rejected = RuntimeError("HeyGen接口失败: HTTP 400")
        rejected.__cause__ = urllib.error.HTTPError(
            "https://example.invalid", 400, "bad request", {}, None,
        )
        with mock.patch.object(video, "_HEYGEN_DIRECT", False), \
             mock.patch.object(video, "_upload_heygen_image_asset", return_value="img"), \
             mock.patch.object(video, "_heygen_upload_asset", return_value="aud"), \
             mock.patch.object(video, "_heygen_create_video", side_effect=rejected) as create, \
             mock.patch.object(video, "generate_heygen_video_direct") as fallback:
            with self.assertRaisesRegex(RuntimeError, "HTTP 400"):
                video.generate_heygen_video_recoverable(
                    "image/a.png", "audio/a.mp3", "720p", "9:16", "medium", lifecycle,
                )
        create.assert_called_once()
        fallback.assert_not_called()
        self.assertEqual([name for name, _ in events], ["submitting", "rejected"])

    def test_mcp_plan_credit_rejection_clears_submitting_without_paid_retry(self):
        self._image("image/no-plan-credit.png")
        self._audio("audio/no-plan-credit.mp3", "mp3")
        events = []
        lifecycle = {
            "state": {},
            "on_submitting": lambda data: events.append(("submitting", data)),
            "on_rejected": lambda data: events.append(("rejected", data)),
        }
        with mock.patch.object(video, "_HEYGEN_DIRECT", False), \
             mock.patch.object(video, "_upload_heygen_image_asset", return_value="img"), \
             mock.patch.object(video, "_heygen_upload_asset", return_value="aud"), \
             mock.patch.object(
                 video, "_heygen_create_video",
                 side_effect=video.HeyGenMCPPlanCreditsExhausted(
                     "HeyGen 套餐额度不足，供应商未受理任务"
                 ),
             ) as create, \
             mock.patch.object(video, "_heygen_poll_video") as poll:
            with self.assertRaises(video.HeyGenMCPPlanCreditsExhausted):
                video.generate_heygen_video_recoverable(
                    "image/no-plan-credit.png", "audio/no-plan-credit.mp3",
                    "720p", "9:16", "medium", lifecycle,
                )

        create.assert_called_once()
        poll.assert_not_called()
        self.assertEqual([name for name, _ in events], ["submitting", "rejected"])

    def test_missing_plan_oauth_is_refundable_and_never_posts_api_wallet_job(self):
        self._image("image/no-oauth.png")
        self._audio("audio/no-oauth.mp3", "mp3")
        events = []
        lifecycle = {
            "state": {},
            "on_submitting": lambda data: events.append(("submitting", data)),
            "on_rejected": lambda data: events.append(("rejected", data)),
        }
        with mock.patch.object(video, "_HEYGEN_MCP_CREDENTIALS", ""), \
             mock.patch.object(video, "_HEYGEN_ALLOW_API_WALLET", False), \
             mock.patch.object(video, "_HEYGEN_DIRECT", True), \
             mock.patch.object(video, "HEYGEN_API_KEY", "configured"), \
             mock.patch.object(video, "_upload_heygen_image_asset", return_value="img"), \
             mock.patch.object(video, "_heygen_upload_asset", return_value="aud"), \
             mock.patch.object(video, "_heygen_request_json") as paid_request:
            with self.assertRaisesRegex(
                    video.HeyGenMCPAuthError, "已阻止回退到 API 钱包"):
                video.generate_heygen_video_recoverable(
                    "image/no-oauth.png", "audio/no-oauth.mp3",
                    "720p", "9:16", "medium", lifecycle,
                )
        paid_request.assert_not_called()
        self.assertEqual(events, [])

    def test_script_pipeline_definitive_rejection_preserves_http_error_and_consistent_phase(self):
        job_id = self._job({}, status="running")

        def reject_provider(_payload, provider_lifecycle=None):
            provider_lifecycle["on_prepared"]({
                "audio_file": "audio/speech.mp3", "image_file": "image/avatar.png",
            })
            provider_lifecycle["on_submitting"]({
                "provider": "heygen_relay", "image_asset_id": "img-1",
                "audio_asset_id": "aud-1",
            })
            provider_lifecycle["on_rejected"]({"provider": "heygen_relay"})
            raise RuntimeError("HeyGen接口失败: HTTP 400")

        with mock.patch.object(video, "gen_video", side_effect=reject_provider), \
             mock.patch.object(script_to_video, "_prepare_frozen_materials", return_value=[]), \
             mock.patch.object(script_to_video, "_provider_file_exists", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "HTTP 400"):
                script_to_video.gen_script_to_video({
                    "_username": "alice", "_job_id": job_id,
                    "scenes": [{"line": "介绍产品"}], "material_plan": [],
                    "subtitle": False,
                })

        state = script_to_video.get_recovery_state(job_id)
        self.assertEqual(state["phase"], "materials_ready")
        self.assertIsNone(state.get("provider_video_id"))
        with sqlite3.connect(core.AUDIO_DB) as conn:
            phase = conn.execute(
                "SELECT phase FROM video_assets WHERE job_id=?", (job_id,),
            ).fetchone()[0]
        self.assertEqual(phase, "materials_ready")

    def test_script_pipeline_persists_oauth_route_transport_and_actual_resolution(self):
        job_id = self._job({}, status="running")
        base = self._image("video/oauth-base.mp4", b"video")

        def oauth_provider(_payload, provider_lifecycle=None):
            provider_lifecycle["on_prepared"]({
                "audio_file": "audio/speech.mp3", "image_file": "image/avatar.png",
            })
            common = {
                "provider": "mcp_oauth", "provider_transport": "mcp",
                "image_asset_id": "oauth-image", "audio_asset_id": "oauth-audio",
                "actual_resolution": "720p",
            }
            provider_lifecycle["on_submitting"](common)
            provider_lifecycle["on_submitted"]({
                **common, "provider_video_id": "oauth-video",
            })
            result = {
                **common, "video_id": "oauth-video",
                "video_file": base.relative_to(self.out).as_posix(),
                "video_url": "/oauth-base",
            }
            provider_lifecycle["on_completed"](result)
            return {"provider_video_id": "oauth-video", **result}

        with mock.patch.object(video, "gen_video", side_effect=oauth_provider), \
             mock.patch.object(script_to_video, "_prepare_frozen_materials", return_value=[]), \
             mock.patch.object(script_to_video, "_provider_file_exists", return_value=False):
            script_to_video.gen_script_to_video({
                "_username": "alice", "_job_id": job_id,
                "scenes": [{"line": "介绍产品"}], "material_plan": [],
                "subtitle": False, "resolution": "1080p",
            })

        state = script_to_video.get_recovery_state(job_id)
        self.assertEqual(state["provider"], "mcp_oauth")
        self.assertEqual(state["provider_transport"], "mcp")
        self.assertEqual(state["provider_video_id"], "oauth-video")
        self.assertEqual(state["actual_resolution"], "720p")
        self.assertEqual(state["provider_result"]["actual_resolution"], "720p")

    def test_compose_failure_retries_local_only_and_keeps_base_video(self):
        plan = []
        job_id = self._job({"material_plan": plan}, status="running")
        base = self.out / "video/base.mp4"
        base.write_bytes(b"video")
        provider_calls = []

        def provider(_payload, provider_lifecycle=None):
            provider_calls.append(1)
            provider_lifecycle["on_submitting"]({
                "provider": "heygen_relay", "image_asset_id": "img", "audio_asset_id": "aud",
            })
            provider_lifecycle["on_submitted"]({
                "provider": "heygen_relay", "provider_video_id": "vid-1",
                "image_asset_id": "img", "audio_asset_id": "aud",
            })
            result = {"video_id": "vid-1", "video_file": "video/base.mp4", "video_url": "/base"}
            provider_lifecycle["on_completed"](result)
            return {"provider_video_id": "vid-1", **result}

        video.gen_video = provider
        material = [{"scene_index": 0, "prompt": "产品", "source": "asset", "file": "frozen.png"}]
        with mock.patch.object(script_to_video, "_prepare_frozen_materials", return_value=material), \
             mock.patch.object(script_to_video, "_compose_materials", side_effect=RuntimeError("disk full")):
            with self.assertRaises(script_to_video.ScriptToVideoRecoveryRequired):
                script_to_video.gen_script_to_video({
                    "_username": "alice", "_job_id": job_id,
                    "scenes": [{"line": "介绍产品"}], "material_plan": material,
                    "subtitle": False,
                })
        with mock.patch.object(video, "gen_video", side_effect=AssertionError("must not create")), \
             mock.patch.object(script_to_video, "_prepare_frozen_materials", return_value=material), \
             mock.patch.object(script_to_video, "_compose_materials", return_value="video/final.mp4"), \
             mock.patch.object(video, "public_url", return_value="/final"):
            result = script_to_video.gen_script_to_video({
                "_username": "alice", "_job_id": job_id,
                "scenes": [{"line": "介绍产品"}], "material_plan": material,
                "subtitle": False,
            })
        self.assertEqual(result["video_file"], "video/final.mp4")
        self.assertEqual(provider_calls, [1])

    def test_ambiguous_submitting_is_held_and_known_id_is_requeued(self):
        first = self._job({"_script_to_video_state": {
            "phase": "provider_submitting", "provider": "heygen_relay",
        }}, status="running")
        second = self._job({"_script_to_video_state": {
            "phase": "provider_submitted", "provider": "heygen_relay",
            "provider_video_id": "vid-2",
        }}, status="running")
        requeued = []
        result = script_to_video.reclaim_orphaned_jobs(
            lambda job_id: requeued.append(job_id) or True,
            logger=lambda *_args, **_kwargs: None,
        )
        self.assertEqual(result["held"], {first})
        self.assertEqual(requeued, [second])

    def test_invalid_or_incomplete_recovery_payloads_are_held_fail_closed(self):
        payloads = [
            "{",
            json.dumps([], ensure_ascii=False),
            json.dumps({}, ensure_ascii=False),
            json.dumps({"_script_to_video_state": "invalid"}, ensure_ascii=False),
            json.dumps({"_script_to_video_state": {}}, ensure_ascii=False),
            json.dumps({"_script_to_video_state": {"phase": "unknown"}}, ensure_ascii=False),
            json.dumps({"_script_to_video_state": {
                "phase": "provider_submitted",
            }}, ensure_ascii=False),
        ]
        job_ids = []
        for raw_payload in payloads:
            job_id = self._job({}, status="running")
            with sqlite3.connect(core.JOB_DB) as conn:
                conn.execute("UPDATE jobs SET payload=? WHERE id=?", (raw_payload, job_id))
            job_ids.append(job_id)

        requeued = []
        result = script_to_video.reclaim_orphaned_jobs(
            lambda job_id: requeued.append(job_id) or True,
            logger=lambda *_args, **_kwargs: None,
        )
        self.assertEqual(result["held"], set(job_ids))
        self.assertEqual(result["handled"], 0)
        self.assertEqual(requeued, [])

    def test_startup_invalid_payload_never_refunds_or_posts_provider(self):
        job_id = self._job({}, status="running")
        with sqlite3.connect(core.JOB_DB) as conn:
            conn.execute("UPDATE jobs SET payload=? WHERE id=?", ("{", job_id))

        refunds = []
        with mock.patch.object(
                core, "_fail_job_and_schedule_refund",
                side_effect=lambda *args, **_kwargs: refunds.append(args[0]) or True,
             ), mock.patch.object(video, "gen_video") as provider:
            core.reclaim_orphaned_running()

        self.assertEqual(refunds, [])
        provider.assert_not_called()
        with sqlite3.connect(core.JOB_DB) as conn:
            status = conn.execute(
                "SELECT status FROM jobs WHERE id=?", (job_id,),
            ).fetchone()[0]
        self.assertEqual(status, "running")

    def test_done_handler_result_is_replayed_after_refresh_without_provider_or_compose(self):
        final = {"type": "script_to_video", "pipeline": "talking_with_materials",
                 "video_file": "video/final.mp4", "video_url": "/final"}
        (self.out / "video/final.mp4").write_bytes(b"video")
        job_id = self._job({"_script_to_video_state": {
            "phase": "done", "provider_video_id": "vid-1", "final_result": final,
        }}, status="running")
        with mock.patch.object(video, "gen_video") as provider, \
             mock.patch.object(script_to_video, "_prepare_frozen_materials", return_value=[]), \
             mock.patch.object(script_to_video, "_compose_materials") as compose:
            replay = script_to_video.gen_script_to_video({
                "_username": "alice", "_job_id": job_id,
                "scenes": [{"line": "介绍产品"}], "material_plan": [],
            })
        self.assertEqual(replay, final)
        provider.assert_not_called()
        compose.assert_not_called()

    def test_pre_provider_failure_refunds_at_most_once_via_job_terminal_cas(self):
        job_id = self._job({}, status="pending")
        refunds = []
        original = core.HANDLERS.get("script_to_video")
        core.HANDLERS["script_to_video"] = lambda _payload: (_ for _ in ()).throw(
            RuntimeError("material missing")
        )
        try:
            with mock.patch.object(core, "_start_job_heartbeat", return_value=lambda: None), \
                 mock.patch.object(core, "_fail_job_and_schedule_refund", side_effect=lambda *a, **k: refunds.append(a[0]) or True), \
                 mock.patch.object(core, "_mark_video_asset_failed"), \
                 mock.patch.object(core, "_recover_pending_jobs"):
                core.run_job(job_id)
                # A second execution cannot claim the same task in production;
                # emulate the terminal CAS outcome for the repeated callback.
                with sqlite3.connect(core.JOB_DB) as conn:
                    conn.execute("UPDATE jobs SET status='error' WHERE id=?", (job_id,))
                core.run_job(job_id)
        finally:
            if original is None:
                core.HANDLERS.pop("script_to_video", None)
            else:
                core.HANDLERS["script_to_video"] = original
        self.assertEqual(refunds, [job_id])

    def test_mcp_plan_credit_rejection_refunds_once_instead_of_holding_running(self):
        job_id = self._job({}, status="pending")
        refunds = []
        original = core.HANDLERS.get("script_to_video")

        def reject(_payload):
            script_to_video._persist_job_state(
                job_id, "alice", "provider_submitting",
                provider="heygen_direct", image_asset_id="img", audio_asset_id="aud",
            )
            script_to_video._persist_job_state(
                job_id, "alice", "materials_ready",
                provider=None, provider_video_id=None,
                image_asset_id=None, audio_asset_id=None,
            )
            raise video.HeyGenMCPPlanCreditsExhausted(
                "HeyGen 套餐额度不足，供应商未受理任务"
            )

        core.HANDLERS["script_to_video"] = reject
        try:
            with mock.patch.object(core, "_start_job_heartbeat", return_value=lambda: None), \
                 mock.patch.object(core, "_requeue_running_job") as requeue, \
                 mock.patch.object(
                     core, "_fail_job_and_schedule_refund",
                     side_effect=lambda *args, **_kwargs: refunds.append(args[0]) or True,
                 ), \
                 mock.patch.object(core, "_mark_video_asset_failed"), \
                 mock.patch.object(core, "_recover_pending_jobs"):
                core.run_job(job_id)
                with sqlite3.connect(core.JOB_DB) as conn:
                    conn.execute("UPDATE jobs SET status='error' WHERE id=?", (job_id,))
                core.run_job(job_id)
        finally:
            if original is None:
                core.HANDLERS.pop("script_to_video", None)
            else:
                core.HANDLERS["script_to_video"] = original

        self.assertEqual(refunds, [job_id])
        requeue.assert_not_called()

    def test_explicit_provider_failed_terminal_refunds_once_without_requeue(self):
        job_id = self._job({"_script_to_video_state": {
            "phase": "provider_submitted",
            "provider": "heygen_relay",
            "provider_video_id": "vid-failed",
        }}, status="pending")
        refunds = []
        original = core.HANDLERS.get("script_to_video")
        core.HANDLERS["script_to_video"] = lambda _payload: (_ for _ in ()).throw(
            video.HeyGenProviderFailed("HeyGen视频生成失败: provider rejected")
        )
        try:
            with mock.patch.object(core, "_start_job_heartbeat", return_value=lambda: None), \
                 mock.patch.object(core, "_requeue_running_job") as requeue, \
                 mock.patch.object(core, "_fail_job_and_schedule_refund", side_effect=lambda *a, **k: refunds.append(a[0]) or True), \
                 mock.patch.object(core, "_mark_video_asset_failed"), \
                 mock.patch.object(core, "_recover_pending_jobs"):
                core.run_job(job_id)
                with sqlite3.connect(core.JOB_DB) as conn:
                    conn.execute("UPDATE jobs SET status='error' WHERE id=?", (job_id,))
                core.run_job(job_id)
        finally:
            if original is None:
                core.HANDLERS.pop("script_to_video", None)
            else:
                core.HANDLERS["script_to_video"] = original

        self.assertEqual(refunds, [job_id])
        requeue.assert_not_called()

    def test_running_error_holds_when_recovery_state_database_is_unavailable(self):
        job_id = self._job({}, status="pending")
        refunds = []
        original = core.HANDLERS.get("script_to_video")
        core.HANDLERS["script_to_video"] = lambda _payload: (_ for _ in ()).throw(
            RuntimeError("worker failed")
        )
        try:
            with mock.patch.object(core, "_start_job_heartbeat", return_value=lambda: None), \
                 mock.patch.object(script_to_video, "get_recovery_state", side_effect=sqlite3.OperationalError("database is locked")), \
                 mock.patch.object(core, "_fail_job_and_schedule_refund", side_effect=lambda *a, **k: refunds.append(a[0]) or True), \
                 mock.patch.object(core, "_mark_video_asset_failed"), \
                 mock.patch.object(core, "_recover_pending_jobs"), \
                 mock.patch.object(video, "gen_video") as provider:
                core.run_job(job_id)
        finally:
            if original is None:
                core.HANDLERS.pop("script_to_video", None)
            else:
                core.HANDLERS["script_to_video"] = original

        self.assertEqual(refunds, [])
        provider.assert_not_called()
        with sqlite3.connect(core.JOB_DB) as conn:
            status = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()[0]
        self.assertEqual(status, "running")

    def test_reaper_holds_when_recovery_state_database_is_unavailable(self):
        job_id = self._job({}, status="running")
        refunds = []
        points_domain = SimpleNamespace(retry_breakdown_refunds=lambda _limit: None)
        video_domain = SimpleNamespace(retry_pending_seedance_cleanups=lambda **_kwargs: None)
        with mock.patch.object(core, "_domains", return_value=(None, points_domain, video_domain)), \
             mock.patch.object(script_to_video, "get_recovery_state", side_effect=sqlite3.OperationalError("database is locked")), \
             mock.patch.object(core, "_fail_job_and_schedule_refund", side_effect=lambda *a, **k: refunds.append(a[0]) or True), \
             mock.patch.object(core.time, "sleep", side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                core.reaper()

        self.assertEqual(refunds, [])
        with sqlite3.connect(core.JOB_DB) as conn:
            status = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()[0]
        self.assertEqual(status, "running")

    def test_startup_holds_all_when_script_recovery_query_is_unavailable(self):
        job_id = self._job({"_script_to_video_state": {
            "phase": "provider_submitting", "provider": "heygen_relay",
        }}, status="running")
        with mock.patch.object(script_to_video, "jdb", side_effect=sqlite3.OperationalError("database is locked")), \
             mock.patch.object(core.startup_recovery, "reclaim_orphaned_running") as generic_reclaimer, \
             mock.patch.object(core, "_fail_job_and_schedule_refund") as refund:
            self.assertEqual(core.reclaim_orphaned_running(), 0)

        generic_reclaimer.assert_not_called()
        refund.assert_not_called()
        with sqlite3.connect(core.JOB_DB) as conn:
            status = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()[0]
        self.assertEqual(status, "running")


if __name__ == "__main__":
    unittest.main()
