# -*- coding: utf-8 -*-
import copy
import hashlib
import io
import importlib.util
import json
import os
import pathlib
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
EXECUTOR = ROOT / "scripts" / "deploy_locked_manifest.py"
MANIFEST = (
    ROOT / "deploy" / "test-runtime" / "director-agent-v2-20260821.json"
)
HISTORICAL_MANIFEST = (
    ROOT / "deploy" / "test-runtime" / "director-agent-v1-20260820.json"
)


def _load_executor():
    spec = importlib.util.spec_from_file_location("director_release", EXECUTOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeHooks:
    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on

    def _record(self, value):
        self.calls.append(value)
        if self.fail_on and value.startswith(self.fail_on):
            raise RuntimeError("injected hook failure: " + value)

    def validate_node(self, path):
        self._record("node:" + pathlib.Path(path).name)

    def validate_import(self, python_root, modules):
        self._record("compile")
        if not pathlib.Path(python_root).is_dir() or not modules:
            raise AssertionError("invalid import validation contract")

    def service_active(self, service):
        self._record("active:" + service)
        return True

    def restart(self, service):
        self._record("restart:" + service)

    def probe_feature(self, url, feature, enabled):
        self._record("feature:%s:%s" % (feature, enabled))

    def probe(self, url, method, expected_status):
        self._record("probe:%s:%s" % (method, expected_status))

    def probe_static(self, url, expected_status, expected_sha256):
        self._record("static:%s:%s" % (
            expected_status, expected_sha256,
        ))

    def acceptance(self, specification):
        self._record("acceptance")
        if not specification.get("token_environment"):
            raise AssertionError("acceptance is not authenticated")


class DirectorAgentReleaseExecutorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load_executor()
        cls.base_manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.runtime = self.root / "runtime"
        self.backups = self.root / "backups"
        self.runtime.mkdir()
        self.manifest = copy.deepcopy(self.base_manifest)

        executor_data = EXECUTOR.read_bytes()
        self.manifest["release_executor"]["sha256"] = hashlib.sha256(
            executor_data
        ).hexdigest()
        self.manifest["release_executor"]["git_blob"] = self._blob(
            executor_data
        )
        self.original = {}
        for entry in self.manifest["files"]:
            source = (ROOT / entry["repository_path"]).read_bytes()
            entry["source_sha256"] = hashlib.sha256(source).hexdigest()
            entry["source_blob"] = self._blob(source)
            entry["expected_postimage_sha256"] = entry["source_sha256"]
            entry["expected_postimage_blob"] = entry["source_blob"]
            target = self._target(entry["runtime_path"])
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

        python_root = self._target("/home/ubuntu/content-api")
        (python_root / "content_domains").mkdir(parents=True, exist_ok=True)
        database = self._target(
            self.manifest["feature_activation"]["database_path"]
        )
        with closing(sqlite3.connect(str(database))) as connection:
            connection.execute(
                """CREATE TABLE feature_flags(
                    feature TEXT PRIMARY KEY, enabled INTEGER NOT NULL,
                    updated_by TEXT, updated_at INTEGER NOT NULL
                )"""
            )
            connection.execute(
                "INSERT INTO feature_flags VALUES(?,?,?,?)",
                ("director_agent", 0, "before-release", 123456),
            )
            connection.commit()
        self.original_feature = self._feature_row()

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def _blob(data):
        return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()

    def _target(self, runtime_path):
        return self.runtime.joinpath(
            *pathlib.PurePosixPath(runtime_path).parts[1:]
        )

    def _feature_row(self):
        database = self._target(
            self.manifest["feature_activation"]["database_path"]
        )
        with closing(sqlite3.connect(str(database))) as connection:
            return connection.execute(
                "SELECT feature,enabled,updated_by,updated_at FROM feature_flags "
                "WHERE feature='director_agent'"
            ).fetchone()

    def _snapshot(self):
        values = {}
        for entry in self.manifest["files"]:
            target = self._target(entry["runtime_path"])
            values[entry["runtime_path"]] = (
                target.read_bytes()
                if target.is_file() and not target.is_symlink() else None
            )
        return values

    def _execute(self, checkpoint=None, hooks=None):
        manifest_path = self.root / "manifest.json"
        manifest_path.write_text(
            json.dumps(self.manifest, ensure_ascii=False), encoding="utf-8"
        )
        hooks = hooks or FakeHooks()
        result = self.module.execute_locked_release(
            manifest_path, ROOT, self.runtime, self.backups,
            hooks=hooks, verify_repository=False,
            checkpoint=checkpoint,
            reviewed_head="1" * 40, merged_main="2" * 40,
        )
        return result, hooks

    def test_manifest_loads_and_executes_real_seven_file_contract(self):
        loaded = self.module._load_manifest(MANIFEST)
        self.assertEqual(
            "director_agent_seven_file_v2",
            loaded["release_executor"]["contract"],
        )
        result, hooks = self._execute()
        self.assertEqual("deployed", result["status"])
        self.assertEqual(7, len(self.manifest["files"]))
        for entry in self.manifest["files"]:
            self.assertEqual(
                (ROOT / entry["repository_path"]).read_bytes(),
                self._target(entry["runtime_path"]).read_bytes(),
            )
        feature = self._feature_row()
        self.assertEqual(1, feature[1])
        self.assertEqual("release:pr276", feature[2])
        self.assertEqual(1, sum(call.startswith("restart:") for call in hooks.calls))
        self.assertIn("acceptance", hooks.calls)
        audit = json.loads(
            (pathlib.Path(result["backup"]) / "audit.json").read_text("utf-8")
        )
        self.assertEqual("1" * 40, audit["reviewed_head"])
        self.assertEqual("2" * 40, audit["merged_main"])
        self.assertEqual(
            self.manifest["release_executor"]["git_blob"],
            audit["executor_git_blob"],
        )
        self.assertEqual(7, len(audit["files"]))
        self.assertEqual(7, len(audit["final_files"]))
        self.assertTrue(all(
            {"mode", "uid", "gid"}.issubset(item) for item in audit["files"]
        ))
        self.assertEqual(2, sum(call.startswith("static:") for call in hooks.calls))

    def test_successor_manifest_preserves_all_historical_file_locks(self):
        historical = json.loads(HISTORICAL_MANIFEST.read_text(encoding="utf-8"))
        lock_fields = {
            "repository_path", "runtime_path", "source_blob", "source_sha256",
            "target_preimage_state", "target_preimage_blob",
            "target_preimage_sha256", "expected_postimage_blob",
            "expected_postimage_sha256",
        }
        expected = [
            {key: entry.get(key) for key in lock_fields}
            for entry in historical["files"]
        ]
        actual = [
            {key: entry.get(key) for key in lock_fields}
            for entry in self.base_manifest["files"]
        ]
        self.assertEqual(expected, actual)
        self.assertEqual(
            "release20260821",
            historical["release_executor"]["authenticated_acceptance"]
            ["request"]["page_revision"],
        )

    def test_successor_manifest_locks_current_executor_exactly(self):
        data = EXECUTOR.read_bytes()
        self.assertEqual(
            hashlib.sha256(data).hexdigest(),
            self.base_manifest["release_executor"]["sha256"],
        )
        self.assertEqual(
            self._blob(data),
            self.base_manifest["release_executor"]["git_blob"],
        )

    def test_invalid_acceptance_revision_fails_before_backup_or_hooks(self):
        self.manifest["release_executor"]["authenticated_acceptance"]["request"][
            "page_revision"
        ] = "release20260821"
        hooks = FakeHooks()
        with self.assertRaisesRegex(
            self.module.ReleaseError, "page_revision must match",
        ):
            self._execute(hooks=hooks)
        self.assertEqual([], hooks.calls)
        self.assertFalse(self.backups.exists())
        self.assertEqual(self.original, self._snapshot())

    def test_node_resolution_prefers_path_node(self):
        with mock.patch.object(
            self.module.shutil, "which", return_value="/usr/bin/node",
        ) as which:
            resolved = self.module._resolve_node_binary({"PATH": "/usr/bin"})
        self.assertEqual("/usr/bin/node", resolved)
        which.assert_called_once_with("node", path="/usr/bin")

    def test_node_resolution_uses_controlled_fallback(self):
        with mock.patch.object(
            self.module.shutil, "which",
            side_effect=[None, "/home/ubuntu/.local/hq-node/bin/node"],
        ) as which:
            resolved = self.module._resolve_node_binary({"PATH": "/usr/sbin"})
        self.assertEqual("/home/ubuntu/.local/hq-node/bin/node", resolved)
        self.assertEqual([
            mock.call("node", path="/usr/sbin"),
            mock.call("/home/ubuntu/.local/hq-node/bin/node"),
        ], which.call_args_list)

    def test_node_resolution_fails_closed_when_all_candidates_are_missing(self):
        with mock.patch.object(
            self.module.shutil, "which", side_effect=[None, None],
        ):
            with self.assertRaisesRegex(
                self.module.ReleaseError,
                "Node.js executable not found.*hq-node/bin/node",
            ):
                self.module._resolve_node_binary({"PATH": "/usr/sbin"})

    def test_absent_feature_row_is_removed_on_rollback(self):
        database = self._target(
            self.manifest["feature_activation"]["database_path"]
        )
        with closing(sqlite3.connect(str(database))) as connection:
            connection.execute(
                "DELETE FROM feature_flags WHERE feature='director_agent'"
            )
            connection.commit()
        self.original_feature = None

        def fail_after_activation(stage):
            if stage == "after_activate":
                raise RuntimeError("injected activation failure")

        with self.assertRaisesRegex(RuntimeError, "activation"):
            self._execute(checkpoint=fail_after_activation)
        self.assertIsNone(self._feature_row())
        self.assertEqual(self.original, self._snapshot())
        audits = list(self.backups.glob("director-agent-*/audit.json"))
        self.assertEqual(1, len(audits))
        audit = json.loads(audits[0].read_text("utf-8"))
        self.assertEqual("rolled_back", audit["status"])
        self.assertEqual(7, len(audit["final_files"]))

    def test_every_forward_stage_restores_seven_files_and_feature_row(self):
        stages = [
            *("after_replace_%d" % index for index in range(7)),
            "after_compile", "after_restart", "after_health_disabled",
            "after_activate", "after_health_enabled", "after_acceptance",
            "after_final_audit",
        ]
        for stage in stages:
            with self.subTest(stage=stage):
                def inject(current, expected=stage):
                    if current == expected:
                        raise RuntimeError("injected %s failure" % expected)

                with self.assertRaisesRegex(RuntimeError, "injected"):
                    self._execute(checkpoint=inject)
                self.assertEqual(self.original, self._snapshot())
                self.assertEqual(self.original_feature, self._feature_row())

    def test_real_static_hook_failure_restores_seven_files_and_feature_row(self):
        with self.assertRaisesRegex(RuntimeError, "static"):
            self._execute(hooks=FakeHooks(fail_on="static:"))
        self.assertEqual(self.original, self._snapshot())
        self.assertEqual(self.original_feature, self._feature_row())

    def test_rollback_retries_slow_service_and_records_success(self):
        class SlowStartHooks(FakeHooks):
            def __init__(self):
                super().__init__(fail_on="acceptance")
                self.active_calls = 0

            def service_active(self, service):
                self._record("active:" + service)
                self.active_calls += 1
                return self.active_calls <= 2 or self.active_calls >= 5

        clock = {"now": 0.0}

        def monotonic():
            return clock["now"]

        def sleep(seconds):
            clock["now"] += seconds

        hooks = SlowStartHooks()
        with (
            mock.patch.object(self.module.time, "monotonic", side_effect=monotonic),
            mock.patch.object(self.module.time, "sleep", side_effect=sleep),
            self.assertRaisesRegex(RuntimeError, "acceptance"),
        ):
            self._execute(hooks=hooks)
        audit_path = next(self.backups.glob("director-agent-*/audit.json"))
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        self.assertEqual("rolled_back", audit["status"])
        self.assertEqual([], audit["rollback_errors"])
        self.assertGreaterEqual(hooks.active_calls, 5)
        self.assertEqual(self.original, self._snapshot())
        self.assertEqual(self.original_feature, self._feature_row())

    def test_rollback_health_timeout_remains_fail_closed(self):
        class NeverReadyHooks(FakeHooks):
            def __init__(self):
                super().__init__(fail_on="acceptance")
                self.active_calls = 0

            def service_active(self, service):
                self._record("active:" + service)
                self.active_calls += 1
                return self.active_calls <= 2

        self.manifest["release_executor"]["rollback_health_policy"] = {
            "timeout_seconds": 2, "interval_seconds": 1,
        }
        clock = {"now": 0.0}

        def monotonic():
            return clock["now"]

        def sleep(seconds):
            clock["now"] += seconds

        with (
            mock.patch.object(self.module.time, "monotonic", side_effect=monotonic),
            mock.patch.object(self.module.time, "sleep", side_effect=sleep),
            self.assertRaisesRegex(
                self.module.ReleaseError,
                "forward release failed and rollback failed: service:ReleaseError",
            ),
        ):
            self._execute(hooks=NeverReadyHooks())
        audit_path = next(self.backups.glob("director-agent-*/audit.json"))
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        self.assertEqual("rollback_failed", audit["status"])
        self.assertEqual(["service:ReleaseError"], audit["rollback_errors"])
        self.assertEqual(self.original, self._snapshot())
        self.assertEqual(self.original_feature, self._feature_row())

    def test_authenticated_acceptance_polls_original_zero_cost_job_to_done(self):
        responses = [
            {"job_id": 77, "cost": 0, "points_left": 321},
            {"job_id": 77, "cost": 0, "points_left": 321},
            {"id": 77, "kind": "director_agent", "cost": 0,
             "status": "pending"},
            {"id": 77, "kind": "director_agent", "cost": 0,
             "status": "done",
             "result": {"type": "director_agent", "content": "ok"}},
        ]
        requests = []

        class Response:
            status = 200

            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                return json.dumps(self.payload).encode("utf-8")

        class Opener:
            def open(self, request, timeout):
                requests.append(request)
                return Response(responses.pop(0))

        specification = copy.deepcopy(
            self.manifest["release_executor"]["authenticated_acceptance"]
        )
        with (
            mock.patch.dict(os.environ, {
                specification["token_environment"]: "release-token",
            }),
            mock.patch.object(
                self.module.urllib.request, "build_opener",
                return_value=Opener(),
            ),
            mock.patch.object(self.module.secrets, "token_hex", return_value="a" * 32),
            mock.patch.object(self.module.time, "sleep"),
        ):
            self.module.SystemHooks().acceptance(specification)

        self.assertEqual([], responses)
        self.assertEqual(["POST", "POST", "GET", "GET"], [
            request.get_method() for request in requests
        ])
        keys = [request.get_header("Idempotency-key") for request in requests]
        self.assertEqual(1, len(set(keys)))
        self.assertEqual("release-pr276-" + "a" * 32, keys[0])

    def test_authenticated_acceptance_http_error_includes_backend_diagnostics(self):
        specification = copy.deepcopy(
            self.manifest["release_executor"]["authenticated_acceptance"]
        )
        body = json.dumps({
            "error": {
                "code": "HQ-REQUEST-001",
                "message": "页面版本无效",
            },
        }, ensure_ascii=False).encode("utf-8")
        error = self.module.urllib.error.HTTPError(
            specification["submit_url"], 400, "Bad Request", {}, io.BytesIO(body),
        )
        opener = mock.Mock()
        opener.open.side_effect = error
        with (
            mock.patch.dict(os.environ, {
                specification["token_environment"]: "release-token",
            }),
            mock.patch.object(
                self.module.urllib.request, "build_opener", return_value=opener,
            ),
            self.assertRaisesRegex(
                self.module.ReleaseError,
                "HTTP 400: HQ-REQUEST-001 / 页面版本无效",
            ),
        ):
            self.module.SystemHooks().acceptance(specification)

    def test_static_probe_checks_the_served_response_bytes(self):
        data = b"locked static response"

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                return data

        opener = mock.Mock()
        opener.open.return_value = Response()
        with mock.patch.object(
            self.module.urllib.request, "build_opener", return_value=opener,
        ):
            hooks = self.module.SystemHooks()
            hooks.probe_static(
                "https://test.example/static.js", 200,
                hashlib.sha256(data).hexdigest(),
            )
            with self.assertRaisesRegex(
                self.module.ReleaseError, "static bytes",
            ):
                hooks.probe_static(
                    "https://test.example/static.js", 200, "0" * 64,
                )


if __name__ == "__main__":
    unittest.main()
