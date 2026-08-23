import copy
import hashlib
import importlib.util
import json
import os
import pathlib
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
EXECUTOR = ROOT / "scripts/deploy_unified_voice_v6_locked_manifest.py"
MANIFEST_NAME = "digital-human-unified-voice-v6-20260823.json"
HISTORICAL_LOCKS = {
    "scripts/deploy_precision_director_v5_locked_manifest.py": (
        "af6c4e6295ddff641091a4a2223abc8118b0857e",
        "aa98b9b6288cf93b1b452ecf62b9ba51cac5eac4fe7c7c059c4313bc736035c1",
    ),
    "deploy/test-runtime/digital-human-precision-director-v5-20260822.json": (
        "01c355c2b3c9254207ba8e1dbb9b49322d355735",
        "c4394b919a092d0b06fb9da4950766cf8d4b768407f5a94068662fc7489befaf",
    ),
}
LOCKS = {
    "server/content_domains/audio.py": (
        "32f948f451d0f527d992425ae1eaa8bc28583c6f", "1482e90c5cba03778a5c00b53eea21a9018fb4683c6b31eb329c54d12b012651",
        "db47e4afa6b61e0254024ebe7b60002a52eea440", "f167dae67940bb07693648b3e05282f04236acee8befdf1064774921a54a4e80",
    ),
    "server/content_domains/core.py": (
        "332d5fab975ee3e070b27aa27a58bd8135435af5", "ce2977e30a02d5dec1a38e2e39d98e75731c6fd8f5744854e2f5933c4e2e0398",
        "9b0d195008553d60c664983068cc9b5665266e25", "efc5bb837b9d7e9664a5755c5dca8f0891cd29e3f1fd68e814d1d655e2c035f2",
    ),
    "server/content_domains/digital_human_oneclick.py": (
        "44bded5ddcfdc049fdb1dcb4c39ae9e9dbe35635", "ad21190220a72cd8ff86e311d5068bf7156a8332b423c9fe60e2347a09a154c4",
        "a0c211239dfcef188707d6202daf4884f97476ec", "98a21d40355e142ec0a8a39d261d4821995c2331f114894b0a9c550c6e9e6f45",
    ),
    "server/content_domains/video.py": (
        "14eb06a8a39b31809e63386aefb767da884a2fd3", "d7416e7e9471793fda0452c405631010cdb13505eae632c80e92de1ffaafaf4a",
        "388a9b32ad418bbb9458bfcfd27df9a60a4a61d8", "c468dc4b0df57bb4d747b0186306258d2aa2eca91a04074c07d228bad6e50cb8",
    ),
    "site/workbench/digital-human-one-click.html": (
        "e16c6bfd7fe9beed35880174b99a2840de3b6a15", "2d3c9a6c57fcfc5be637d68dcb6c72a984829fcd3468c5b361f09fab3776e204",
        "a1036c606258f95150fa12866652ff942c06e3af", "98d12653c32d48f63c699c56496c54a90a2857867fef1425cd1284e8a8edb041",
    ),
    "site/workbench/digital-human-oneclick.html": (
        "34fb66a8f03c137149371cecead51e66b5caee66", "d26e956ff3168acc9bfcdde357366212c54033e8433ab4dbc80063fb69736784",
        "68d00a3abf51bcf00d477a30936e03f00bcd2ff3", "8ff3497d40282b81a67cdb69e51ac53bd0761e90fa567a5c8df08a6f88223c1d",
    ),
    "site/workbench/script.html": (
        "ea4bad20da05b624f2c77b7f6283734997b19553", "22e18e4c6f230030feecee990c18974682477c39bbf49e5f00ab48834f17ca4e",
        "26aaf0a4a8bc048534462cdc1cbe5af400c3fc23", "8ce245e020501e16bb2ec91e1ead6ec602c1a9792a7a84f5da662191f46fc2b3",
    ),
    "site/workbench/digital-human-unified-state.js": (
        None, None, "1fe597e7c684759ba1fd88c37239d48c81e693fc", "6d1c7c65ca7635e9c3515b1e1a6d3d6d4f56d561d962de1684aefa498cd0916d",
    ),
    "site/workbench/digital-human-unified.js": (
        None, None, "85e4d71b3efce19d19ed2bba9ef23507f850f06b", "a95f7d805c3c23d6f041c9045c9a65e0294d3d87481d30294cab286bfc13ec92",
    ),
}


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
    specification = importlib.util.spec_from_file_location("unified_v6_release", EXECUTOR)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


