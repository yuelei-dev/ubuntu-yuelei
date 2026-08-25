import concurrent.futures
import hashlib
import io
import json
import multiprocessing
import os
import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from content_domains import private_domain_media


def _hold_snapshot_cache_lock(cache_root, active, release):
    root = pathlib.Path(cache_root)
    with private_domain_media._snapshot_cache_lock(root):
        (root / ".pending-active.part").write_bytes(b"in progress")
        active.set()
        release.wait(timeout=5)


class PrivateDomainMediaLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.snapshot_root = self.root / "snapshots"
        private_domain_media._CACHE_KEY = None
        private_domain_media._CACHE_ITEMS = ()
        private_domain_media._CACHE_WATCHED = ()

    def tearDown(self):
        self.temporary.cleanup()

    def _write_record(self, relative_path, media_type, digest=None, **extra):
        path = self.root.joinpath(*pathlib.PurePosixPath(relative_path).parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        content = extra.pop("content", (relative_path + "\n").encode("utf-8"))
        path.write_bytes(content)
        digest = digest or hashlib.sha256(content).hexdigest()
        record = {
            "SHA256": digest,
            "server_relative_path": relative_path,
            "素材类型": media_type,
            "素材名称": extra.pop("title", "测试素材"),
            "状态": "可使用",
            "一级场景": "商务人物与活动",
            "二级场景": "商务交流",
            "使用环节": ["信任建立"],
            "标签": ["客户交流", "私域"],
            "内容安全": "需人工复核",
            "许可类型": "用户提供素材",
            "来源链接": "https://must-not-leak.example/secret",
            "素材文件": [{"file_token": "must-not-leak"}],
        }
        record.update(extra)
        index = self.root / "index.jsonl"
        with index.open("a", encoding="utf-8") as target:
            target.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _catalog(self):
        return mock.patch.dict(
            os.environ,
            {
                "PRIVATE_DOMAIN_MATERIAL_ROOT": str(self.root),
                "PRIVATE_DOMAIN_SNAPSHOT_CACHE_ROOT": str(self.snapshot_root),
            },
        )

    def test_catalog_returns_only_safe_fields_and_three_media_types(self):
        self._write_record("files/图片/a.jpg", "图片")
        self._write_record("files/视频/b.mp4", "视频")
        self._write_record("files/BGM/c.mp3", "BGM")
        with self._catalog():
            items = private_domain_media.list_materials(300)
        self.assertEqual(["image", "video", "bgm"], [item["media_type"] for item in items])
        self.assertTrue(all(item["stream_url"].startswith(
            "/api/gen/private-domain/material?path=") for item in items))
        serialized = json.dumps(items, ensure_ascii=False)
        self.assertNotIn("must-not-leak", serialized)
        self.assertNotIn("来源链接", serialized)
        self.assertNotIn("素材文件", serialized)

    def test_catalog_rejects_invalid_hash_missing_file_and_unsafe_path(self):
        self._write_record("files/图片/good.jpg", "图片")
        index = self.root / "index.jsonl"
        invalid = [
            {"SHA256": "short", "server_relative_path": "files/图片/good.jpg",
             "素材类型": "图片", "状态": "可使用"},
            {"SHA256": "b" * 64, "server_relative_path": "files/图片/missing.jpg",
             "素材类型": "图片", "状态": "可使用"},
            {"SHA256": "c" * 64, "server_relative_path": "../outside.jpg",
             "素材类型": "图片", "状态": "可使用"},
        ]
        with index.open("a", encoding="utf-8") as target:
            for record in invalid:
                target.write(json.dumps(record, ensure_ascii=False) + "\n")
        with self._catalog():
            items = private_domain_media.list_materials(300)
        self.assertEqual(["files/图片/good.jpg"], [item["relative_path"] for item in items])

    def test_resolver_only_accepts_current_index_declared_file(self):
        self._write_record("files/视频/allowed.mp4", "视频")
        undeclared = self.root / "files/视频/undeclared.mp4"
        undeclared.write_bytes(b"video")
        with self._catalog():
            allowed = private_domain_media.resolve_material("files/视频/allowed.mp4")
            denied = private_domain_media.resolve_material("files/视频/undeclared.mp4")
            escaped = private_domain_media.resolve_material("../outside.mp4")
        self.assertEqual((self.root / "files/视频/allowed.mp4").resolve(), allowed.path)
        allowed.close()
        self.assertIsNone(denied)
        self.assertIsNone(escaped)

    def test_hash_mismatch_is_neither_listed_nor_resolved(self):
        relative_path = "files/图片/mismatch.jpg"
        self._write_record(relative_path, "图片", "f" * 64, content=b"actual")
        with self._catalog():
            self.assertEqual([], private_domain_media.list_materials(300))
            self.assertIsNone(private_domain_media.resolve_material(relative_path))

    def test_file_replaced_after_listing_fails_closed_before_streaming(self):
        relative_path = "files/视频/replaced.mp4"
        self._write_record(relative_path, "视频", content=b"reviewed bytes")
        path = self.root.joinpath(*pathlib.PurePosixPath(relative_path).parts)
        with self._catalog():
            self.assertEqual(
                [relative_path],
                [item["relative_path"] for item in private_domain_media.list_materials(300)],
            )
            path.write_bytes(b"replacement bytes")
            self.assertIsNone(private_domain_media.resolve_material(relative_path))

    def test_source_replaced_after_resolution_fails_closed(self):
        relative_path = "files/视频/snapshot.mp4"
        reviewed = b"reviewed bytes"
        self._write_record(relative_path, "视频", content=reviewed)
        path = self.root.joinpath(*pathlib.PurePosixPath(relative_path).parts)
        with self._catalog():
            resolved = private_domain_media.resolve_material(relative_path)
            self.assertIsNotNone(resolved)
            path.write_bytes(b"in-place replacement")
            with self.assertRaisesRegex(OSError, "not readable"):
                resolved.open("rb")
            resolved.close()

    def test_one_byte_range_reuses_snapshot_without_copying_source(self):
        from content_domains import core

        class Handler:
            def __init__(self):
                self.headers = {"Range": "bytes=0-0"}
                self.wfile = io.BytesIO()
                self.status = None
                self.response_headers = {}

            def send_response(self, status):
                self.status = status

            def send_header(self, name, value):
                self.response_headers[name] = value

            def end_headers(self):
                pass

        relative_path = "files/视频/large.mp4"
        content = b"a" * (12 * 1024 * 1024)
        self._write_record(relative_path, "视频", content=content)
        with self._catalog():
            self.assertEqual(1, len(private_domain_media.list_materials(300)))
            with mock.patch.object(
                    private_domain_media, "_materialize_snapshot",
                    wraps=private_domain_media._materialize_snapshot) as materialize:
                resolved = private_domain_media.resolve_material(relative_path)
                handler = Handler()
                core._send_out_file(handler, resolved, sensitive=True)
        self.assertEqual(0, materialize.call_count)
        self.assertEqual(206, handler.status)
        self.assertEqual("1", handler.response_headers["Content-Length"])
        self.assertEqual(b"a", handler.wfile.getvalue())
        self.assertEqual(1, len(list(self.snapshot_root.glob("*.blob"))))

    def test_concurrent_ranges_reuse_one_content_addressed_snapshot(self):
        relative_path = "files/视频/concurrent.mp4"
        self._write_record(relative_path, "视频", content=b"range-content")
        with self._catalog():
            private_domain_media.list_materials(300)
            with mock.patch.object(
                    private_domain_media, "_materialize_snapshot",
                    wraps=private_domain_media._materialize_snapshot) as materialize:
                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                    resolved = list(pool.map(
                        lambda _: private_domain_media.resolve_material(relative_path),
                        range(16),
                    ))
        try:
            self.assertTrue(all(item is not None for item in resolved))
            identities = {
                private_domain_media._snapshot_identity(item.stat())
                for item in resolved
            }
            self.assertEqual(1, len(identities))
            self.assertEqual(0, materialize.call_count)
            self.assertEqual(1, len(list(self.snapshot_root.glob("*.blob"))))
        finally:
            for item in resolved:
                if item is not None:
                    item.close()

    def test_snapshot_cache_capacity_is_strict_and_fail_closed(self):
        self._write_record("files/视频/first.mp4", "视频", content=b"123456")
        self._write_record("files/视频/second.mp4", "视频", content=b"abcdef")
        with self._catalog(), mock.patch.object(
                private_domain_media, "SNAPSHOT_CACHE_MAX_BYTES", 10):
            items = private_domain_media.list_materials(300)
        self.assertEqual(1, len(items))
        self.assertLessEqual(sum(
            path.stat().st_size for path in self.snapshot_root.iterdir()
            if path.is_file()
        ), 10)

    def test_restart_removes_orphaned_part_before_capacity_decision(self):
        self._write_record("files/视频/restart.mp4", "视频", content=b"v")
        with self._catalog():
            self.assertEqual(1, len(private_domain_media.list_materials(300)))
        orphan = self.snapshot_root / ".pending-crashed.part"
        orphan.write_bytes(b"x" * 12)
        private_domain_media._CACHE_KEY = None
        private_domain_media._CACHE_ITEMS = ()
        private_domain_media._CACHE_WATCHED = ()
        with self._catalog(), mock.patch.object(
                private_domain_media, "SNAPSHOT_CACHE_MAX_BYTES", 10):
            items = private_domain_media.list_materials(300)
        self.assertEqual(1, len(items))
        self.assertFalse(orphan.exists())
        self.assertLessEqual(sum(
            path.stat().st_size for path in self.snapshot_root.iterdir()
            if path.is_file()
        ), 10)

    def test_active_part_is_protected_by_cross_process_cache_lock(self):
        self._write_record("files/视频/active.mp4", "视频", content=b"active")
        context = multiprocessing.get_context("spawn")
        active = context.Event()
        release = context.Event()

        with self._catalog():
            cache_root = private_domain_media._snapshot_cache_root()
            part = cache_root / ".pending-active.part"
            holder = context.Process(
                target=_hold_snapshot_cache_lock,
                args=(str(cache_root), active, release),
            )
            holder.start()
            try:
                self.assertTrue(active.wait(timeout=3))
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(private_domain_media.list_materials, 300)
                    time.sleep(0.1)
                    self.assertFalse(future.done())
                    self.assertTrue(part.exists())
                    release.set()
                    items = future.result(timeout=5)
            finally:
                release.set()
                holder.join(timeout=3)
                if holder.is_alive():
                    holder.terminate()
                    holder.join(timeout=2)

        self.assertFalse(holder.is_alive())
        self.assertEqual(0, holder.exitcode)
        self.assertEqual(1, len(items))
        self.assertFalse(part.exists())

    def test_uncleanable_orphan_counts_toward_capacity_and_fails_closed(self):
        self.snapshot_root.mkdir(parents=True)
        orphan = self.snapshot_root / ".pending-uncleanable.part"
        orphan.write_bytes(b"x" * 12)
        self._write_record("files/视频/blocked.mp4", "视频", content=b"v")
        original_remove = private_domain_media._remove_snapshot

        def deny_orphan_removal(path):
            if path == orphan:
                return False
            return original_remove(path)

        with self._catalog(), mock.patch.object(
                private_domain_media, "SNAPSHOT_CACHE_MAX_BYTES", 10), \
                mock.patch.object(
                    private_domain_media, "_remove_snapshot",
                    side_effect=deny_orphan_removal):
            items = private_domain_media.list_materials(300)

        self.assertEqual([], items)
        self.assertTrue(orphan.exists())
        self.assertEqual([], list(self.snapshot_root.glob("*.blob")))
        self.assertGreater(sum(
            path.stat().st_size for path in self.snapshot_root.iterdir()
            if path.is_file()
        ), 10)

    def test_expired_snapshot_is_rebuilt_and_old_object_removed(self):
        relative_path = "files/视频/ttl.mp4"
        self._write_record(relative_path, "视频", content=b"ttl-content")
        with self._catalog(), mock.patch.object(
                private_domain_media, "SNAPSHOT_CACHE_TTL_SECONDS", 1):
            private_domain_media.list_materials(300)
            snapshot = next(self.snapshot_root.glob("*.blob"))
            self.assertEqual(0, snapshot.stat().st_mode & 0o222)
            os.utime(snapshot, (0, 0))
            with mock.patch.object(
                    private_domain_media, "_materialize_snapshot",
                    wraps=private_domain_media._materialize_snapshot) as materialize:
                items = private_domain_media.list_materials(300)
        self.assertEqual(1, len(items))
        self.assertEqual(1, materialize.call_count)
        self.assertEqual(1, len(list(self.snapshot_root.glob("*.blob"))))

    def test_snapshot_creation_failure_removes_partial_file(self):
        self._write_record("files/视频/failure.mp4", "视频", content=b"failure")
        with self._catalog(), mock.patch.object(
                private_domain_media.os, "replace",
                side_effect=OSError("injected replace failure")):
            self.assertEqual([], private_domain_media.list_materials(300))
        self.assertEqual([], list(self.snapshot_root.glob(".pending-*.part")))
        self.assertEqual([], list(self.snapshot_root.glob("*.blob")))

    def test_core_routes_require_authentication_and_stream_sensitive_files(self):
        source = (ROOT / "server/content_domains/core.py").read_text(encoding="utf-8")
        self.assertIn('if p == "/api/gen/private-domain/materials":', source)
        self.assertIn('if p == "/api/gen/private-domain/material":', source)
        route = source[source.index('if p == "/api/gen/private-domain/materials":'):
                       source.index('if p == "/api/gen/audio/slots":')]
        self.assertGreaterEqual(route.count("verify(self._token())"), 2)
        self.assertIn("private_domain_media.resolve_material", route)
        self.assertIn("_send_out_file(self, fp, sensitive=True)", route)
        self.assertIn("fp.close()", route)

    def test_skill_contains_no_generated_bgm_escape_hatch(self):
        skill_root = ROOT / "codex-skills/private-domain-short-video"
        self.assertFalse((skill_root / "scripts/generate_bgm.py").exists())
        for relative_path in ("SKILL.md", "references/music-and-editing.md"):
            source = (skill_root / relative_path).read_text(encoding="utf-8")
            self.assertIn("Do not generate", source)
            self.assertNotIn("generate_bgm", source)


if __name__ == "__main__":
    unittest.main()
