import hashlib
import importlib.util
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
import urllib.error
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SUCCESSOR_PATH = (
    ROOT / "docs" / "release-manifests" /
    "digital-human-material-feishu-priority-20260823.json"
)
HISTORICAL_MANIFEST_PATH = (
    ROOT / "deploy" / "test-runtime" /
    "digital-human-material-seedream-v3-20260821.json"
)
REJECTED_HISTORICAL_MANIFEST_PATH = (
    ROOT / "deploy" / "test-runtime" / "digital-human-material-v2-20260818.json"
)
REJECTED_DOCS_MANIFEST_PATH = (
    ROOT / "docs" / "release-manifests" / "unapproved-successor.json"
)
HISTORICAL_EXECUTOR_PATH = ROOT / "scripts" / "deploy_content_whisper_runtime.py"
VERSIONED_EXECUTOR_PATH = ROOT / "scripts" / "deploy_seedream_v3_locked_manifest.py"
HISTORICAL_MANIFEST_BLOB = "1a1bef4ec64323208fe0212961e72e38206a69bf"
HISTORICAL_MANIFEST_SHA256 = (
    "1cb48ad76a69d05ea39d96f3bc0607ee18d2566354d7317e5bdb0dd6308d8578"
)
HISTORICAL_EXECUTOR_BLOB = "c9dd02ba92e03a476751ae15e03f4e1c5f68886a"
HISTORICAL_EXECUTOR_SHA256 = (
    "fbc4a9200d4b769aee37cb8de18ea614521e92baa19744d5e2a954021085c36f"
)
REVIEWED_SOURCE = "1" * 40
REVIEWED_MAIN = "2" * 40


