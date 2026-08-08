import base64
import hashlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server.content_domains import cli_uploads


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEklEQVR4nGOskDvBwMDAxAAGABBCAWKm3yc5AAAAAElFTkSuQmCC"
)
JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAACAAIDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDi6KKK++PcP//Z"
)


class CLIImageUploadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root_patch = mock.patch.object(cli_uploads, "UPLOAD_ROOT", Path(self.temp.name))
        self.root_patch.start()

    def tearDown(self):
        self.root_patch.stop()
        self.temp.cleanup()

    def upload(self, raw=PNG, mime="image/png", username="alice", now=100):
        return cli_uploads.store_image(
            io.BytesIO(raw), len(raw), username, mime, hashlib.sha256(raw).hexdigest(), now=now,
        )

    def test_private_upload_expands_for_owner_only(self):
        uploaded = self.upload()
        raw, meta = cli_uploads.read_image_bytes(uploaded["upload_id"], "alice", now=101)
        self.assertEqual(PNG, raw)
        self.assertEqual((2, 2), (meta["width"], meta["height"]))
        body = cli_uploads.expand_image_payload(
            {"provider": "openai", "image_upload_id": uploaded["upload_id"]}, "alice", now=101,
        )
        self.assertEqual(PNG, base64.b64decode(body["image"]))
        self.assertNotIn("image_upload_id", body)
        with self.assertRaisesRegex(ValueError, "不存在或已失效"):
            cli_uploads.expand_image_payload(
                {"provider": "openai", "image_upload_id": uploaded["upload_id"]}, "bob", now=101,
            )

    def test_real_decode_approval_and_owner_bound_discard(self):
        corrupt = b"\x89PNG\r\n\x1a\n" + b"not-a-real-image"
        with self.assertRaisesRegex(ValueError, "无法读取"):
            self.upload(corrupt)
        uploaded = self.upload()
        approved = cli_uploads.approve_image(
            uploaded["upload_id"], "alice", "smart_montage", now=101,
        )
        self.assertEqual("smart_montage", approved["approved_for"])
        self.assertFalse(cli_uploads.discard_image(uploaded["upload_id"], "bob"))
        self.assertTrue(cli_uploads.discard_image(uploaded["upload_id"], "alice"))
        with self.assertRaisesRegex(ValueError, "不存在或已失效"):
            cli_uploads.read_image_bytes(uploaded["upload_id"], "alice", now=102)

    def test_approved_upload_can_be_hard_linked_and_leased_for_a_long_job(self):
        uploaded = self.upload(now=100)
        approved = cli_uploads.approve_image(
            uploaded["upload_id"], "alice", "smart_montage", now=101,
            lease_seconds=4 * 60 * 60,
        )
        self.assertEqual(101 + 4 * 60 * 60, approved["expires_at"])
        inspected = cli_uploads.inspect_image(
            uploaded["upload_id"], "alice", now=102,
        )
        destination = Path(self.temp.name) / "frozen" / "scene-00.png"
        cli_uploads.freeze_image(
            uploaded["upload_id"], "alice", destination, "smart_montage",
            inspected["sha256"], now=102,
        )
        self.assertEqual(PNG, destination.read_bytes())
        self.assertTrue(cli_uploads.discard_image(uploaded["upload_id"], "alice"))
        self.assertEqual(PNG, destination.read_bytes())

    def test_quiet_period_janitor_removes_expired_and_orphaned_data(self):
        uploaded = self.upload(now=100)
        orphan = Path(self.temp.name) / ("img_" + "a" * 32 + ".png")
        orphan.write_bytes(PNG)
        os.utime(orphan, (0, 0))
        cli_uploads.cleanup_expired_uploads(now=100 + cli_uploads.TTL + 1)
        self.assertFalse(orphan.exists())
        with self.assertRaisesRegex(ValueError, "不存在或已失效"):
            cli_uploads.inspect_image(
                uploaded["upload_id"], "alice", now=100 + cli_uploads.TTL + 1,
            )

    def test_multi_reference_and_png_mask_contract(self):
        first, second = self.upload(now=100), self.upload(now=100)
        body = cli_uploads.expand_image_payload({
            "provider": "xiaole", "reference_upload_ids": [first["upload_id"], second["upload_id"]],
        }, "alice", now=101)
        self.assertEqual(2, len(body["reference_images"]))
        jpg = self.upload(JPEG, "image/jpeg", now=100)
        with self.assertRaisesRegex(ValueError, "蒙版必须是 PNG"):
            cli_uploads.expand_image_payload({
                "provider": "openai", "image_upload_id": first["upload_id"],
                "mask_upload_id": jpg["upload_id"],
            }, "alice", now=101)

    def test_digest_mime_expiry_and_combinations_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "发生变化"):
            cli_uploads.store_image(io.BytesIO(PNG), len(PNG), "alice", "image/png", "0" * 64, now=100)
        with self.assertRaisesRegex(ValueError, "声明格式"):
            cli_uploads.store_image(
                io.BytesIO(PNG), len(PNG), "alice", "image/jpeg", hashlib.sha256(PNG).hexdigest(), now=100,
            )
        uploaded = self.upload(now=100)
        with self.assertRaisesRegex(ValueError, "已过期"):
            cli_uploads.expand_image_payload(
                {"provider": "openai", "image_upload_id": uploaded["upload_id"]},
                "alice", now=100 + cli_uploads.TTL + 1,
            )
        with self.assertRaisesRegex(ValueError, "单参考图和多参考图"):
            cli_uploads.expand_image_payload({
                "provider": "xiaole", "image_upload_id": "img_" + "a" * 32,
                "reference_upload_ids": ["img_" + "b" * 32],
            }, "alice", now=101)

    def test_account_quota_disk_reserve_and_stale_temp_cleanup(self):
        stale = Path(self.temp.name) / ".img_stale.tmp"
        stale.write_bytes(b"stale")
        os.utime(stale, (0, 0))
        with mock.patch.object(cli_uploads, "MAX_USER_FILES", 1):
            self.upload(now=1000)
            with self.assertRaisesRegex(ValueError, "临时图片已达上限"):
                self.upload(now=1001)
        self.assertFalse(stale.exists())
        with mock.patch.object(
            cli_uploads.shutil, "disk_usage",
            return_value=mock.Mock(free=cli_uploads.MIN_FREE_BYTES),
        ):
            with self.assertRaises(OSError):
                self.upload(username="bob", now=1002)


if __name__ == "__main__":
    unittest.main()
