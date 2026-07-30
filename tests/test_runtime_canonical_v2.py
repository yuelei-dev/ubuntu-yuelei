import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import runtime_canonical as rc


class RuntimeCanonicalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "server").mkdir()
        (self.source / "server" / "app.py").write_text("print('ok')\n", encoding="utf-8")
        (self.source / "site").mkdir()
        (self.source / "site" / "index.html").write_text("<h1>ok</h1>\n", encoding="utf-8")
        self.scope = json.loads((ROOT / "deploy/runtime-canonical/scope.json").read_text(encoding="utf-8"))
        self.provenance = {
            "capture_kind": "repository-candidate",
            "captured_at_utc": "2026-07-30T00:00:00+00:00",
            "server_role": "test",
            "server_address": "8.148.158.106",
            "production_accessed": False,
            "source_revision": "test",
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_inventory_excludes_state_and_credentials(self):
        (self.source / "server" / "users.db").write_text("private", encoding="utf-8")
        manifest = rc.inventory(self.source, self.scope, self.provenance)
        self.assertEqual(["server/app.py", "site/index.html"], [item["path"] for item in manifest["files"]])
        self.assertIn(
            {"path": "server/users.db", "reason": "excluded_suffix"},
            manifest["excluded"],
        )

    def test_secret_like_content_is_hard_failure(self):
        (self.source / "server" / "bad.py").write_text(
            'api_key="abcdefghijklmnop1234567890"\n', encoding="utf-8"
        )
        with self.assertRaises(rc.GateError):
            rc.inventory(self.source, self.scope, self.provenance)

    def test_tamper_is_hard_failure(self):
        manifest = rc.inventory(self.source, self.scope, self.provenance)
        (self.source / "server" / "app.py").write_text("print('changed')\n", encoding="utf-8")
        with self.assertRaises(rc.GateError):
            rc.verify_tree(self.source, manifest, self.scope)

    def test_release_is_content_addressed_and_immutable(self):
        manifest = rc.inventory(self.source, self.scope, self.provenance)
        releases = self.root / "releases"
        release = rc.build_release(self.source, manifest, self.scope, releases)
        self.assertEqual(manifest["content_id"], release.name)
        with self.assertRaises(rc.GateError):
            rc.build_release(self.source, manifest, self.scope, releases)

    def test_server_verified_gate_rejects_repository_candidate(self):
        manifest = rc.inventory(self.source, self.scope, self.provenance)
        with self.assertRaises(rc.GateError):
            rc.validate_manifest(manifest, self.scope, require_server_verified=True)

    def test_production_access_is_rejected(self):
        manifest = rc.inventory(self.source, self.scope, self.provenance)
        bad = copy.deepcopy(manifest)
        bad["provenance"]["production_accessed"] = True
        payload = dict(bad)
        payload.pop("content_id")
        bad["content_id"] = __import__("hashlib").sha256(rc.canonical_json(payload)).hexdigest()
        with self.assertRaises(rc.GateError):
            rc.validate_manifest(bad, self.scope, require_server_verified=False)


if __name__ == "__main__":
    unittest.main()
