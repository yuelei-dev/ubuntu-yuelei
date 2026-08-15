# -*- coding: utf-8 -*-
import hashlib
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch


ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "deploy" / "test-runtime" / "ip12-openai-egress-20260815.json"
EXECUTOR = ROOT / "scripts" / "deploy_locked_manifest.py"
_SPEC = importlib.util.spec_from_file_location("deploy_locked_manifest", EXECUTOR)
release = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(release)
LOCKED_PREIMAGES = {
    "server/content_domains/digital_ip.py": (
        "814999a3654df59b41e889c53237adca99c9471a",
        "45cc5ad234e3fc95f9e77fd0796e1ae9ca1a133fbbb3cee0fc0d01df3c111618",
    ),
    "server/content_domains/egress.py": (
        "bcedb7c52101b95acc238031808100b22385874e",
        "102798fbdfc0e581bce1c254b426d6c78d1f2d77654a07a9812da69fe5dd2e79",
    ),
}


def _blob_id(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


class IP12OpenAIEgressReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    def test_manifest_is_test_only_and_never_copies_secrets_or_state(self):
        self.assertEqual(self.manifest["target"]["role"], "test")
        self.assertEqual(self.manifest["target"]["host"], "8.148.158.106")
        policy = self.manifest["deployment_policy"]
        self.assertFalse(policy["production_server_write_allowed"])
        self.assertFalse(policy["copy_environment_or_database"])

    def test_file_scope_preimages_and_current_postimages_are_exact(self):
        entries = self.manifest["files"]
        self.assertEqual(
            {entry["repository_path"] for entry in entries}, set(LOCKED_PREIMAGES)
        )
        actual_preimages = {}
        for entry in entries:
            data = (ROOT / entry["repository_path"]).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), entry["postimage_sha256"])
            self.assertEqual(_blob_id(data), entry["postimage_blob"])
            actual_preimages[entry["repository_path"]] = (
                entry["preimage_blob"], entry["preimage_sha256"],
            )
        self.assertEqual(actual_preimages, LOCKED_PREIMAGES)

    def test_deployment_fails_closed_and_rollback_restores_both_files(self):
        policy = self.manifest["deployment_policy"]
        self.assertTrue(policy["fail_closed_on_preimage_mismatch"])
        self.assertTrue(policy["fail_closed_on_source_blob_mismatch"])
        self.assertTrue(policy["stage_and_validate_all_candidates_before_first_write"])
        self.assertTrue(policy["rollback_both_files_as_one_unit"])
        checks = "\n".join(self.manifest["pre_write_checks"])
        self.assertIn("both runtime files", checks)
        self.assertIn("py_compile", checks)
        self.assertIn("isolated import", checks)
        rollback = json.dumps(self.manifest["rollback"], ensure_ascii=False)
        self.assertIn("both", rollback)
        self.assertIn("preimage", rollback)
        self.assertIn("HTTP 200", rollback)
        self.assertIn("HTTP 401", rollback)

    def test_deployment_and_rollback_probes_cannot_issue_paid_requests(self):
        deploy_checks = "\n".join(self.manifest["post_deploy_checks"])
        rollback_checks = "\n".join(self.manifest["rollback"]["verify"])
        for checks in (deploy_checks, rollback_checks):
            self.assertIn("unauthenticated POST", checks)
            self.assertIn("outbound model request", checks)

    def test_manifest_names_the_executable_release_entry(self):
        executor = self.manifest["release_executor"]
        self.assertEqual(executor["repository_path"], "scripts/deploy_locked_manifest.py")
        executor_data = EXECUTOR.read_bytes()
        self.assertEqual(executor["sha256"], hashlib.sha256(executor_data).hexdigest())
        self.assertEqual(executor["git_blob"], _blob_id(executor_data))
        self.assertEqual(executor["runtime_package"], "content_domains")
        self.assertEqual(
            executor["import_modules"],
            ["content_domains.egress", "content_domains.digital_ip"],
        )
        self.assertEqual(executor["unauthenticated_probe"]["expected_status"], 401)
        self.assertEqual(executor["unauthenticated_probe"]["paid_model_calls"], 0)


class _FakeHooks:
    def __init__(self):
        self.imports = 0
        self.restarts = 0
        self.probes = []
        self.fail_import_call = None
        self.fail_restart_once = False
        self.fail_probe_once = False

    def validate_import(self, python_root, modules):
        self.imports += 1
        if self.imports == self.fail_import_call:
            raise release.ReleaseError("injected import failure")
        release.SystemHooks().validate_import(python_root, modules)

    def service_active(self, service):
        return True

    def restart(self, service):
        self.restarts += 1
        if self.fail_restart_once:
            self.fail_restart_once = False
            raise release.ReleaseError("injected restart failure")

    def probe(self, url, method, expected_status):
        self.probes.append((url, method, expected_status))
        if self.fail_probe_once:
            self.fail_probe_once = False
            raise release.ReleaseError("injected health failure")


