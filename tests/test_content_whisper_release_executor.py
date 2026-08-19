# -*- coding: utf-8 -*-
import copy
import contextlib
import hashlib
import http.server
import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
MANIFEST_PATH = (
    ROOT / "deploy" / "test-runtime" / "digital-human-material-v2-20260818.json"
)
EXECUTOR_PATH = SCRIPTS / "deploy_content_whisper_runtime.py"
REVIEWED_SOURCE = "1" * 40
REVIEWED_MAIN = "2" * 40
TARGET_COUNT = len(json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))["files"])
LAST_WRITE = "write_%d" % TARGET_COUNT


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
    def __init__(self, fail_count=0, connection_fail_count=0):
        self.fail_count = fail_count
        self.connection_fail_count = connection_fail_count
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        if self.connection_fail_count:
            self.connection_fail_count -= 1
            raise ConnectionRefusedError("service is still starting")
        if self.fail_count:
            self.fail_count -= 1
            return 500
        return 200 if url.endswith("/health") else 401


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


@contextlib.contextmanager
def _local_http_server(status_getter):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.server.hits.append(self.path)
            self.send_response(int(self.server.status_getter(self.path)))
            self.end_headers()

        def log_message(self, _format, *_args):
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.hits = []
    server.status_getter = status_getter
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


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
            reviewed_main=REVIEWED_MAIN, use_real_health=False, clock=None):
        return self.release_module.ContentWhisperRelease(
            manifest or self.manifest,
            ROOT,
            self.runtime_root,
            self.backup_root,
            runner=runner or FakeRunner(),
            health_getter=None if use_real_health else (health or HealthProbe()),
            checkpoint=checkpoint,
            git_runner=git_runner or FakeGitRunner(),
            reviewed_source_commit=reviewed_source,
            reviewed_main_commit=reviewed_main,
            monotonic=clock.monotonic if clock else None,
            sleeper=clock.sleep if clock else None,
        )

    def _assert_restored_without_residue(self):
        self.assertEqual(self.original, self._snapshot())
        self.assertEqual(b"keep", self.cache_marker.read_bytes())
        self.assertEqual(
            [], list(self.runtime_root.rglob(".hq-release-*")),
        )
        staged = self.runtime_root / "home" / "ubuntu" / "content-api" / ".deploy"
        if staged.exists():
            self.assertTrue(staged.is_dir())

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
        self.assertEqual(TARGET_COUNT, result["files"])
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
        self.assertEqual(TARGET_COUNT, len(state["files"]))
        self.assertEqual(
            [entry["target_preimage_state"] for entry in self.manifest["files"]],
            [entry["state"] for entry in state["files"]],
        )
        self.assertEqual(2, len(health.calls))
        self.assertLess(events.index("backup_complete"), events.index("write_1"))
        self.assertLess(events.index("write_1"), events.index(LAST_WRITE))
        self.assertLess(events.index(LAST_WRITE), events.index("health"))

    def test_mixed_state_with_one_already_postimage_is_backed_up_and_deployed(self):
        video = next(
            entry for entry in self.manifest["files"]
            if entry["repository_path"] == "server/content_domains/script_to_video.py"
        )
        source = (ROOT / video["repository_path"]).read_bytes()
        target = self._target(video)
        target.write_bytes(source)
        video["target_preimage_sha256"] = hashlib.sha256(source).hexdigest()
        video["target_preimage_blob"] = self._blob(source)
        self.original[video["runtime_path"]] = source

        result = self._release().execute(
            self.release_module.AUTHORIZED_TARGET
        )

        backup = pathlib.Path(result["backup"])
        state = json.loads(
            (backup / "backup-state.json").read_text(encoding="utf-8")
        )
        record = next(
            entry for entry in state["files"]
            if entry["runtime_path"] == video["runtime_path"]
        )
        self.assertEqual(record["sha256"], hashlib.sha256(source).hexdigest())
        self.assertEqual(record["blob"], self._blob(source))
        self.assertEqual((backup / record["backup_file"]).read_bytes(), source)
        for entry in self.manifest["files"]:
            self.assertEqual(
                (ROOT / entry["repository_path"]).read_bytes(),
                self._target(entry).read_bytes(),
            )

    def test_first_middle_and_last_write_failures_restore_every_target(self):
        for fault in ("write_1", "write_%d" % (TARGET_COUNT // 2 + 1), LAST_WRITE):
            with self.subTest(fault=fault):
                self.tearDown()
                self.setUp()
                runner = FakeRunner()

                def checkpoint(name):
                    if name == fault:
                        raise RuntimeError("injected %s" % fault)

                with self.assertRaisesRegex(
                        self.release_module.ReleaseError, "all manifest targets were restored"):
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
                        self.release_module.ReleaseError, "all manifest targets were restored"):
                    self._release(runner=runner).execute(
                        self.release_module.AUTHORIZED_TARGET
                    )
                self._assert_restored_without_residue()
                self.assertNotIn("restart", runner.calls)
                self.assertNotIn("rollback_restart", runner.calls)

    def test_daemon_reload_failure_restores_without_restart(self):
        runner = FakeRunner("daemon_reload")
        with self.assertRaisesRegex(
                self.release_module.ReleaseError, "all manifest targets were restored"):
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
                self.release_module.ReleaseError, "all manifest targets were restored"):
            self._release(runner=runner).execute(
                self.release_module.AUTHORIZED_TARGET
            )
        self._assert_restored_without_residue()
        self.assertEqual(1, runner.calls.count("restart"))
        self.assertEqual(1, runner.calls.count("rollback_restart"))
        self.assertIn("rollback_daemon_reload", runner.calls)

    def test_health_failure_restores_and_restarts_old_version_once(self):
        runner = FakeRunner()
        health = HealthProbe(fail_count=3)
        clock = FakeClock()
        self.manifest["health_probe_policy"] = {
            "startup_timeout_seconds": 2,
            "interval_seconds": 1,
        }
        with self.assertRaisesRegex(
                self.release_module.ReleaseError, "all manifest targets were restored"):
            self._release(runner=runner, health=health, clock=clock).execute(
                self.release_module.AUTHORIZED_TARGET
            )
        self._assert_restored_without_residue()
        self.assertEqual(1, runner.calls.count("restart"))
        self.assertEqual(1, runner.calls.count("rollback_restart"))
        self.assertGreaterEqual(len(health.calls), 3)

    def test_restart_health_waits_for_delayed_listener_without_rollback(self):
        runner = FakeRunner()
        health = HealthProbe(connection_fail_count=19)
        clock = FakeClock()
        result = self._release(
            runner=runner, health=health, clock=clock,
        ).execute(self.release_module.AUTHORIZED_TARGET)
        self.assertTrue(result["ok"])
        self.assertEqual(21, len(health.calls))
        self.assertEqual([1] * 19, clock.sleeps)
        self.assertEqual(1, runner.calls.count("restart"))
        self.assertNotIn("rollback_restart", runner.calls)

    def test_health_wait_is_bounded_and_rollback_gets_a_fresh_deadline(self):
        runner = FakeRunner()
        health = HealthProbe(connection_fail_count=3)
        clock = FakeClock()
        self.manifest["health_probe_policy"] = {
            "startup_timeout_seconds": 2,
            "interval_seconds": 1,
        }
        with self.assertRaisesRegex(
                self.release_module.ReleaseError, "all manifest targets were restored"):
            self._release(
                runner=runner, health=health, clock=clock,
            ).execute(self.release_module.AUTHORIZED_TARGET)
        self.assertEqual([1, 1], clock.sleeps)
        self.assertEqual(1, runner.calls.count("rollback_restart"))
        self.assertGreaterEqual(len(health.calls), 5)
        self._assert_restored_without_residue()

    def test_success_and_rollback_health_probes_ignore_environment_proxy(self):
        proxy_status = lambda _path: 418
        for rollback in (False, True):
            with self.subTest(rollback=rollback):
                self.tearDown()
                self.setUp()
                health_calls = {"count": 0}
                clock = FakeClock()
                if rollback:
                    self.manifest["health_probe_policy"] = {
                        "startup_timeout_seconds": 2,
                        "interval_seconds": 1,
                    }

                def target_status(path):
                    if path == "/api/gen/health":
                        health_calls["count"] += 1
                        if rollback and health_calls["count"] <= 3:
                            return 500
                        return 200
                    return 401

                with _local_http_server(target_status) as target, \
                     _local_http_server(proxy_status) as proxy:
                    port = target.server_address[1]
                    self.manifest["health_checks"] = [
                        {
                            "url": "http://127.0.0.1:%d/api/gen/health" % port,
                            "expected_status": 200,
                        },
                        {
                            "url": "http://127.0.0.1:%d/api/gen/history" % port,
                            "expected_status": 401,
                        },
                    ]
                    proxy_url = "http://127.0.0.1:%d" % proxy.server_address[1]
                    runner = FakeRunner()
                    with patch.dict(os.environ, {
                            "HTTP_PROXY": proxy_url,
                            "HTTPS_PROXY": proxy_url,
                            "NO_PROXY": "",
                            "http_proxy": proxy_url,
                            "https_proxy": proxy_url,
                            "no_proxy": "",
                    }), patch("urllib.request.proxy_bypass", return_value=False):
                        release = self._release(
                            runner=runner, use_real_health=True, clock=clock,
                        )
                        if rollback:
                            with self.assertRaisesRegex(
                                    self.release_module.ReleaseError,
                                    "all manifest targets were restored"):
                                release.execute(self.release_module.AUTHORIZED_TARGET)
                        else:
                            self.assertTrue(
                                release.execute(
                                    self.release_module.AUTHORIZED_TARGET
                                )["ok"]
                            )

                self.assertEqual([], proxy.hits)
                expected_target_hits = 5 if rollback else 2
                self.assertEqual(expected_target_hits, len(target.hits))
                if rollback:
                    self._assert_restored_without_residue()
                    self.assertEqual(1, runner.calls.count("rollback_restart"))
                else:
                    self.assertNotIn("rollback_restart", runner.calls)

    def test_health_probe_rejects_non_manifest_and_nonlocal_urls(self):
        release = self._release(use_real_health=True)
        for url in (
                "http://127.0.0.1:8096/api/gen/not-health",
                "http://localhost:8096/api/gen/health",
                "https://127.0.0.1:8096/api/gen/health",
                "http://127.0.0.1:8096/api/gen/health?proxy=1"):
            with self.subTest(url=url), self.assertRaisesRegex(
                    self.release_module.ReleaseError, "approved local endpoint"):
                release._http_status(url)

    def test_invalid_health_wait_policy_fails_before_backup_or_commands(self):
        for policy in (
                {"startup_timeout_seconds": 0, "interval_seconds": 1},
                {"startup_timeout_seconds": 60, "interval_seconds": 0},
                {"startup_timeout_seconds": 2, "interval_seconds": 3}):
            with self.subTest(policy=policy):
                runner = FakeRunner()
                manifest = copy.deepcopy(self.manifest)
                manifest["health_probe_policy"] = policy
                with self.assertRaisesRegex(
                        self.release_module.ReleaseError, "health"):
                    self._release(runner=runner, manifest=manifest).execute(
                        self.release_module.AUTHORIZED_TARGET
                    )
                self.assertEqual([], runner.calls)
                self.assertFalse(self.backup_root.exists())

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
            {"startup_timeout_seconds": 60, "interval_seconds": 1},
            self.manifest["health_probe_policy"],
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
            {
                "tests/test_digital_human_timeline.py",
                "tests/test_digital_human_v2.py",
                "tests/test_digital_human_v2_ui.py",
                "tests/test_digital_human_v2_deployment_manifest.py",
                "tests/test_digital_human_v2_compose.py",
            },
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
        self.assertIn("py_compile", rendered)
        self.assertEqual(1, len(commands))
        self.assertEqual(["/usr/bin/python3", "-m", "py_compile"], commands[0]["argv"][:3])
        for runtime_path in (
                "digital_human_timeline.py", "digital_human_v2.py",
                "digital_human_oneclick.py", "script_to_video.py", "points.py"):
            self.assertIn(runtime_path, rendered)

    def test_runtime_preflights_use_the_real_content_service_identity(self):
        for stage in ("cache", "offline", "font"):
            commands = self.manifest["release_commands"][stage]
            self.assertEqual(1, len(commands))
            self.assertEqual("{runtime:/home/ubuntu/content-api}", commands[0]["cwd"])
        self.assertIn(
            "HF_HUB_OFFLINE=1",
            self.manifest["release_commands"]["offline"][0]["argv"],
        )
        self.assertIn(
            "get('font')",
            " ".join(self.manifest["release_commands"]["font"][0]["argv"]),
        )


if __name__ == "__main__":
    unittest.main()
