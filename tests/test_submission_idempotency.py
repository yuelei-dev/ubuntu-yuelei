import pathlib
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing


ROOT = pathlib.Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from content_domains import script_to_video, submission_idempotency


class SubmissionIdempotencyLookupTests(unittest.TestCase):
    def test_durable_attempt_survives_charge_crashes_and_links_with_job_transaction(self):
        with tempfile.TemporaryDirectory() as raw:
            database = pathlib.Path(raw) / "jobs.db"

            def connect():
                connection = sqlite3.connect(database)
                connection.row_factory = sqlite3.Row
                return connection

            with closing(connect()) as connection:
                connection.execute("""CREATE TABLE jobs(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT, username TEXT, cost INTEGER, payload TEXT
                )""")
                connection.commit()

            request = {
                "pipeline": "smart_montage", "copy": "恢复测试",
                "style": "luxe", "ratio": "16:9",
                "plan_digest": "a" * 64,
            }
            frozen = {
                **request,
                "material_plan": [{
                    "scene_index": 0, "source": "upload",
                    "file": "_smart_materials/task_one/scene-00.png",
                    "sha256": "b" * 64,
                }],
            }
            key = "smart-durable-attempt-0001"
            charge_key = "job-charge:fang:script:" + key
            state, attempt = submission_idempotency.begin_attempt(
                connect, "fang", "/api/gen/script_to_video", key,
                request, frozen, 30, charge_key,
            )
            self.assertEqual("new", state)
            self.assertEqual("frozen", attempt["state"])
            self.assertEqual(frozen, attempt["payload"])

            submission_idempotency.mark_charged(
                connect, "fang", "/api/gen/script_to_video", key,
                charge_key, 970,
            )
            charged = submission_idempotency.load_attempt(
                connect, "fang", "/api/gen/script_to_video", key, request,
            )
            self.assertEqual("charged", charged["state"])
            self.assertEqual(970, charged["points_left"])
            submission_idempotency.abort(
                connect, "fang", "/api/gen/script_to_video", key,
            )
            self.assertEqual(
                "charged",
                submission_idempotency.load_attempt(
                    connect, "fang", "/api/gen/script_to_video", key,
                    request,
                )["state"],
            )

            # A rollback must remove both the job and its attempted binding.
            with closing(connect()) as connection:
                cursor = connection.execute(
                    "INSERT INTO jobs(kind,username,cost,payload) VALUES(?,?,?,?)",
                    ("script_to_video", "fang", 30, "{}"),
                )
                submission_idempotency.link_job(
                    connection, "fang", "/api/gen/script_to_video", key,
                    charge_key, cursor.lastrowid, 970,
                )
                connection.rollback()
            after_rollback = submission_idempotency.load_attempt(
                connect, "fang", "/api/gen/script_to_video", key, request,
            )
            self.assertEqual("charged", after_rollback["state"])
            self.assertIsNone(after_rollback["job_id"])

            with closing(connect()) as connection:
                cursor = connection.execute(
                    "INSERT INTO jobs(kind,username,cost,payload) VALUES(?,?,?,?)",
                    ("script_to_video", "fang", 30, "{}"),
                )
                job_id = cursor.lastrowid
                submission_idempotency.link_job(
                    connection, "fang", "/api/gen/script_to_video", key,
                    charge_key, job_id, 970,
                )
                connection.commit()
            linked = submission_idempotency.load_attempt(
                connect, "fang", "/api/gen/script_to_video", key, request,
            )
            self.assertEqual("linked", linked["state"])
            self.assertEqual(job_id, linked["job_id"])
            submission_idempotency.abort(
                connect, "fang", "/api/gen/script_to_video", key,
            )
            still_linked = submission_idempotency.load_attempt(
                connect, "fang", "/api/gen/script_to_video", key, request,
            )
            self.assertEqual("linked", still_linked["state"])
            self.assertEqual(job_id, still_linked["job_id"])

    def test_smart_material_order_is_canonical_and_empty_request_stays_legacy_compatible(self):
        request = {
            "pipeline": "smart_montage",
            "copy": "专业护理让状态自然稳定。",
            "style": "luxe",
            "ratio": "16:9",
            "plan_digest": "a" * 64,
        }
        canonical = script_to_video.normalize_smart_montage_submission(request)
        self.assertEqual(canonical, request)
        self.assertNotIn("material_upload_ids", canonical)

        first = "img_" + "1" * 32
        second = "img_" + "2" * 32
        ordered = script_to_video.normalize_smart_montage_submission({
            **request, "material_upload_ids": [first, None, second],
        })
        swapped = script_to_video.normalize_smart_montage_submission({
            **request, "material_upload_ids": [second, None, first],
        })
        self.assertEqual([first, None, second], ordered["material_upload_ids"])
        self.assertNotEqual(ordered, swapped)

    def test_lookup_replays_original_request_without_creating_a_new_claim(self):
        with tempfile.TemporaryDirectory() as raw:
            database = pathlib.Path(raw) / "jobs.db"

            def connect():
                connection = sqlite3.connect(database)
                connection.row_factory = sqlite3.Row
                return connection

            request = {
                "pipeline": "smart_montage",
                "copy": "专业护理让状态自然稳定。",
                "style": "luxe",
                "ratio": "16:9",
                "plan_digest": "a" * 64,
            }
            endpoint = "/api/gen/script_to_video"
            key = "smart-montage-luxe-0001"

            self.assertEqual(
                ("missing", None),
                submission_idempotency.lookup(
                    connect, "fang", endpoint, key, request,
                ),
            )
            with closing(connect()) as connection:
                self.assertEqual(
                    0,
                    connection.execute(
                        "SELECT COUNT(*) FROM submission_idempotency"
                    ).fetchone()[0],
                )

            self.assertEqual(
                ("new", None),
                submission_idempotency.begin(
                    connect, "fang", endpoint, key, request,
                ),
            )
            self.assertEqual(
                ("processing", None),
                submission_idempotency.lookup(
                    connect, "fang", endpoint, key, request,
                ),
            )
            accepted = {"job_id": 42, "cost": 150}
            submission_idempotency.complete(
                connect, "fang", endpoint, key, accepted,
            )
            self.assertEqual(
                ("replay", accepted),
                submission_idempotency.lookup(
                    connect, "fang", endpoint, key, request,
                ),
            )

            changed = dict(request, style="pop")
            self.assertEqual(
                ("conflict", None),
                submission_idempotency.lookup(
                    connect, "fang", endpoint, key, changed,
                ),
            )


if __name__ == "__main__":
    unittest.main()