class IP12LockedReleaseExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.target_root = self.root / "target"
        self.backup_root = self.root / "backups"
        self.package = (
            self.target_root / "home" / "ubuntu" / "content-api" / "content_domains"
        )
        self.package.mkdir(parents=True)
        (self.package / "__init__.py").write_text("", encoding="utf-8")
        (self.package / "core.py").write_text(
            "OPENAI_BASE='https://relay.invalid'\nOPENAI_KEY='test-only'\n",
            encoding="utf-8",
        )
        (self.package / "ip12_pdf.py").write_text("", encoding="utf-8")
        self.manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.preimages = {}
        for item in self.manifest["files"]:
            data = subprocess.check_output(
                ["git", "show", "%s:%s" % (
                    self.manifest["source"]["development_base_commit"],
                    item["repository_path"],
                )],
                cwd=ROOT,
            )
            target = self._target(item)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            self.preimages[item["repository_path"]] = data

    def tearDown(self):
        self.temp.cleanup()

    def _target(self, item):
        return self.target_root.joinpath(*pathlib.PurePosixPath(item["runtime_path"]).parts[1:])

    def _run(self, hooks=None, **kwargs):
        return release.execute_locked_release(
            MANIFEST, ROOT, self.target_root, self.backup_root,
            hooks=hooks or _FakeHooks(), verify_repository=False, **kwargs,
        )

    def _assert_preimages_restored(self):
        for item in self.manifest["files"]:
            self.assertEqual(
                self._target(item).read_bytes(),
                self.preimages[item["repository_path"]],
            )

    def test_success_replaces_both_files_and_uses_only_unauthed_probes(self):
        hooks = _FakeHooks()
        result = self._run(hooks)
        self.assertEqual(result["status"], "deployed")
        for item in self.manifest["files"]:
            data = self._target(item).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), item["postimage_sha256"])
        self.assertEqual(hooks.imports, 2)
        self.assertEqual(hooks.restarts, 1)
        self.assertEqual(
            [(method, status) for _, method, status in hooks.probes],
            [("GET", 200), ("POST", 401)],
        )

    def test_preimage_mismatch_fails_before_any_replace_or_restart(self):
        first = self._target(self.manifest["files"][0])
        first.write_bytes(b"runtime drift")
        calls = []
        hooks = _FakeHooks()
        with self.assertRaises(release.ReleaseError):
            self._run(hooks, replace=lambda source, target: calls.append((source, target)))
        self.assertEqual(calls, [])
        self.assertEqual(hooks.restarts, 0)
        self.assertEqual(first.read_bytes(), b"runtime drift")

    def test_source_hash_mismatch_fails_before_any_target_write(self):
        source = self.root / "source"
        executor_path = source / self.manifest["release_executor"]["repository_path"]
        executor_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(EXECUTOR, executor_path)
        for item in self.manifest["files"]:
            path = source / item["repository_path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / item["repository_path"], path)
        (source / self.manifest["files"][0]["repository_path"]).write_text(
            "tampered", encoding="utf-8",
        )
        hooks = _FakeHooks()
        with self.assertRaises(release.ReleaseError):
            release.execute_locked_release(
                MANIFEST, source, self.target_root, self.backup_root,
                hooks=hooks, verify_repository=False,
            )
        self._assert_preimages_restored()
        self.assertEqual(hooks.restarts, 0)

    def test_first_and_second_replace_failures_restore_both_files(self):
        for fail_at in (1, 2):
            with self.subTest(fail_at=fail_at):
                calls = 0

                def fail_once(source, target):
                    nonlocal calls
                    calls += 1
                    if calls == fail_at:
                        raise OSError("injected replace failure")
                    os.replace(source, target)

                with self.assertRaises(OSError):
                    self._run(_FakeHooks(), replace=fail_once)
                self._assert_preimages_restored()

    def test_postimage_import_restart_and_health_failures_restore_both_files(self):
        scenarios = ("postimage", "import", "restart", "health")
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                hooks = _FakeHooks()

                def checkpoint(name):
                    if scenario == "postimage" and name == "before_postimage_verify":
                        self._target(self.manifest["files"][0]).write_bytes(b"corrupt")

                if scenario == "import":
                    hooks.fail_import_call = 2
                elif scenario == "restart":
                    hooks.fail_restart_once = True
                elif scenario == "health":
                    hooks.fail_probe_once = True
                with self.assertRaises(BaseException):
                    self._run(hooks, checkpoint=checkpoint)
                self._assert_preimages_restored()
                self.assertGreaterEqual(hooks.restarts, 1)

    def test_repository_gate_requires_clean_live_main(self):
        outputs = iter([
            "main", "", "a" * 40, "%s\trefs/heads/main" % ("a" * 40), "",
        ])
        with patch.object(release, "_run", side_effect=lambda *a, **k: next(outputs)):
            self.assertEqual(
                release._verify_repository(ROOT, self.manifest), "a" * 40,
            )

        with patch.object(release, "_run", return_value="feature"):
            with self.assertRaises(release.ReleaseError):
                release._verify_repository(ROOT, self.manifest)


if __name__ == "__main__":
    unittest.main()
