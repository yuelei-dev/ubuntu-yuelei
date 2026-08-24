import copy
import hashlib
import importlib.util
import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing


ROOT = pathlib.Path(__file__).resolve().parents[1]
EXECUTOR = ROOT / "scripts/deploy_private_domain_video_v1_locked_manifest.py"
MANIFEST = ROOT / "deploy/test-runtime/private-domain-video-v1-20260824.json"
BGM_PREIMAGE = ROOT / "tests/fixtures/private-domain-bgm-manifest-preimage.json"


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def git_blob(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def git_bytes(blob):
    return subprocess.run(
        ["git", "cat-file", "blob", blob], cwd=ROOT, check=True,
        stdout=subprocess.PIPE,
    ).stdout


def load_executor():
    specification = importlib.util.spec_from_file_location(
        "private_domain_release", EXECUTOR,
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

    def probe_external_asset(self, specification):
        self._record("external:" + specification["url"].rsplit("/", 1)[-1])

    def validate_node(self, path):
        self._record("node")

    def validate_import(self, python_root, modules):
        self._record("import")
        if not pathlib.Path(python_root).is_dir() or not modules:
            raise AssertionError("invalid import contract")

    def service_active(self, service):
        self._record("active")
        return True

    def restart(self, service):
        self._record("restart")

    def probe_feature(self, url, feature, enabled):
        self._record("feature:" + str(enabled))

    def probe(self, url, method, expected_status):
        self._record("probe:%s:%s" % (method, expected_status))

    def acceptance(self, specification):
        self._record("acceptance")
        if specification["expected_action"] != {
                "type": "fill_field", "field": "private_domain_copy"}:
            raise AssertionError("acceptance does not prove private copy fill")


class PrivateDomainReleaseTests(unittest.TestCase):
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
        machine_id = "0123456789abcdef0123456789abcdef"
        hostname = "huangque-test-fixture"
        etc = self.runtime / "etc"
        etc.mkdir(parents=True, exist_ok=True)
        (etc / "machine-id").write_text(machine_id + "\n", encoding="ascii")
        (etc / "hostname").write_text(hostname + "\n", encoding="utf-8")
        identity = {
            "schema_version": 1,
            "environment": "test",
            "host_id": "yuelei-test-01",
            "public_host": "8.148.158.106",
            "hostname": hostname,
            "machine_id_sha256": sha256(machine_id.encode("ascii")),
        }
        identity_path = etc / "huangque/release-identity.json"
        identity_path.parent.mkdir(parents=True, exist_ok=True)
        identity_path.write_text(json.dumps(identity), encoding="utf-8")
        os.chmod(identity_path, 0o600)
        shutil.copytree(
            ROOT / "server/content_domains",
            self.runtime / "home/ubuntu/content-api/content_domains",
        )
        self.original = {}
        for item in self.manifest["files"]:
            target = self._target(item["runtime_path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            if item["target_preimage_state"] == "file":
                data = (BGM_PREIMAGE.read_bytes()
                        if item["repository_path"] ==
                        "site/assets/bgm/private-domain-v1/manifest.json"
                        else git_bytes(item["preimage_blob"]))
                target.write_bytes(data)
                os.chmod(target, 0o640)
                self.original[item["runtime_path"]] = data
            else:
                self.original[item["runtime_path"]] = None
        database = self._target(self.manifest["feature_activation"]["database_path"])
        with closing(sqlite3.connect(str(database))) as connection:
            connection.execute(
                "CREATE TABLE feature_flags(feature TEXT PRIMARY KEY, enabled INTEGER NOT NULL, updated_by TEXT, updated_at INTEGER NOT NULL)"
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
        return self.runtime.joinpath(*pathlib.PurePosixPath(runtime_path).parts[1:])

    def _feature_row(self):
        database = self._target(self.manifest["feature_activation"]["database_path"])
        with closing(sqlite3.connect(str(database))) as connection:
            return connection.execute(
                "SELECT feature,enabled,updated_by,updated_at FROM feature_flags WHERE feature='director_agent'"
            ).fetchone()

    def _snapshot(self):
        result = {}
        for item in self.manifest["files"]:
            target = self._target(item["runtime_path"])
            result[item["runtime_path"]] = target.read_bytes() if target.exists() else None
        return result

    def _execute(self, hooks=None, checkpoint=None):
        return self.module._execute_manifest(
            self.manifest, ROOT, self.runtime, self.backups,
            hooks=hooks or FakeHooks(), verify_repository=False,
            checkpoint=checkpoint, reviewed_head="1" * 40,
            merged_main="2" * 40,
            confirm_target="test@8.148.158.106",
        )

    def test_manifest_locks_exact_sources_assets_and_six_file_scope(self):
        self.assertEqual(self.module.REQUIRED_REPOSITORY_PATHS, {
            item["repository_path"] for item in self.manifest["files"]
        })
        self.assertEqual(6, len(self.manifest["release_executor"]["external_assets"]))
        for item in self.manifest["files"]:
            data = (ROOT / item["repository_path"]).read_bytes()
            self.assertEqual(item["postimage_blob"], git_blob(data))
            self.assertEqual(item["postimage_sha256"], sha256(data))
        executor = EXECUTOR.read_bytes()
        self.assertEqual(self.manifest["release_executor"]["git_blob"], git_blob(executor))
        self.assertEqual(self.manifest["release_executor"]["sha256"], sha256(executor))

    def test_bgm_manifest_requires_stable_utf8_titles(self):
        catalog = json.loads(
            (ROOT / "site/assets/bgm/private-domain-v1/manifest.json").read_text(
                encoding="utf-8",
            )
        )
        self.assertEqual(6, len(self.module._validate_bgm_manifest(
            json.dumps(catalog, ensure_ascii=False).encode("utf-8"),
        )["tracks"]))
        del catalog["tracks"][0]["title"]
        with self.assertRaisesRegex(self.module.ReleaseError, "missing or corrupt"):
            self.module._validate_bgm_manifest(
                json.dumps(catalog, ensure_ascii=False).encode("utf-8"),
            )
        catalog["tracks"][0]["title"] = "坏\ufffd标题"
        with self.assertRaisesRegex(self.module.ReleaseError, "missing or corrupt"):
            self.module._validate_bgm_manifest(
                json.dumps(catalog, ensure_ascii=False).encode("utf-8"),
            )

    def test_success_installs_page_navigation_agent_and_runs_all_gates(self):
        hooks = FakeHooks()
        result = self._execute(hooks=hooks)
        self.assertEqual("deployed", result["status"])
        self.assertEqual(6, sum(call.startswith("external:") for call in hooks.calls))
        self.assertEqual(1, hooks.calls.count("acceptance"))
        self.assertEqual(1, hooks.calls.count("restart"))
        for item in self.manifest["files"]:
            self.assertEqual(
                (ROOT / item["repository_path"]).read_bytes(),
                self._target(item["runtime_path"]).read_bytes(),
            )

    def test_external_asset_failure_happens_before_backup_or_install(self):
        before = self._snapshot()
        with self.assertRaisesRegex(RuntimeError, "external"):
            self._execute(hooks=FakeHooks(fail_on="external:"))
        self.assertEqual(before, self._snapshot())
        self.assertFalse(self.backups.exists())

    def test_target_host_confirmation_and_machine_identity_fail_before_preflight(self):
        changed = copy.deepcopy(self.manifest)
        changed["target"]["host"] = "129.204.166.13"
        with self.assertRaisesRegex(self.module.ReleaseError, "test target"):
            self.module._validate_manifest(changed)
        hooks = FakeHooks()
        with self.assertRaisesRegex(self.module.ReleaseError, "confirm-target"):
            self.module._execute_manifest(
                self.manifest, ROOT, self.runtime, self.backups,
                hooks=hooks, verify_repository=False,
                confirm_target="test@129.204.166.13",
            )
        self.assertEqual([], hooks.calls)
        self.assertFalse(self.backups.exists())
        identity_path = self._target("/etc/huangque/release-identity.json")
        identity = json.loads(identity_path.read_text(encoding="utf-8"))
        identity["machine_id_sha256"] = "0" * 64
        identity_path.write_text(json.dumps(identity), encoding="utf-8")
        with self.assertRaisesRegex(self.module.ReleaseError, "does not match"):
            self._execute(hooks=hooks)
        self.assertEqual([], hooks.calls)
        self.assertFalse(self.backups.exists())

    def test_every_post_backup_stage_restores_files_absence_and_feature(self):
        stages = [
            "after_backup", "after_disable", "after_health_disabled_before_install",
            *("after_replace_%d" % index for index in range(6)),
            "after_compile", "after_restart", "after_health_disabled",
            "after_activate", "after_local_static", "after_acceptance", "after_final_audit",
        ]
        for stage in stages:
            with self.subTest(stage=stage):
                def inject(current, expected=stage):
                    if current == expected:
                        raise RuntimeError("injected " + expected)
                with self.assertRaisesRegex(RuntimeError, "injected"):
                    self._execute(checkpoint=inject)
                self.assertEqual(self.original, self._snapshot())
                self.assertEqual(self.original_feature, self._feature_row())

    def test_authenticated_acceptance_failure_rolls_back_complete_unit(self):
        with self.assertRaisesRegex(RuntimeError, "injected"):
            self._execute(hooks=FakeHooks(fail_on="acceptance"))
        self.assertEqual(self.original, self._snapshot())
        self.assertEqual(self.original_feature, self._feature_row())

    def test_current_head_is_manifest_only_child_of_locked_code_source(self):
        parents = subprocess.run(
            ["git", "rev-list", "--parents", "-n", "1", "HEAD"],
            cwd=ROOT, check=True, text=True, stdout=subprocess.PIPE,
        ).stdout.split()
        # pull_request CI checks out GitHub's synthetic merge commit. Its second
        # parent is the exact PR Head; a normal branch checkout uses HEAD itself.
        candidate = parents[2] if len(parents) == 3 else parents[0]
        parent = subprocess.run(
            ["git", "rev-parse", candidate + "^"], cwd=ROOT, check=True,
            text=True, stdout=subprocess.PIPE,
        ).stdout.strip()
        changed = set(filter(None, subprocess.run(
            ["git", "diff", "--name-only", parent, candidate], cwd=ROOT,
            check=True, text=True, stdout=subprocess.PIPE,
        ).stdout.splitlines()))
        self.assertEqual(parent, self.manifest["source"]["code_source_commit"])
        self.assertEqual({MANIFEST.relative_to(ROOT).as_posix()}, changed)


if __name__ == "__main__":
    unittest.main()
