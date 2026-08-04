import hashlib
import importlib.util
import io
import json
import pathlib
import subprocess
import sys
import tempfile
import types
import unittest
import urllib.request
from contextlib import closing, redirect_stdout
from unittest import mock

from tests import test_drift_active_overlay as drift_overlay_tests
from tests import test_test_runtime_ship_rollback as ship_rollback_tests


ROOT = pathlib.Path(__file__).resolve().parents[1]
DEPLOY_DIR = ROOT / "deploy" / "test-runtime" / "pr182"
MANIFEST_PATH = DEPLOY_DIR / "manifest.json"
PR180_MANIFEST_PATH = ROOT / "deploy" / "test-runtime" / "pr180" / "manifest.json"
FEATURE_MERGE = "bbfeac84d4e60811ba9988bcb468438a94921427"
DEPLOYMENT_BASE = "4d5e86d3e2eed2eb43336d12b05d8945fe6f9f30"
PR180_DEPLOYMENT_MERGE = "55de5bdae592316aa9f542c2be0d5181eda7a355"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


overlay_tool = load_module("pr182_overlay_tool", ROOT / "scripts" / "test_runtime_overlay.py")
builder_tool = load_module("pr182_builder_tool", DEPLOY_DIR / "build_overlay.py")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def git_blob(data):
    header = ("blob %d\0" % len(data)).encode("ascii")
    return hashlib.sha1(header + data).hexdigest()


