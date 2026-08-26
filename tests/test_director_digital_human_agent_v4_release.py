import copy
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
EXECUTOR = (
    ROOT / "release-manifests/tools/deploy_director_digital_human_agent_v4_locked_manifest.py"
)
BASE_EXECUTOR = ROOT / "scripts/deploy_director_locked_manifest.py"
MANIFEST = (
    ROOT / "release-manifests/test-runtime/director-digital-human-agent-v4-20260826.json"
)
HISTORICAL_BASE_LOCK = {
    "git_blob": "4805b8f79e0f650be3185b505253231520e74e63",
    "sha256": "a319b3edf1ac66d44e1c3e1b10defc753eeb7a7b8e172f1f12cd89ebf17e0aa4",
}


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def git_blob(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def load_executor():
    specification = importlib.util.spec_from_file_location(
        "director_digital_human_release", EXECUTOR,
    )
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
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
        self._record("import")
        if not pathlib.Path(python_root).is_dir() or not modules:
            raise AssertionError("invalid candidate import contract")

    def service_active(self, service):
        self._record("active:" + service)
        return True

    def restart(self, service):
        self._record("restart:" + service)

    def probe(self, url, method, expected_status):
        self._record("probe:%s:%s" % (method, expected_status))

    def probe_feature(self, url, feature, enabled):
        self._record("feature:%s:%s" % (feature, enabled))

    def probe_static(self, url, expected_status, expected_sha256):
        self._record("static:%s:%s" % (expected_status, expected_sha256))

    def acceptance(self, specification):
        self._record("acceptance")
        if specification.get("expected_action") != {
            "type": "fill_field", "field": "digital_human_script",
            "value": "发布验收数字人口播",
        }:
            raise AssertionError("acceptance does not prove the digital-human fill")


class DirectorDigitalHumanAgentReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_executor()
        cls.base_manifest = cls.module._load_manifest(MANIFEST)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.runtime = self.root / "runtime"
        self.backups = self.root / "backups"
        self.manifest = copy.deepcopy(self.base_manifest)

        runtime_package = self.runtime / "home/ubuntu/content-api/content_domains"
        shutil.copytree(ROOT / "server/content_domains", runtime_package)
        self.original = {}
        for item in self.manifest["files"]:
            data = subprocess.run(
                ["git", "cat-file", "blob", item["preimage_blob"]],
                cwd=ROOT, check=True, stdout=subprocess.PIPE,
            ).stdout
            self.assertEqual(item["preimage_sha256"], sha256(data))
            target = self._target(item["runtime_path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            os.chmod(target, 0o640)
            self.original[item["runtime_path"]] = data

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
                ("director_agent", 1, "release:pr276", 123456),
            )
            connection.commit()
        self.original_feature = self._feature_row()

    def tearDown(self):
        self.temporary.cleanup()

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
        return {
            item["runtime_path"]: self._target(item["runtime_path"]).read_bytes()
            for item in self.manifest["files"]
        }

    def _execute(self, checkpoint=None, hooks=None):
        hooks = hooks or FakeHooks()
        with mock.patch.dict(os.environ, {
            "DIRECTOR_AGENT_RELEASE_TOKEN": "test-only-token",
        }):
            result = self.module._execute_manifest(
                self.manifest, ROOT, self.runtime, self.backups,
                hooks=hooks, verify_repository=False, checkpoint=checkpoint,
                reviewed_head="1" * 40, merged_main="2" * 40,
            )
        return result, hooks

    def test_locked_manifest_covers_exact_four_file_delta_and_executors(self):
        self.assertEqual(
            "director_digital_human_agent_four_file_v4",
            self.base_manifest["release_executor"]["contract"],
        )
        self.assertEqual(
            self.module.REQUIRED_REPOSITORY_PATHS,
            {item["repository_path"] for item in self.base_manifest["files"]},
        )
        self.assertEqual(4, len(self.base_manifest["files"]))
        self.assertEqual(
            [
                'data-director-guide-contract="digital-human-oneclick-guide-v1"',
                "script-agent.js?v=b1c3f8c3",
            ],
            self.base_manifest["release_executor"]["html_required_markers"]
            ["site/workbench/digital-human-oneclick.html"],
        )
        development_base = self.base_manifest["expected_preimage"]["online_main_commit"]
        for item in self.base_manifest["files"]:
            data = (ROOT / item["repository_path"]).read_bytes()
            self.assertEqual(item["postimage_blob"], git_blob(data))
            self.assertEqual(item["postimage_sha256"], sha256(data))
            self.assertEqual(
                item["preimage_blob"],
                subprocess.run(
                    ["git", "rev-parse", development_base + ":" +
                     item["repository_path"]],
                    cwd=ROOT, check=True, text=True, stdout=subprocess.PIPE,
                ).stdout.strip(),
            )
        static_hashes = {
            probe["expected_sha256"]
            for probe in self.base_manifest["release_executor"]["static_probes"]
        }
        self.assertEqual({
            item["postimage_sha256"] for item in self.base_manifest["files"]
            if item["repository_path"].startswith("site/")
        }, static_hashes)
        executor_data = EXECUTOR.read_bytes()
        self.assertEqual(
            self.base_manifest["release_executor"]["git_blob"],
            git_blob(executor_data),
        )
        self.assertEqual(
            self.base_manifest["release_executor"]["sha256"],
            sha256(executor_data),
        )
        base_data = BASE_EXECUTOR.read_bytes()
        self.assertEqual(HISTORICAL_BASE_LOCK["git_blob"], git_blob(base_data))
        self.assertEqual(HISTORICAL_BASE_LOCK["sha256"], sha256(base_data))
        for lock in self.base_manifest["release_contract_sources"]:
            data = (ROOT / lock["repository_path"]).read_bytes()
            self.assertEqual(lock["git_blob"], git_blob(data))
            self.assertEqual(lock["sha256"], sha256(data))
        impact = json.loads((
            ROOT / "deploy/test-release/impacts/"
            "pr-293-digital-human-guide-v1.json"
        ).read_text(encoding="utf-8"))
        self.assertEqual({
            "server/content_domains/director_agent.py",
            "site/workbench/digital-human-oneclick.html",
        }, set(impact["runtime_changes"]))

    def test_public_entry_rejects_every_other_manifest_path(self):
        with tempfile.TemporaryDirectory() as directory:
            copied = pathlib.Path(directory) / MANIFEST.name
            shutil.copy2(MANIFEST, copied)
            with self.assertRaisesRegex(
                self.module.ReleaseError, "rejects every other",
            ):
                self.module._load_manifest(copied)

    def test_missing_deployment_token_fails_before_hooks_backup_or_writes(self):
        before = self._snapshot()
        feature_before = self._feature_row()
        hooks = FakeHooks()
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            self.assertRaisesRegex(
                self.module.ReleaseError, "deployment-only acceptance token",
            ),
        ):
            self.module._execute_manifest(
                self.manifest, ROOT, self.runtime, self.backups,
                hooks=hooks, verify_repository=False,
                reviewed_head="1" * 40, merged_main="2" * 40,
            )
        self.assertEqual([], hooks.calls)
        self.assertFalse(self.backups.exists())
        self.assertEqual(before, self._snapshot())
        self.assertEqual(feature_before, self._feature_row())

    def test_success_backs_up_and_deploys_four_files_and_enabled_feature(self):
        result, hooks = self._execute()
        self.assertEqual("deployed", result["status"])
        for item in self.manifest["files"]:
            self.assertEqual(
                (ROOT / item["repository_path"]).read_bytes(),
                self._target(item["runtime_path"]).read_bytes(),
            )
        feature = self._feature_row()
        self.assertEqual(1, feature[1])
        self.assertEqual("release:director-dh-agent-v4", feature[2])
        self.assertEqual(1, sum(call.startswith("restart:") for call in hooks.calls))
        self.assertEqual(3, sum(call.startswith("static:") for call in hooks.calls))
        self.assertIn("feature:director_agent_enabled:False", hooks.calls)
        self.assertIn("feature:director_agent_enabled:True", hooks.calls)
        self.assertIn("acceptance", hooks.calls)
        audit = json.loads(
            (pathlib.Path(result["backup"]) / "audit.json").read_text("utf-8")
        )
        self.assertEqual("deployed", audit["status"])
        self.assertEqual(self.original_feature[2], audit["feature_preimage"]["updated_by"])
        self.assertEqual(4, len(audit["files"]))
        self.assertEqual(4, len(audit["final_files"]))
        self.assertEqual(
            self.manifest["release_executor"]["locked_base_executor"]["git_blob"],
            audit["base_executor_git_blob"],
        )

    def test_every_post_backup_stage_restores_four_files_and_exact_feature_row(self):
        stages = [
            "after_backup", "after_disable", "after_health_disabled_before_install",
            *("after_replace_%d" % index for index in range(4)),
            "after_compile", "after_restart", "after_health_disabled",
            "after_activate", "after_static", "after_acceptance",
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

    def test_real_static_and_acceptance_failures_restore_complete_preimage(self):
        for hook in ("static:", "acceptance"):
            with self.subTest(hook=hook), self.assertRaisesRegex(
                RuntimeError, "injected hook",
            ):
                self._execute(hooks=FakeHooks(fail_on=hook))
            self.assertEqual(self.original, self._snapshot())
            self.assertEqual(self.original_feature, self._feature_row())

    def test_mixed_pre_and_postimage_start_rolls_back_exactly(self):
        for item in self.manifest["files"][::2]:
            postimage = (ROOT / item["repository_path"]).read_bytes()
            self._target(item["runtime_path"]).write_bytes(postimage)
            self.original[item["runtime_path"]] = postimage
        with self.assertRaisesRegex(RuntimeError, "injected hook"):
            self._execute(hooks=FakeHooks(fail_on="static:"))
        self.assertEqual(self.original, self._snapshot())
        audit_path = next(self.backups.glob("*/audit.json"))
        audit = json.loads(audit_path.read_text("utf-8"))
        self.assertEqual(
            ["already_installed", "needs_install", "unchanged", "unchanged"],
            [item["start_state"] for item in audit["files"]],
        )

    def test_disabled_feature_preimage_aborts_before_backup_or_write(self):
        database = self._target(
            self.manifest["feature_activation"]["database_path"]
        )
        with closing(sqlite3.connect(str(database))) as connection:
            connection.execute(
                "UPDATE feature_flags SET enabled=0 WHERE feature='director_agent'"
            )
            connection.commit()
        before = self._snapshot()
        with self.assertRaisesRegex(
            self.module.ReleaseError, "feature flag preimage",
        ):
            self._execute()
        self.assertEqual(before, self._snapshot())
        self.assertFalse(self.backups.exists())

    def test_source_and_live_preimage_drift_fail_before_backup(self):
        self.manifest["files"][0]["postimage_sha256"] = "0" * 64
        with self.assertRaisesRegex(
            self.module.ReleaseError, "candidate lock",
        ):
            self._execute()
        self.assertFalse(self.backups.exists())
        self.manifest = copy.deepcopy(self.base_manifest)
        self._target(self.manifest["files"][0]["runtime_path"]).write_bytes(b"drift")
        with self.assertRaisesRegex(
            self.module.ReleaseError, "runtime preimage",
        ):
            self._execute()
        self.assertFalse(self.backups.exists())

    def test_reviewed_head_must_be_exact_manifest_and_test_lock_commit(self):
        parent = self.manifest["source"]["code_source_commit"]
        reviewed = "b" * 40
        merged = "c" * 40
        valid_delta = set(self.module.ALLOWED_REVIEW_DELTA)

        def git_result(command, cwd=None, env=None):
            if command[:2] == ["git", "rev-parse"]:
                self.assertEqual(reviewed + "^", command[2])
                return parent
            if command[:3] == ["git", "diff", "--name-only"]:
                return "\n".join(sorted(valid_delta))
            raise AssertionError(command)

        with (
            mock.patch.object(
                self.module.BASE, "_verify_director_checkout", return_value=merged,
            ),
            mock.patch.object(self.module.BASE, "_run", side_effect=git_result),
        ):
            self.assertEqual(
                merged,
                self.module._verify_checkout(ROOT, self.manifest, reviewed, merged),
            )
            with (
                mock.patch.object(
                    self.module, "ALLOWED_REVIEW_DELTA", {"unexpected.py"},
                ),
                self.assertRaisesRegex(
                    self.module.ReleaseError, "manifest-and-test",
                ),
            ):
                self.module._verify_checkout(ROOT, self.manifest, reviewed, merged)

    def test_authenticated_acceptance_replays_original_digital_human_job(self):
        specification = copy.deepcopy(
            self.manifest["release_executor"]["authenticated_acceptance"]
        )
        revision = specification["request"]["page_revision"]
        responses = [
            {"job_id": 77, "cost": 0},
            {"job_id": 77, "cost": 0},
            {"id": 77, "kind": "director_agent", "cost": 0,
             "status": "pending"},
            {"id": 77, "kind": "director_agent", "cost": 0,
             "status": "done", "result": {
                 "type": "director_agent", "plan": {
                     "page_revision": revision,
                     "actions": [{"type": "fill_field",
                                  "field": "digital_human_script",
                                  "value": "发布验收数字人口播"}],
                 },
             }},
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

        with (
            mock.patch.dict(os.environ, {
                specification["token_environment"]: "release-token",
            }),
            mock.patch.object(
                self.module.urllib.request, "build_opener", return_value=Opener(),
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
        self.assertEqual("release-dh-agent-v4-" + "a" * 32, keys[0])

    def test_authenticated_acceptance_rejects_done_job_without_fill_action(self):
        specification = copy.deepcopy(
            self.manifest["release_executor"]["authenticated_acceptance"]
        )
        result = {
            "type": "director_agent",
            "plan": {"page_revision": specification["request"]["page_revision"],
                     "actions": []},
        }
        responses = [
            {"job_id": 77, "cost": 0}, {"job_id": 77, "cost": 0},
            {"id": 77, "kind": "director_agent", "cost": 0,
             "status": "done", "result": result},
        ]

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

        opener = mock.Mock()
        opener.open.side_effect = [Response(payload) for payload in responses]
        with (
            mock.patch.dict(os.environ, {
                specification["token_environment"]: "release-token",
            }),
            mock.patch.object(
                self.module.urllib.request, "build_opener", return_value=opener,
            ),
            self.assertRaisesRegex(
                self.module.ReleaseError, "digital-human Agent acceptance",
            ),
        ):
            self.module.SystemHooks().acceptance(specification)

    def test_http_error_includes_backend_diagnostics(self):
        specification = copy.deepcopy(
            self.manifest["release_executor"]["authenticated_acceptance"]
        )
        body = json.dumps({
            "error": {"code": "HQ-REQUEST-001", "message": "页面版本无效"},
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


if __name__ == "__main__":
    unittest.main()
