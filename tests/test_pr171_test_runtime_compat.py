import hashlib
import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
OVERLAY = ROOT / "deploy" / "test-runtime" / "pr171"
CORE = OVERLAY / "content_domains" / "core.py"
MANIFEST = OVERLAY / "manifest.json"
VERIFY_PREDEPLOY = OVERLAY / "verify_predeploy.py"
SPEC = importlib.util.spec_from_file_location("pr171_verify_predeploy", VERIFY_PREDEPLOY)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("could not load PR171 predeploy verifier")
verify_predeploy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify_predeploy)


EXPECTED_TARGET_PREIMAGES = {
    "/home/ubuntu/content-api/content_domains/core.py": (
        "d8a75bfdc8f3bb09ed39f2b7f271f39190ca81123a73a4c393a2b73f045e98af",
        "22f4c73894dec69353bdac309749516ade41d98a",
    ),
    "/home/ubuntu/content-api/content_domains/video.py": (
        "4bb22ffc1bf49983b42ed67294ea42328b0254056d45690bc3c47ea874b07172",
        "b4892fc424afb2f293337e32e1c23784f5a3c98c",
    ),
    "/var/www/huangquechuanmei/workbench/script.html": (
        "5d9d77b839f3796ae9e3a4ee2725530a34c84b0fd935fd8a94b086ee4d761b38",
        "0922e656fedd1e8bcac40025cc8d2798f7cc337b",
    ),
}


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

    def test_every_target_preimage_is_pinned_to_the_read_only_snapshot(self):
        self.assertEqual(self.manifest["schema_version"], 2)
        self.assertEqual(
            self.manifest["target_preimage_observed_at_utc"],
            "2026-08-01T09:56:20Z",
        )
        self.assertEqual(
            self.manifest["predeploy_verifier"],
            "python3 deploy/test-runtime/pr171/verify_predeploy.py",
        )
        actual = {
            entry["target"]: (
                entry["expected_target_sha256"],
                entry["expected_target_git_blob"],
            )
            for entry in self.manifest["deploy_files"]
        }
        self.assertEqual(actual, EXPECTED_TARGET_PREIMAGES)

    def test_predeploy_default_repo_root_is_the_checkout_root(self):
        self.assertEqual(verify_predeploy.REPO_ROOT, ROOT)

    def test_predeploy_cli_uses_default_repo_root_when_option_is_omitted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            targets = root / "targets"
            source_relative = pathlib.PurePosixPath("server/content_domains/video.py")
            source = ROOT.joinpath(*source_relative.parts)
            target = targets / "opt" / "video.py"
            target.parent.mkdir(parents=True)
            target.write_bytes(source.read_bytes())
            data = source.read_bytes()
            header = f"blob {len(data)}\0".encode("ascii")
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "deploy_files": [
                            {
                                "source": str(source_relative),
                                "target": "/opt/video.py",
                                "sha256": hashlib.sha256(data).hexdigest(),
                                "git_blob": hashlib.sha1(header + data).hexdigest(),
                                "expected_target_sha256": hashlib.sha256(data).hexdigest(),
                                "expected_target_git_blob": hashlib.sha1(header + data).hexdigest(),
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(VERIFY_PREDEPLOY),
                    "--manifest",
                    str(manifest),
                    "--target-root",
                    str(targets),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("verification passed", result.stdout)

    def test_predeploy_verifier_fails_closed_on_any_source_or_target_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            repo = root / "repo"
            targets = root / "targets"
            source = repo / "artifact.py"
            target = targets / "opt" / "artifact.py"
            source.parent.mkdir(parents=True)
            target.parent.mkdir(parents=True)
            source.write_bytes(b"new artifact\n")
            target.write_bytes(b"old artifact\n")

            def digest(data):
                return hashlib.sha256(data).hexdigest()

            def blob(data):
                header = f"blob {len(data)}\0".encode("ascii")
                return hashlib.sha1(header + data).hexdigest()

            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "deploy_files": [
                            {
                                "source": "artifact.py",
                                "target": "/opt/artifact.py",
                                "sha256": digest(source.read_bytes()),
                                "git_blob": blob(source.read_bytes()),
                                "expected_target_sha256": digest(target.read_bytes()),
                                "expected_target_git_blob": blob(target.read_bytes()),
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                verify_predeploy.verify_predeploy(manifest, repo, targets),
                [],
            )

            target.write_bytes(b"unexpected target drift\n")
            errors = verify_predeploy.verify_predeploy(manifest, repo, targets)
            self.assertTrue(any("target preimage sha256 mismatch" in item for item in errors))
            self.assertTrue(any("target preimage git blob mismatch" in item for item in errors))

            target.write_bytes(b"old artifact\n")
            source.write_bytes(b"unexpected source drift\n")
            errors = verify_predeploy.verify_predeploy(manifest, repo, targets)
            self.assertTrue(any("source sha256 mismatch" in item for item in errors))
            self.assertTrue(any("source git blob mismatch" in item for item in errors))

    def test_predeploy_verifier_rejects_missing_preimage_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "deploy_files": [
                            {
                                "source": "artifact.py",
                                "target": "/opt/artifact.py",
                                "sha256": "a" * 64,
                                "git_blob": "b" * 40,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            errors = verify_predeploy.verify_predeploy(manifest, root, root)
            self.assertEqual(len(errors), 1)
            self.assertIn("expected_target_sha256", errors[0])
            self.assertIn("expected_target_git_blob", errors[0])

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