def _blob(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _load_executor(path, name, scripts_path=None):
    if scripts_path is not None:
        scripts = str(scripts_path)
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        sys.modules.pop("verify_content_whisper_deployment", None)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeGitRunner:
    def run(self, arguments, *, source_root, allow_failure=False):
        if arguments[:2] == ["status", "--porcelain"]:
            output = ""
        elif arguments[:3] == ["symbolic-ref", "--short", "HEAD"]:
            output = "main\n"
        elif arguments == ["rev-parse", "HEAD"]:
            output = REVIEWED_MAIN + "\n"
        elif arguments == ["rev-parse", "refs/remotes/origin/main"]:
            output = REVIEWED_MAIN + "\n"
        elif arguments[:2] == ["ls-remote", "--exit-code"]:
            output = REVIEWED_MAIN + "\trefs/heads/main\n"
        elif arguments[:2] == ["merge-base", "--is-ancestor"]:
            return types.SimpleNamespace(stdout="", returncode=0)
        else:
            raise AssertionError("unexpected git call: %r" % (arguments,))
        return types.SimpleNamespace(stdout=output, returncode=0)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _http_error(status, retry_after=None):
    headers = {} if retry_after is None else {"Retry-After": retry_after}
    return urllib.error.HTTPError(
        "https://redacted.invalid", status, "transient", headers, None,
    )


class SeedreamV3ReleaseExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.source_root = self.root / "source"
        self.runtime_root = self.root / "runtime"
        self.backup_root = self.root / "backups"
        self.source_root.mkdir()
        self.runtime_root.mkdir()
        self.manifest = json.loads(SUCCESSOR_PATH.read_text(encoding="utf-8"))

        manifest_path = self.source_root / SUCCESSOR_PATH.relative_to(ROOT)
        manifest_path.parent.mkdir(parents=True)
        manifest_path.write_bytes(SUCCESSOR_PATH.read_bytes())
        self.manifest["_manifest_path"] = str(manifest_path)

        locked_tools = (
            self.manifest["executor"],
            self.manifest["executor"]["verifier"],
            self.manifest["executor"]["requirements_verifier"],
        )
        for lock in locked_tools:
            source = ROOT / lock["repository_path"]
            target = self.source_root / lock["repository_path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)

        executor_path = self.source_root / self.manifest["executor"]["repository_path"]
        self.versioned = _load_executor(
            executor_path, "seedream_v3_release_test", self.source_root / "scripts",
        )

    def tearDown(self):
        self.temporary.cleanup()

    def _release(self, module, manifest=None, **kwargs):
        return module.ContentWhisperRelease(
            manifest or self.manifest,
            self.source_root,
            self.runtime_root,
            self.backup_root,
            git_runner=FakeGitRunner(),
            reviewed_source_commit=REVIEWED_SOURCE,
            reviewed_main_commit=REVIEWED_MAIN,
            **kwargs,
        )

    @staticmethod
    def _service_environment():
        return {
            "FEISHU_APP_ID": "test-app-id",
            "FEISHU_APP_SECRET": "test-app-secret",
        }

    def _health_getter(self, statuses):
        queues = {url: list(values) for url, values in statuses.items()}
        calls = []

        def getter(url):
            calls.append(url)
            if url not in queues or not queues[url]:
                self.fail("unexpected health probe: %s" % url)
            value = queues[url].pop(0)
            if isinstance(value, BaseException):
                raise value
            return value

        return getter, calls

    def _prepare_execute_release(self, statuses, *, installed=1):
        clock = FakeClock()
        health_getter, health_calls = self._health_getter(statuses)
        release = self._release(
            self.versioned,
            health_getter=health_getter,
            monotonic=clock.monotonic,
            sleeper=clock.sleep,
            service_environment_getter=self._service_environment,
        )
        release._validate_target = mock.Mock()
        release._verify_source_checkout = mock.Mock()
        release._verify_release_tools = mock.Mock()
        release._preflight_release_commands = mock.Mock()
        release._source_payloads = mock.Mock(return_value={})
        release._run_stage = mock.Mock()
        release._verify_feishu_operational = mock.Mock()
        release._backup_all = mock.Mock()
        release._install_all = mock.Mock(return_value=installed)
        return release, health_calls

    def _start_states(self, digital_disposition, *, other_disposition=None):
        records = []
        for entry in self.manifest["files"]:
            disposition = (
                digital_disposition
                if entry["repository_path"] ==
                "server/content_domains/digital_human_v2.py"
                else (other_disposition or digital_disposition)
            )
            if disposition == "needs_install":
                state = entry["target_preimage_state"]
                sha256 = entry["target_preimage_sha256"]
                blob = entry.get("target_preimage_blob")
            elif disposition == "already_installed":
                state = "file"
                sha256 = entry["expected_postimage_sha256"]
                blob = entry["expected_postimage_blob"]
            else:
                self.fail("test helper only creates reachable start states")
            records.append({
                "repository_path": entry["repository_path"],
                "runtime_path": entry["runtime_path"],
                "state": state,
                "sha256": sha256,
                "blob": blob,
                "disposition": disposition,
            })
        return records

    def test_versioned_executor_accepts_locked_successor(self):
        release = self._release(self.versioned)
        release._verify_release_tools()
        release._verify_source_checkout(REVIEWED_SOURCE, REVIEWED_MAIN)

    def test_versioned_executor_rejects_historical_manifest(self):
        manifest = dict(self.manifest)
        manifest["_manifest_path"] = str(
            self.source_root / REJECTED_HISTORICAL_MANIFEST_PATH.relative_to(ROOT)
        )
        with self.assertRaisesRegex(
                self.versioned.ReleaseError,
                "manifest must come from the locked source checkout"):
            self._release(self.versioned, manifest)._verify_source_checkout(
                REVIEWED_SOURCE, REVIEWED_MAIN,
            )

    def test_versioned_executor_rejects_arbitrary_docs_manifest(self):
        manifest = dict(self.manifest)
        manifest["_manifest_path"] = str(
            self.source_root / REJECTED_DOCS_MANIFEST_PATH.relative_to(ROOT)
        )
        with self.assertRaisesRegex(
                self.versioned.ReleaseError,
                "manifest must come from the locked source checkout"):
            self._release(self.versioned, manifest)._verify_source_checkout(
                REVIEWED_SOURCE, REVIEWED_MAIN,
            )

    def test_historical_executor_rejects_successor_and_bytes_remain_exact(self):
        historical = _load_executor(
            HISTORICAL_EXECUTOR_PATH, "historical_seedream_release_test", ROOT / "scripts",
        )
        with self.assertRaisesRegex(
                historical.ReleaseError,
                "manifest must come from the locked source checkout"):
            self._release(historical)._verify_source_checkout(
                REVIEWED_SOURCE, REVIEWED_MAIN,
            )

        for path, blob_id, sha256 in (
            (
                HISTORICAL_EXECUTOR_PATH,
                HISTORICAL_EXECUTOR_BLOB,
                HISTORICAL_EXECUTOR_SHA256,
            ),
            (
                HISTORICAL_MANIFEST_PATH,
                HISTORICAL_MANIFEST_BLOB,
                HISTORICAL_MANIFEST_SHA256,
            ),
        ):
            data = path.read_bytes()
            self.assertEqual(_blob(data), blob_id)
            self.assertEqual(hashlib.sha256(data).hexdigest(), sha256)
            locked = subprocess.run(
                [
                    "git", "-c", "safe.directory=" + ROOT.as_posix(),
                    "cat-file", "blob", blob_id,
                ],
                cwd=ROOT,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ).stdout
            self.assertEqual(locked, data)

    def test_feishu_preflight_fails_closed_when_service_credentials_are_missing(self):
        release = self._release(
            self.versioned, service_environment_getter=lambda: {},
            feishu_json_getter=lambda _request, _environment: self.fail(
                "network must not run"
            ),
        )
        with self.assertRaisesRegex(
                self.versioned.ReleaseError, "credentials are missing"):
            release._verify_feishu_operational("pre-deployment")

    def test_default_http_status_accepts_only_the_three_exact_locked_urls(self):
        expected = {
            "http://127.0.0.1:8096/api/gen/health": 200,
            "http://127.0.0.1:8096/api/gen/history": 401,
            "http://127.0.0.1:8096/api/gen/digital-human-v2/history": 401,
        }
        release = self._release(self.versioned)
        for locked_url, status in expected.items():
            with self.subTest(url=locked_url):
                opener = mock.Mock()
                if status == 200:
                    response = mock.MagicMock()
                    response.__enter__.return_value.status = 200
                    opener.open.return_value = response
                else:
                    opener.open.side_effect = urllib.error.HTTPError(
                        locked_url, status, "Unauthorized", {}, None,
                    )
                with mock.patch.object(
                        self.versioned.urllib.request, "build_opener",
                        return_value=opener):
                    self.assertEqual(status, release._http_status(locked_url))
                request = opener.open.call_args.args[0]
                self.assertEqual(locked_url, request.full_url)

    def test_default_http_status_rejects_unapproved_path_even_if_manifest_locked(self):
        unapproved_url = "http://127.0.0.1:8096/api/gen/arbitrary"
        self.manifest["health_checks"].append({
            "url": unapproved_url,
            "target_repository_path": "server/content_domains/digital_human_v2.py",
            "target_runtime_path": (
                "/home/ubuntu/content-api/content_domains/digital_human_v2.py"
            ),
            "pre_status_by_disposition": {
                "needs_install": 401,
                "already_installed": 401,
                "unchanged": 401,
            },
            "post_expected_status": 401,
            "rollback_status_by_disposition": {
                "needs_install": 401,
                "already_installed": 401,
                "unchanged": 401,
            },
        })
        release = self._release(self.versioned)
        with mock.patch.object(
                self.versioned.urllib.request, "build_opener") as build_opener:
            with self.assertRaisesRegex(
                    self.versioned.ReleaseError,
                    "health probe URL is not an approved local endpoint"):
                release._http_status(unapproved_url)
        build_opener.assert_not_called()

    def test_default_http_status_rejects_every_non_exact_local_url_variant(self):
        variants = (
            "https://127.0.0.1:8096/api/gen/health",
            "http://localhost:8096/api/gen/health",
            "http://127.0.0.1:8097/api/gen/health",
            "http://127.0.0.1:8096/api/gen/health?ready=1",
            "http://127.0.0.1:8096/api/gen/health#ready",
            "http://127.0.0.1:8096/api/gen/digital-human-v2/history/extra",
        )
        release = self._release(self.versioned)
        with mock.patch.object(
                self.versioned.urllib.request, "build_opener") as build_opener:
            for url in variants:
                with self.subTest(url=url), self.assertRaisesRegex(
                        self.versioned.ReleaseError,
                        "health probe URL is not an approved local endpoint"):
                    release._http_status(url)
        build_opener.assert_not_called()

    def test_old_preimage_health_can_install_then_post_health_is_exact(self):
        health = "http://127.0.0.1:8096/api/gen/health"
        history = "http://127.0.0.1:8096/api/gen/history"
        digital_history = (
            "http://127.0.0.1:8096/api/gen/digital-human-v2/history"
        )
        release, calls = self._prepare_execute_release({
            health: [200, 200],
            history: [401, 401],
            digital_history: [404, 401],
        })
        start_states = self._start_states("needs_install")
        with mock.patch.object(
                self.versioned.manifest_verify, "classify_start_states",
                return_value=start_states):
            result = release.execute("test@8.148.158.106")
        self.assertEqual("deployed", result["status"])
        release._backup_all.assert_called_once_with(start_states)
        release._install_all.assert_called_once()
        self.assertEqual(
            calls,
            [health, history, digital_history, health, history, digital_history],
        )

    def test_needs_install_rejects_401_before_backup(self):
        health = "http://127.0.0.1:8096/api/gen/health"
        history = "http://127.0.0.1:8096/api/gen/history"
        digital_history = (
            "http://127.0.0.1:8096/api/gen/digital-human-v2/history"
        )
        release, _calls = self._prepare_execute_release({
            health: [200],
            history: [401],
            digital_history: [401, 401, 401],
        })
        release.manifest["health_probe_policy"] = {
            "startup_timeout_seconds": 2,
            "interval_seconds": 1,
        }
        start_states = self._start_states("needs_install")
        with mock.patch.object(
                self.versioned.manifest_verify, "classify_start_states",
                return_value=start_states):
            with self.assertRaisesRegex(
                    self.versioned.ReleaseError,
                    "pre-deployment health readiness timeout.*expected 404.*last status 401"):
                release.execute("test@8.148.158.106")
        release._backup_all.assert_not_called()
        release._install_all.assert_not_called()

    def test_already_installed_rejects_404_before_backup(self):
        health = "http://127.0.0.1:8096/api/gen/health"
        history = "http://127.0.0.1:8096/api/gen/history"
        digital_history = (
            "http://127.0.0.1:8096/api/gen/digital-human-v2/history"
        )
        release, _calls = self._prepare_execute_release({
            health: [200],
            history: [401],
            digital_history: [404, 404, 404],
        }, installed=0)
        release.manifest["health_probe_policy"] = {
            "startup_timeout_seconds": 2,
            "interval_seconds": 1,
        }
        start_states = self._start_states("already_installed")
        with mock.patch.object(
                self.versioned.manifest_verify, "classify_start_states",
                return_value=start_states):
            with self.assertRaisesRegex(
                    self.versioned.ReleaseError,
                    "pre-deployment health readiness timeout.*expected 401.*last status 404"):
                release.execute("test@8.148.158.106")
        release._backup_all.assert_not_called()
        release._install_all.assert_not_called()

    def test_mixed_start_state_selects_digital_human_disposition(self):
        health = "http://127.0.0.1:8096/api/gen/health"
        history = "http://127.0.0.1:8096/api/gen/history"
        digital_history = (
            "http://127.0.0.1:8096/api/gen/digital-human-v2/history"
        )
        release, _calls = self._prepare_execute_release({
            health: [200, 200],
            history: [401, 401],
            digital_history: [404, 401],
        })
        start_states = self._start_states(
            "needs_install", other_disposition="already_installed",
        )
        with mock.patch.object(
                self.versioned.manifest_verify, "classify_start_states",
                return_value=start_states):
            result = release.execute("test@8.148.158.106")
        self.assertEqual("deployed", result["status"])
        release._backup_all.assert_called_once_with(start_states)

    def test_post_health_failure_rolls_back_and_accepts_restored_404(self):
        health = "http://127.0.0.1:8096/api/gen/health"
        history = "http://127.0.0.1:8096/api/gen/history"
        digital_history = (
            "http://127.0.0.1:8096/api/gen/digital-human-v2/history"
        )
        release, calls = self._prepare_execute_release({
            health: [200, 200, 200],
            history: [401, 401, 401],
            digital_history: [404, 500, 500, 500, 404],
        })
        release.manifest["health_probe_policy"] = {
            "startup_timeout_seconds": 2,
            "interval_seconds": 1,
        }
        start_states = self._start_states("needs_install")
        with mock.patch.object(
                self.versioned.manifest_verify, "classify_start_states",
                return_value=start_states), mock.patch.object(
                    release, "_restore_all", wraps=release._restore_all,
                ) as restore:
            with self.assertRaisesRegex(
                    self.versioned.ReleaseError,
                    "post-deployment health readiness timeout.*last status 500"):
                release.execute("test@8.148.158.106")
        restore.assert_called_once_with()
        self.assertEqual(digital_history, calls[-1])
        self.assertIn(
            mock.call("rollback_restart"), release._run_stage.call_args_list,
        )

    def test_rollback_rejects_401_when_needs_install_was_restored(self):
        health = "http://127.0.0.1:8096/api/gen/health"
        history = "http://127.0.0.1:8096/api/gen/history"
        digital_history = (
            "http://127.0.0.1:8096/api/gen/digital-human-v2/history"
        )
        release, _calls = self._prepare_execute_release({
            health: [200, 200, 200],
            history: [401, 401, 401],
            digital_history: [404, 500, 500, 500, 401, 401, 401],
        })
        release.manifest["health_probe_policy"] = {
            "startup_timeout_seconds": 2,
            "interval_seconds": 1,
        }
        start_states = self._start_states("needs_install")
        with mock.patch.object(
                self.versioned.manifest_verify, "classify_start_states",
                return_value=start_states):
            with self.assertRaisesRegex(
                    self.versioned.RollbackError,
                    "release failed.*post-deployment.*rollback failed.*rollback service"):
                release.execute("test@8.148.158.106")
        self.assertIn(
            mock.call("rollback_restart"), release._run_stage.call_args_list,
        )

    def test_health_start_state_mapping_is_fail_closed(self):
        base = self._start_states("needs_install")
        cases = {}
        cases["incomplete"] = base[:-1]
        cases["duplicate"] = base + [dict(base[0])]
        unknown = [dict(item) for item in base]
        unknown[0]["runtime_path"] += ".unknown"
        cases["unknown runtime"] = unknown
        disposition = [dict(item) for item in base]
        disposition[0]["disposition"] = "maybe_installed"
        cases["disposition is unknown"] = disposition
        mismatch = [dict(item) for item in base]
        mismatch[0]["sha256"] = "0" * 64
        cases["does not match manifest file lock"] = mismatch
        for expected, start_states in cases.items():
            with self.subTest(expected=expected):
                release = self._release(
                    self.versioned,
                    health_getter=lambda _url: self.fail(
                        "health network must not run for invalid mapping"
                    ),
                )
                with self.assertRaisesRegex(
                        self.versioned.ReleaseError, expected):
                    release._verify_health(
                        phase="pre-deployment",
                        status_field="pre_status_by_disposition",
                        start_states=start_states,
                    )

    def test_unapproved_pre_health_fails_before_backup_or_install(self):
        health = "http://127.0.0.1:8096/api/gen/health"
        history = "http://127.0.0.1:8096/api/gen/history"
        digital_history = (
            "http://127.0.0.1:8096/api/gen/digital-human-v2/history"
        )
        release, _calls = self._prepare_execute_release({
            health: [200],
            history: [401],
            digital_history: [200, 200, 200],
        })
        release.manifest["health_probe_policy"] = {
            "startup_timeout_seconds": 2,
            "interval_seconds": 1,
        }
        start_states = self._start_states("needs_install")
        with mock.patch.object(
                self.versioned.manifest_verify, "classify_start_states",
                return_value=start_states):
            with self.assertRaisesRegex(
                    self.versioned.ReleaseError,
                    "pre-deployment health readiness timeout.*last status 200"):
                release.execute("test@8.148.158.106")
        release._backup_all.assert_not_called()
        release._install_all.assert_not_called()

    def test_pre_health_connection_failure_is_fail_closed_before_backup(self):
        health = "http://127.0.0.1:8096/api/gen/health"
        history = "http://127.0.0.1:8096/api/gen/history"
        digital_history = (
            "http://127.0.0.1:8096/api/gen/digital-human-v2/history"
        )
        release, _calls = self._prepare_execute_release({
            health: [200],
            history: [401],
            digital_history: [
                ConnectionError("not ready"),
                ConnectionError("not ready"),
                ConnectionError("not ready"),
            ],
        })
        release.manifest["health_probe_policy"] = {
            "startup_timeout_seconds": 2,
            "interval_seconds": 1,
        }
        start_states = self._start_states("needs_install")
        with mock.patch.object(
                self.versioned.manifest_verify, "classify_start_states",
                return_value=start_states):
            with self.assertRaisesRegex(
                    self.versioned.ReleaseError,
                    "pre-deployment health readiness timeout.*connection unavailable"):
                release.execute("test@8.148.158.106")
        release._backup_all.assert_not_called()
        release._install_all.assert_not_called()

    def test_already_deployed_pre_401_is_idempotent(self):
        health = "http://127.0.0.1:8096/api/gen/health"
        history = "http://127.0.0.1:8096/api/gen/history"
        digital_history = (
            "http://127.0.0.1:8096/api/gen/digital-human-v2/history"
        )
        release, calls = self._prepare_execute_release({
            health: [200, 200],
            history: [401, 401],
            digital_history: [401, 401],
        }, installed=0)
        start_states = self._start_states("already_installed")
        with mock.patch.object(
                self.versioned.manifest_verify, "classify_start_states",
                return_value=start_states):
            result = release.execute("test@8.148.158.106")
        self.assertEqual("already_deployed", result["status"])
        self.assertEqual(0, result["restart_count"])
        self.assertNotIn(mock.call("restart"), release._run_stage.call_args_list)
        self.assertEqual(
            calls,
            [health, history, digital_history, health, history, digital_history],
        )

    def test_feishu_preflight_rejects_table_or_view_permission_failure(self):
        responses = iter([
            {"code": 0, "tenant_access_token": "tenant-token"},
            {"code": 1254302, "msg": "permission denied"},
        ])
        release = self._release(
            self.versioned,
            service_environment_getter=self._service_environment,
            feishu_json_getter=lambda _request, _environment: next(responses),
        )
        with self.assertRaisesRegex(
                self.versioned.ReleaseError, "permission verification failed"):
            release._verify_feishu_operational("pre-deployment")

    def test_token_429_and_records_503_share_bounded_retry_policy(self):
        clock = FakeClock()
        calls = {"token": 0, "records": 0, "media": 0}

        def json_getter(request, _environment):
            if "/auth/" in request.full_url:
                calls["token"] += 1
                if calls["token"] == 1:
                    raise _http_error(429, "2")
                return {"code": 0, "tenant_access_token": "tenant-token"}
            calls["records"] += 1
            if calls["records"] == 1:
                raise _http_error(503)
            return {"code": 0, "data": {
                "items": [{"fields": {"material": [{
                    "file_token": "locked-attachment",
                }]}}],
                "has_more": False,
            }}

        release = self._release(
            self.versioned,
            monotonic=clock.monotonic,
            sleeper=clock.sleep,
            service_environment_getter=self._service_environment,
            feishu_json_getter=json_getter,
            feishu_media_getter=lambda _request, _environment: calls.__setitem__(
                "media", calls["media"] + 1,
            ),
        )
        release._verify_feishu_operational("pre-deployment")
        self.assertEqual(calls, {"token": 2, "records": 2, "media": 1})
        self.assertEqual(clock.now, 3.0)

    def test_token_records_and_attachment_share_one_total_deadline(self):
        clock = FakeClock()
        calls = {"token": 0, "records": 0}

        def json_getter(request, _environment):
            if "/auth/" in request.full_url:
                calls["token"] += 1
                if calls["token"] < 4:
                    raise _http_error(429, "15")
                return {"code": 0, "tenant_access_token": "tenant-token"}
            calls["records"] += 1
            raise _http_error(503, "15")

        release = self._release(
            self.versioned,
            monotonic=clock.monotonic,
            sleeper=clock.sleep,
            service_environment_getter=self._service_environment,
            feishu_json_getter=json_getter,
            feishu_media_getter=lambda _request, _environment: self.fail(
                "attachment must not run after the shared deadline"
            ),
        )
        with self.assertRaisesRegex(
                self.versioned.ReleaseError,
                "records request retry deadline exhausted after 1 attempts"):
            release._verify_feishu_operational("pre-deployment")
        self.assertEqual(calls, {"token": 4, "records": 1})
        self.assertEqual(clock.now, 60.0)

    def test_retry_after_invalid_negative_and_excessive_values_are_bounded(self):
        for retry_after, expected_wait in (
                ("1.5", 1.5), ("not-a-number", 1.0),
                ("-5", 1.0), ("999", 15.0)):
            with self.subTest(retry_after=retry_after):
                clock = FakeClock()
                attempts = []

                def getter(_request, _environment):
                    attempts.append(True)
                    if len(attempts) == 1:
                        raise _http_error(429, retry_after)
                    return {"ok": True}

                release = self._release(
                    self.versioned,
                    monotonic=clock.monotonic,
                    sleeper=clock.sleep,
                )
                result = release._feishu_request_with_retry(
                    getter, urllib.request.Request("https://redacted.invalid"),
                    {}, "test request",
                )
                self.assertEqual(result, {"ok": True})
                self.assertEqual(len(attempts), 2)
                self.assertEqual(clock.now, expected_wait)

    def test_non_retryable_auth_statuses_fail_on_the_first_request(self):
        for status in (401, 403):
            with self.subTest(status=status):
                calls = []

                def getter(_request, _environment):
                    calls.append(True)
                    raise _http_error(status, "15")

                release = self._release(self.versioned)
                with self.assertRaisesRegex(
                        self.versioned.ReleaseError,
                        "HTTP status %s after 1 attempt" % status):
                    release._feishu_request_with_retry(
                        getter,
                        urllib.request.Request("https://redacted.invalid"),
                        {}, "token request",
                    )
                self.assertEqual(len(calls), 1)

    def test_retry_error_does_not_expose_request_or_credentials(self):
        secret_url = "https://redacted.invalid/records?tenant_token=secret-token"
        error = urllib.error.HTTPError(
            secret_url, 429, "transient", {"Retry-After": "0"}, None,
        )
        release = self._release(self.versioned)
        with self.assertRaises(self.versioned.ReleaseError) as raised:
            release._feishu_request_with_retry(
                lambda _request, _environment: (_ for _ in ()).throw(error),
                urllib.request.Request(
                    secret_url, headers={"Authorization": "Bearer hidden"},
                ),
                {"FEISHU_APP_SECRET": "hidden-secret"},
                "records request",
            )
        message = str(raised.exception)
        self.assertIn("HTTP status 429 after 4 attempts", message)
        for secret in (secret_url, "secret-token", "Bearer", "hidden-secret"):
            self.assertNotIn(secret, message)

    def test_retry_policy_drift_is_rejected_before_any_request(self):
        self.manifest["configuration_requirements"]["feishu"][
            "retry_policy"
        ]["max_attempts"] = 5
        release = self._release(
            self.versioned,
            service_environment_getter=lambda: self.fail(
                "service environment must not be read"
            ),
            feishu_json_getter=lambda _request, _environment: self.fail(
                "Feishu request must not run"
            ),
        )
        with self.assertRaisesRegex(
                self.versioned.ReleaseError,
                "retry policy does not match the locked contract"):
            release._verify_feishu_operational("pre-deployment")

    def test_four_transient_failures_stop_before_backup_or_install(self):
        health = "http://127.0.0.1:8096/api/gen/health"
        history = "http://127.0.0.1:8096/api/gen/history"
        digital_history = (
            "http://127.0.0.1:8096/api/gen/digital-human-v2/history"
        )
        release, _calls = self._prepare_execute_release({
            health: [200], history: [401], digital_history: [404],
        })
        release._verify_feishu_operational = types.MethodType(
            self.versioned.ContentWhisperRelease._verify_feishu_operational,
            release,
        )
        attempts = []

        def exhausted(_request, _environment):
            attempts.append(True)
            raise _http_error(429, "0")

        release.feishu_json_getter = exhausted
        start_states = self._start_states("needs_install")
        with mock.patch.object(
                self.versioned.manifest_verify, "classify_start_states",
                return_value=start_states):
            with self.assertRaisesRegex(
                    self.versioned.ReleaseError,
                    "token request HTTP status 429 after 4 attempts"):
                release.execute("test@8.148.158.106")
        self.assertEqual(len(attempts), 4)
        release._backup_all.assert_not_called()
        release._install_all.assert_not_called()

    def test_attachment_429_retries_and_still_enforces_mime_allowlist(self):
        def json_getter(request, _environment):
            if "/auth/" in request.full_url:
                return {"code": 0, "tenant_access_token": "tenant-token"}
            return {"code": 0, "data": {
                "items": [{"fields": {"material": [{
                    "file_token": "locked-attachment",
                }]}}],
                "has_more": False,
            }}

        for mime, should_pass in (("image/png", True), ("text/plain", False)):
            with self.subTest(mime=mime):
                clock = FakeClock()
                response = mock.MagicMock()
                response.__enter__.return_value.headers = {
                    "Content-Type": mime + "; charset=binary",
                }
                response.__enter__.return_value.read.return_value = b"material"
                opener = mock.Mock()
                opener.open.side_effect = [_http_error(429, "0"), response]
                release = self._release(
                    self.versioned,
                    monotonic=clock.monotonic,
                    sleeper=clock.sleep,
                    service_environment_getter=self._service_environment,
                    feishu_json_getter=json_getter,
                )
                with mock.patch.object(
                        self.versioned.urllib.request, "build_opener",
                        return_value=opener):
                    if should_pass:
                        release._verify_feishu_operational("pre-deployment")
                    else:
                        with self.assertRaisesRegex(
                                self.versioned.ReleaseError,
                                "MIME is not an approved material type"):
                            release._verify_feishu_operational("pre-deployment")
                self.assertEqual(opener.open.call_count, 2)

    def test_attachment_retry_success_still_enforces_20mb_limit(self):
        def json_getter(request, _environment):
            if "/auth/" in request.full_url:
                return {"code": 0, "tenant_access_token": "tenant-token"}
            return {"code": 0, "data": {
                "items": [{"fields": {"material": [{
                    "file_token": "locked-attachment",
                }]}}],
                "has_more": False,
            }}

        response = mock.MagicMock()
        response.__enter__.return_value.headers = {"Content-Type": "image/png"}
        response.__enter__.return_value.read.return_value = (
            b"x" * (20 * 1024 * 1024 + 1)
        )
        opener = mock.Mock()
        opener.open.side_effect = [_http_error(503), response]
        release = self._release(
            self.versioned,
            service_environment_getter=self._service_environment,
            feishu_json_getter=json_getter,
            sleeper=lambda _seconds: None,
        )
        with mock.patch.object(
                self.versioned.urllib.request, "build_opener",
                return_value=opener):
            with self.assertRaisesRegex(
                    self.versioned.ReleaseError,
                    "attachment is empty or exceeds 20MB"):
                release._verify_feishu_operational("pre-deployment")
        self.assertEqual(opener.open.call_count, 2)

    def test_feishu_preflight_rejects_invalid_pagination_before_attachment(self):
        responses = iter([
            {"code": 0, "tenant_access_token": "tenant-token"},
            {"code": 0, "data": {
                "items": [], "has_more": True, "page_token": "",
            }},
        ])
        release = self._release(
            self.versioned,
            service_environment_getter=self._service_environment,
            feishu_json_getter=lambda _request, _environment: next(responses),
            feishu_media_getter=lambda _request, _environment: self.fail(
                "media must not run"
            ),
        )
        with self.assertRaisesRegex(
                self.versioned.ReleaseError, "pagination cursor is invalid"):
            release._verify_feishu_operational("pre-deployment")

    def test_feishu_preflight_reads_all_pages_and_downloads_attachment(self):
        calls = []

        def json_getter(request, environment):
            self.assertEqual(environment["FEISHU_APP_ID"], "test-app-id")
            calls.append(request.full_url)
            if "/auth/" in request.full_url:
                return {"code": 0, "tenant_access_token": "tenant-token"}
            if "page_token=next-page" in request.full_url:
                return {"code": 0, "data": {
                    "items": [{"fields": {"素材": [{
                        "file_token": "locked-attachment", "name": "material.png",
                    }]}}], "has_more": False,
                }}
            return {"code": 0, "data": {
                "items": [], "has_more": True, "page_token": "next-page",
            }}

        media_calls = []
        release = self._release(
            self.versioned,
            service_environment_getter=self._service_environment,
            feishu_json_getter=json_getter,
            feishu_media_getter=lambda request, _environment: media_calls.append(
                request.full_url
            ),
        )
        release._verify_feishu_operational("post-restart")
        self.assertEqual(len(calls), 3)
        self.assertIn("view_id=vewa9ZW0Og", calls[1])
        self.assertIn("page_token=next-page", calls[2])
        self.assertEqual(len(media_calls), 1)
        self.assertTrue(media_calls[0].endswith("/locked-attachment/download"))

    def test_post_restart_feishu_failure_triggers_full_restore(self):
        release = self._release(self.versioned)
        release._validate_target = mock.Mock()
        release._verify_source_checkout = mock.Mock()
        release._verify_release_tools = mock.Mock()
        release._preflight_release_commands = mock.Mock()
        release._source_payloads = mock.Mock(return_value={})
        release._health_probe_policy = mock.Mock(return_value=(60, 1))
        release._run_stage = mock.Mock()
        release._verify_health = mock.Mock()
        release._verify_feishu_operational = mock.Mock(side_effect=[
            None, self.versioned.ReleaseError("post-restart Feishu failed"),
        ])
        release._backup_all = mock.Mock()
        release._install_all = mock.Mock(return_value=1)
        release._restore_all = mock.Mock()
        with mock.patch.object(
                self.versioned.manifest_verify, "classify_start_states",
                return_value=[]):
            with self.assertRaisesRegex(
                    self.versioned.ReleaseError,
                    "all manifest targets were restored"):
                release.execute("test@8.148.158.106")
        release._restore_all.assert_called_once_with()
        self.assertEqual(
            release._verify_feishu_operational.call_args_list,
            [mock.call("pre-deployment"), mock.call("post-restart")],
        )


if __name__ == "__main__":
    unittest.main()