class Hooks:
    def __init__(self, executor, fail_static=False, fail_migration=False):
        self.executor = executor
        self.fail_static = fail_static
        self.fail_migration = fail_migration
        self.restarts = 0
        self.calls = []

    def validate_import(self, root, modules):
        self.calls.append(("imports", tuple(modules)))

    def validate_node(self, path):
        self.calls.append(("node", pathlib.Path(path).name))

    def service_active(self, service):
        return True

    def restart(self, service):
        self.restarts += 1
        self.calls.append(("restart", service))

    def probe(self, url, method, expected_status):
        self.calls.append(("probe", url, method, expected_status))

    def probe_static(self, url, expected_status, expected_sha256):
        self.calls.append(("static", url, expected_status, expected_sha256))
        if self.fail_static:
            raise RuntimeError("served static hash mismatch")

    def migrate_consent(self, python_root, database_path, specification):
        self.calls.append(("migration", specification["migration_callable"]))
        if self.fail_migration:
            raise RuntimeError("migration failed")
        columns = sorted(set(specification["required_columns"]) - {"id"})
        with closing(sqlite3.connect(str(database_path))) as connection:
            connection.execute(
                "CREATE TABLE digital_human_video_consents(id TEXT PRIMARY KEY,%s)"
                % ",".join('"%s" TEXT' % column for column in columns)
            )
            connection.commit()


