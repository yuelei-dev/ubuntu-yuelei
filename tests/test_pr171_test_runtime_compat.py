import hashlib
import json
import pathlib
import subprocess
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
OVERLAY = ROOT / "deploy" / "test-runtime" / "pr171"
CORE = OVERLAY / "content_domains" / "core.py"
MANIFEST = OVERLAY / "manifest.json"


class Pr171TestRuntimeCompatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = CORE.read_text(encoding="utf-8")
        cls.manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    def test_overlay_is_pinned_to_the_observed_test_runtime(self):
        self.assertEqual(self.manifest["target"], "test:8.148.158.106")
        self.assertEqual(
            self.manifest["base_core_git_blob"],
            "22f4c73894dec69353bdac309749516ade41d98a",
        )
        digest = hashlib.sha256(CORE.read_bytes()).hexdigest()
        self.assertEqual(digest, self.manifest["target_core_sha256"])
        self.assertEqual(
            self.manifest["target_base_commit"],
            "c33b99412aa107c0b2c13215d31fe3aaaeaa1f57",
        )

    def test_every_deploy_source_is_hash_and_blob_pinned(self):
        for entry in self.manifest["deploy_files"]:
            source = ROOT / entry["source"]
            self.assertTrue(source.is_file(), entry["source"])
            self.assertEqual(
                hashlib.sha256(source.read_bytes()).hexdigest(),
                entry["sha256"],
            )
            blob = subprocess.check_output(
                ["git", "hash-object", "--", str(source)],
                cwd=ROOT, text=True,
            ).strip()
            self.assertEqual(blob, entry["git_blob"])

    def test_overlay_compiles_without_loading_runtime_dependencies(self):
        compile(self.source, str(CORE), "exec")

    def test_reference_staging_is_claimed_before_charge(self):
        route = self.source.index(
            "if not is_still_route: idem_state, idem_response = _idempotency_begin")
        claim = self.source.index("_idempotency_begin", route)
        precheck = self.source.index("xiaole_reference_precheck", claim)
        stage = self.source.index("stage_xiaole_video_references", precheck)
        charge = self.source.index("jobs_store.create_paid_job", stage)
        self.assertLess(claim, precheck)
        self.assertLess(precheck, stage)
        self.assertLess(stage, charge)
        self.assertIn("link_staged_seedance_references", self.source[stage:charge + 1000])
        upload_window = self.source[precheck:charge]
        self.assertLess(
            upload_window.index("_submission_lock.release()"),
            upload_window.index("stage_xiaole_video_references"),
        )
        self.assertLess(
            upload_window.index("stage_xiaole_video_references"),
            upload_window.index("_submission_lock.acquire()"),
        )
        self.assertGreaterEqual(upload_window.count("_user_video_submit_limit"), 1)
        self.assertGreaterEqual(upload_window.count("_user_active_job_count"), 1)

    def test_staged_objects_have_terminal_and_retry_cleanup(self):
        terminal = self.source.index("def _set_terminal")
        refund = self.source.index("def _refund_once", terminal)
        self.assertIn(
            "cleanup_job_staged_seedance_references",
            self.source[terminal:refund],
        )
        self.assertGreaterEqual(
            self.source.count("cleanup_staged_seedance_references"), 4)
        self.assertGreaterEqual(
            self.source.count("retry_pending_seedance_cleanups"), 2)

    def test_health_exposes_the_no_avatar_channel(self):
        health = self.source.index('if p == "/api/gen/health"')
        block = self.source[health:health + 1800]
        self.assertIn('"reverse_remake_video_channel"', block)
        self.assertIn('"seedance_reference_images_enabled"', block)
        self.assertIn('getattr(video_domain, "sora_video_is_open"', block)
        self.assertIn('getattr(video_domain, "omni_video_is_open"', block)
        self.assertIn('getattr(video_domain, "seedance_upscale_is_open"', block)

    def test_manifest_contains_only_test_runtime_targets(self):
        self.assertEqual(
            [entry["source"] for entry in self.manifest["deploy_files"]],
            [
                "deploy/test-runtime/pr171/content_domains/core.py",
                "server/content_domains/video.py",
                "site/workbench/script.html",
            ],
        )
        targets = [entry["target"] for entry in self.manifest["deploy_files"]]
        self.assertEqual(len(targets), 3)
        self.assertTrue(all(path.startswith(("/home/ubuntu/", "/var/www/")) for path in targets))
        serialized = MANIFEST.read_text(encoding="utf-8") + self.source
        for forbidden in ("BEGIN PRIVATE KEY", "BEGIN OPENSSH PRIVATE KEY"):
            self.assertNotIn(forbidden, serialized)


if __name__ == "__main__":
    unittest.main()
