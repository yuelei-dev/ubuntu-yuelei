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

    def tearDown(self):
        self.temporary.cleanup()

    def _write_record(self, relative_path, media_type, digest, **extra):
        path = self.root.joinpath(*pathlib.PurePosixPath(relative_path).parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((relative_path + "\n").encode("utf-8"))
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
        self._write_record("files/图片/a.jpg", "图片", "1" * 64)
        self._write_record("files/视频/b.mp4", "视频", "2" * 64)
        self._write_record("files/BGM/c.mp3", "BGM", "3" * 64)
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
        self._write_record("files/图片/good.jpg", "图片", "a" * 64)
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
        self._write_record("files/视频/allowed.mp4", "视频", "d" * 64)
        undeclared = self.root / "files/视频/undeclared.mp4"
        undeclared.write_bytes(b"video")
        with self._catalog():
            allowed = private_domain_media.resolve_material("files/视频/allowed.mp4")
            denied = private_domain_media.resolve_material("files/视频/undeclared.mp4")
            escaped = private_domain_media.resolve_material("../outside.mp4")
        self.assertEqual((self.root / "files/视频/allowed.mp4").resolve(), allowed)
        self.assertIsNone(denied)
        self.assertIsNone(escaped)

    def test_core_routes_require_authentication_and_stream_sensitive_files(self):
        source = (ROOT / "server/content_domains/core.py").read_text(encoding="utf-8")
        self.assertIn('if p == "/api/gen/private-domain/materials":', source)
        self.assertIn('if p == "/api/gen/private-domain/material":', source)
        route = source[source.index('if p == "/api/gen/private-domain/materials":'):
                       source.index('if p == "/api/gen/audio/slots":')]
        self.assertGreaterEqual(route.count("verify(self._token())"), 2)
        self.assertIn("private_domain_media.resolve_material", route)
        self.assertIn("_send_out_file(self, fp, sensitive=True)", route)


if __name__ == "__main__":
    unittest.main()