class Pr182DeploymentManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        cls.pr180 = json.loads(PR180_MANIFEST_PATH.read_text(encoding="utf-8"))

    def test_feature_and_deployment_bases_are_exact(self):
        self.assertEqual(self.manifest["source_pr"], 182)
        self.assertEqual(self.manifest["feature_merge_commit"], FEATURE_MERGE)
        self.assertEqual(self.manifest["feature_merge_tree"], "7e27c461d344019c941bb9f2548f366d4bb6e8cb")
        self.assertEqual(self.manifest["deployment_base_commit"], DEPLOYMENT_BASE)
        self.assertEqual(
            self.manifest["required_predecessor"],
            {
                "manifest": "deploy/test-runtime/pr180/manifest.json",
                "merge_commit": PR180_DEPLOYMENT_MERGE,
                "reason": "PR182 core and points build on the PR180 dynamic-pricing runtime; overlapping preimages are pinned to the deployed PR180 postimages",
            },
        )

    def test_manifest_contains_complete_minimal_runtime_dependency_set(self):
        entries = self.manifest["deploy_files"]
        self.assertEqual(len(entries), 9)
        self.assertEqual(
            {entry["feature_source_path"] for entry in entries},
            {
                "server/content_domains/breakdown.py",
                "server/content_domains/core.py",
                "server/content_domains/jobs_store.py",
                "server/content_domains/payment_recovery.py",
                "server/content_domains/points.py",
                "server/content_domains/submission_idempotency.py",
                "site/workbench/script.html",
                "server/pricing_config.py",
                "scripts/drift_sentinel.py",
            },
        )
        self.assertEqual(
            {service for entry in entries for service in entry["services"]},
            {"huangque-content"},
        )

    def test_generic_overlay_validator_accepts_exact_source_set(self):
        sources = [entry["source"] for entry in self.manifest["deploy_files"]]
        output = io.StringIO()
        with redirect_stdout(output):
            overlay_tool.validate("deploy/test-runtime/pr182/manifest.json", sources)
        self.assertIn("9 exact files", output.getvalue())

    def test_generic_overlay_validator_rejects_missing_or_extra_source(self):
        sources = [entry["source"] for entry in self.manifest["deploy_files"]]
        with self.assertRaisesRegex(ValueError, "exact source set mismatch"):
            overlay_tool.validate("deploy/test-runtime/pr182/manifest.json", sources[:-1])
        with self.assertRaisesRegex(ValueError, "exact source set mismatch"):
            overlay_tool.validate("deploy/test-runtime/pr182/manifest.json", sources + ["server/core.py"])

    def test_every_postimage_and_preimage_is_hash_and_blob_pinned(self):
        for entry in self.manifest["deploy_files"]:
            source = ROOT / entry["source"]
            data = source.read_bytes()
            self.assertEqual(sha256(data), entry["sha256"], entry["source"])
            self.assertEqual(git_blob(data), entry["git_blob"], entry["source"])
            self.assertTrue(entry["preimage_evidence"])
            if entry["expected_target_state"] == "absent":
                self.assertIsNone(entry["expected_target_sha256"])
                self.assertIsNone(entry["expected_target_git_blob"])
                continue
            if entry.get("preimage"):
                preimage = (ROOT / entry["preimage"]).read_bytes()
            else:
                preimage = subprocess.check_output(
                    ["git", "show", entry["preimage_ref"]], cwd=ROOT
                )
            self.assertEqual(sha256(preimage), entry["expected_target_sha256"], entry["target"])
            self.assertEqual(git_blob(preimage), entry["expected_target_git_blob"], entry["target"])

    def test_overlap_is_explicit_and_pinned_to_pr180_postimages(self):
        old = {entry["target"]: entry for entry in self.pr180["deploy_files"]}
        new = {entry["target"]: entry for entry in self.manifest["deploy_files"]}
        overlaps = set(old) & set(new)
        self.assertEqual(
            overlaps,
            {
                "/home/ubuntu/content-api/content_domains/core.py",
                "/home/ubuntu/content-api/content_domains/points.py",
                "/home/ubuntu/content-api/pricing_config.py",
                "/home/ubuntu/hq-drift/drift_sentinel.py",
            },
        )
        self.assertEqual(self.manifest["overrides"], ["deploy/test-runtime/pr180/manifest.json"])
        for target in overlaps:
            self.assertEqual(new[target]["expected_target_sha256"], old[target]["sha256"], target)
            self.assertEqual(new[target]["expected_target_git_blob"], old[target]["git_blob"], target)

    def test_runtime_contracts_keep_pr182_payment_and_pr180_pricing(self):
        by_path = {
            entry["feature_source_path"]: (ROOT / entry["source"]).read_text(encoding="utf-8")
            for entry in self.manifest["deploy_files"]
            if entry["source"].endswith(".py")
        }
        self.assertIn("payment_recovery.reconcile_local_uploads", by_path["server/content_domains/core.py"])
        self.assertIn("def refund_once_recoverable", by_path["server/content_domains/jobs_store.py"])
        self.assertIn("owner=\"content\"", by_path["server/content_domains/payment_recovery.py"])
        self.assertIn("def breakdown_local_upload_cost", by_path["server/content_domains/points.py"])
        self.assertIn("pricing_config.get_price(\"breakdown.local_upload\")", by_path["server/content_domains/points.py"])
        self.assertIn("transaction_key", by_path["server/content_domains/points.py"])

    def test_points_overlay_is_exact_pr180_postimage_plus_local_upload_helper(self):
        baseline = (
            ROOT / "deploy" / "test-runtime" / "pr180" / "runtime" /
            "server" / "content_domains" / "points.py"
        ).read_text(encoding="utf-8")
        overlay = (
            DEPLOY_DIR / "runtime" / "server" / "content_domains" / "points.py"
        ).read_text(encoding="utf-8")
        helper = (
            '\n\ndef breakdown_local_upload_cost():\n'
            '    """Authoritative price for one local image/video reverse submission."""\n'
            '    return pricing_config.get_price("breakdown.local_upload")\n'
        )
        expected = baseline.replace("\n\ndef breakdown_batch_refund", helper + "\n\ndef breakdown_batch_refund", 1)
        self.assertEqual(overlay, expected)

    def test_points_overlay_really_imports_and_keeps_dynamic_and_frozen_prices(self):
        package = "_pr182_runtime_probe"
        module_names = [
            package, package + ".content_domains", package + ".content_domains.core",
            "pricing_config", "func_names",
        ]
        saved = {name: sys.modules.get(name) for name in module_names}
        prices = {
            "collect.main": 41,
            "collect.transcript": 7,
            "leads": 53,
            "image.openai.hd": 37,
            "tryon.single": 27,
            "tryon.combo": 45,
            "xiaole_video.per_sec": 32,
            "breakdown.per_link": 25,
            "breakdown.local_upload": 37,
            "copy": 17,
        }
        root_package = types.ModuleType(package)
        root_package.__path__ = []
        domains_package = types.ModuleType(package + ".content_domains")
        domains_package.__path__ = []
        core = types.ModuleType(package + ".content_domains.core")
        core.AUTH_BASE = ""
        core.AUTH_INTERNAL_TOKEN = ""
        core.COST = {"fallback": 9}
        core.closing = closing
        core.jdb = lambda: None
        core.json = json
        core.urllib = urllib.request
        core._ensure_column = lambda *args, **kwargs: None
        pricing = types.ModuleType("pricing_config")
        func_names = types.ModuleType("func_names")
        func_names.func_name = lambda kind, payload: str(kind or "")

        def get_price(key):
            if key not in prices:
                raise RuntimeError("missing price: " + key)
            return prices[key]

        pricing.get_price = get_price
        sys.modules[package] = root_package
        sys.modules[package + ".content_domains"] = domains_package
        sys.modules[package + ".content_domains.core"] = core
        sys.modules["pricing_config"] = pricing
        sys.modules["func_names"] = func_names
        try:
            points = load_module(
                package + ".content_domains.points",
                DEPLOY_DIR / "runtime" / "server" / "content_domains" / "points.py",
            )
            self.assertEqual(points.cost_of("collect", {"want": ["comments"]}), 41)
            self.assertEqual(points.cost_of("collect", {"want": ["transcript"]}), 7)
            self.assertEqual(points.cost_of("leads", {}), 53)
            self.assertEqual(points.cost_of("image", {"provider": "openai", "quality": "hd", "count": 2}), 74)
            self.assertEqual(points.cost_of("tryon", {"clothes_data": "x"}), 27)
            self.assertEqual(points.cost_of("xiaole_video", {"channel": "micro", "duration": 3}), 96)
            self.assertEqual(points.cost_of("breakdown", {"urls": ["a", "b", "c", "d"]}), 100)
            self.assertEqual(points.breakdown_local_upload_cost(), 37)
            self.assertEqual(points.cost_of("copy", {}), 17)
            self.assertEqual(points.cost_of("fallback", {}), 9)
            self.assertEqual(points.breakdown_batch_refund(100, 4, 2), 50)

            prices.pop("breakdown.local_upload")
            with self.assertRaisesRegex(RuntimeError, "missing price"):
                points.breakdown_local_upload_cost()
        finally:
            sys.modules.pop(package + ".content_domains.points", None)
            for name, value in saved.items():
                if value is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = value

    def test_builder_reproduces_and_verifies_all_feature_artifacts(self):
        result = subprocess.run(
            [__import__("sys").executable, str(DEPLOY_DIR / "build_overlay.py"), "--check"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertIn("verification passed: 9 exact dependencies", result.stdout)

    def test_builder_is_self_contained_in_a_shallow_checkout(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(builder_tool, "object_exists", return_value=False):
            with redirect_stdout(stdout), __import__("contextlib").redirect_stderr(stderr):
                result = builder_tool.check()
        self.assertEqual(result, 0, stderr.getvalue())
        self.assertIn("verification passed: 9 exact dependencies", stdout.getvalue())

    def test_invalid_override_declaration_is_rejected(self):
        document = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        document["overrides"] = ["../outside.json"]
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            path = pathlib.Path(directory) / "manifest.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "invalid overlay overrides"):
                overlay_tool.load_manifest(path)

    def test_legacy_exception_registry_is_not_rewritten_by_unapplied_overlay(self):
        exceptions = json.loads((ROOT / "deploy" / "test-server-exceptions.json").read_text(encoding="utf-8"))
        paths = {entry["path"] for entry in exceptions["exceptions"]}
        self.assertIn("server/content_domains/breakdown.py", paths)
        self.assertIn("server/content_domains/jobs_store.py", paths)
        self.assertIn("site/workbench/script.html", paths)


class Pr182LayeredDriftSentinelTests(drift_overlay_tests.ActiveOverlayDriftTests):
    """Run the baseline drift contracts plus explicit stacking on the PR182 artifact."""

    def setUp(self):
        self._old_sentinel_path = drift_overlay_tests.SENTINEL_PATH
        drift_overlay_tests.SENTINEL_PATH = DEPLOY_DIR / "runtime" / "scripts" / "drift_sentinel.py"
        super().setUp()

    def tearDown(self):
        try:
            super().tearDown()
        finally:
            drift_overlay_tests.SENTINEL_PATH = self._old_sentinel_path

    def _commit_overlapping_overlay(self, name, content, overrides=None):
        overlay = self.repo / "deploy" / "test-runtime" / name
        source = overlay / "runtime" / "site" / "changed.html"
        source.parent.mkdir(parents=True)
        source.write_bytes(content)
        document = {
            "schema_version": 1,
            "target": "test:8.148.158.106",
            "deploy_files": [{
                "source": "deploy/test-runtime/%s/runtime/site/changed.html" % name,
                "target": "/var/www/huangquechuanmei/changed.html",
                "sha256": hashlib.sha256(content).hexdigest(),
            }],
        }
        if overrides is not None:
            document["overrides"] = overrides
        (overlay / "manifest.json").write_text(json.dumps(document), encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "overlay " + name)
        return self.git("rev-parse", "HEAD").stdout.decode().strip()

    def test_explicit_later_overlay_can_override_one_target_and_preserve_stack(self):
        old_sha = self._commit_overlapping_overlay("old", b"old postimage\n")
        new_sha = self._commit_overlapping_overlay(
            "new", b"new postimage\n",
            overrides=["deploy/test-runtime/old/manifest.json"],
        )
        self.ds.runtime_to_git_path = lambda target: (
            "site/changed.html" if target.endswith("/changed.html") else None
        )
        self.ds.activate_overlay("deploy/test-runtime/old/manifest.json", old_sha, pr="184")
        self.ds.activate_overlay("deploy/test-runtime/new/manifest.json", new_sha, pr="186")
        state = json.loads((self.drift_dir / "active_overlays.json").read_text(encoding="utf-8"))
        self.assertEqual(2, len(state["overlays"]))
        resolved = self.ds.load_active_overlay_expectations()
        self.assertEqual(new_sha, resolved["site/changed.html"]["deploy_sha"])

    def test_undeclared_overlap_is_rejected_without_changing_active_state(self):
        old_sha = self._commit_overlapping_overlay("old", b"old postimage\n")
        new_sha = self._commit_overlapping_overlay("new", b"new postimage\n")
        self.ds.runtime_to_git_path = lambda target: (
            "site/changed.html" if target.endswith("/changed.html") else None
        )
        self.ds.activate_overlay("deploy/test-runtime/old/manifest.json", old_sha, pr="184")
        state_path = self.drift_dir / "active_overlays.json"
        before = state_path.read_bytes()
        with self.assertRaisesRegex(ValueError, "未显式覆盖"):
            self.ds.activate_overlay("deploy/test-runtime/new/manifest.json", new_sha, pr="186")
        self.assertEqual(before, state_path.read_bytes())


@unittest.skipUnless(ship_rollback_tests.os.name == "posix" and ship_rollback_tests.shutil.which("bash"), "requires POSIX bash")
class Pr182ExactManifestShipRollbackTests(ship_rollback_tests.TestRuntimeShipRollbackTests):
    """Run the unified atomic ship fault matrix against the PR182 source set."""

    def setUp(self):
        self._old_manifest = ship_rollback_tests.MANIFEST
        self._old_sources = ship_rollback_tests.SOURCES
        ship_rollback_tests.MANIFEST = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        ship_rollback_tests.SOURCES = [entry["source"] for entry in ship_rollback_tests.MANIFEST["deploy_files"]]
        super().setUp()

    def tearDown(self):
        try:
            super().tearDown()
        finally:
            ship_rollback_tests.MANIFEST = self._old_manifest
            ship_rollback_tests.SOURCES = self._old_sources


if __name__ == "__main__":
    unittest.main()
