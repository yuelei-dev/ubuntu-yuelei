# -*- coding: utf-8 -*-
import hashlib
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SENTINEL_PATH = ROOT / "scripts" / "drift_sentinel.py"


def load_sentinel():
    spec = importlib.util.spec_from_file_location("drift_active_overlay", SENTINEL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def md5(data):
    return hashlib.md5(data).hexdigest()


class ActiveOverlayDriftTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.repo = base / "repo"
        self.webroot = base / "webroot"
        self.drift_dir = base / "hq-drift"
        self.repo.mkdir()
        self.webroot.mkdir()
        self.drift_dir.mkdir()

        def git(*args):
            return subprocess.run(
                ["git", "-C", str(self.repo), *args], check=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )

        self.git = git
        git("init", "-q", "-b", "main")
        git("config", "user.email", "test@example.com")
        git("config", "user.name", "test")
        (self.repo / "site").mkdir()
        (self.repo / "site" / "changed.html").write_bytes(b"git baseline\n")
        git("add", ".")
        git("commit", "-q", "-m", "baseline")

        self.ds = load_sentinel()
        self.ds.REPO = str(self.repo)
        self.ds.GIT_REF = "HEAD"
        self.ds.WEBROOT = str(self.webroot)
        self.ds.DRIFT_DIR = str(self.drift_dir)
        self.ds.LOG = str(self.drift_dir / "sentinel.log")
        self.ds.DEPLOY_LOG = str(self.drift_dir / "deploy_bless.jsonl")
        self.ds.BASELINE = str(self.drift_dir / "baseline.json")
        self.ds.STATE = str(self.drift_dir / ".state.json")
        self.ds.BACKEND_RUNTIME = {}
        self.ds.CONTENT_DOMAINS_RUNTIME = str(base / "domains")
        self.ds.SYSTEMD_DIR = str(base / "systemd")

        self.exceptions = base / "exceptions.json"
        self.exceptions.write_text(json.dumps({
            "schema_version": 1,
            "exceptions": [{
                "path": "site/changed.html",
                "drift_kind": "changed",
                "expected": {
                    "type": "sha256",
                    "value": hashlib.sha256(b"old test-server preimage\n").hexdigest(),
                },
                "reason": "existing test-server exception",
                "registered": "2026-08-03",
            }],
        }), encoding="utf-8")
        self.old_exceptions = os.environ.get("HQ_DRIFT_EXCEPTIONS")
        os.environ["HQ_DRIFT_EXCEPTIONS"] = str(self.exceptions)

    def tearDown(self):
        if self.old_exceptions is None:
            os.environ.pop("HQ_DRIFT_EXCEPTIONS", None)
        else:
            os.environ["HQ_DRIFT_EXCEPTIONS"] = self.old_exceptions
        self.tmp.cleanup()

    def test_normal_patrol_uses_postimage_and_does_not_mark_old_exception_stale(self):
        postimage = b"deployed immutable postimage\n"
        runtime = self.webroot / "changed.html"
        runtime.write_bytes(postimage)
        self.ds.load_active_overlay_expectations = lambda: {
            "site/changed.html": {
                "runtime": str(runtime),
                "md5": md5(postimage),
                "deploy_sha": "a" * 40,
                "manifest": "deploy/test-runtime/pr180/manifest.json",
            }
        }

        result = self.ds.diff_paths()

        self.assertEqual([], result["changed"])
        self.assertEqual([], result["exceptions_stale"])
        self.assertEqual([], result["registered"])
        self.assertEqual(["site/changed.html"], result["active_overlays"])
        self.assertEqual(0, self.ds.handle_detect(print_only=True))

    def test_one_byte_postimage_drift_still_alarms(self):
        postimage = b"deployed immutable postimage\n"
        runtime = self.webroot / "changed.html"
        runtime.write_bytes(postimage)
        self.ds.load_active_overlay_expectations = lambda: {
            "site/changed.html": {
                "runtime": str(runtime),
                "md5": md5(postimage),
                "deploy_sha": "a" * 40,
                "manifest": "deploy/test-runtime/pr180/manifest.json",
            }
        }
        runtime.write_bytes(postimage[:-2] + b"X\n")

        result = self.ds.diff_paths()

        self.assertEqual(["site/changed.html"], result["changed"])
        self.assertEqual([], result["exceptions_stale"])
        self.assertEqual(1, self.ds.handle_detect(print_only=True))

    def test_activation_is_atomic_and_resolves_manifest_from_immutable_sha(self):
        overlay = self.repo / "deploy" / "test-runtime" / "prtest"
        source = overlay / "runtime" / "site" / "changed.html"
        source.parent.mkdir(parents=True)
        postimage = b"committed postimage\n"
        source.write_bytes(postimage)
        manifest = overlay / "manifest.json"
        manifest.write_text(json.dumps({
            "schema_version": 1,
            "target": "test:8.148.158.106",
            "deploy_files": [{
                "source": "deploy/test-runtime/prtest/runtime/site/changed.html",
                "target": "/var/www/huangquechuanmei/changed.html",
                "sha256": hashlib.sha256(postimage).hexdigest(),
            }],
        }), encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "overlay")
        deploy_sha = self.git("rev-parse", "HEAD").stdout.decode().strip()
        source.write_bytes(b"uncommitted working-tree replacement\n")

        self.ds.runtime_to_git_path = lambda target: (
            "site/changed.html" if target.endswith("/changed.html") else None
        )
        self.ds.activate_overlay(
            "deploy/test-runtime/prtest/manifest.json", deploy_sha, pr="184"
        )

        state_path = self.drift_dir / "active_overlays.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(1, state["schema_version"])
        self.assertEqual(deploy_sha, state["overlays"][0]["deploy_sha"])
        self.assertEqual("184", state["overlays"][0]["pr"])
        resolved = self.ds.load_active_overlay_expectations()
        self.assertEqual(md5(postimage), resolved["site/changed.html"]["md5"])
        self.assertFalse(list(self.drift_dir.glob("active_overlays.json.tmp.*")))

    def test_corrupt_active_overlay_state_fails_closed(self):
        (self.drift_dir / "active_overlays.json").write_text(
            '{"schema_version":1,"overlays":"not-an-array"}', encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "活动 overlay 清单无效"):
            self.ds.diff_paths()


if __name__ == "__main__":
    unittest.main()
