# -*- coding: utf-8 -*-
import copy
import hashlib
import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import types
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
MANIFEST_PATH = (
    ROOT / "deploy" / "test-runtime" / "digital-human-whisper-runtime-20260815.json"
)
EXECUTOR_PATH = SCRIPTS / "deploy_content_whisper_runtime.py"
REVIEWED_SOURCE = "1" * 40
REVIEWED_MAIN = "2" * 40


def _load_executor():
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("whisper_release", EXECUTOR_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeRunner:
    def __init__(self, fail_stage=None):
        self.fail_stage = fail_stage
        self.calls = []

    def run(self, stage, commands, *, source_root, runtime_root):
        self.calls.append(stage)
        if stage == self.fail_stage:
            raise RuntimeError("injected %s failure" % stage)


class HealthProbe:
    def __init__(self, fail_first=False):
        self.fail_first = fail_first
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        if self.fail_first:
            self.fail_first = False
            return 500
        return 200 if url.endswith("/health") else 401


class FakeGitRunner:
    def __init__(
            self, *, dirty=False, branch="main", head=REVIEWED_MAIN,
            origin_main=REVIEWED_MAIN, remote_main=REVIEWED_MAIN,
            ancestor=True, network_error=False):
        self.dirty = dirty
        self.branch = branch
        self.head = head
        self.origin_main = origin_main
        self.remote_main = remote_main
        self.ancestor = ancestor
        self.network_error = network_error
        self.calls = []

    def run(self, arguments, *, source_root, allow_failure=False):
        self.calls.append(tuple(arguments))
        if arguments[:2] == ["status", "--porcelain"]:
            stdout, code = (" M scripts/release.py\n" if self.dirty else ""), 0
        elif arguments[:3] == ["symbolic-ref", "--short", "HEAD"]:
            stdout, code = self.branch + "\n", 0
        elif arguments == ["rev-parse", "HEAD"]:
            stdout, code = self.head + "\n", 0
        elif arguments == ["rev-parse", "refs/remotes/origin/main"]:
            stdout, code = self.origin_main + "\n", 0
        elif arguments[:2] == ["ls-remote", "--exit-code"]:
            if self.network_error:
                raise self._error()
            stdout, code = self.remote_main + "\trefs/heads/main\n", 0
        elif arguments[:2] == ["merge-base", "--is-ancestor"]:
            stdout, code = "", 0 if self.ancestor else 1
        else:
            raise AssertionError("unexpected git command: %r" % (arguments,))
        if code and not allow_failure:
            raise self._error()
        return types.SimpleNamespace(stdout=stdout, returncode=code)

    @staticmethod
    def _error():
        return RuntimeError("injected Git verification failure")


class ContentWhisperReleaseExecutorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.release_module = _load_executor()
        cls.base_manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.runtime_root = self.root / "runtime"
        self.backup_root = self.root / "backups"
        self.runtime_root.mkdir()
        self.manifest = copy.deepcopy(self.base_manifest)
        self.manifest["_manifest_path"] = str(MANIFEST_PATH)
        self.original = {}
        for entry in self.manifest["files"]:
            target = self._target(entry)
            if entry["target_preimage_state"] == "file":
                data = ("old:" + entry["repository_path"]).encode("utf-8")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                os.chmod(target, 0o640)
                entry["target_preimage_sha256"] = hashlib.sha256(data).hexdigest()
                entry["target_preimage_blob"] = self._blob(data)
                self.original[entry["runtime_path"]] = data
            else:
                self.original[entry["runtime_path"]] = None
        self.cache_marker = (
            self.runtime_root / "var" / "cache" / "huangque" /
            "faster-whisper" / "keep.model"
        )
        self.cache_marker.parent.mkdir(parents=True)
        self.cache_marker.write_bytes(b"keep")

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def _blob(data):
        return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()

    def _target(self, entry):
        return self.runtime_root.joinpath(*pathlib.PurePosixPath(
            entry["runtime_path"]
        ).parts[1:])

    def _snapshot(self):
        result = {}
        for entry in self.manifest["files"]:
            target = self._target(entry)
            result[entry["runtime_path"]] = (
                target.read_bytes() if target.is_file() and not target.is_symlink()
                else None
            )
        return result

    def _release(
            self, *, runner=None, health=None, checkpoint=None, manifest=None,
            git_runner=None, reviewed_source=REVIEWED_SOURCE,
            reviewed_main=REVIEWED_MAIN):
        return self.release_module.ContentWhisperRelease(
            manifest or self.manifest,
            ROOT,
            self.runtime_root,
            self.backup_root,
            runner=runner or FakeRunner(),
            health_getter=health or HealthProbe(),
            checkpoint=checkpoint,
            git_runner=git_runner or FakeGitRunner(),
            reviewed_source_commit=reviewed_source,
            reviewed_main_commit=reviewed_main,
        )

    def _assert_restored_without_residue(self):
        self.assertEqual(self.original, self._snapshot())
        self.assertEqual(b"keep", self.cache_marker.read_bytes())
        self.assertEqual(
            [], list(self.runtime_root.rglob(".hq-release-*")),
        )
        staged = self.runtime_root / "home" / "ubuntu" / "content-api" / ".deploy"
        self.assertFalse(staged.exists())

    def test_success_installs_all_files_and_restarts_exactly_once(self):
        runner = FakeRunner()
        health = HealthProbe()
        events = []
        result = self._release(
            runner=runner, health=health, checkpoint=events.append,
        ).execute(
            self.release_module.AUTHORIZED_TARGET
        )
        self.assertTrue(result["ok"])
        self.assertEqual(7, result["files"])
        self.assertEqual(1, result["restart_count"])
        self.assertEqual(1, runner.calls.count("restart"))
        self.assertNotIn("rollback_restart", runner.calls)
        self.assertEqual(
            [
                "pre_service_active", "dependencies", "cache", "offline",
                "font", "no_charge", "daemon_reload", "restart",
                "service_active",
            ],
            runner.calls,
        )
        for entry in self.manifest["files"]:
            self.assertEqual(
                (ROOT / entry["repository_path"]).read_bytes(),
                self._target(entry).read_bytes(),
            )
        state = json.loads(
            (pathlib.Path(result["backup"]) / "backup-state.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(7, len(state["files"]))
        self.assertEqual(
            ["file", "file", "file", "file", "absent", "absent", "absent"],
            [entry["state"] for entry in state["files"]],
        )
        self.assertEqual(2, len(health.calls))
        self.assertLess(events.index("backup_complete"), events.index("write_1"))
        self.assertLess(events.index("write_1"), events.index("write_7"))
        self.assertLess(events.index("write_7"), events.index("health"))

    def test_first_middle_and_seventh_write_failures_restore_every_target(self):
        for fault in ("write_1", "write_4", "write_7"):
            with self.subTest(fault=fault):
                self.tearDown()
                self.setUp()
                runner = FakeRunner()

                def checkpoint(name):
                    if name == fault:
                        raise RuntimeError("injected %s" % fault)

                with self.assertRaisesRegex(
                        self.release_module.ReleaseError, "all seven targets were restored"):
                    self._release(runner=runner, checkpoint=checkpoint).execute(
                        self.release_module.AUTHORIZED_TARGET
                    )
                self._assert_restored_without_residue()
                self.assertNotIn("restart", runner.calls)
                self.assertNotIn("rollback_restart", runner.calls)

    def test_every_pre_restart_stage_failure_restores_without_restart(self):
        for fault in ("dependencies", "cache", "offline", "font", "no_charge"):
            with self.subTest(fault=fault):
                self.tearDown()
                self.setUp()
                runner = FakeRunner(fault)
                with self.assertRaisesRegex(
                        self.release_module.ReleaseError, "all seven targets were restored"):
                    self._release(runner=runner).execute(
                        self.release_module.AUTHORIZED_TARGET
                    )
                self._assert_restored_without_residue()
                self.assertNotIn("restart", runner.calls)
                self.assertNotIn("rollback_restart", runner.calls)

    def test_daemon_reload_failure_restores_without_restart(self):
        runner = FakeRunner("daemon_reload")
        with self.assertRaisesRegex(
                self.release_module.ReleaseError, "all seven targets were restored"):
            self._release(runner=runner).execute(
                self.release_module.AUTHORIZED_TARGET
            )
        self._assert_restored_without_residue()
        self.assertNotIn("restart", runner.calls)
        self.assertNotIn("rollback_restart", runner.calls)
        self.assertIn("rollback_daemon_reload", runner.calls)

    def test_restart_failure_restores_and_restarts_old_version_once(self):
        runner = FakeRunner("restart")
        with self.assertRaisesRegex(
                self.release_module.ReleaseError, "all seven targets were restored"):
            self._release(runner=runner).execute(
                self.release_module.AUTHORIZED_TARGET
            )
        self._assert_restored_without_residue()
        self.assertEqual(1, runner.calls.count("restart"))
        self.assertEqual(1, runner.calls.count("rollback_restart"))
        self.assertIn("rollback_daemon_reload", runner.calls)

    def test_health_failure_restores_and_restarts_old_version_once(self):
        runner = FakeRunner()
        health = HealthProbe(fail_first=True)
        with self.assertRaisesRegex(
                self.release_module.ReleaseError, "all seven targets were restored"):
            self._release(runner=runner, health=health).execute(
                self.release_module.AUTHORIZED_TARGET
            )
        self._assert_restored_without_residue()
        self.assertEqual(1, runner.calls.count("restart"))
        self.assertEqual(1, runner.calls.count("rollback_restart"))
        self.assertGreaterEqual(len(health.calls), 3)

    def test_backup_failure_happens_before_first_target_write(self):
        runner = FakeRunner()

        def checkpoint(name):
            if name == "backup_4":
                raise RuntimeError("injected backup failure")

        with self.assertRaisesRegex(RuntimeError, "injected backup failure"):
            self._release(runner=runner, checkpoint=checkpoint).execute(
                self.release_module.AUTHORIZED_TARGET
            )
        self._assert_restored_without_residue()
        self.assertEqual(["pre_service_active"], runner.calls)

    def test_preimage_drift_fails_before_backup_commands_or_writes(self):
        first = self._target(self.manifest["files"][0])
        first.write_bytes(b"drift")
        runner = FakeRunner()
        with self.assertRaisesRegex(RuntimeError, "preimage mismatch"):
            self._release(runner=runner).execute(
                self.release_module.AUTHORIZED_TARGET
            )
        self.assertEqual([], runner.calls)
        self.assertFalse(self.backup_root.exists())

    def test_production_manifest_and_wrong_confirmation_are_forbidden(self):
        production = copy.deepcopy(self.manifest)
        production["target"] = {"role": "production", "host": "129.204.166.13"}
        with self.assertRaisesRegex(self.release_module.ReleaseError, "not authorized"):
            self._release(manifest=production).execute(
                self.release_module.AUTHORIZED_TARGET
            )
        with self.assertRaisesRegex(self.release_module.ReleaseError, "confirmation"):
            self._release().execute("test@wrong-host")
        self.assertEqual(self.original, self._snapshot())

    def test_manifest_commands_keep_all_preflights_before_restart(self):
        commands = self.manifest["release_commands"]
        for stage in (
                "dependencies", "cache", "offline", "font", "no_charge",
                "daemon_reload", "restart", "service_active",
                "rollback_daemon_reload", "rollback_restart",
                "rollback_service_active"):
            self.assertTrue(commands[stage])
            for command in commands[stage]:
                self.assertIsInstance(command["argv"], list)
                self.assertNotIn("shell", command)
        self.assertEqual(
            {200, 401},
            {int(check["expected_status"]) for check in self.manifest["health_checks"]},
        )
        self.assertEqual(
            "scripts/deploy_content_whisper_runtime.py",
            self.manifest["executor"]["repository_path"],
        )
        self.assertEqual(
            self.release_module.AUTHORIZED_TARGET,
            self.manifest["executor"]["confirm_target"],
        )
        self.assertEqual(
            {"tests/test_script_to_video.py", "tests/test_digital_human_oneclick.py"},
            {
                entry["repository_path"]
                for entry in self.manifest["release_contract_sources"]
            },
        )

    def test_no_charge_contract_test_drift_fails_before_preimage_or_backup(self):
        drifted = copy.deepcopy(self.manifest)
        drifted["release_contract_sources"][0]["source_sha256"] = "0" * 64
        runner = FakeRunner()
        with self.assertRaisesRegex(
                self.release_module.ReleaseError, "contract SHA-256 mismatch"):
            self._release(runner=runner, manifest=drifted).execute(
                self.release_module.AUTHORIZED_TARGET
            )
        self.assertEqual([], runner.calls)
        self.assertFalse(self.backup_root.exists())

    def test_executor_and_verifier_single_byte_drift_fail_before_backup(self):
        for key_path in (("source_sha256",), ("verifier", "source_sha256")):
            with self.subTest(key_path=key_path):
                drifted = copy.deepcopy(self.manifest)
                target = drifted["executor"]
                for key in key_path[:-1]:
                    target = target[key]
                target[key_path[-1]] = "0" * 64
                runner = FakeRunner()
                with self.assertRaisesRegex(
                        self.release_module.ReleaseError,
                        "release tool SHA-256 mismatch"):
                    self._release(runner=runner, manifest=drifted).execute(
                        self.release_module.AUTHORIZED_TARGET
                    )
                self.assertEqual([], runner.calls)
                self.assertFalse(self.backup_root.exists())

    def test_source_checkout_gate_fails_closed_before_backup_or_commands(self):
        cases = {
            "dirty": FakeGitRunner(dirty=True),
            "non_main": FakeGitRunner(branch="feature/test"),
            "local_origin_drift": FakeGitRunner(origin_main="3" * 40),
            "live_origin_drift": FakeGitRunner(remote_main="4" * 40),
            "reviewed_commit_missing": FakeGitRunner(ancestor=False),
            "network_failure": FakeGitRunner(network_error=True),
        }
        for name, git_runner in cases.items():
            with self.subTest(name=name):
                runner = FakeRunner()
                with self.assertRaises(Exception):
                    self._release(
                        runner=runner, git_runner=git_runner,
                    ).execute(self.release_module.AUTHORIZED_TARGET)
                self.assertEqual([], runner.calls)
                self.assertFalse(self.backup_root.exists())

    def test_exact_reviewed_commits_are_required_before_backup(self):
        for reviewed_source, reviewed_main in (
                ("short", REVIEWED_MAIN),
                (REVIEWED_SOURCE, "not-a-commit"),
                (REVIEWED_SOURCE, "3" * 40)):
            with self.subTest(
                    reviewed_source=reviewed_source,
                    reviewed_main=reviewed_main):
                runner = FakeRunner()
                with self.assertRaises(self.release_module.ReleaseError):
                    self._release(
                        runner=runner,
                        reviewed_source=reviewed_source,
                        reviewed_main=reviewed_main,
                    ).execute(self.release_module.AUTHORIZED_TARGET)
                self.assertEqual([], runner.calls)
                self.assertFalse(self.backup_root.exists())

    def test_dependency_gate_is_read_only_and_never_installs_packages(self):
        commands = self.manifest["release_commands"]["dependencies"]
        rendered = json.dumps(commands, ensure_ascii=False)
        self.assertNotIn("pip\", \"install", rendered)
        self.assertNotIn("pip install", rendered)
        self.assertIn("verify_content_python_requirements.py", rendered)
        self.assertIn("pip\", \"check", rendered)
        self.assertEqual(
            "verify_exact_existing_content_dependencies_without_mutation",
            self.manifest["ordered_release_steps"][5],
        )


if __name__ == "__main__":
    unittest.main()
