import base64
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


class XiaoleVideoTests(unittest.TestCase):
    def setUp(self):
        server_dir = str(Path(__file__).resolve().parents[1] / "server")
        if server_dir not in sys.path:
            sys.path.insert(0, server_dir)
        from content_domains import video
        self.video = video

    def test_generate_xiaole_video_sends_size_without_aspect_ratio(self):
        calls = []

        def fake_request(method, path, body=None, timeout=90):
            calls.append((method, path, body, timeout))
            if method == "POST":
                return {"code": 200, "data": {"request_id": "rid-1", "status_url": "/status/rid-1"}}
            return {"data": {"status": "completed", "output": {"videos": [{"url": "https://cdn.example/video.mp4"}]}}}

        with patch.object(self.video, "_xiaole_request", side_effect=fake_request), \
             patch.object(self.video, "_download_xiaole_video", return_value="video/grok_demo.mp4"), \
             patch.object(self.video, "GROK_VIDEO_PROVIDER", "xiaole"):
            result = self.video.generate_xiaole_video("Grok Image Video", "demo", size="1280x720", prefix="grok")

        self.assertEqual(result["video_file"], "video/grok_demo.mp4")
        self.assertEqual(calls[0][2]["input"]["size"], "1280x720")
        self.assertNotIn("aspect_ratio", calls[0][2]["input"])

    def test_xiaole_download_candidates_prefers_tunnel_over_relay(self):
        import os as _os
        url = "https://vidgen.x.ai/abc/video.mp4"
        with patch.dict(_os.environ, {"HEYGEN_RELAY_BASE": "https://heygen.zelong.vip"}, clear=False):
            cands = self.video._xiaole_download_candidates(url, "http://127.0.0.1:10809")
        # ① 快隧道优先：原始 URL + 隧道代理
        self.assertEqual(cands[0][0], url)
        self.assertEqual(cands[0][2], "http://127.0.0.1:10809")
        # ② heygen 中转兜底：走 relay /cdn/，不强制代理(None)
        self.assertIn("heygen.zelong.vip/cdn/vidgen.x.ai/", cands[1][0])
        self.assertIsNone(cands[1][2])
        # ③ 最后直连原始 URL
        self.assertEqual(cands[-1][0], url)
        self.assertIsNone(cands[-1][2])

    def test_xiaole_download_candidates_no_tunnel_is_legacy_order(self):
        import os as _os
        url = "https://vidgen.x.ai/abc/video.mp4"
        with patch.dict(_os.environ, {"HEYGEN_RELAY_BASE": "https://heygen.zelong.vip"}, clear=False):
            cands = self.video._xiaole_download_candidates(url, "")
        # 无隧道 → 退化为老行为：heygen 中转在前、直连兜底，无隧道档
        self.assertNotIn("10809", str(cands))
        self.assertIn("heygen.zelong.vip/cdn/", cands[0][0])
        self.assertIsNone(cands[0][2])
        self.assertEqual(cands[-1][0], url)
        self.assertIsNone(cands[-1][2])

    def test_authenticated_download_header_is_not_forwarded_to_relay(self):
        import os as _os
        url = "https://openrouter.ai/api/v1/videos/job/content?index=0"
        with patch.dict(_os.environ, {"HEYGEN_RELAY_BASE": "https://relay.example"}, clear=False):
            cands = self.video._xiaole_download_candidates(
                url, "http://127.0.0.1:10809",
                origin_headers={"Authorization": "Bearer secret"},
            )
        self.assertEqual(cands[0][1]["Authorization"], "Bearer secret")
        self.assertNotIn("Authorization", cands[1][1])
        self.assertEqual(cands[-1][1]["Authorization"], "Bearer secret")

    def test_cross_origin_redirect_strips_authorization(self):
        import urllib.request
        handler = self.video._OriginAuthRedirectHandler()
        request = urllib.request.Request(
            "https://openrouter.ai/api/v1/videos/job/content",
            headers={"Authorization": "Bearer secret"},
        )
        redirected = handler.redirect_request(
            request, None, 302, "Found", {}, "https://cdn.example/video.mp4"
        )
        self.assertNotIn("Authorization", redirected.headers)

    def test_gen_xiaole_video_maps_ratio_to_size_and_defaults_unknown_ratio(self):
        calls = []

        def fake_request(method, path, body=None, timeout=90):
            calls.append((method, path, body, timeout))
            if method == "POST":
                return {"code": 200, "data": {"request_id": "rid-1", "status_url": "/status/rid-1"}}
            return {"data": {"status": "completed", "output": {"videos": [{"url": "https://cdn.example/video.mp4"}]}}}

        with patch.object(self.video, "_xiaole_request", side_effect=fake_request), \
             patch.object(self.video, "_download_xiaole_video", return_value="video/grok_demo.mp4"), \
             patch.object(self.video, "GROK_VIDEO_PROVIDER", "xiaole"):
            ok = self.video.gen_xiaole_video({"channel": "grok", "prompt": "demo", "ratio": "1:1"})
            fallback = self.video.gen_xiaole_video({"channel": "grok", "prompt": "demo", "ratio": "2:3"})

        self.assertEqual(ok["ratio"], "1:1")
        self.assertEqual(calls[0][2]["input"]["size"], "1024x1024")
        self.assertEqual(fallback["ratio"], "9:16")
        self.assertEqual(calls[2][2]["input"]["size"], "720x1280")
        self.assertNotIn("aspect_ratio", calls[0][2]["input"])
        self.assertNotIn("aspect_ratio", calls[2][2]["input"])

    def test_xiaole_ratio_channel_error_matches_supplier_size_message(self):
        self.assertTrue(self.video._is_xiaole_ratio_channel_error(
            '视频接口失败: HTTP 404 {"code":404,"message":"当前模型暂无支持该视频参数的可用渠道：渠道不支持当前视频尺寸"}'
        ))

    def test_generate_xiaole_video_normalizes_supplier_size_error(self):
        with patch.object(
            self.video,
            "_xiaole_request",
            side_effect=RuntimeError('视频接口失败: HTTP 404 {"code":404,"message":"当前模型暂无支持该视频参数的可用渠道：渠道不支持当前视频尺寸"}')
        ):
            with self.assertRaisesRegex(RuntimeError, "当前仅部分比例可用，请优先尝试 16:9（横屏）"):
                self.video.generate_xiaole_video("Grok Image Video", "demo", size="720x1280", prefix="grok")

    def test_validate_micro_duration(self):
        for duration in (5, 10, 15):
            body = self.video.validate_xiaole_video_payload({
                "channel": "micro", "prompt": "cinematic demo", "duration": duration,
            })
            self.assertEqual(body["duration"], duration)
        with self.assertRaisesRegex(ValueError, "5、10 或 15"):
            self.video.validate_xiaole_video_payload({
                "channel": "micro", "prompt": "cinematic demo", "duration": 7,
            })

    def test_validate_micro_uses_official_seedance_contract(self):
        body = self.video.validate_xiaole_video_payload({
            "channel": "micro", "prompt": "cinematic demo", "duration": 15,
            "reference_images": ["https://example.com/ref.jpg"],
        })
        self.assertEqual(body["model"], "doubao-seedance-2-0-260128")
        self.assertEqual(body["duration"], 15)
        self.assertEqual(body["ratio"], "9:16")
        self.assertEqual(body["resolution"], "720p")

    @staticmethod
    def _seedance_png_data(tag=b""):
        import io as _io
        from PIL import Image
        shade = (tag[0] if tag else 0) % 256
        buf = _io.BytesIO()
        Image.new("RGB", (8, 8), (shade, 128, 255 - shade)).save(buf, "PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")

    def _stage_mocks(self, put_side_effect=None):
        from content_domains import cos

        signed = "https://bucket-1250000000.cos.ap-guangzhou.myqcloud.com/seedance/reference/x?q-sign-algorithm=sha1&q-sign-time=1"
        return [
            patch.object(self.video, "seedance_reference_upload_is_open", return_value=True),
            patch.object(cos, "enabled", return_value=True),
            patch.object(cos, "put_bytes", side_effect=put_side_effect),
            patch.object(self.video, "_seedance_cos_presign", return_value=signed),
            patch.object(self.video, "_persist_staging_cleanup_intent"),
            patch.object(self.video, "_enqueue_pending_cleanup"),
            patch.object(self.video, "_remove_cleanup_record"),
        ]

    def test_validate_micro_keeps_data_images_local_until_staging(self):
        from content_domains import cos

        refs = [self._seedance_png_data(str(index).encode("ascii")) for index in range(4)]
        with patch.object(cos, "put_bytes") as put:
            body = self.video.validate_xiaole_video_payload({
                "channel": "micro", "prompt": "demo", "duration": 5,
                "reference_images": refs,
            }, "fang")
        put.assert_not_called()   # 校验阶段不做任何网络上传
        self.assertEqual(refs, body["reference_images"])

    def test_stage_seedance_references_uploads_private_and_returns_cos_keys(self):
        refs = [self._seedance_png_data(str(index).encode("ascii")) for index in range(4)]
        patches = self._stage_mocks()
        with patches[0], patches[1], patches[2] as put, patches[3] as presign, \
             patches[4], patches[5], patches[6]:
            first = self.video.validate_xiaole_video_payload({
                "channel": "micro", "prompt": "demo", "duration": 5, "reference_images": refs}, "fang")
            second = self.video.validate_xiaole_video_payload({
                "channel": "micro", "prompt": "demo", "duration": 5, "reference_images": refs}, "fang")
            first_keys = self.video.stage_seedance_references(first, "fang", "idem-token-a")
            second_keys = self.video.stage_seedance_references(second, "fang", "idem-token-b")
            repeat = self.video.validate_xiaole_video_payload({
                "channel": "micro", "prompt": "demo", "duration": 5, "reference_images": refs}, "fang")
            repeat_keys = self.video.stage_seedance_references(repeat, "fang", "idem-token-a")

        self.assertEqual(12, put.call_count)
        for call in put.call_args_list:
            self.assertIs(call.kwargs.get("private"), True)   # 强制私有 ACL
        presign.assert_not_called()   # 提交时不签名，签名推迟到 worker 提交时
        first_keys_uploaded = [call.args[1] for call in put.call_args_list[:4]]
        second_keys_uploaded = [call.args[1] for call in put.call_args_list[4:8]]
        repeat_keys_uploaded = [call.args[1] for call in put.call_args_list[8:]]
        # 跨提交不共享对象键（消除失败清理竞态）；同幂等键重试覆盖同一对象
        self.assertNotEqual(first_keys_uploaded, second_keys_uploaded)
        self.assertNotEqual(first_keys_uploaded, repeat_keys_uploaded)
        self.assertEqual(4, len(set(first_keys_uploaded)))
        self.assertRegex(first_keys_uploaded[0],
                         r"^seedance/reference/[0-9a-f]{16}/[0-9a-f]{32}-[0-9a-f]{16}\.png$")
        self.assertEqual(first_keys, first_keys_uploaded)
        self.assertEqual(first_keys, first["_seedance_staged_keys"])   # 随 payload 落库供终态清理
        # payload 只存 cos-key:// 内部引用，不存签名 URL；跨提交引用不同（键不同）
        self.assertEqual(["cos-key://" + k for k in first_keys], first["reference_images"])
        self.assertNotEqual(first["reference_images"], second["reference_images"])
        self.assertNotIn("data:", str(first["reference_images"]))
        self.assertNotIn("q-sign-algorithm", str(first["reference_images"]))

    def test_staging_token_is_unique_per_physical_attempt(self):
        # 仅标点不同的两个合法 Idempotency-Key 不得映射为同一 token
        first = self.video._seedance_staging_token("same-idempotency-key")
        retry = self.video._seedance_staging_token("same-idempotency-key")
        self.assertNotEqual(first, retry)
        self.assertRegex(first, r"^[0-9a-f]{32}$")
        self.assertRegex(retry, r"^[0-9a-f]{32}$")

    def test_duplicate_images_in_one_batch_get_distinct_object_keys(self):
        import sqlite3
        import tempfile
        from contextlib import closing as _closing
        from content_domains import core, cos

        image = self._seedance_png_data(b"same")
        with tempfile.TemporaryDirectory() as td, \
             patch.object(core, "JOB_DB", str(Path(td) / "jobs.db")), \
             patch.object(self.video, "_cleanup_table_ready", False), \
             patch.object(self.video, "seedance_reference_upload_is_open", return_value=True), \
             patch.object(cos, "put_bytes") as put:
            body = self.video.validate_xiaole_video_payload({
                "channel": "micro", "prompt": "demo", "duration": 5,
                "reference_images": [image, image],
            }, "fang")
            keys = self.video.stage_seedance_references(
                body, "fang", "same-idempotency-key"
            )
            self.assertEqual(2, put.call_count)
            self.assertEqual(2, len(keys))
            self.assertEqual(2, len(set(keys)))
            with _closing(sqlite3.connect(core.JOB_DB)) as db:
                self.assertEqual(
                    2, db.execute("SELECT COUNT(*) FROM seedance_pending_cleanup").fetchone()[0]
                )

    def test_validate_micro_strips_client_internal_fields(self):
        body = self.video.validate_xiaole_video_payload({
            "channel": "micro", "prompt": "demo", "duration": 5,
            "_seedance_staged_keys": ["seedance/reference/0000000000000000/evil.png"],
            "_username": "admin", "_job_id": 1,
        }, "fang")
        self.assertNotIn("_seedance_staged_keys", body)   # 注入的暂存键不得进入任务 payload
        self.assertNotIn("_username", body)
        self.assertNotIn("_job_id", body)

    def test_validate_micro_rejects_client_cos_key_reference(self):
        with self.assertRaisesRegex(ValueError, "公网|asset://"):
            self.video.validate_xiaole_video_payload({
                "channel": "micro", "prompt": "demo", "duration": 5,
                "reference_images": ["cos-key://seedance/reference/0000000000000000/evil.png"],
            }, "fang")

    def test_worker_signs_cos_key_references_at_submit_time(self):
        import hashlib as _hashlib

        owner = _hashlib.sha256(b"fang").hexdigest()[:16]
        key = "seedance/reference/%s/%s.png" % (owner, "t" * 24 + "-" + "c" * 16)
        signed = "https://bucket-1250000000.cos.ap-guangzhou.myqcloud.com/x?q-sign-algorithm=sha1"
        fake = {"request_id": "seedance-1", "source_video_url": "https://example.com/micro.mp4",
                "model": "doubao-seedance-2-0-260128"}
        with patch("content_domains.video_seedance.generate", return_value=fake) as generate, \
             patch.object(self.video, "_seedance_cos_presign", return_value=signed) as presign, \
             patch.object(self.video, "_download_xiaole_video", return_value="video/seedance.mp4"), \
             patch.object(self.video, "_extract_first_frame_cover", return_value=None):
            self.video.gen_xiaole_video({
                "channel": "micro", "prompt": "demo", "duration": 5,
                "reference_images": ["cos-key://" + key], "_username": "fang",
            })
        presign.assert_called_once_with(key)   # worker 提交时才生成新鲜签名 URL
        self.assertEqual([signed], generate.call_args.kwargs["reference_images"])

    def test_worker_rejects_cos_key_of_other_owner(self):
        with patch("content_domains.video_seedance.generate") as generate, \
             patch.object(self.video, "_seedance_cos_presign") as presign:
            with self.assertRaisesRegex(ValueError, "对象键不合法"):
                self.video.gen_xiaole_video({
                    "channel": "micro", "prompt": "demo", "duration": 5,
                    "reference_images": ["cos-key://seedance/reference/0000000000000000/x.png"],
                    "_username": "fang",
                })
        generate.assert_not_called()
        presign.assert_not_called()

    def test_stage_seedance_references_partial_failure_cleans_uploaded_batch(self):
        refs = [self._seedance_png_data(str(index).encode("ascii")) for index in range(3)]
        patches = self._stage_mocks(put_side_effect=[None, None, RuntimeError("cos boom")])
        with patches[0], patches[1], patches[2] as put, patches[3], patches[4], patches[5], patches[6], \
             patch.object(self.video, "_seedance_cos_delete") as delete:
            body = self.video.validate_xiaole_video_payload({
                "channel": "micro", "prompt": "demo", "duration": 5, "reference_images": refs}, "fang")
            with self.assertRaisesRegex(self.video.SeedanceReferenceUnavailable, "上传失败.*未扣点"):
                self.video.stage_seedance_references(body, "fang")

        self.assertEqual(3, put.call_count)
        attempted_keys = [call.args[1] for call in put.call_args_list]
        self.assertEqual(3, delete.call_count)
        self.assertCountEqual(attempted_keys, [call.args[0] for call in delete.call_args_list])
        self.assertEqual(refs, body["reference_images"])   # 失败不回退、不改写

    def test_stage_seedance_references_unavailable_is_explicit(self):
        from content_domains import cos

        with patch.object(self.video, "seedance_reference_upload_is_open", return_value=False), \
             patch.object(cos, "put_bytes") as put:
            body = {"channel": "micro", "reference_images": [self._seedance_png_data()]}
            with self.assertRaisesRegex(self.video.SeedanceReferenceUnavailable, "未配置.*未扣点"):
                self.video.stage_seedance_references(body, "fang")
        put.assert_not_called()

    def test_stage_seedance_references_upload_failure_never_falls_back_to_data_url(self):
        patches = self._stage_mocks(put_side_effect=ModuleNotFoundError("qcloud_cos"))
        ref = self._seedance_png_data()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            body = {"channel": "micro", "reference_images": [ref]}
            with self.assertRaisesRegex(self.video.SeedanceReferenceUnavailable, "上传失败.*未扣点"):
                self.video.stage_seedance_references(body, "fang")
        self.assertEqual([ref], body["reference_images"])   # body 未被半成品 URL 污染

    def test_stage_seedance_references_skips_non_micro_channels(self):
        from content_domains import cos

        with patch.object(cos, "put_bytes") as put:
            body = {"channel": "grok", "reference_images": ["data:image/png;base64,AAAA"]}
            self.assertEqual([], self.video.stage_seedance_references(body, "fang"))
        put.assert_not_called()

    def test_cleanup_staged_seedance_references_is_best_effort(self):
        import tempfile
        from content_domains import core

        with tempfile.TemporaryDirectory() as td, \
             patch.object(core, "JOB_DB", str(Path(td) / "jobs.db")), \
             patch.object(self.video, "_cleanup_table_ready", False), \
             patch.object(self.video, "_seedance_cos_delete",
                          side_effect=[None, RuntimeError("already gone")]) as delete:
            self.video.cleanup_staged_seedance_references(["k1", "k2"])
        self.assertEqual(2, delete.call_count)   # 单个失败不阻断其余清理

    def test_cleanup_failure_persists_and_retry_converges(self):
        import sqlite3
        import tempfile
        from contextlib import closing as _closing
        from content_domains import core

        def pending_rows():
            with _closing(sqlite3.connect(core.JOB_DB)) as db:
                return db.execute("SELECT key,job_id,attempts,state FROM seedance_pending_cleanup").fetchall()

        def make_due():
            with _closing(sqlite3.connect(core.JOB_DB)) as db:
                db.execute("UPDATE seedance_pending_cleanup SET next_attempt_at=0")
                db.commit()

        with tempfile.TemporaryDirectory() as td, \
             patch.object(core, "JOB_DB", str(Path(td) / "jobs.db")), \
             patch.object(self.video, "_cleanup_table_ready", False):
            # 删除失败 → 持久化待清理
            with patch.object(self.video, "_seedance_cos_delete", side_effect=RuntimeError("cos down")):
                self.video.cleanup_staged_seedance_references(["k1"], job_id=7)
            self.assertEqual([("k1", 7, 1, "cleanup_pending")], [tuple(r) for r in pending_rows()])
            # 重试仍失败 → attempts+1，记录保留
            with patch.object(self.video, "_seedance_cos_delete", side_effect=RuntimeError("still down")):
                make_due()
                self.video.retry_pending_seedance_cleanups()
            self.assertEqual([("k1", 7, 2, "cleanup_pending")], [tuple(r) for r in pending_rows()])
            # 故障恢复 → 重试成功 → 记录移除
            with patch.object(self.video, "_seedance_cos_delete") as delete:
                make_due()
                self.video.retry_pending_seedance_cleanups()
            delete.assert_called_once_with("k1")
            self.assertEqual([], pending_rows())

    def test_cleanup_scan_does_not_starve_eligible_rows(self):
        import tempfile
        from contextlib import closing as _closing
        from content_domains import core

        with tempfile.TemporaryDirectory() as td, \
             patch.object(core, "JOB_DB", str(Path(td) / "jobs.db")), \
             patch.object(self.video, "_cleanup_table_ready", False):
            with _closing(self.video._cleanup_db()) as db:
                for index in range(100):
                    db.execute(
                        "INSERT INTO seedance_pending_cleanup(key,job_id,created_at,attempts,state,next_attempt_at,updated_at) VALUES(?,?,?,?,'cleanup_pending',0,0)",
                        ("exhausted-%03d" % index, None, index,
                         self.video.SEEDANCE_CLEANUP_MAX_ATTEMPTS),
                    )
                db.execute(
                    "INSERT INTO seedance_pending_cleanup(key,job_id,created_at,attempts,state,next_attempt_at,updated_at) VALUES('eligible',NULL,1000,0,'cleanup_pending',0,0)"
                )
                db.commit()
            with patch.object(self.video, "_seedance_cos_delete") as delete:
                processed = sum(
                    self.video.retry_pending_seedance_cleanups(limit=50)
                    for _ in range(3)
                )
            self.assertEqual(101, processed)
            self.assertIn("eligible", [call.args[0] for call in delete.call_args_list])

    def test_cleanup_keeps_retrying_after_alert_threshold(self):
        import tempfile
        from contextlib import closing as _closing
        from content_domains import core

        with tempfile.TemporaryDirectory() as td, \
             patch.object(core, "JOB_DB", str(Path(td) / "jobs.db")), \
             patch.object(self.video, "_cleanup_table_ready", False):
            with _closing(self.video._cleanup_db()) as db:
                db.execute(
                    "INSERT INTO seedance_pending_cleanup(key,job_id,created_at,attempts,state,next_attempt_at,updated_at) VALUES('recover-after-five',NULL,0,?,'cleanup_pending',0,0)",
                    (self.video.SEEDANCE_CLEANUP_MAX_ATTEMPTS,),
                )
                db.commit()
            with patch.object(self.video, "_seedance_cos_delete") as delete:
                self.assertEqual(1, self.video.retry_pending_seedance_cleanups())
            delete.assert_called_once_with("recover-after-five")

    def test_orphaned_staging_intent_is_recovered_after_grace(self):
        import sqlite3
        import tempfile
        from contextlib import closing as _closing
        from content_domains import core

        with tempfile.TemporaryDirectory() as td, \
             patch.object(core, "JOB_DB", str(Path(td) / "jobs.db")), \
             patch.object(self.video, "_cleanup_table_ready", False):
            self.video._persist_staging_cleanup_intent("orphan-key")
            with _closing(sqlite3.connect(core.JOB_DB)) as db:
                db.execute("UPDATE seedance_pending_cleanup SET created_at=0,next_attempt_at=0")
                db.commit()
            with patch.object(self.video, "_seedance_cos_delete") as delete:
                self.assertEqual(1, self.video.retry_pending_seedance_cleanups())
            delete.assert_called_once_with("orphan-key")

    def test_outcome_unknown_keeps_reference_until_signed_url_expires(self):
        from content_domains import video_seedance
        payload = {"channel": "micro"}
        delay = self.video.seedance_reference_cleanup_delay(
            "xiaole_video", payload, video_seedance.CreateOutcomeUnknown("unknown")
        )
        self.assertGreater(delay, self.video.SEEDANCE_REFERENCE_SIGN_EXPIRE)
        self.assertEqual(0, self.video.seedance_reference_cleanup_delay(
            "xiaole_video", payload, video_seedance.SeedanceRejected("rejected")
        ))

    def test_delayed_cleanup_is_durable_and_runs_once_when_due(self):
        import sqlite3
        import tempfile
        from contextlib import closing as _closing
        from content_domains import core

        with tempfile.TemporaryDirectory() as td, \
             patch.object(core, "JOB_DB", str(Path(td) / "jobs.db")), \
             patch.object(self.video, "_cleanup_table_ready", False), \
             patch.object(self.video, "_seedance_cos_delete") as delete:
            self.video._persist_staging_cleanup_intent("unknown-key")
            self.video.cleanup_staged_seedance_references(
                ["unknown-key"], job_id=8, delay_seconds=120
            )
            delete.assert_not_called()
            self.assertEqual(0, self.video.retry_pending_seedance_cleanups())
            with _closing(sqlite3.connect(core.JOB_DB)) as db:
                row = db.execute(
                    "SELECT state,job_id FROM seedance_pending_cleanup WHERE key='unknown-key'"
                ).fetchone()
                self.assertEqual(("cleanup_pending", 8), row)
                db.execute("UPDATE seedance_pending_cleanup SET next_attempt_at=0")
                db.commit()
            self.assertEqual(1, self.video.retry_pending_seedance_cleanups())
            delete.assert_called_once_with("unknown-key")
            self.assertEqual(0, self.video.retry_pending_seedance_cleanups())

    def test_terminal_cleanup_refuses_foreign_or_wrong_kind_keys(self):
        import hashlib as _hashlib
        import json as _json
        import sqlite3
        import tempfile
        from contextlib import closing as _closing
        from content_domains import core

        own = "seedance/reference/%s/t.png" % _hashlib.sha256(b"fang").hexdigest()[:16]
        foreign = "seedance/reference/%s/t.png" % _hashlib.sha256(b"other").hexdigest()[:16]
        with tempfile.TemporaryDirectory() as td, \
             patch.object(core, "JOB_DB", str(Path(td) / "jobs.db")), \
             patch.object(self.video, "_cleanup_table_ready", False):
            with _closing(sqlite3.connect(core.JOB_DB)) as db:
                db.execute("CREATE TABLE jobs(id INTEGER PRIMARY KEY, kind TEXT, username TEXT, payload TEXT)")
                db.execute("INSERT INTO jobs VALUES(1,'xiaole_video','fang',?)",
                           (_json.dumps({"_seedance_staged_keys": [own, foreign, " arbitrary/key.png"]}),))
                db.execute("INSERT INTO jobs VALUES(2,'video','fang',?)",
                           (_json.dumps({"_seedance_staged_keys": [own]}),))
                db.commit()
            with patch.object(self.video, "_seedance_cos_delete") as delete:
                self.video.cleanup_job_staged_seedance_references(1)
                self.assertEqual([own], [c.args[0] for c in delete.call_args_list])  # 只删本账号前缀的键
                delete.reset_mock()
                self.video.cleanup_job_staged_seedance_references(2)   # 非 xiaole_video 一律不删
                delete.assert_not_called()

    def test_validate_micro_accepts_public_and_authorized_asset_references(self):
        import tempfile
        from content_domains import assets_store

        with tempfile.TemporaryDirectory() as td, \
             patch.object(assets_store, "ASSET_DB", str(Path(td) / "assets.db")), \
             patch.object(assets_store, "_initialized", False):
            assets_store.init_assets()
            from contextlib import closing as _closing
            import json as _json
            with _closing(assets_store.adb()) as c:
                # 显式登记了 Seedance provider 映射的素材（meta.seedance_asset_id）
                c.execute("INSERT INTO assets(id,kind,stage,username,meta,created_at) VALUES(120,'collect','material','fang',?,1)",
                          (_json.dumps({"seedance_asset_id": "prov-abc123"}),))
                # 普通 copy 资产：没有 provider 映射，哪怕编号相同也不得授权
                c.execute("INSERT INTO assets(id,kind,stage,username,meta,created_at) VALUES(130,'copy','work','fang',?,1)",
                          (_json.dumps({"text": "普通文案资产"}),))
                # 别人的 provider 映射
                c.execute("INSERT INTO assets(id,kind,stage,username,meta,created_at) VALUES(140,'collect','material','other',?,1)",
                          (_json.dumps({"seedance_asset_id": "prov-other-9"}),))
                # 已删除的映射
                c.execute("INSERT INTO assets(id,kind,stage,username,meta,created_at,deleted) VALUES(150,'collect','material','fang',?,1,1)",
                          (_json.dumps({"seedance_asset_id": "prov-deleted"}),))
                c.commit()
            refs = ["https://cdn.example/ref.jpg", "asset://asset-prov-abc123"]
            body = self.video.validate_xiaole_video_payload({
                "channel": "micro", "prompt": "demo", "duration": 5,
                "reference_images": refs,
            }, "fang")
            self.assertEqual(refs, body["reference_images"])
            for ref, owner in (("asset://asset-130", "fang"),          # 普通 copy 资产同编号误授权探针
                               ("asset://asset-prov-other-9", "fang"), # 别人的 provider 素材
                               ("asset://asset-prov-deleted", "fang"), # 已删除的映射
                               ("asset://asset-999", "fang"),          # 不存在的编号
                               ("asset://asset-prov-abc123", "other")):# 归属不符
                with self.subTest(ref=ref, owner=owner):
                    with self.assertRaisesRegex(ValueError, "不存在或未授权"):
                        self.video.validate_xiaole_video_payload({
                            "channel": "micro", "prompt": "demo", "duration": 5,
                            "reference_images": [ref],
                        }, owner)

    def test_validate_micro_rejects_local_or_malformed_references(self):
        for ref in ("/api/gen/file/ref.jpg", "file:///tmp/ref.jpg", "http://127.0.0.1/ref.jpg",
                    "http://localhost/ref.jpg", "asset://reference/2"):
            with self.subTest(ref=ref):
                with self.assertRaisesRegex(ValueError, "公网|asset://"):
                    self.video.validate_xiaole_video_payload({
                        "channel": "micro", "prompt": "demo", "duration": 5,
                        "reference_images": [ref],
                    }, "fang")

    def test_validate_micro_rejects_spoofed_or_corrupt_images(self):
        import io as _io
        from PIL import Image

        def data_url(mime, raw):
            return "data:%s;base64,%s" % (mime, base64.b64encode(raw).decode("ascii"))

        png_buf, jpg_buf = _io.BytesIO(), _io.BytesIO()
        Image.new("RGB", (8, 8)).save(png_buf, "PNG")
        Image.new("RGB", (8, 8)).save(jpg_buf, "JPEG")
        truncated_jpg = jpg_buf.getvalue()[:-64]
        cases = [
            data_url("image/png", jpg_buf.getvalue()),     # 损坏/伪装探针：JPEG 声明成 image/png
            data_url("image/jpeg", png_buf.getvalue()),    # PNG 声明成 image/jpeg
            data_url("image/jpeg", truncated_jpg),         # 截断的 JPEG
            data_url("image/png", b"\x89PNG\r\n\x1a\n" + b"not-a-real-png"),  # 只有魔数
        ]
        for ref in cases:
            with self.subTest(ref=ref[:40]):
                with self.assertRaisesRegex(ValueError, "无效|损坏|不一致"):
                    self.video.validate_xiaole_video_payload({
                        "channel": "micro", "prompt": "demo", "duration": 5,
                        "reference_images": [ref],
                    }, "fang")

    def test_validate_micro_fails_closed_when_pillow_missing(self):
        ref = self._seedance_png_data()
        with patch.dict(sys.modules, {"PIL": None, "PIL.Image": None}):
            with self.assertRaisesRegex(self.video.SeedanceReferenceUnavailable, "校验组件不可用.*未扣点"):
                self.video.validate_xiaole_video_payload({
                    "channel": "micro", "prompt": "demo", "duration": 5,
                    "reference_images": [ref],
                }, "fang")

    def test_seedance_worker_defense_rejects_unstaged_data_url(self):
        with patch("content_domains.video_seedance.generate") as generate:
            with self.assertRaisesRegex(ValueError, "公网 URL"):
                self.video.gen_xiaole_video({
                    "channel": "micro", "prompt": "demo", "duration": 5,
                    "reference_images": [self._seedance_png_data()],
                })
        generate.assert_not_called()

    def test_seedance_reference_health_requires_cos_and_sdk(self):
        from content_domains import cos

        with patch.object(cos, "enabled", return_value=True), \
             patch.object(self.video.importlib.util, "find_spec", return_value=object()):
            self.assertTrue(self.video.seedance_reference_upload_is_open())
        with patch.object(cos, "enabled", return_value=False):
            self.assertFalse(self.video.seedance_reference_upload_is_open())
        with patch.object(cos, "enabled", return_value=True), \
             patch.object(self.video.importlib.util, "find_spec", return_value=None):
            self.assertFalse(self.video.seedance_reference_upload_is_open())

    def test_content_service_dependency_manifest_pins_cos_sdk(self):
        root = Path(__file__).resolve().parents[1]
        requirements = (root / "deploy/requirements-content.txt").read_text(encoding="utf-8")
        self.assertIn("cos-python-sdk-v5==1.9.44", requirements)
        self.assertIn("Pillow==", requirements)   # 真实解码校验的运行依赖必须闭环

    def test_ci_installs_content_python_dependencies(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertIn("pip install -r deploy/requirements-content.txt", workflow)

    def test_gen_micro_uses_official_seedance_without_shared_provider(self):
        fake = {
            "request_id": "seedance-1",
            "source_video_url": "https://example.com/micro.mp4",
            "model": "doubao-seedance-2-0-260128",
            "duration": 15,
        }
        payload = self.video.validate_xiaole_video_payload({
            "channel": "micro",
            "prompt": "cinematic demo",
            "duration": 15,
            "ratio": "4:3",
            "resolution": "1080p",
        })
        with patch("content_domains.video_seedance.generate", return_value=fake) as generate, \
             patch.object(self.video, "_download_xiaole_video", return_value="video/seedance.mp4"), \
             patch.object(self.video, "_extract_first_frame_cover", return_value=None):
            result = self.video.gen_xiaole_video(payload)
        self.assertEqual(generate.call_args.kwargs["duration"], 15)
        self.assertEqual(generate.call_args.kwargs["model"], "doubao-seedance-2-0-260128")
        self.assertEqual(generate.call_args.kwargs["ratio"], "4:3")
        self.assertEqual(generate.call_args.kwargs["resolution"], "1080p")
        self.assertEqual(result["provider_video_id"], "seedance-1")
        self.assertEqual(result["duration"], 15)
        self.assertEqual(result["ratio"], "4:3")
        self.assertEqual(result["resolution"], "1080p")

    def test_validate_official_grok_parameters(self):
        with patch.object(self.video, "GROK_VIDEO_PROVIDER", "xai"):
            body = self.video.validate_xiaole_video_payload({
                "channel": "grok", "prompt": "cinematic demo", "ratio": "2:3",
                "duration": 15, "resolution": "720p", "model": "grok-imagine-video",
            })
        self.assertEqual(body["ratio"], "2:3")
        self.assertEqual(body["duration"], 15)

    def test_validate_official_edit_verifies_server_side_duration(self):
        source = "data:video/mp4;base64,AAAA"
        with patch.object(self.video, "GROK_VIDEO_PROVIDER", "xai"), \
             patch.object(self.video, "_probe_data_video_duration", return_value=8.6):
            body = self.video.validate_xiaole_video_payload({"channel": "grok", "operation": "edit",
                                                              "prompt": "change person", "reference_video_data": source})
        self.assertEqual(body["source_duration"], 8.6)
        self.assertEqual(body["model"], "grok-imagine-video")

    def test_validate_official_edit_rejects_over_8_7_seconds(self):
        with patch.object(self.video, "GROK_VIDEO_PROVIDER", "xai"), \
             patch.object(self.video, "_probe_data_video_duration", return_value=8.71):
            with self.assertRaisesRegex(ValueError, "8.7"):
                self.video.validate_xiaole_video_payload({"channel": "grok", "operation": "edit", "prompt": "demo",
                                                          "reference_video_data": "data:video/mp4;base64,AAAA"})

    def test_validate_official_grok_rejects_over_max_references(self):
        with patch.object(self.video, "GROK_VIDEO_PROVIDER", "xai"):
            n = self.video.XIAOLE_MAX_REF + 1
            with self.assertRaisesRegex(ValueError, "最多支持%d张" % self.video.XIAOLE_MAX_REF):
                self.video.validate_xiaole_video_payload({
                    "channel": "grok", "prompt": "cinematic demo",
                    "reference_images": ["https://a/%d.jpg" % i for i in range(n)],
                })

    def test_validate_official_grok_accepts_multiple_references(self):
        with patch.object(self.video, "GROK_VIDEO_PROVIDER", "xai"):
            cleaned = self.video.validate_xiaole_video_payload({
                "channel": "grok", "prompt": "cinematic demo",
                "reference_images": ["https://a/1.jpg", "https://a/2.jpg", "https://a/3.jpg"],
            })
            self.assertEqual(len(cleaned["reference_images"]), 3)

    def test_validate_video_15_requires_reference(self):
        with patch.object(self.video, "GROK_VIDEO_PROVIDER", "xai"):
            with self.assertRaisesRegex(ValueError, "仅支持图生视频"):
                self.video.validate_xiaole_video_payload({
                    "channel": "grok", "prompt": "cinematic demo",
                    "model": "grok-imagine-video-1.5",
                })

    def test_gen_grok_official_preserves_result_contract(self):
        fake = {
            "request_id": "xai-1", "model": "grok-imagine-video",
            "source_video_url": "https://vidgen.x.ai/demo.mp4", "duration": 10,
        }
        with patch.object(self.video, "GROK_VIDEO_PROVIDER", "xai"), \
             patch("content_domains.video_xai.generate", return_value=fake) as generate, \
             patch.object(self.video, "_download_xiaole_video", return_value="video/grok_xai_demo.mp4"), \
             patch.object(self.video, "_extract_first_frame_cover", return_value="video/grok_xai_demo_cover.jpg"), \
             patch.object(self.video, "public_url", return_value="https://cos.example/cover.jpg"):
            result = self.video.gen_xiaole_video({
                "channel": "grok", "prompt": "cinematic demo", "ratio": "9:16",
                "duration": 10, "resolution": "720p", "model": "grok-imagine-video",
            })
        self.assertEqual(result["video_file"], "video/grok_xai_demo.mp4")
        self.assertEqual(result["provider_video_id"], "xai-1")
        self.assertEqual(result["model"], "grok-imagine-video")
        self.assertEqual(result["duration"], 10)
        generate.assert_called_once()

    def test_grok_uses_openrouter_only_after_safe_xai_create_failure(self):
        from content_domains import video_xai

        fallback = {
            "request_id": "or-1", "model": "grok-imagine-video",
            "source_video_url": "https://openrouter.ai/api/v1/videos/or-1/content",
            "duration": 10, "provider": "openrouter",
        }
        with patch.object(self.video, "GROK_VIDEO_PROVIDER", "xai"), \
             patch("content_domains.video_xai.generate",
                   side_effect=video_xai.XaiCreateUnavailableError("xAI quota")), \
             patch("content_domains.video_openrouter.available", return_value=True), \
             patch("content_domains.video_openrouter.generate", return_value=fallback) as generate, \
             patch("content_domains.video_openrouter.download_headers",
                   return_value={"Authorization": "Bearer test"}), \
             patch.object(self.video, "_download_xiaole_video", return_value="video/grok_or.mp4") as download, \
             patch.object(self.video, "_extract_first_frame_cover", return_value=None):
            result = self.video.gen_xiaole_video({
                "channel": "grok", "prompt": "cinematic demo", "ratio": "9:16",
                "duration": 10, "resolution": "720p", "model": "grok-imagine-video",
            })
        generate.assert_called_once()
        download.assert_called_once_with(
            fallback["source_video_url"], "grok_openrouter",
            origin_headers={"Authorization": "Bearer test"},
        )
        self.assertEqual(result["provider_video_id"], "or-1")
        self.assertEqual(result["video_file"], "video/grok_or.mp4")

    def test_missing_openrouter_key_rethrows_original_xai_error(self):
        from content_domains import video_xai

        original = video_xai.XaiCreateUnavailableError("xAI quota")
        with patch.object(self.video, "GROK_VIDEO_PROVIDER", "xai"), \
             patch("content_domains.video_xai.generate", side_effect=original), \
             patch("content_domains.video_openrouter.available", return_value=False), \
             patch("content_domains.video_openrouter.generate") as fallback:
            with self.assertRaises(video_xai.XaiCreateUnavailableError) as raised:
                self.video.gen_xiaole_video({
                    "channel": "grok", "prompt": "cinematic demo", "ratio": "9:16",
                    "duration": 10, "resolution": "720p", "model": "grok-imagine-video",
                })
        self.assertIs(raised.exception, original)
        fallback.assert_not_called()

    def test_grok_does_not_fallback_after_ambiguous_xai_network_failure(self):
        with patch.object(self.video, "GROK_VIDEO_PROVIDER", "xai"), \
             patch("content_domains.video_xai.generate",
                   side_effect=RuntimeError("xAI视频网络异常: connection reset")), \
             patch("content_domains.video_openrouter.generate") as fallback:
            with self.assertRaisesRegex(RuntimeError, "网络异常"):
                self.video.gen_xiaole_video({
                    "channel": "grok", "prompt": "cinematic demo", "ratio": "9:16",
                    "duration": 10, "resolution": "720p", "model": "grok-imagine-video",
                })
        fallback.assert_not_called()

    def test_grok_does_not_fallback_after_xai_poll_credential_failure(self):
        from content_domains import video_xai

        with patch.object(self.video, "GROK_VIDEO_PROVIDER", "xai"), \
             patch("content_domains.video_xai.generate",
                   side_effect=video_xai.XaiCredentialError("poll token expired")), \
             patch("content_domains.video_openrouter.generate") as fallback:
            with self.assertRaises(video_xai.XaiCredentialError):
                self.video.gen_xiaole_video({
                    "channel": "grok", "prompt": "cinematic demo", "ratio": "9:16",
                    "duration": 10, "resolution": "720p", "model": "grok-imagine-video",
                })
        fallback.assert_not_called()

    def test_grok_does_not_fallback_after_successful_xai_download_failure(self):
        generated = {
            "request_id": "xai-1", "model": "grok-imagine-video",
            "source_video_url": "https://vidgen.x.ai/demo.mp4", "duration": 10,
        }
        with patch.object(self.video, "GROK_VIDEO_PROVIDER", "xai"), \
             patch("content_domains.video_xai.generate", return_value=generated), \
             patch("content_domains.video_openrouter.generate") as fallback, \
             patch.object(self.video, "_download_xiaole_video",
                          side_effect=RuntimeError("视频下载失败")):
            with self.assertRaisesRegex(RuntimeError, "下载失败"):
                self.video.gen_xiaole_video({
                    "channel": "grok", "prompt": "cinematic demo", "ratio": "9:16",
                    "duration": 10, "resolution": "720p", "model": "grok-imagine-video",
                })
        fallback.assert_not_called()

    def test_gen_grok_official_edit_uploads_source_and_preserves_contract(self):
        fake = {"request_id": "edit-1", "model": "grok-imagine-video",
                "source_video_url": "https://vidgen.x.ai/edit.mp4", "duration": 6.2}
        with patch.object(self.video, "GROK_VIDEO_PROVIDER", "xai"), \
             patch.object(self.video, "_save_data_file", return_value="video/source.mp4"), \
             patch.object(self.video, "public_url", side_effect=["https://cos.example/source.mp4", "https://cos.example/cover.jpg"]), \
             patch.object(self.video, "_file_url", return_value="/api/files/video/source.mp4"), \
             patch("content_domains.video_xai.edit", return_value=fake) as edit, \
             patch("content_domains.video_openrouter.generate") as fallback, \
             patch.object(self.video, "_download_xiaole_video", return_value="video/edit.mp4"), \
             patch.object(self.video, "_extract_first_frame_cover", return_value="video/edit_cover.jpg"):
            result = self.video.gen_xiaole_video({"channel": "grok", "operation": "edit", "prompt": "change person",
                                                  "reference_video_data": "data:video/mp4;base64,AAAA", "source_duration": 6.2})
        self.assertEqual(result["operation"], "edit")
        self.assertEqual(result["reference_video_file"], "video/source.mp4")
        self.assertIsNone(result["resolution"])
        edit.assert_called_once()
        fallback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
