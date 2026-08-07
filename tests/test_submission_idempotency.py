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

from content_domains import submission_idempotency


class SubmissionIdempotencyLookupTests(unittest.TestCase):
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
