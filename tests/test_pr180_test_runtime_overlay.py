# -*- coding: utf-8 -*-
import hashlib
import importlib.util
import json
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
OVERLAY = ROOT / "deploy/test-runtime/pr180"
MANIFEST_PATH = OVERLAY / "manifest.json"
MANIFEST = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
ENTRIES = MANIFEST["deploy_files"]
VIDEO = (OVERLAY / "runtime/site/workbench/video.html").read_text(encoding="utf-8")
POINTS = (OVERLAY / "runtime/server/content_domains/points.py").read_text(encoding="utf-8")
SHIP = (ROOT / "ship").read_text(encoding="utf-8")


class Pr180OverlayManifestTests(unittest.TestCase):
    def test_exact_scope_and_absent_new_files(self):
        sources = {entry["source"] for entry in ENTRIES}
        self.assertEqual(13, len(sources))
        self.assertNotIn("server/content_domains/local_reverse_upload.py", sources)
        self.assertEqual(
            {"server/pricing_config.py", "site/admin/pricing.js", "site/workbench/pricing-gate.js"},
            {entry["source"] for entry in ENTRIES if entry["expected_target_state"] == "absent"},
        )
        self.assertTrue(all(entry["target"].startswith(("/home/ubuntu/", "/var/www/huangquechuanmei/"))
                            for entry in ENTRIES))

    def test_committed_sources_match_manifest_hashes(self):
        for entry in ENTRIES:
            source = ROOT / entry["source"]
            self.assertTrue(source.is_file(), entry["source"])
            self.assertEqual(entry["sha256"], hashlib.sha256(source.read_bytes()).hexdigest(), entry["source"])
            blob = subprocess.check_output(["git", "hash-object", "--", str(source)], cwd=ROOT, text=True).strip()
            self.assertEqual(entry["git_blob"], blob, entry["source"])
            self.assertNotIn("PENDING", (entry["sha256"], entry["git_blob"]))

    def test_overlay_builder_and_generic_validator_pass(self):
        for command in (
            ["python", str(OVERLAY / "build_overlay.py"), "--check"],
            ["python", str(ROOT / "scripts/test_runtime_overlay.py"), "validate",
             str(MANIFEST_PATH.relative_to(ROOT)), *[entry["source"] for entry in ENTRIES]],
        ):
            result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)


class Pr180OverlayPricingContractTests(unittest.TestCase):
    def test_test_only_video_features_are_preserved(self):
        for marker in ("submitSora", "SORA_RATES", "seedanceAvailable", "omniAvailable",
                       "officialVideoHealthKnown", "data-function=\"sora\""):
            self.assertIn(marker, VIDEO)

    def test_authoritative_catalog_fails_closed_all_paid_main_paths(self):
        self.assertIn("requiredKeys:VIDEO_PRICING_KEYS", VIDEO)
        self.assertIn("var PRICING_VALUES={};", VIDEO)
        self.assertIn("loadPricingValues();", VIDEO)
        for name in ("submitCreateAvatar", "submitCinematic", "submitTryon",
                     "submitVideoBatch", "submitVideo", "submitXiaole"):
            marker = "function %s(" % name
            start = VIDEO.index(marker)
            body = VIDEO.index("{", start)
            self.assertEqual("if(!ensurePricingReady()) return;",
                             VIDEO[body + 1:body + 100].strip().splitlines()[0], name)
        for element_id in ("generateBtn", "cineGenerateBtn", "tryonGenerateBtn",
                           "grokGenerateBtn", "microGenerateBtn", "omniGenerateBtn"):
            self.assertRegex(VIDEO, r'id="%s"[^>]*\bdisabled\b' % element_id)

    def test_dynamic_rates_cover_test_seedance_and_omni_without_touching_sora(self):
        self.assertIn("selectedSeedanceDuration*rate", VIDEO)
        self.assertIn("selectedOmniDuration*rate", VIDEO)
        self.assertIn("PRICING_VALUES['xiaole_video.per_sec']", VIDEO)
        self.assertNotIn("selectedSeedanceDuration*30", VIDEO)
        self.assertNotIn("selectedOmniDuration*30", VIDEO)
        self.assertIn("var SORA_RATES=", VIDEO)
        self.assertIn("rate = SORA_VIDEO_RATE.get", POINTS)
        self.assertIn('pricing_config.get_price("xiaole_video.per_sec")', POINTS)

    def test_backend_overlay_uses_shared_pricing_module(self):
        paths = [
            "runtime/server/admin_api.py", "runtime/server/content_domains/audio.py",
            "runtime/server/content_domains/core.py", "runtime/server/content_domains/points.py",
            "runtime/server/content_domains/video.py", "runtime/server/imggen_api.py",
            "runtime/server/leadgen_api.py",
        ]
        for path in paths:
            self.assertIn("pricing_config", (OVERLAY / path).read_text(encoding="utf-8"), path)


class TestRuntimeDeploymentGateTests(unittest.TestCase):
    def test_ship_requires_test_role_pr_exact_main_and_preflights_before_push(self):
        for marker in ("HQ_SHIP_TARGET=test", "8.148.158.106", "必须显式传 --pr",
                       "HEAD == origin/main", "test-runtime 全量预哈希", "preimage 漂移",
                       "expected-absent.txt", "restore.sh"):
            self.assertIn(marker, SHIP)
        preflight = SHIP.index('echo "==> 2.5/5 test-runtime 全量预哈希')
        deployment_loop = SHIP.index('for f in "$@"; do\n  if [ -n "$OVERLAY_MANIFEST" ]')
        self.assertLess(preflight, deployment_loop)
        self.assertIn("测试服本机 nginx /api/gen/health", SHIP)
        self.assertIn("权威价格接口返回完整 13 key", SHIP)

    def test_drift_overlay_mapping_comes_from_committed_manifest(self):
        spec = importlib.util.spec_from_file_location("drift_overlay_test", ROOT / "scripts/drift_sentinel.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        payload = MANIFEST_PATH.read_bytes()
        module.git = lambda args, check=False: SimpleNamespace(returncode=0, stdout=payload, stderr=b"")
        source = "deploy/test-runtime/pr180/runtime/site/workbench/video.html"
        self.assertEqual("/var/www/huangquechuanmei/workbench/video.html",
                         module.git_path_to_runtime(source))
        self.assertIsNone(module.git_path_to_runtime("deploy/test-runtime/pr180/runtime/site/workbench/not-declared.html"))


if __name__ == "__main__":
    unittest.main()
