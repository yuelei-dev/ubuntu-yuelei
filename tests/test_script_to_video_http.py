import base64
import hashlib
import io
import json
import pathlib
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import ExitStack, closing
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest import mock

from PIL import Image


ROOT = pathlib.Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from content_domains import (
    cli_uploads,
    core,
    jobs_store,
    points as points_domain,
    script_to_video,
    script_video_montage,
    submission_idempotency,
    upstream_guard,
    video,
)


PNG_ONE = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEklEQVR4nGOskDvBwMDAxAAGABBCAWKm3yc5AAAAAElFTkSuQmCC"
)
PNG_TWO = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEklEQVR4nGOU2xLFwMDAxAAGAA3+ATB3z0T/AAAAAElFTkSuQmCC"
)


class SmartMontageSubmissionHttpTests(unittest.TestCase):
    def test_key_digest_and_original_request_idempotency_contract(self):
        class FakePointsError(Exception):
            def __init__(self, status, detail):
                self.status = status
                self.detail = detail

        class FakePoints:
            AuthPointsError = FakePointsError

            def __init__(self):
                self.deductions = []
                self.refunds = []
                self.fail_refunds = False
                self.fail_next_deduct_after_booking = False
                self.reject_next_deduct = False
                self.booked_deductions = {}

            @staticmethod
            def cost_of(kind, body):
                return points_domain.cost_of(kind, body)

            def deduct_points(self, username, cost, reason, transaction_key=""):
                if self.reject_next_deduct:
                    self.reject_next_deduct = False
                    raise FakePointsError(402, "insufficient points")
                if transaction_key in self.booked_deductions:
                    return self.booked_deductions[transaction_key]
                self.deductions.append(
                    (username, cost, reason, transaction_key)
                )
                points_left = 1000 - cost
                self.booked_deductions[transaction_key] = points_left
                if self.fail_next_deduct_after_booking:
                    self.fail_next_deduct_after_booking = False
                    raise FakePointsError(502, "Auth response lost after booking")
                return points_left

            def get_points_transaction(self, transaction_key):
                if transaction_key not in self.booked_deductions:
                    return None
                booked = next(
                    item for item in self.deductions if item[3] == transaction_key
                )
                return {
                    "username": booked[0], "delta": -booked[1],
                    "after_points": self.booked_deductions[transaction_key],
                }

            def refund_points(self, username, cost, reason, transaction_key=""):
                self.refunds.append((username, cost, reason, transaction_key))
                if self.fail_refunds:
                    raise ConnectionError("refund confirmation unavailable")
                return 1000

        points = FakePoints()
        fake_short_drama = SimpleNamespace(
            RevisionConflict=type("RevisionConflict", (Exception,), {}),
            _http_error=lambda *_args, **_kwargs: None,
            fail_linked_character_reference_job=lambda *_args, **_kwargs: None,
            short_drama_production=SimpleNamespace(
                fail_linked_job=lambda *_args, **_kwargs: None,
            ),
        )
        server = None
        with tempfile.TemporaryDirectory() as raw, ExitStack() as stack:
            database = str(pathlib.Path(raw) / "jobs.db")
            output_root = pathlib.Path(raw) / "out"
            upload_root = pathlib.Path(raw) / "uploads"
            stack.enter_context(mock.patch.object(core, "JOB_DB", database))
            stack.enter_context(mock.patch.object(core, "OUT_DIR", output_root))
            stack.enter_context(mock.patch.object(script_to_video, "OUT_DIR", output_root))
            stack.enter_context(mock.patch.object(cli_uploads, "UPLOAD_ROOT", upload_root))
            stack.enter_context(mock.patch.object(
                core, "verify",
                lambda _token: {
                    "username": "fang", "must_change": False, "points": 1000,
                },
            ))
            stack.enter_context(mock.patch.object(
                core, "_domains", lambda: (None, points, video),
            ))
            stack.enter_context(mock.patch.object(
                core, "_dispatch_short_drama", lambda *_args, **_kwargs: False,
            ))
            stack.enter_context(mock.patch.object(
                core, "_short_drama_domain", lambda: fake_short_drama,
            ))
            stack.enter_context(mock.patch.object(
                core.feature_flags, "require_enabled", lambda _kind: None,
            ))
            stack.enter_context(mock.patch.object(
                core.miniprogram_security, "check_payload", lambda _body: None,
            ))
            stack.enter_context(mock.patch.object(
                upstream_guard, "exhausted_reason", lambda *_args: None,
            ))
            stack.enter_context(mock.patch.object(
                core.cli_gateway, "reject_changed_cost", lambda *_args: False,
            ))
            stack.enter_context(mock.patch.object(
                core, "enqueue_job", lambda *_args: True,
            ))
            stack.enter_context(mock.patch.object(
                video, "record_video_pending_asset", lambda *_args: None,
            ))
            stack.enter_context(mock.patch.object(
                core, "MAX_USER_ACTIVE_JOBS", 5,
            ))
            stack.enter_context(mock.patch.object(
                core, "HANDLERS",
                {"script_to_video": script_to_video.gen_script_to_video},
            ))
            core._shutting_down.clear()

            with closing(sqlite3.connect(database)) as connection:
                connection.execute("""CREATE TABLE jobs(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT, username TEXT, cost INTEGER,
                    status TEXT DEFAULT 'pending', payload TEXT,
                    result TEXT, error TEXT, created_at INTEGER,
                    updated_at INTEGER, deleted INTEGER DEFAULT 0,
                    refunded INTEGER DEFAULT 0, owner TEXT
                )""")
                connection.commit()

            server = ThreadingHTTPServer(("127.0.0.1", 0), core.H)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            stack.callback(server.server_close)
            stack.callback(server.shutdown)
            endpoint = "http://127.0.0.1:%d/api/gen/script_to_video" % (
                server.server_address[1],
            )

            def post(body, key=""):
                headers = {
                    "Authorization": "Bearer test",
                    "Content-Type": "application/json",
                }
                if key:
                    headers["Idempotency-Key"] = key
                request = urllib.request.Request(
                    endpoint,
                    data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                    method="POST",
                    headers=headers,
                )
                try:
                    with urllib.request.urlopen(request, timeout=5) as response:
                        return response.status, json.loads(response.read())
                except urllib.error.HTTPError as error:
                    return error.code, json.loads(error.read() or b"{}")

            def get_job(job_id):
                request = urllib.request.Request(
                    endpoint.rsplit("/", 1)[0] + "/job/" + str(job_id),
                    headers={"Authorization": "Bearer test"},
                )
                try:
                    with urllib.request.urlopen(request, timeout=5) as response:
                        return response.status, json.loads(response.read())
                except urllib.error.HTTPError as error:
                    return error.code, json.loads(error.read() or b"{}")

            request = {
                "pipeline": "smart_montage",
                "copy": "专业评估理解真实需求，温和护理让状态自然稳定。",
                "style": "luxe",
                "ratio": "16:9",
            }
            preview = script_to_video.smart_montage_plan_response(request)
            request["plan_digest"] = preview["plan_digest"]

            status, response = post(request)
            self.assertEqual(400, status)
            self.assertEqual("idempotency_key_required", response["code"])
            self.assertEqual([], points.deductions)

            key = "smart-montage-luxe-http-0001"
            status, accepted = post(request, key)
            self.assertEqual(200, status)
            expected_full_cost = 10 + 20 * preview["plan"]["scene_count"]
            self.assertEqual(expected_full_cost, accepted["cost"])
            self.assertEqual(
                preview["plan"]["scene_count"],
                accepted["cost_breakdown"]["material_generate_count"],
            )
            self.assertEqual(1, len(points.deductions))
            self.assertTrue(points.deductions[0][3].endswith(":" + key))

            canonical_request = script_to_video.normalize_smart_montage_submission(
                request,
            )
            with closing(core.jdb()) as connection:
                claim = connection.execute(
                    "SELECT request_hash,response_json FROM submission_idempotency "
                    "WHERE username='fang' AND endpoint=? AND idem_key=?",
                    ("/api/gen/script_to_video", key),
                ).fetchone()
                job_payload = json.loads(connection.execute(
                    "SELECT payload FROM jobs WHERE id=?", (accepted["job_id"],),
                ).fetchone()[0])
            self.assertEqual(
                submission_idempotency._request_hash(canonical_request),
                claim["request_hash"],
            )
            self.assertNotEqual(
                submission_idempotency._request_hash(job_payload),
                claim["request_hash"],
            )

            # A completed request replays before the versioned planner runs.
            with mock.patch.object(
                script_video_montage,
                "plan_script_video",
                side_effect=AssertionError("planner must not run during replay"),
            ):
                status, replay = post(request, key)
            self.assertEqual(200, status)
            self.assertEqual(accepted, replay)
            self.assertEqual(1, len(points.deductions))

            def approved_upload(raw_bytes, username="fang"):
                uploaded = cli_uploads.store_image(
                    io.BytesIO(raw_bytes), len(raw_bytes), username, "image/png",
                    hashlib.sha256(raw_bytes).hexdigest(),
                )
                cli_uploads.approve_image(
                    uploaded["upload_id"], username, "smart_montage",
                )
                return uploaded

            first = approved_upload(PNG_ONE)
            second = approved_upload(PNG_TWO)
            slots = [first["upload_id"], second["upload_id"]] + [
                None
            ] * (preview["plan"]["scene_count"] - 2)
            material_request = dict(request, material_upload_ids=slots)
            material_key = "smart-montage-luxe-material-http-0001"
            deductions_before = len(points.deductions)
            status, material_accepted = post(material_request, material_key)
            self.assertEqual(200, status)
            self.assertEqual(
                10 + 20 * (preview["plan"]["scene_count"] - 2),
                material_accepted["cost"],
            )
            self.assertEqual(
                2, material_accepted["cost_breakdown"]["material_reused_count"],
            )
            self.assertEqual(deductions_before + 1, len(points.deductions))
            with closing(core.jdb()) as connection:
                material_payload = json.loads(connection.execute(
                    "SELECT payload FROM jobs WHERE id=?",
                    (material_accepted["job_id"],),
                ).fetchone()[0])
            self.assertEqual(
                ["upload", "upload"],
                [item["source"] for item in material_payload["material_plan"][:2]],
            )
            frozen_files = [
                output_root / item["file"]
                for item in material_payload["material_plan"]
                if item["source"] == "upload"
            ]
            self.assertTrue(all(path.is_file() for path in frozen_files))

            status, material_replay = post(material_request, material_key)
            self.assertEqual(200, status)
            self.assertEqual(material_accepted, material_replay)
            self.assertEqual(deductions_before + 1, len(points.deductions))
            swapped = list(slots)
            swapped[0], swapped[1] = swapped[1], swapped[0]
            status, conflict = post(
                dict(request, material_upload_ids=swapped), material_key,
            )
            self.assertEqual(409, status)
            self.assertEqual("idempotency_conflict", conflict["code"])

            foreign = approved_upload(PNG_ONE, username="other")
            status, unavailable = post(dict(
                request,
                material_upload_ids=[foreign["upload_id"]]
                + [None] * (preview["plan"]["scene_count"] - 1),
            ), "smart-montage-foreign-material")
            self.assertEqual(409, status)
            self.assertEqual("material_upload_unavailable", unavailable["code"])
            self.assertEqual(deductions_before + 1, len(points.deductions))

            script_to_video.cleanup_smart_montage_uploads(material_payload)
            self.assertTrue(all(not path.exists() for path in frozen_files))
            with closing(core.jdb()) as connection:
                connection.execute(
                    "UPDATE jobs SET status='done' WHERE id=?",
                    (material_accepted["job_id"],),
                )
                connection.commit()

            all_uploads = []
            for index in range(preview["plan"]["scene_count"]):
                buffer = io.BytesIO()
                Image.new(
                    "RGB", (2, 2), ((index * 37) % 255, 80, 160),
                ).save(buffer, format="PNG")
                all_uploads.append(approved_upload(buffer.getvalue()))
            all_material_request = dict(
                request,
                material_upload_ids=[item["upload_id"] for item in all_uploads],
            )

            def image_upstream_only(kind, _body):
                return "图片上游不可用" if kind == "image" else None

            with mock.patch.object(
                upstream_guard, "exhausted_reason", side_effect=image_upstream_only,
            ):
                status, all_material_accepted = post(
                    all_material_request, "smart-montage-all-user-materials",
                )
            self.assertEqual(200, status)
            self.assertEqual(10, all_material_accepted["cost"])
            self.assertEqual(
                0,
                all_material_accepted["cost_breakdown"]["material_generate_count"],
            )
            self.assertEqual(
                preview["plan"]["scene_count"],
                all_material_accepted["cost_breakdown"]["material_reused_count"],
            )
            with closing(core.jdb()) as connection:
                all_material_payload = json.loads(connection.execute(
                    "SELECT payload FROM jobs WHERE id=?",
                    (all_material_accepted["job_id"],),
                ).fetchone()[0])
                connection.execute(
                    "UPDATE jobs SET status='done' WHERE id=?",
                    (all_material_accepted["job_id"],),
                )
                connection.commit()
            script_to_video.cleanup_smart_montage_uploads(all_material_payload)

            # If Auth books the idempotent transaction but its response is
            # lost, retrying the same submission key must create exactly one
            # job without a second points deduction.
            lost_response_key = "smart-montage-auth-response-lost"
            deductions_before = len(points.deductions)
            points.fail_next_deduct_after_booking = True
            status, lost_response = post(request, lost_response_key)
            self.assertEqual(502, status)
            self.assertEqual("upstream_error", lost_response["code"])
            self.assertEqual(deductions_before + 1, len(points.deductions))
            status, recovered_charge = post(request, lost_response_key)
            self.assertEqual(200, status)
            self.assertIsInstance(recovered_charge["job_id"], int)
            self.assertEqual(deductions_before + 1, len(points.deductions))
            with closing(core.jdb()) as connection:
                connection.execute(
                    "UPDATE jobs SET status='done' WHERE id=?",
                    (recovered_charge["job_id"],),
                )
                connection.commit()

            # The durable attempt owns hard-linked material files.  Auth may
            # commit and lose its response, then the original temporary upload
            # may be deleted before the browser retries; recovery must not need
            # that upload ID again.
            crash_buffer = io.BytesIO()
            Image.new("RGB", (3, 3), (31, 113, 207)).save(
                crash_buffer, format="PNG",
            )
            crash_upload = approved_upload(crash_buffer.getvalue())
            crash_slots = [crash_upload["upload_id"]] + [
                None
            ] * (preview["plan"]["scene_count"] - 1)
            crash_material_request = dict(
                request, material_upload_ids=crash_slots,
            )
            crash_material_key = "smart-montage-crash-material-recovery"
            deductions_before_crash_material = len(points.deductions)
            points.fail_next_deduct_after_booking = True
            status, ambiguous_material = post(
                crash_material_request, crash_material_key,
            )
            self.assertEqual(502, status)
            self.assertEqual("upstream_error", ambiguous_material["code"])
            canonical_crash_material = (
                script_to_video.normalize_smart_montage_submission(
                    crash_material_request,
                )
            )
            durable_material = submission_idempotency.load_attempt(
                core.jdb, "fang", "/api/gen/script_to_video",
                crash_material_key, canonical_crash_material,
            )
            self.assertEqual("frozen", durable_material["state"])
            frozen_material_file = output_root / next(
                item["file"] for item in durable_material["payload"]["material_plan"]
                if item["source"] == "upload"
            )
            self.assertTrue(frozen_material_file.is_file())
            with mock.patch.object(
                cli_uploads, "cleanup_expired_uploads",
            ), mock.patch.object(
                script_to_video, "cleanup_orphaned_smart_montage_roots",
                return_value=0,
            ) as cleanup_smart_roots:
                core._cleanup_temporary_materials()
            protected_payloads = cleanup_smart_roots.call_args.args[0]
            frozen_material_rel = frozen_material_file.relative_to(
                output_root,
            ).as_posix()
            self.assertTrue(any(
                any(
                    item.get("file") == frozen_material_rel
                    for item in payload.get("material_plan") or []
                    if isinstance(item, dict)
                )
                for payload in protected_payloads
            ))
            self.assertTrue(cli_uploads.discard_image(
                crash_upload["upload_id"], "fang",
            ))
            status, recovered_material = post(
                crash_material_request, crash_material_key,
            )
            self.assertEqual(200, status)
            self.assertEqual(
                deductions_before_crash_material + 1,
                len(points.deductions),
            )
            with closing(core.jdb()) as connection:
                recovered_payload = json.loads(connection.execute(
                    "SELECT payload FROM jobs WHERE id=?",
                    (recovered_material["job_id"],),
                ).fetchone()[0])
                connection.execute(
                    "UPDATE jobs SET status='done' WHERE id=?",
                    (recovered_material["job_id"],),
                )
                connection.commit()
            self.assertEqual(
                frozen_material_file.relative_to(output_root).as_posix(),
                recovered_payload["material_plan"][0]["file"],
            )

            # A definitive Auth rejection is the opposite case: no ledger
            # mutation occurred, so the claim and frozen hard links are removed
            # and the same upload may be submitted in a fresh attempt.
            rejected_buffer = io.BytesIO()
            Image.new("RGB", (3, 3), (180, 41, 92)).save(
                rejected_buffer, format="PNG",
            )
            rejected_upload = approved_upload(rejected_buffer.getvalue())
            rejected_request = dict(
                request,
                material_upload_ids=[rejected_upload["upload_id"]] + [
                    None
                ] * (preview["plan"]["scene_count"] - 1),
            )
            rejected_key = "smart-montage-definitive-charge-rejection"
            private_root = output_root / "_smart_materials"
            private_before_rejection = (
                set(private_root.rglob("*")) if private_root.exists() else set()
            )
            deductions_before_rejection = len(points.deductions)
            points.reject_next_deduct = True
            status, rejected = post(rejected_request, rejected_key)
            self.assertEqual(402, status)
            self.assertEqual(deductions_before_rejection, len(points.deductions))
            private_after_rejection = (
                set(private_root.rglob("*")) if private_root.exists() else set()
            )
            self.assertEqual(private_before_rejection, private_after_rejection)
            with closing(core.jdb()) as connection:
                self.assertIsNone(connection.execute(
                    "SELECT 1 FROM submission_idempotency WHERE idem_key=?",
                    (rejected_key,),
                ).fetchone())

            # Simulate a process death after Auth confirmation was durably
            # recorded but before INSERT jobs.  The retry must insert exactly
            # one job without contacting the mutating charge path again.
            crash_after_charge_key = "smart-montage-crash-after-charge"
            deductions_before_after_charge = len(points.deductions)
            original_mark_charged = core._idempotency_mark_charged

            def crash_after_charge(*args, **kwargs):
                result = original_mark_charged(*args, **kwargs)
                raise RuntimeError("simulated crash after charge journal")

            with mock.patch.object(
                core, "_idempotency_mark_charged",
                side_effect=crash_after_charge,
            ):
                try:
                    post(request, crash_after_charge_key)
                except Exception:
                    pass
            charged_attempt = submission_idempotency.load_attempt(
                core.jdb, "fang", "/api/gen/script_to_video",
                crash_after_charge_key,
                script_to_video.normalize_smart_montage_submission(request),
            )
            self.assertEqual("charged", charged_attempt["state"])
            self.assertIsNone(charged_attempt["job_id"])
            status, recovered_after_charge = post(
                request, crash_after_charge_key,
            )
            self.assertEqual(200, status)
            self.assertEqual(
                deductions_before_after_charge + 1,
                len(points.deductions),
            )
            with closing(core.jdb()) as connection:
                connection.execute(
                    "UPDATE jobs SET status='done' WHERE id=?",
                    (recovered_after_charge["job_id"],),
                )
                connection.commit()

            # Commit of jobs and its attempt link is atomic, but asset
            # registration and in-memory enqueue necessarily follow it.  A
            # retry of a linked attempt must finish both side effects.
            crash_before_delivery_key = "smart-montage-crash-before-delivery"
            deductions_before_delivery = len(points.deductions)
            original_create_job_after_charge = jobs_store.create_job_after_charge
            recovered_asset = mock.Mock()
            recovered_enqueue = mock.Mock(return_value=True)

            def crash_after_job_commit(*args, **kwargs):
                original_create_job_after_charge(*args, **kwargs)
                raise RuntimeError("simulated crash after job commit")

            with mock.patch.object(
                video, "record_video_pending_asset", recovered_asset,
            ), mock.patch.object(
                core, "enqueue_job", recovered_enqueue,
            ):
                with mock.patch.object(
                    jobs_store, "create_job_after_charge",
                    side_effect=crash_after_job_commit,
                ):
                    try:
                        post(request, crash_before_delivery_key)
                    except Exception:
                        pass
                self.assertEqual(0, recovered_asset.call_count)
                self.assertEqual(0, recovered_enqueue.call_count)
                delivery_attempt = submission_idempotency.load_attempt(
                    core.jdb, "fang", "/api/gen/script_to_video",
                    crash_before_delivery_key,
                    script_to_video.normalize_smart_montage_submission(request),
                )
                self.assertEqual("linked", delivery_attempt["state"])
                status, recovered_delivery = post(
                    request, crash_before_delivery_key,
                )
                self.assertEqual(200, status)
                self.assertEqual(
                    delivery_attempt["job_id"], recovered_delivery["job_id"],
                )
                self.assertEqual(1, recovered_asset.call_count)
                self.assertEqual(1, recovered_enqueue.call_count)
            self.assertEqual(
                deductions_before_delivery + 1, len(points.deductions),
            )
            with closing(core.jdb()) as connection:
                connection.execute(
                    "UPDATE jobs SET status='done' WHERE id=?",
                    (recovered_delivery["job_id"],),
                )
                connection.commit()

            # Simulate death after the job+attempt transaction committed but
            # before response_json was written.  Linked replay must recover the
            # same job ID and finish the normal asset/enqueue path.
            crash_after_link_key = "smart-montage-crash-after-link"
            deductions_before_after_link = len(points.deductions)
            original_complete = core._idempotency_complete

            def crash_before_response(username, endpoint_path, idem_key, value):
                if idem_key == crash_after_link_key:
                    raise RuntimeError("simulated crash before response journal")
                return original_complete(
                    username, endpoint_path, idem_key, value,
                )

            with mock.patch.object(
                core, "_idempotency_complete",
                side_effect=crash_before_response,
            ):
                try:
                    post(request, crash_after_link_key)
                except Exception:
                    pass
            linked_attempt = submission_idempotency.load_attempt(
                core.jdb, "fang", "/api/gen/script_to_video",
                crash_after_link_key,
                script_to_video.normalize_smart_montage_submission(request),
            )
            self.assertEqual("linked", linked_attempt["state"])
            self.assertIsInstance(linked_attempt["job_id"], int)
            with mock.patch.object(
                upstream_guard, "exhausted_reason",
                return_value="simulated upstream breaker",
            ):
                status, recovered_after_link = post(
                    request, crash_after_link_key,
                )
            self.assertEqual(200, status)
            self.assertEqual(
                linked_attempt["job_id"], recovered_after_link["job_id"],
            )
            self.assertEqual(
                deductions_before_after_link + 1,
                len(points.deductions),
            )
            with closing(core.jdb()) as connection:
                connection.execute(
                    "UPDATE jobs SET status='done' WHERE id=?",
                    (recovered_after_link["job_id"],),
                )
                connection.commit()

            original_planner = script_video_montage.plan_script_video

            def upgraded_planner(payload):
                plan = original_planner(payload)
                plan["planner_version"] += "_upgraded"
                plan["scenes"][0]["headline"] += "新"
                return plan

            deductions_before_stale = len(points.deductions)
            with closing(core.jdb()) as connection:
                idempotency_before_stale = connection.execute(
                    "SELECT COUNT(*) FROM submission_idempotency"
                ).fetchone()[0]
            with mock.patch.object(
                script_video_montage,
                "plan_script_video",
                side_effect=upgraded_planner,
            ):
                status, stale = post(
                    request, "smart-montage-luxe-http-0002",
                )
            self.assertEqual(409, status)
            self.assertEqual("plan_digest_mismatch", stale["code"])
            self.assertEqual(deductions_before_stale, len(points.deductions))
            with closing(core.jdb()) as connection:
                self.assertEqual(
                    idempotency_before_stale,
                    connection.execute(
                        "SELECT COUNT(*) FROM submission_idempotency"
                    ).fetchone()[0],
                )

            # A charged local INSERT failure is retryable state, not a refund
            # event.  The durable attempt keeps both the one confirmed charge
            # and its frozen payload until INSERT+link succeeds atomically.
            with closing(core.jdb()) as connection:
                connection.execute("""CREATE TRIGGER reject_pending_smart_montage
                    BEFORE INSERT ON jobs WHEN NEW.status='pending'
                    BEGIN SELECT RAISE(FAIL, 'insert failed'); END""")
                connection.commit()
            pending_key = "smart-montage-insert-retryable"
            deductions_before = len(points.deductions)
            refunds_before = len(points.refunds)
            status, pending = post(request, pending_key)
            self.assertEqual(503, status)
            self.assertEqual("job_create_retryable", pending["code"])
            self.assertEqual(deductions_before + 1, len(points.deductions))
            self.assertEqual(refunds_before, len(points.refunds))
            pending_attempt = submission_idempotency.load_attempt(
                core.jdb, "fang", "/api/gen/script_to_video", pending_key,
                script_to_video.normalize_smart_montage_submission(request),
            )
            self.assertEqual("charged", pending_attempt["state"])
            self.assertIsNone(pending_attempt["job_id"])

            status, pending_replay = post(request, pending_key)
            self.assertEqual(503, status)
            self.assertEqual(pending, pending_replay)
            self.assertEqual(deductions_before + 1, len(points.deductions))
            self.assertEqual(refunds_before, len(points.refunds))

            with closing(core.jdb()) as connection:
                connection.execute("DROP TRIGGER reject_pending_smart_montage")
                connection.commit()
            status, inserted_retry = post(request, pending_key)
            self.assertEqual(200, status)
            self.assertIsInstance(inserted_retry["job_id"], int)
            self.assertEqual(deductions_before + 1, len(points.deductions))
            self.assertEqual(refunds_before, len(points.refunds))
            with closing(core.jdb()) as connection:
                connection.execute(
                    "UPDATE jobs SET status='done' WHERE id=?",
                    (inserted_retry["job_id"],),
                )
                connection.commit()

            # A full render queue has already created and charged its job.  If
            # refund confirmation is unavailable, return the same queryable
            # tracker contract instead of the rotatable queue_full error.
            points.fail_refunds = True
            queue_key = "smart-montage-queue-refund-pending"
            deductions_before = len(points.deductions)
            with mock.patch.object(core, "enqueue_job", lambda *_args: False):
                status, queue_pending = post(request, queue_key)
            self.assertEqual(202, status)
            self.assertEqual("pending", queue_pending["refund_state"])
            self.assertEqual(deductions_before + 1, len(points.deductions))
            status, queue_job = get_job(queue_pending["job_id"])
            self.assertEqual(200, status)
            self.assertEqual("pending", queue_job["refund_state"])
            with mock.patch.object(core, "enqueue_job", lambda *_args: False):
                status, queue_replay = post(request, queue_key)
            self.assertEqual(202, status)
            self.assertEqual(queue_pending, queue_replay)
            self.assertEqual(deductions_before + 1, len(points.deductions))

            # Once Auth has explicitly confirmed the refund, queue_full becomes
            # terminal and it is safe for the client to rotate the key.
            points.fail_refunds = False
            refunded_queue_key = "smart-montage-queue-refund-confirmed"
            deductions_before = len(points.deductions)
            private_root = output_root / "_smart_materials"
            private_before = set(private_root.rglob("*")) if private_root.exists() else set()
            with mock.patch.object(core, "enqueue_job", lambda *_args: False):
                status, refunded_queue = post(material_request, refunded_queue_key)
            self.assertEqual(429, status)
            self.assertEqual("queue_full", refunded_queue["code"])
            self.assertIs(True, refunded_queue["operation_terminal"])
            self.assertEqual(deductions_before + 1, len(points.deductions))
            private_after = set(private_root.rglob("*")) if private_root.exists() else set()
            self.assertEqual(private_before, private_after)
            with mock.patch.object(core, "enqueue_job", lambda *_args: False):
                status, refunded_queue_replay = post(
                    material_request, refunded_queue_key,
                )
            self.assertEqual(429, status)
            self.assertEqual(refunded_queue, refunded_queue_replay)
            self.assertEqual(deductions_before + 1, len(points.deductions))

            # Asset registration is another post-charge failure and follows the
            # identical durable-refund contract.
            points.fail_refunds = True
            asset_key = "smart-montage-asset-refund-pending"
            deductions_before = len(points.deductions)
            with mock.patch.object(
                video, "record_video_pending_asset",
                side_effect=RuntimeError("asset registration failed"),
            ):
                status, asset_pending = post(request, asset_key)
            self.assertEqual(202, status)
            self.assertEqual("pending", asset_pending["refund_state"])
            self.assertEqual(deductions_before + 1, len(points.deductions))
            status, asset_job = get_job(asset_pending["job_id"])
            self.assertEqual(200, status)
            self.assertEqual("pending", asset_job["refund_state"])
            status, asset_replay = post(request, asset_key)
            self.assertEqual(202, status)
            self.assertEqual(asset_pending, asset_replay)
            self.assertEqual(deductions_before + 1, len(points.deductions))

if __name__ == "__main__":
    unittest.main()
