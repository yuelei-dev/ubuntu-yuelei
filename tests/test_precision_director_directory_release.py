import hashlib
import importlib.util
import json
import os
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "deploy/test-runtime/digital-human-precision-director-v5-20260822.json"
EXECUTOR = ROOT / "scripts/deploy_precision_director_v5_locked_manifest.py"
HISTORICAL_V4 = {
    "scripts/deploy_precision_director_v4_locked_manifest.py": (
        "f5f8910c5d3e7d16024e1d1febb10b351291a882",
        "79cc58b826e7d4546cbd8fa8b2e68836bd5b6d3c9d22d45a7adf3eed810021ed",
    ),
    "deploy/test-runtime/digital-human-precision-director-v4-20260822.json": (
        "268ef29e950c8f3375560d1ae217f8cec12a3393",
        "306cdf8889b17f42bc08054aee3aa33dea78f547276638fb387a48714eeeb3b0",
    ),
}


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def git_blob(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


class FilesystemHooks:
    def __init__(
        self, executor, fail_probe=False, forward_health_failures=0,
        fail_static=False, inactive_forward=False,
    ):
        self.fail_probe = fail_probe
        self.forward_health_failures = forward_health_failures
        self.fail_static = fail_static
        self.inactive_forward = inactive_forward
        self.forward_health_attempts = 0
        self.restart_count = 0
        self.calls = []
        self.links = {}

    def link_state(self, path):
        return dict(self.links.get(str(path), {"state": "absent", "target": None}))

    def replace_symlink(self, path, target):
        self.links[str(path)] = {"state": "symlink", "target": target}

    def validate_import(self, python_root, modules):
        self.calls.append(("imports", tuple(modules)))

    def validate_node(self, path):
        self.calls.append(("node", pathlib.Path(path).name))

    def validate_nginx(self):
        self.calls.append(("nginx-test",))

    def validate_nginx_candidate(self, candidate, reviewed_source):
        self.calls.append(("nginx-candidate", pathlib.Path(candidate).read_bytes()))

    def reload_nginx(self):
        self.calls.append(("nginx-reload",))

    def service_active(self, service):
        self.calls.append(("active", service))
        if self.inactive_forward and self.restart_count == 1:
            return False
        return True

    def restart(self, service):
        self.restart_count += 1
        self.calls.append(("restart", service))

    def probe(self, url, method, expected_status):
        self.calls.append(("probe", url, method, expected_status))
        if expected_status == 200 and self.restart_count == 1:
            self.forward_health_attempts += 1
            if (self.fail_probe or self.forward_health_attempts
                    <= self.forward_health_failures):
                raise RuntimeError("HTTP 502")

    def probe_static(self, url, expected_status, expected_sha256):
        self.calls.append(("static", url, expected_status, expected_sha256))
        if self.fail_static:
            raise RuntimeError("static resource hash mismatch")


class SimulatedClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class PrecisionDirectorDirectoryReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        specification = importlib.util.spec_from_file_location(
            "precision_directory_release", EXECUTOR,
        )
        cls.executor = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(cls.executor)
        cls.manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        renderer_spec = importlib.util.spec_from_file_location(
            "precision_directory_renderer",
            ROOT / "deploy/render_yuelei_test_nginx.py",
        )
        cls.renderer = importlib.util.module_from_spec(renderer_spec)
        renderer_spec.loader.exec_module(cls.renderer)

    def _blob(self, blob_id):
        return subprocess.run(
            ["git", "cat-file", "blob", blob_id], cwd=ROOT, check=True,
            stdout=subprocess.PIPE,
        ).stdout

    def _runtime(self, root, absolute_path):
        return pathlib.Path(root).joinpath(
            *pathlib.PurePosixPath(absolute_path).parts[1:]
        )

    def _target_tree(self, root):
        root = pathlib.Path(root)
        required_parents = [
            "/home/ubuntu/content-api/content_domains",
            "/var/www/huangquechuanmei/api-docs",
            "/var/www/huangquechuanmei/workbench",
            "/var/www/huangquechuanmei/assets/one-click",
            "/etc/nginx/sites-available",
            "/etc/nginx/sites-enabled",
        ]
        for runtime_path in required_parents:
            self._runtime(root, runtime_path).mkdir(parents=True, exist_ok=True)
        preview_directory = self._runtime(
            root, self.manifest["directories"][0]["runtime_path"],
        )
        self.assertFalse(preview_directory.exists())
        for item in self.manifest["files"]:
            target = self._runtime(root, item["runtime_path"])
            if item["target_preimage_state"] == "file":
                data = self._blob(item["preimage_blob"])
                self.assertEqual(item["preimage_sha256"], sha256(data))
                target.write_bytes(data)
            else:
                self.assertFalse(target.exists())
        nginx = self.manifest["nginx_contract"]
        base_source = subprocess.run(
            [
                "git", "show",
                self.manifest["expected_preimage"]["development_base_commit"]
                + ":" + nginx["source_repository_path"],
            ],
            cwd=ROOT, check=True, stdout=subprocess.PIPE,
        ).stdout.decode("utf-8")
        nginx_preimage = self.renderer.render_config(base_source).encode("utf-8")
        self.assertEqual(nginx["preimage_blob"], git_blob(nginx_preimage))
        self.assertEqual(nginx["preimage_sha256"], sha256(nginx_preimage))
        self._runtime(root, nginx["runtime_path"]).write_bytes(nginx_preimage)
        self._runtime(
            root, "/etc/nginx/sites-available/huangquechuanmei",
        ).write_bytes(b"protected-main-site-vhost\n")
        return root, preview_directory

    def _execute(self, target, backup, *, hooks=None, checkpoint=None):
        hooks = hooks or FilesystemHooks(self.executor)
        nginx = self.manifest["nginx_contract"]
        enabled = self._runtime(target, nginx["enabled_runtime_path"])
        hooks.links[str(enabled)] = {
            "state": nginx["enabled_preimage_state"],
            "target": nginx["enabled_preimage_target"],
        }
        return self.executor.execute_locked_release(
            MANIFEST, ROOT, target, pathlib.Path(backup),
            hooks=hooks,
            verify_repository=False, checkpoint=checkpoint,
            reviewed_head="r" * 40, merged_main="m" * 40,
        )

    def _assert_all_22_preimages(self, target, hooks):
        checked = 0
        for item in self.manifest["files"]:
            runtime = self._runtime(target, item["runtime_path"])
            if item["target_preimage_state"] == "file":
                self.assertTrue(runtime.is_file(), item["runtime_path"])
                self.assertFalse(runtime.is_symlink(), item["runtime_path"])
                self.assertEqual(
                    item["preimage_sha256"], sha256(runtime.read_bytes()),
                    item["runtime_path"],
                )
            else:
                self.assertFalse(runtime.exists(), item["runtime_path"])
                self.assertFalse(runtime.is_symlink(), item["runtime_path"])
            checked += 1
        directory = self.manifest["directories"][0]
        runtime_directory = self._runtime(target, directory["runtime_path"])
        self.assertFalse(runtime_directory.exists())
        self.assertFalse(runtime_directory.is_symlink())
        checked += 1
        nginx = self.manifest["nginx_contract"]
        nginx_runtime = self._runtime(target, nginx["runtime_path"])
        self.assertEqual(nginx["preimage_sha256"], sha256(nginx_runtime.read_bytes()))
        checked += 1
        enabled = self._runtime(target, nginx["enabled_runtime_path"])
        self.assertEqual(
            {
                "state": nginx["enabled_preimage_state"],
                "target": nginx["enabled_preimage_target"],
            },
            hooks.link_state(enabled),
        )
        checked += 1
        self.assertEqual(22, checked)

    def test_historical_v4_manifest_and_executor_are_exact_locked_blobs(self):
        for relative, (blob_id, digest) in HISTORICAL_V4.items():
            data = (ROOT / relative).read_bytes()
            self.assertEqual(blob_id, git_blob(data), relative)
            self.assertEqual(digest, sha256(data), relative)
            self.assertEqual(data, self._blob(blob_id), relative)

    def test_v5_manifest_locks_directory_and_real_entry(self):
        loaded = self.executor._load_manifest(MANIFEST)
        release = loaded["release_executor"]
        data = EXECUTOR.read_bytes()
        self.assertEqual("digital_human_precision_director_v5", release["contract"])
        self.assertEqual(release["git_blob"], git_blob(data))
        self.assertEqual(release["sha256"], sha256(data))
        self.assertEqual(
            {"timeout_seconds": 30, "interval_seconds": 1},
            release["forward_health_policy"],
        )
        directory = loaded["directories"][0]
        self.assertEqual("absent", directory["preimage_state"])
        self.assertEqual("directory", directory["postimage_state"])
        self.assertEqual("0755", directory["install_mode"])
        self.assertEqual(3, len(directory["child_runtime_paths"]))
        with self.assertRaisesRegex(self.executor.ReleaseError, "other release contract"):
            self.executor._load_manifest(
                ROOT / "deploy/test-runtime/digital-human-precision-director-v4-20260822.json"
            )

    def test_forward_health_policy_is_rejected_before_runtime_work(self):
        cases = [
            ("missing", None, "policy is missing"),
            (
                "zero interval",
                {"timeout_seconds": 30, "interval_seconds": 0},
                "interval is invalid",
            ),
            (
                "interval exceeds timeout",
                {"timeout_seconds": 30, "interval_seconds": 31},
                "interval is invalid",
            ),
            (
                "timeout exceeds cap",
                {"timeout_seconds": 121, "interval_seconds": 1},
                "timeout is invalid",
            ),
        ]
        for label, policy, message in cases:
            with self.subTest(label=label):
                manifest = json.loads(json.dumps(self.manifest))
                if policy is None:
                    manifest["release_executor"].pop("forward_health_policy", None)
                else:
                    manifest["release_executor"]["forward_health_policy"] = policy
                with mock.patch.object(
                    pathlib.Path, "read_text", return_value=json.dumps(manifest),
                ):
                    with self.assertRaisesRegex(
                            self.executor.ReleaseError, message):
                        self.executor._load_manifest(MANIFEST)

    def test_v5_source_binding_requires_exact_reviewed_parent(self):
        manifest = json.loads(json.dumps(self.manifest))
        parent = "a" * 40
        reviewed = "b" * 40
        merged = "c" * 40
        manifest["source"]["code_source_commit"] = parent

        def git_result(command, cwd=None, env=None):
            if command[:2] == ["git", "rev-parse"]:
                return parent
            if command[:3] == ["git", "diff", "--name-only"]:
                return MANIFEST.relative_to(ROOT).as_posix()
            raise AssertionError(command)

        with mock.patch.object(
                self.executor, "_verify_director_checkout", return_value=merged), \
             mock.patch.object(self.executor, "_run", side_effect=git_result):
            self.assertEqual(
                merged,
                self.executor._verify_precision_checkout(
                    ROOT, manifest, reviewed, merged,
                ),
            )
            manifest["source"]["code_source_commit"] = "d" * 40
            with self.assertRaisesRegex(self.executor.ReleaseError, "exact parent"):
                self.executor._verify_precision_checkout(
                    ROOT, manifest, reviewed, merged,
                )

    def test_success_creates_locked_directory_after_backup(self):
        with tempfile.TemporaryDirectory() as target_dir, \
                tempfile.TemporaryDirectory() as backup_dir:
            target, preview = self._target_tree(target_dir)
            checkpoints = []
            result = self._execute(
                target, backup_dir, checkpoint=checkpoints.append,
            )
            self.assertEqual("deployed", result["status"])
            self.assertTrue(preview.is_dir())
            self.assertFalse(preview.is_symlink())
            if os.name != "nt":
                self.assertEqual(0o755, preview.stat().st_mode & 0o777)
                owner = self._runtime(
                    target,
                    self.manifest["directories"][0][
                        "ownership_source_runtime_path"
                    ],
                ).stat()
                self.assertEqual(owner.st_uid, preview.stat().st_uid)
                self.assertEqual(owner.st_gid, preview.stat().st_gid)
            self.assertLess(
                checkpoints.index("after_backup"),
                checkpoints.index("after_directory_0"),
            )
            for runtime_path in self.manifest["directories"][0]["child_runtime_paths"]:
                self.assertTrue(self._runtime(target, runtime_path).is_file())
            audit = json.loads(
                (pathlib.Path(result["backup"]) / "audit.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual("deployed", audit["status"])
            self.assertEqual("directory", audit["directory_postimages"][0]["state"])

    def test_forward_health_retries_502_502_200_before_existing_gates(self):
        with tempfile.TemporaryDirectory() as target_dir, \
                tempfile.TemporaryDirectory() as backup_dir:
            target, _ = self._target_tree(target_dir)
            hooks = FilesystemHooks(self.executor, forward_health_failures=2)
            clock = SimulatedClock()
            with mock.patch.object(
                    self.executor.time, "monotonic", clock.monotonic), \
                 mock.patch.object(self.executor.time, "sleep", clock.sleep):
                result = self._execute(target, backup_dir, hooks=hooks)
            self.assertEqual("deployed", result["status"])
            self.assertEqual(3, hooks.forward_health_attempts)
            self.assertEqual([1.0, 1.0], clock.sleeps)
            health_call = (
                "probe", self.manifest["release_executor"]["health_url"],
                "GET", 200,
            )
            health_indices = [
                index for index, call in enumerate(hooks.calls)
                if call == health_call
            ]
            static_indices = [
                index for index, call in enumerate(hooks.calls)
                if call[0] == "static"
            ]
            unauthenticated = self.manifest["release_executor"][
                "unauthenticated_probes"
            ][0]
            unauthenticated_call = (
                "probe", unauthenticated["url"], unauthenticated["method"],
                unauthenticated["expected_status"],
            )
            self.assertEqual(3, len(static_indices))
            self.assertIn(("nginx-test",), hooks.calls)
            self.assertIn(("nginx-reload",), hooks.calls)
            self.assertIn(unauthenticated_call, hooks.calls)
            self.assertLess(health_indices[-1], static_indices[0])
            self.assertLess(static_indices[-1], hooks.calls.index(unauthenticated_call))

    def test_forward_health_timeout_rolls_back_all_22_targets(self):
        with tempfile.TemporaryDirectory() as target_dir, \
                tempfile.TemporaryDirectory() as backup_dir:
            target, _ = self._target_tree(target_dir)
            hooks = FilesystemHooks(self.executor, fail_probe=True)
            clock = SimulatedClock()
            with mock.patch.object(
                    self.executor.time, "monotonic", clock.monotonic), \
                 mock.patch.object(self.executor.time, "sleep", clock.sleep):
                with self.assertRaisesRegex(
                        self.executor.ReleaseError,
                        "forward health did not become ready"):
                    self._execute(target, backup_dir, hooks=hooks)
            self.assertGreaterEqual(hooks.forward_health_attempts, 30)
            self.assertEqual(30.0, sum(clock.sleeps))
            self._assert_all_22_preimages(target, hooks)
            audit = json.loads(next(
                pathlib.Path(backup_dir).glob("*/audit.json")
            ).read_text(encoding="utf-8"))
            self.assertEqual("rolled_back", audit["status"])
            self.assertEqual([], audit["rollback_errors"])

    def test_forward_service_becoming_inactive_times_out_and_rolls_back(self):
        with tempfile.TemporaryDirectory() as target_dir, \
                tempfile.TemporaryDirectory() as backup_dir:
            target, _ = self._target_tree(target_dir)
            hooks = FilesystemHooks(self.executor, inactive_forward=True)
            clock = SimulatedClock()
            with mock.patch.object(
                    self.executor.time, "monotonic", clock.monotonic), \
                 mock.patch.object(self.executor.time, "sleep", clock.sleep):
                with self.assertRaisesRegex(
                        self.executor.ReleaseError,
                        "forward health did not become ready") as caught:
                    self._execute(target, backup_dir, hooks=hooks)
            self.assertIsInstance(caught.exception.__cause__, self.executor.ReleaseError)
            self.assertIn("forward service is not active", str(caught.exception.__cause__))
            self.assertEqual(0, hooks.forward_health_attempts)
            self._assert_all_22_preimages(target, hooks)

    def test_static_hash_failure_after_health_rolls_back_all_22_targets(self):
        with tempfile.TemporaryDirectory() as target_dir, \
                tempfile.TemporaryDirectory() as backup_dir:
            target, _ = self._target_tree(target_dir)
            hooks = FilesystemHooks(self.executor, fail_static=True)
            with self.assertRaisesRegex(RuntimeError, "static resource hash mismatch"):
                self._execute(target, backup_dir, hooks=hooks)
            self.assertEqual(1, hooks.forward_health_attempts)
            self._assert_all_22_preimages(target, hooks)
            audit = json.loads(next(
                pathlib.Path(backup_dir).glob("*/audit.json")
            ).read_text(encoding="utf-8"))
            self.assertEqual("rolled_back", audit["status"])
            self.assertEqual("RuntimeError", audit["forward_error"])
            self.assertEqual([], audit["rollback_errors"])

    def test_preexisting_directory_fails_before_backup(self):
        with tempfile.TemporaryDirectory() as target_dir, \
                tempfile.TemporaryDirectory() as backup_dir:
            target, preview = self._target_tree(target_dir)
            preview.mkdir()
            with self.assertRaisesRegex(
                    self.executor.ReleaseError, "preimage must be absent"):
                self._execute(target, backup_dir)
            self.assertEqual([], list(pathlib.Path(backup_dir).iterdir()))

    def test_directory_appearing_after_backup_is_retained_as_conflict(self):
        with tempfile.TemporaryDirectory() as target_dir, \
                tempfile.TemporaryDirectory() as backup_dir:
            target, preview = self._target_tree(target_dir)

            def create_concurrent_directory(name):
                if name == "after_backup":
                    preview.mkdir()
                    (preview / "external.txt").write_text(
                        "concurrent content", encoding="utf-8",
                    )

            with self.assertRaisesRegex(
                    self.executor.ReleaseError, "directory:conflict") as caught:
                self._execute(
                    target, backup_dir,
                    checkpoint=create_concurrent_directory,
                )
            forward_error = caught.exception.__cause__
            self.assertIsInstance(forward_error, self.executor.ReleaseError)
            self.assertIn("appeared after backup", str(forward_error))
            self.assertIsInstance(forward_error.__cause__, FileExistsError)
            self.assertTrue((preview / "external.txt").is_file())
            audit_path = next(pathlib.Path(backup_dir).glob("*/audit.json"))
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
            self.assertEqual("rollback_failed", audit["status"])
            self.assertEqual("ReleaseError", audit["forward_error"])
            self.assertEqual(["directory:conflict"], audit["rollback_errors"])
            self.assertEqual([], audit["created_directory_runtime_paths"])
            self.assertEqual([], audit["installed_runtime_paths"])
            self.assertEqual("directory", audit["directory_final"][0]["state"])

    def test_symlinked_parent_is_rejected_before_backup(self):
        with tempfile.TemporaryDirectory() as target_dir, \
                tempfile.TemporaryDirectory() as backup_dir, \
                tempfile.TemporaryDirectory() as outside_dir:
            target, _ = self._target_tree(target_dir)
            one_click = self._runtime(
                target, "/var/www/huangquechuanmei/assets/one-click",
            )
            one_click.rmdir()
            os.symlink(outside_dir, one_click, target_is_directory=True)
            with self.assertRaisesRegex(
                    self.executor.ReleaseError, "parent is missing or unsafe"):
                self._execute(target, backup_dir)
            self.assertEqual([], list(pathlib.Path(backup_dir).iterdir()))

    def test_replaced_managed_parent_never_unlinks_external_child(self):
        with tempfile.TemporaryDirectory() as target_dir, \
                tempfile.TemporaryDirectory() as backup_dir, \
                tempfile.TemporaryDirectory() as outside_dir:
            target, preview = self._target_tree(target_dir)
            first_child_runtime = self.manifest["directories"][0][
                "child_runtime_paths"
            ][0]
            first_child_index = next(
                index for index, item in enumerate(self.manifest["files"])
                if item["runtime_path"] == first_child_runtime
            )
            outside = pathlib.Path(outside_dir)
            external_child = outside / pathlib.PurePosixPath(
                first_child_runtime
            ).name
            external_child.write_bytes(b"external file must survive")
            stashed = preview.with_name(preview.name + ".created-by-release")

            def replace_parent_after_first_child(name):
                if name == "after_replace_%d" % first_child_index:
                    preview.rename(stashed)
                    os.symlink(outside, preview, target_is_directory=True)

            with self.assertRaisesRegex(
                    self.executor.ReleaseError, "file:ReleaseError"):
                self._execute(
                    target, backup_dir,
                    checkpoint=replace_parent_after_first_child,
                )
            self.assertEqual(b"external file must survive", external_child.read_bytes())
            self.assertTrue(preview.is_symlink())
            self.assertTrue((stashed / external_child.name).is_file())
            audit_path = next(pathlib.Path(backup_dir).glob("*/audit.json"))
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
            self.assertEqual("rollback_failed", audit["status"])
            self.assertIn("file:ReleaseError", audit["rollback_errors"])
            self.assertIn("directory:ReleaseError", audit["rollback_errors"])
            self.assertEqual(
                [self.manifest["directories"][0]["runtime_path"]],
                audit["created_directory_runtime_paths"],
            )
            self.assertIn(first_child_runtime, audit["installed_runtime_paths"])
            self.assertEqual("symlink", audit["directory_final"][0]["state"])

    def test_forward_failure_restores_files_and_removes_created_directory(self):
        with tempfile.TemporaryDirectory() as target_dir, \
                tempfile.TemporaryDirectory() as backup_dir:
            target, preview = self._target_tree(target_dir)
            hooks = FilesystemHooks(self.executor, fail_probe=True)
            clock = SimulatedClock()
            with mock.patch.object(
                    self.executor.time, "monotonic", clock.monotonic), \
                 mock.patch.object(self.executor.time, "sleep", clock.sleep):
                with self.assertRaisesRegex(
                        self.executor.ReleaseError,
                        "forward health did not become ready"):
                    self._execute(target, backup_dir, hooks=hooks)
            self.assertFalse(preview.exists())
            self.assertFalse(preview.is_symlink())
            for runtime_path in self.manifest["directories"][0]["child_runtime_paths"]:
                child = self._runtime(target, runtime_path)
                self.assertFalse(child.exists())
                self.assertFalse(child.is_symlink())
            audit_path = next(pathlib.Path(backup_dir).glob("*/audit.json"))
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
            self.assertEqual("rolled_back", audit["status"])
            self.assertEqual([], audit["rollback_errors"])
            self.assertEqual("absent", audit["directory_final"][0]["state"])

    def test_rollback_retains_directory_when_unexpected_content_appears(self):
        with tempfile.TemporaryDirectory() as target_dir, \
                tempfile.TemporaryDirectory() as backup_dir:
            target, preview = self._target_tree(target_dir)

            def inject_unexpected_content(name):
                if name == "after_directory_0":
                    (preview / "unexpected.txt").write_text(
                        "retain for diagnosis", encoding="utf-8",
                    )
                    raise RuntimeError("injected directory-stage failure")

            with self.assertRaisesRegex(
                    self.executor.ReleaseError, "directory:ReleaseError"):
                self._execute(
                    target, backup_dir,
                    checkpoint=inject_unexpected_content,
                )
            self.assertTrue(preview.is_dir())
            self.assertTrue((preview / "unexpected.txt").is_file())
            audit_path = next(pathlib.Path(backup_dir).glob("*/audit.json"))
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
            self.assertEqual("rollback_failed", audit["status"])
            self.assertEqual(["directory:ReleaseError"], audit["rollback_errors"])


if __name__ == "__main__":
    unittest.main()
