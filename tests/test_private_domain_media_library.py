import hashlib
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from content_domains import private_domain_media


class PrivateDomainMediaLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
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
            {"PRIVATE_DOMAIN_MATERIAL_ROOT": str(self.root)},
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

    def test_verified_stream_uses_immutable_snapshot_after_resolution(self):
        relative_path = "files/视频/snapshot.mp4"
        reviewed = b"reviewed bytes"
        self._write_record(relative_path, "视频", content=reviewed)
        path = self.root.joinpath(*pathlib.PurePosixPath(relative_path).parts)
        with self._catalog():
            resolved = private_domain_media.resolve_material(relative_path)
            self.assertIsNotNone(resolved)
            path.write_bytes(b"in-place replacement")
            with resolved.open("rb") as source:
                self.assertEqual(reviewed, source.read())

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