class UnifiedVoiceV6ReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.executor = load_executor()
        cls.executor_blob = git_blob(EXECUTOR.read_bytes())
        cls.executor_sha = sha256(EXECUTOR.read_bytes())

    def _manifest(self):
        files = []
        for repository_path, locks in LOCKS.items():
            pre_blob, pre_sha, post_blob, post_sha = locks
            runtime_root = (
                "/home/ubuntu/content-api/" if repository_path.startswith("server/")
                else "/var/www/huangquechuanmei/"
            )
            runtime_tail = repository_path.split("/", 1)[1]
            files.append({
                "repository_path": repository_path,
                "runtime_path": runtime_root + runtime_tail,
                "target_preimage_state": "file" if pre_blob else "absent",
                "preimage_blob": pre_blob, "preimage_sha256": pre_sha,
                "postimage_blob": post_blob, "postimage_sha256": post_sha,
            })
        required_columns = sorted(self.executor.REQUIRED_CONSENT_COLUMNS)
        return {
            "schema_version": 1,
            "target": {"role": "test", "service": "huangque-content.service"},
            "source": {"code_source_commit": "a" * 40},
            "deployment_policy": {
                "production_server_write_allowed": False,
                "require_merged_main": True,
                "fail_closed_on_preimage_mismatch": True,
                "backup_all_targets_before_first_write": True,
                "backup_live_database_before_first_write": True,
                "restore_database_on_failure": True,
                "install_all_files_before_restart": True,
                "restart_backend_once": True,
                "rollback_all_files_as_one_unit": True,
                "copy_environment_or_database": False,
            },
            "release_executor": {
                "contract": self.executor.CONTRACT,
                "repository_path": EXECUTOR.relative_to(ROOT).as_posix(),
                "git_blob": self.executor_blob, "sha256": self.executor_sha,
                "locked_base_executor": {
                    "repository_path": self.executor.BASE_EXECUTOR_PATH,
                    "git_blob": self.executor.BASE_EXECUTOR_BLOB,
                    "sha256": self.executor.BASE_EXECUTOR_SHA256,
                },
                "runtime_python_root": "/home/ubuntu/content-api",
                "import_modules": [
                    "content_domains.audio", "content_domains.core",
                    "content_domains.digital_human_oneclick", "content_domains.video",
                ],
                "required_repository_paths": sorted(LOCKS),
                "health_url": "https://yuelei.huangquechuanmei.com/api/gen/health",
                "forward_health_policy": {"timeout_seconds": 30, "interval_seconds": 1},
                "rollback_health_policy": {"timeout_seconds": 30, "interval_seconds": 1},
                "static_probes": [
                    {"url": "https://yuelei.huangquechuanmei.com/workbench/digital-human-one-click.html", "expected_status": 200, "expected_sha256": LOCKS["site/workbench/digital-human-one-click.html"][3]},
                    {"url": "https://yuelei.huangquechuanmei.com/workbench/digital-human-unified.js", "expected_status": 200, "expected_sha256": LOCKS["site/workbench/digital-human-unified.js"][3]},
                    {"url": "https://yuelei.huangquechuanmei.com/workbench/digital-human-unified-state.js", "expected_status": 200, "expected_sha256": LOCKS["site/workbench/digital-human-unified-state.js"][3]},
                ],
                "unauthenticated_probes": [{
                    "url": "https://yuelei.huangquechuanmei.com/api/gen/video/lipsync-voice-sample",
                    "method": "POST", "expected_status": 401,
                }],
            },
            "database_backup": {
                "runtime_path": self.executor.DATABASE_PATH,
                "preimage_state": "sqlite_file",
                "backup_method": "sqlite_online_backup",
                "migration_module": "content_domains.digital_human_oneclick",
                "migration_callable": "_ensure_unified_video_consent_table",
                "acceptance_table": "digital_human_video_consents",
                "required_columns": required_columns,
            },
            "files": files,
        }

    def _write_manifest(self, root, manifest):
        path = pathlib.Path(root) / "deploy/test-runtime" / MANIFEST_NAME
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return path

    def _target(self, root, manifest):
        root = pathlib.Path(root)
        for item in manifest["files"]:
            target = root.joinpath(*pathlib.PurePosixPath(item["runtime_path"]).parts[1:])
            target.parent.mkdir(parents=True, exist_ok=True)
            if item["target_preimage_state"] == "file":
                target.write_bytes(git_bytes(item["preimage_blob"]))
        database = root.joinpath(*pathlib.PurePosixPath(self.executor.DATABASE_PATH).parts[1:])
        database.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(str(database))) as connection:
            connection.execute("CREATE TABLE existing_authorizations(id TEXT PRIMARY KEY,value TEXT)")
            connection.execute("INSERT INTO existing_authorizations VALUES('keep','original')")
            connection.commit()
        return root, database

    def _execute(self, manifest_path, target, backup, hooks):
        return self.executor.execute_locked_release(
            manifest_path, ROOT, target, backup, hooks=hooks,
            verify_repository=False, reviewed_head="r" * 40,
            merged_main="m" * 40,
        )

    def _assert_preimages(self, target, manifest):
        for item in manifest["files"]:
            path = pathlib.Path(target).joinpath(*pathlib.PurePosixPath(item["runtime_path"]).parts[1:])
            if item["target_preimage_state"] == "file":
                self.assertEqual(item["preimage_sha256"], sha256(path.read_bytes()))
            else:
                self.assertFalse(path.exists())

    def test_historical_v5_executor_and_manifest_are_exact_git_blobs(self):
        for relative, (blob_id, digest) in HISTORICAL_LOCKS.items():
            data = (ROOT / relative).read_bytes()
            self.assertEqual(blob_id, git_blob(data), relative)
            self.assertEqual(digest, sha256(data), relative)
            self.assertEqual(data, git_bytes(blob_id), relative)

    def test_v6_manifest_or_pre_manifest_fixture_locks_exact_source_and_history(self):
        checked_in = ROOT / "deploy/test-runtime" / MANIFEST_NAME
        manifest = (
            json.loads(checked_in.read_text(encoding="utf-8"))
            if checked_in.is_file() else self._manifest()
        )
        with tempfile.TemporaryDirectory() as root:
            path = self._write_manifest(root, manifest)
            loaded = self.executor._load_manifest(path)
        release = loaded["release_executor"]
        self.assertEqual(self.executor_blob, release["git_blob"])
        self.assertEqual(self.executor_sha, release["sha256"])
        self.assertEqual(set(LOCKS), set(release["required_repository_paths"]))
        for item in loaded["files"]:
            data = (ROOT / item["repository_path"]).read_bytes()
            self.assertEqual(item["postimage_blob"], git_blob(data))
            self.assertEqual(item["postimage_sha256"], sha256(data))
            if item["target_preimage_state"] == "file":
                historical = git_bytes(item["preimage_blob"])
                self.assertEqual(item["preimage_sha256"], sha256(historical))
            else:
                self.assertIsNone(item["preimage_blob"])
                self.assertIsNone(item["preimage_sha256"])

    def test_success_accepts_exact_entry_migrates_backs_up_and_runs_all_gates(self):
        manifest = self._manifest()
        with tempfile.TemporaryDirectory() as manifests, tempfile.TemporaryDirectory() as target_dir, tempfile.TemporaryDirectory() as backup_dir:
            path = self._write_manifest(manifests, manifest)
            target, database = self._target(target_dir, manifest)
            hooks = Hooks(self.executor)
            result = self._execute(path, target, backup_dir, hooks)
            self.assertEqual("deployed", result["status"])
            self.assertEqual(1, hooks.restarts)
            self.assertIn(("migration", "_ensure_unified_video_consent_table"), hooks.calls)
            self.assertEqual(3, len([call for call in hooks.calls if call[0] == "static"]))
            self.assertIn(("probe", manifest["release_executor"]["unauthenticated_probes"][0]["url"], "POST", 401), hooks.calls)
            with closing(sqlite3.connect(str(database))) as connection:
                self.assertEqual("original", connection.execute("SELECT value FROM existing_authorizations WHERE id='keep'").fetchone()[0])
            audit = json.loads((pathlib.Path(result["backup"]) / "audit.json").read_text(encoding="utf-8"))
            self.assertEqual("deployed", audit["status"])
            self.assertTrue((pathlib.Path(result["backup"]) / "digital_human_oneclick.db").is_file())

    def test_static_failure_restores_every_file_and_database_preimage(self):
        manifest = self._manifest()
        with tempfile.TemporaryDirectory() as manifests, tempfile.TemporaryDirectory() as target_dir, tempfile.TemporaryDirectory() as backup_dir:
            path = self._write_manifest(manifests, manifest)
            target, database = self._target(target_dir, manifest)
            before = database.read_bytes()
            hooks = Hooks(self.executor, fail_static=True)
            with self.assertRaisesRegex(RuntimeError, "static hash mismatch"):
                self._execute(path, target, backup_dir, hooks)
            self._assert_preimages(target, manifest)
            with closing(sqlite3.connect(str(database))) as connection:
                self.assertIsNone(connection.execute("SELECT 1 FROM sqlite_master WHERE name='digital_human_video_consents'").fetchone())
                self.assertEqual("original", connection.execute("SELECT value FROM existing_authorizations WHERE id='keep'").fetchone()[0])
            self.assertEqual(2, hooks.restarts)
            audit = json.loads(next(pathlib.Path(backup_dir).glob("*/audit.json")).read_text(encoding="utf-8"))
            self.assertEqual("rolled_back", audit["status"])
            self.assertEqual([], audit["rollback_errors"])
            self.assertNotEqual(before, b"")

    def test_migration_failure_restores_all_preimages(self):
        manifest = self._manifest()
        with tempfile.TemporaryDirectory() as manifests, tempfile.TemporaryDirectory() as target_dir, tempfile.TemporaryDirectory() as backup_dir:
            path = self._write_manifest(manifests, manifest)
            target, _ = self._target(target_dir, manifest)
            with self.assertRaisesRegex(RuntimeError, "migration failed"):
                self._execute(path, target, backup_dir, Hooks(self.executor, fail_migration=True))
            self._assert_preimages(target, manifest)

    def test_manifest_policy_inventory_database_and_real_path_fail_closed(self):
        cases = []
        missing = self._manifest(); missing["release_executor"].pop("forward_health_policy"); cases.append((missing, "policy is missing"))
        interval = self._manifest(); interval["release_executor"]["forward_health_policy"]["interval_seconds"] = 0; cases.append((interval, "interval is invalid"))
        timeout = self._manifest(); timeout["release_executor"]["forward_health_policy"]["timeout_seconds"] = 121; cases.append((timeout, "timeout is invalid"))
        database = self._manifest(); database["database_backup"]["backup_method"] = "copy"; cases.append((database, "database contract is incomplete"))
        probes = self._manifest(); probes["release_executor"]["unauthenticated_probes"] = []; cases.append((probes, "401 acceptance is missing"))
        for manifest, message in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as root:
                path = self._write_manifest(root, manifest)
                with self.assertRaisesRegex(self.executor.ReleaseError, message):
                    self.executor._load_manifest(path)
        with tempfile.TemporaryDirectory() as root:
            wrong = pathlib.Path(root) / "wrong.json"
            wrong.write_text(json.dumps(self._manifest()), encoding="utf-8")
            with self.assertRaisesRegex(self.executor.ReleaseError, "locked source path"):
                self.executor._load_manifest(wrong)

    def test_preimage_drift_stops_before_backup_or_migration(self):
        manifest = self._manifest()
        with tempfile.TemporaryDirectory() as manifests, tempfile.TemporaryDirectory() as target_dir, tempfile.TemporaryDirectory() as backup_dir:
            path = self._write_manifest(manifests, manifest)
            target, _ = self._target(target_dir, manifest)
            first = pathlib.Path(target).joinpath(*pathlib.PurePosixPath(manifest["files"][0]["runtime_path"]).parts[1:])
            first.write_bytes(b"drift")
            hooks = Hooks(self.executor)
            with self.assertRaisesRegex(self.executor.ReleaseError, "preimage lock mismatch"):
                self._execute(path, target, backup_dir, hooks)
            self.assertEqual([], list(pathlib.Path(backup_dir).iterdir()))
            self.assertNotIn(("migration", "_ensure_unified_video_consent_table"), hooks.calls)

    def test_source_binding_requires_manifest_only_second_commit(self):
        manifest = self._manifest()
        reviewed, merged = "b" * 40, "c" * 40
        base = mock.Mock()
        base.ReleaseError = RuntimeError
        base._verify_repository.return_value = merged
        base._run.side_effect = ["", "a" * 40, "deploy/test-runtime/" + MANIFEST_NAME]
        self.assertEqual(merged, self.executor._verify_checkout(base, ROOT, manifest, reviewed, merged))
        base._run.side_effect = ["", "a" * 40, "scripts/unexpected.py"]
        with self.assertRaisesRegex(self.executor.ReleaseError, "only finalize"):
            self.executor._verify_checkout(base, ROOT, manifest, reviewed, merged)

    def test_system_hook_executes_the_declared_migration_callable(self):
        base = self.executor._load_base_executor(ROOT)
        hooks = self.executor.SystemHooks(base)
        with tempfile.TemporaryDirectory() as python_root, tempfile.TemporaryDirectory() as data_root:
            package = pathlib.Path(python_root) / "content_domains"
            package.mkdir()
            (package / "__init__.py").write_text("", encoding="utf-8")
            (package / "migration_fixture.py").write_text(
                "def migrate(connection):\n"
                "    connection.execute('CREATE TABLE migration_marker(value TEXT)')\n"
                "    connection.execute(\"INSERT INTO migration_marker VALUES('called')\")\n",
                encoding="utf-8",
            )
            database = pathlib.Path(data_root) / "consent.db"
            hooks.migrate_consent(
                python_root, database,
                {
                    "migration_module": "content_domains.migration_fixture",
                    "migration_callable": "migrate",
                },
            )
            with closing(sqlite3.connect(str(database))) as connection:
                self.assertEqual(
                    "called",
                    connection.execute("SELECT value FROM migration_marker").fetchone()[0],
                )


if __name__ == "__main__":
    unittest.main()
