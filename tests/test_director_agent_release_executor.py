# -*- coding: utf-8 -*-
import copy
import hashlib
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
            "director_agent_seven_file_v1",
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
