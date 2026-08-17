import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


if sys.platform == "win32":
    sys.modules.setdefault(
        "fcntl",
        SimpleNamespace(LOCK_EX=1, LOCK_NB=2, LOCK_UN=8, flock=lambda *_args: None),
    )

SERVER = Path(__file__).resolve().parents[1] / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from content_domains import core, video  # noqa: E402


class ScriptToVideoAssetRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp.name)
        self.out = self.root / "content_out"
        (self.out / "video").mkdir(parents=True)
        self.old_job_db = core.JOB_DB
        self.old_audio_db = core.AUDIO_DB
        self.old_out = core.OUT_DIR
        self.old_video_out = core.VIDEO_OUT_DIR
        core.JOB_DB = str(self.root / "jobs.db")
        core.AUDIO_DB = str(self.root / "audio.db")
        core.OUT_DIR = self.out
        core.VIDEO_OUT_DIR = self.out / "video"
        self._init_databases()

    def tearDown(self):
        core.JOB_DB = self.old_job_db
        core.AUDIO_DB = self.old_audio_db
        core.OUT_DIR = self.old_out
        core.VIDEO_OUT_DIR = self.old_video_out
        self.temp.cleanup()

    def _init_databases(self):
        with sqlite3.connect(core.JOB_DB) as connection:
            connection.execute("""CREATE TABLE jobs(
                id INTEGER PRIMARY KEY, kind TEXT, username TEXT, cost INTEGER,
                status TEXT, payload TEXT, result TEXT, error TEXT,
                created_at INTEGER, updated_at INTEGER, refunded INTEGER DEFAULT 0
            )""")
        with sqlite3.connect(core.AUDIO_DB) as connection:
            connection.execute("""CREATE TABLE video_assets(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER UNIQUE,
                username TEXT NOT NULL,
                mode TEXT NOT NULL,
                image_file TEXT,
                audio_file TEXT,
                reference_video_file TEXT,
                video_file TEXT,
                video_url TEXT,
                text TEXT,
                voice_key TEXT,
                resolution TEXT,
                ratio TEXT,
                motion TEXT,
                phase TEXT,
                image_asset_id TEXT,
                audio_asset_id TEXT,
                reference_asset_id TEXT,
                provider_video_id TEXT,
                provider_key_id TEXT,
                provider_avatar_id TEXT,
                provider_avatar_group_id TEXT,
                source_video_url TEXT,
                background_file TEXT,
                tryon_mode TEXT,
                model TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                error TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )""")
            connection.execute("""CREATE TABLE audio_assets(
                username TEXT, deleted INTEGER, file TEXT
            )""")
            connection.execute("""CREATE TABLE avatars(
                username TEXT, status TEXT, image_file TEXT
            )""")
            connection.execute("""CREATE TABLE audio_voices(
                username TEXT, scope TEXT, preview_file TEXT
            )""")

    def _job(self, job_id, payload, *, cost=120):
        with sqlite3.connect(core.JOB_DB) as connection:
            connection.execute(
                """INSERT INTO jobs(
                    id,kind,username,cost,status,payload,created_at,updated_at,refunded
                ) VALUES(?, 'script_to_video', 'alice', ?, 'pending', ?, 1, 1, 0)""",
                (job_id, cost, json.dumps(payload)),
            )

    @staticmethod
    def _terminal(job_id, status, result=None, error=None, **_kwargs):
        with sqlite3.connect(core.JOB_DB) as connection:
            cursor = connection.execute(
                "UPDATE jobs SET status=?,result=?,error=? WHERE id=? AND status='running'",
                (status, json.dumps(result) if result is not None else None, error, job_id),
            )
        return cursor.rowcount == 1

    def _run(self, job_id, result, recorder=video.record_video_asset):
        handler = mock.Mock(return_value=result)
        points = mock.Mock()
        video_domain = SimpleNamespace(record_video_asset=recorder)
        audio_domain = SimpleNamespace()
        with mock.patch.dict(core.HANDLERS, {"script_to_video": handler}), \
                mock.patch.object(core, "_domains", return_value=(audio_domain, points, video_domain)), \
                mock.patch.object(core, "_set_terminal", side_effect=self._terminal), \
                mock.patch.object(core, "_start_job_heartbeat", return_value=lambda: None), \
                mock.patch.object(core, "_user_running_talking_count", return_value=0), \
                mock.patch.object(core, "_recover_pending_jobs"), \
                mock.patch.object(core.assets_store, "record_asset"):
            core.run_job(job_id)
        return handler, points

    def _asset(self, job_id):
        with sqlite3.connect(core.AUDIO_DB) as connection:
            connection.row_factory = sqlite3.Row
            return connection.execute(
                "SELECT * FROM video_assets WHERE job_id=?", (job_id,),
            ).fetchone()

    def test_missing_result_mode_is_registered_from_job_payload(self):
        self._job(3485, {"mode": "digital_human_oneclick_compose"})
        result = {
            "video_file": "video/final-3485.mp4",
            "video_url": "/api/gen/file/video/final-3485.mp4",
            "status": "done",
            "phase": "complete",
        }

        handler, points = self._run(3485, result)

        asset = self._asset(3485)
        self.assertEqual(asset["username"], "alice")
        self.assertEqual(asset["mode"], "digital_human_oneclick_compose")
        self.assertEqual(asset["video_file"], result["video_file"])
        self.assertEqual(asset["video_url"], result["video_url"])
        self.assertEqual(asset["status"], "done")
        self.assertEqual(asset["phase"], "complete")
        with sqlite3.connect(core.JOB_DB) as connection:
            row = connection.execute(
                "SELECT status,cost,refunded FROM jobs WHERE id=3485"
            ).fetchone()
        self.assertEqual(row, ("done", 120, 0))
        handler.assert_called_once()
        points.safe_refund_points.assert_not_called()
        points.refund_points.assert_not_called()

    def test_running_asset_is_upserted_to_done_with_final_file(self):
        self._job(3486, {"pipeline": "digital_human_oneclick_compose"})
        with sqlite3.connect(core.AUDIO_DB) as connection:
            connection.execute(
                """INSERT INTO video_assets(
                    job_id,username,mode,status,phase,created_at,updated_at
                ) VALUES(3486,'alice','script_to_video','running','composing',1,1)"""
            )

        self._run(3486, {
            "video_file": "video/final-3486.mp4",
            "video_url": "/api/gen/file/video/final-3486.mp4",
            "status": "done",
            "phase": "complete",
        })

        asset = self._asset(3486)
        self.assertEqual(asset["mode"], "digital_human_oneclick_compose")
        self.assertEqual(asset["status"], "done")
        self.assertEqual(asset["phase"], "complete")
        self.assertEqual(asset["video_file"], "video/final-3486.mp4")

    def test_registration_failure_keeps_job_done_and_logs_only_safe_context(self):
        self._job(3487, {
            "mode": "digital_human_oneclick_compose",
            "script": "confidential task body",
        })
        result = {
            "video_url": "https://signed.example/private-token",
            "status": "done",
        }

        def fail_registration(*_args):
            raise sqlite3.IntegrityError(
                "alice https://signed.example/private-token confidential task body"
            )

        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            handler, points = self._run(3487, result, fail_registration)

        with sqlite3.connect(core.JOB_DB) as connection:
            row = connection.execute(
                "SELECT status,cost,refunded FROM jobs WHERE id=3487"
            ).fetchone()
        self.assertEqual(row, ("done", 120, 0))
        self.assertIn(
            "[asset] record failed job=3487 kind=script_to_video error=IntegrityError",
            output.getvalue(),
        )
        self.assertNotIn("alice", output.getvalue())
        self.assertNotIn("private-token", output.getvalue())
        self.assertNotIn("confidential task body", output.getvalue())
        handler.assert_called_once()
        points.safe_refund_points.assert_not_called()
        points.refund_points.assert_not_called()

    def test_download_allows_owner_and_rejects_other_deleted_or_missing_file(self):
        rel = "video/final-3488.mp4"
        final_file = self.out / rel
        final_file.write_bytes(b"video")
        with sqlite3.connect(core.AUDIO_DB) as connection:
            connection.execute(
                """INSERT INTO video_assets(
                    job_id,username,mode,video_file,status,created_at,updated_at
                ) VALUES(3488,'alice','digital_human_oneclick_compose',?,'done',1,1)""",
                (rel,),
            )

        class Handler:
            headers = {}

            def __init__(self, username):
                self.path = "/api/gen/file/" + rel
                self.username = username
                self.status = None
                self.wfile = io.BytesIO()

            def _token(self):
                return self.username

            def _send(self, status, _payload):
                self.status = status

            def send_response(self, status):
                self.status = status

            def send_header(self, *_args):
                pass

            def end_headers(self):
                pass

        domains = (SimpleNamespace(), SimpleNamespace(), SimpleNamespace())

        def request(username):
            handler = Handler(username)
            with mock.patch.object(core, "_domains", return_value=domains), \
                    mock.patch.object(core, "_dispatch_short_drama", return_value=False), \
                    mock.patch.object(
                        core, "_digital_ip_domain",
                        return_value=SimpleNamespace(dispatch_http=lambda *_args: False),
                    ), \
                    mock.patch.object(core, "verify", side_effect=lambda token: {"username": token}), \
                    mock.patch.object(core, "_short_drama_canvas_access", return_value=None):
                core.H.do_GET(handler)
            return handler.status

        self.assertEqual(request("alice"), 200)
        self.assertEqual(request("bob"), 404)
        with sqlite3.connect(core.AUDIO_DB) as connection:
            connection.execute(
                "UPDATE video_assets SET status='deleted' WHERE job_id=3488"
            )
        self.assertEqual(request("alice"), 404)
        with sqlite3.connect(core.AUDIO_DB) as connection:
            connection.execute(
                "UPDATE video_assets SET status='done' WHERE job_id=3488"
            )
        final_file.unlink()
        self.assertEqual(request("alice"), 404)


if __name__ == "__main__":
    unittest.main()
