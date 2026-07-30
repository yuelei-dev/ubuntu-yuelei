import copy
import hashlib
import json
import os
import shutil
import stat
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
        (self.source / "deploy" / "systemd").mkdir(parents=True)
        (self.source / "deploy" / "systemd" / "app.service").write_text(
            "[Service]\nExecStart=/bin/true\n", encoding="utf-8"
        )
        (self.source / "deploy" / "nginx-huangquechuanmei.conf").write_text(
            "server {}\n", encoding="utf-8"
        )
        self.scope = json.loads(
            (ROOT / "deploy/runtime-canonical/scope.json").read_text(encoding="utf-8")
        )
        self.scope["repository_include_prefixes"] = [
            "server", "site", "deploy/systemd",
            "deploy/nginx-huangquechuanmei.conf",
        ]
        self.candidate_provenance = self.provenance("repository-candidate")

    def provenance(self, kind):
        return {
            "capture_kind": kind,
            "captured_at_utc": "2026-07-30T00:00:00+00:00",
            "server_role": "test",
            "server_address": "8.148.158.106",
            "production_accessed": False,
            "source_revision": "test",
        }

    def tearDown(self):
        self.temp.cleanup()

    def make_server_snapshot(self):
        snapshot = self.root / "snapshot"
        for root in self.scope["roots"]:
            path = snapshot / root["source"].lstrip("/")
            path.mkdir(parents=True)
            if root["source"] == "/etc/systemd/system":
                (path / "huangque-content.service").write_text(
                    "[Service]\nExecStart=/bin/true\n", encoding="utf-8"
                )
                (path / "unrelated.service").write_text(
                    "[Service]\nExecStart=/bin/false\n", encoding="utf-8"
                )
            elif root["source"] == "/etc/nginx":
                (path / "nginx.conf").write_text("events {}\n", encoding="utf-8")
            elif root["source"] == "/var/www/huangquechuanmei":
                (path / "index.html").write_text("<h1>test</h1>\n", encoding="utf-8")
            else:
                (path / "app.py").write_text("print('runtime')\n", encoding="utf-8")
        return snapshot

    def test_inventory_excludes_state_and_detects_exclusion_drift(self):
        manifest = rc.inventory(self.source, self.scope, self.candidate_provenance)
        (self.source / "server" / "users.db").write_text("private", encoding="utf-8")
        with self.assertRaises(rc.GateError):
            rc.verify_tree(self.source, manifest, self.scope)

    def test_secret_like_content_is_hard_failure(self):
        (self.source / "server" / "bad.py").write_text(
            'api_key="abcdefghijklmnop1234567890"\n', encoding="utf-8"
        )
        with self.assertRaises(rc.GateError):
            rc.inventory(self.source, self.scope, self.candidate_provenance)

    def test_large_text_and_nul_text_are_hard_failures(self):
        self.scope["max_text_file_bytes"] = 8
        (self.source / "server" / "large.json").write_text('{"long": "value"}', encoding="utf-8")
        with self.assertRaises(rc.GateError):
            rc.inventory(self.source, self.scope, self.candidate_provenance)
        (self.source / "server" / "large.json").unlink()
        self.scope["max_text_file_bytes"] = 1024
        (self.source / "server" / "nul.json").write_bytes(b'{"x":"a\x00b"}')
        with self.assertRaises(rc.GateError):
            rc.inventory(self.source, self.scope, self.candidate_provenance)

    def test_server_roots_are_required_mapped_and_systemd_is_allowlisted(self):
        snapshot = self.make_server_snapshot()
        manifest = rc.inventory(snapshot, self.scope, self.provenance("server-read-only"))
        paths = {item["path"] for item in manifest["files"]}
        self.assertIn("server/app.py", paths)
        self.assertIn("site/index.html", paths)
        self.assertIn("runtime/systemd/huangque-content.service", paths)
        self.assertNotIn("runtime/systemd/unrelated.service", paths)
        shutil_target = snapshot / "home/ubuntu/content-api"
        for child in shutil_target.iterdir():
            child.unlink()
        shutil_target.rmdir()
        with self.assertRaises(rc.GateError):
            rc.inventory(snapshot, self.scope, self.provenance("server-read-only"))

    def test_tamper_and_extra_release_files_are_hard_failures(self):
        manifest = rc.inventory(self.source, self.scope, self.candidate_provenance)
        releases = self.root / "releases"
        release = rc.build_release(self.source, manifest, self.scope, releases)
        rc.verify_release(release, manifest, self.scope, require_server_verified=False)
        release.chmod(0o755)
        extra = release / "unexpected.txt"
        extra.write_text("unexpected\n", encoding="utf-8")
        with self.assertRaises(rc.GateError):
            rc.verify_release(release, manifest, self.scope, require_server_verified=False)
        extra.unlink()
        target = release / "server/app.py"
        target.chmod(0o644)
        target.write_text("tampered\n", encoding="utf-8")
        with self.assertRaises(rc.GateError):
            rc.verify_release(release, manifest, self.scope, require_server_verified=False)

    def test_server_release_is_verified_against_directory_identity(self):
        snapshot = self.make_server_snapshot()
        manifest = rc.inventory(snapshot, self.scope, self.provenance("server-read-only"))
        release = rc.build_release(snapshot, manifest, self.scope, self.root / "server-releases")
        rc.verify_release(release, manifest, self.scope, require_server_verified=True)
        wrong = release.parent / ("f" * 64)
        release.rename(wrong)
        with self.assertRaises(rc.GateError):
            rc.verify_release(wrong, manifest, self.scope, require_server_verified=True)

    def test_release_is_content_addressed_and_not_overwritten(self):
        manifest = rc.inventory(self.source, self.scope, self.candidate_provenance)
        releases = self.root / "releases"
        release = rc.build_release(self.source, manifest, self.scope, releases)
        self.assertEqual(manifest["content_id"], release.name)
        with self.assertRaises(rc.GateError):
            rc.build_release(self.source, manifest, self.scope, releases)

    def test_server_verified_gate_rejects_repository_candidate(self):
        manifest = rc.inventory(self.source, self.scope, self.candidate_provenance)
        with self.assertRaises(rc.GateError):
            rc.validate_manifest(manifest, self.scope, require_server_verified=True)

    def test_production_access_is_rejected(self):
        manifest = rc.inventory(self.source, self.scope, self.candidate_provenance)
        bad = copy.deepcopy(manifest)
        bad["provenance"]["production_accessed"] = True
        payload = dict(bad)
        payload.pop("content_id")
        bad["content_id"] = hashlib.sha256(rc.canonical_json(payload)).hexdigest()
        with self.assertRaises(rc.GateError):
            rc.validate_manifest(bad, self.scope, require_server_verified=False)

    @unittest.skipUnless(os.name == "posix", "release controller requires Linux")
    def test_controller_rejects_missing_cas_and_candidate_release(self):
        manifest = rc.inventory(self.source, self.scope, self.candidate_provenance)
        releases = self.root / "releases"
        release = rc.build_release(self.source, manifest, self.scope, releases)
        ctl = ROOT / "scripts/runtime_release_ctl.sh"
        missing_cas = subprocess.run(
            ["bash", str(ctl), "activate", str(releases), str(self.root / "current"), release.name],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(0, missing_cas.returncode)
        candidate = subprocess.run(
            ["bash", str(ctl), "initialize", str(releases), str(self.root / "current"), release.name],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(0, candidate.returncode)
        self.assertIn("server-verified capture is required", candidate.stderr)


if __name__ == "__main__":
    unittest.main()
