import hashlib
import importlib.util
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "release_test", ROOT / "scripts" / "release_test.py"
)
release_test = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_test)


BASE = "1" * 40
TARGET = "2" * 40
REVIEWED = "3" * 40
RUNTIME_REPOSITORY_PATH = "server/content_domains/example.py"
RUNTIME_PATH = "/home/ubuntu/content-api/content_domains/example.py"
IMPACT_PATH = "deploy/test-release/impacts/pr-999.json"


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def state_hash(data):
    if data is None:
        return {"state": "absent", "sha256": None}
    return {"state": "file", "sha256": sha256(data)}


def catalog_data():
    return {
        "schema_version": 1,
        "target": {"environment": "test", "host_id": "unit-test-host"},
        "impact_prefix": "deploy/test-release/impacts/",
        "runtime_candidate_prefixes": ["server/", "site/", "deploy/systemd/"],
        "ignored_repository_paths": ["server/test_example.py"],
        "allowed_tools": [
            "/usr/bin/env", "/usr/bin/python3", "/usr/bin/systemctl"
        ],
        "allowed_units": ["huangque-content.service", "example.timer"],
        "min_free_bytes": 1,
        "rules": [
            {
                "kind": "prefix",
                "repository": "server/content_domains/",
                "runtime": "/home/ubuntu/content-api/content_domains/",
                "service": "huangque-content.service",
                "mode": "0644",
                "delete_allowed": False,
            },
            {
                "kind": "prefix",
                "repository": "site/",
                "runtime": "/var/www/huangque/",
                "service": None,
                "mode": "0644",
                "delete_allowed": True,
            },
            {
                "kind": "prefix",
                "repository": "deploy/systemd/",
                "runtime": "/etc/systemd/system/",
                "service": None,
                "service_from_repository": True,
                "daemon_reload": True,
                "mode": "0644",
                "delete_allowed": True,
            },
        ],
    }


def impact_data(runtime_changes=None, **overrides):
    value = {
        "schema_version": 1,
        "release_id": "pr-999-example",
        "runtime_changes": runtime_changes or [RUNTIME_REPOSITORY_PATH],
        "restart_services": ["huangque-content.service"],
        "required_env": ["EXAMPLE_API_KEY"],
        "health_checks": [{
            "url": "http://127.0.0.1:8080/health",
            "expected_statuses": [200],
            "timeout_seconds": 1,
            "interval_seconds": 0.1,
        }],
        "pre_health_checks": [{
            "url": "http://127.0.0.1:8080/health",
            "expected_statuses": [200],
            "timeout_seconds": 1,
            "interval_seconds": 0.1,
        }],
        "external_checks": [{
            "name": "syntax",
            "no_charge": True,
            "argv": [
                "/usr/bin/python3", "-m", "py_compile",
                "{source:server/content_domains/example.py}",
            ],
            "cwd": "{source}",
            "timeout_seconds": 30,
        }],
        "migrations": [],
    }
    value.update(overrides)
    return value


class FakeRepository:
    def __init__(self, base_files, target_files, changes):
        self.commits = {BASE: dict(base_files), TARGET: dict(target_files)}
        self.changes = list(changes)
        self.checkout_error = None
        self.checkout_calls = []
        self.modes = {}

    def require_commit(self, commit):
        if commit not in self.commits and commit != REVIEWED:
            raise release_test.ReleaseError("unknown commit")

    def require_ancestor(self, older, newer):
        self.require_commit(older)
        self.require_commit(newer)

    def changed_paths(self, older, newer):
        self.require_ancestor(older, newer)
        return list(self.changes)

    def file_at(self, commit, path):
        return self.commits.get(commit, {}).get(path)

    def file_mode_at(self, commit, path):
        if self.file_at(commit, path) is None:
            return None
        return self.modes.get((commit, path), "100644")

    def files_at(self, commit):
        return sorted(self.commits[commit])

    def verify_apply_checkout(self, target, reviewed, verify_live_origin=True):
        self.checkout_calls.append((target, reviewed, verify_live_origin))
        if self.checkout_error:
            raise release_test.ReleaseError(self.checkout_error)


class FakeRunner:
    def __init__(self):
        self.validated = []
        self.commands = []
        self.services = []
        self.daemon_reloads = 0
        self.validate_error = None
        self.run_error = None
        self.run_callback = None
        self.active = True

    def validate(self, command, catalog):
        self.validated.append(list(command["argv"]))
        if self.validate_error:
            raise release_test.ReleaseError(self.validate_error)

    def run(self, command, *, source_root, runtime_root, environment):
        self.commands.append(list(command["argv"]))
        if self.run_callback:
            self.run_callback(command, Path(runtime_root))
        if self.run_error:
            raise release_test.ReleaseError(self.run_error)

    def service(self, action, service, timeout=180):
        self.services.append((action, service))

    def daemon_reload(self, timeout=180):
        self.daemon_reloads += 1

    def is_active(self, service, timeout=30):
        return self.active


class IncrementingClock:
    def __init__(self):
        self.value = -1

    def __call__(self):
        self.value += 1
        return self.value


class ReleaseEngineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.runtime = self.root / "runtime"
        self.source.mkdir()
        self.runtime.mkdir()
        self.catalog = release_test.RuntimeCatalog(catalog_data())
        self.before = b"before\n"
        self.after = b"after\n"
        raw_impact = json.dumps(impact_data()).encode("utf-8")
        base_files = {RUNTIME_REPOSITORY_PATH: self.before}
        target_files = {
            RUNTIME_REPOSITORY_PATH: self.after,
            IMPACT_PATH: raw_impact,
        }
        changes = [("M", RUNTIME_REPOSITORY_PATH), ("A", IMPACT_PATH)]
        self.repo = FakeRepository(base_files, target_files, changes)
        self.runner = FakeRunner()
        self._write_runtime("/etc/hostname", b"test-host\n")
        self._write_runtime("/etc/machine-id", b"machine-id\n")
        identity = {
            "schema_version": 1,
            "environment": "test",
            "host_id": "unit-test-host",
            "hostname": "test-host",
            "machine_id_sha256": sha256(b"machine-id"),
        }
        self._write_json("/etc/huangque/release-identity.json", identity)
        self._write_runtime(RUNTIME_PATH, self.before)
        self._initialize_state()

    def tearDown(self):
        self.temporary.cleanup()

    def _mapped(self, runtime_path):
        return self.runtime.joinpath(*runtime_path.strip("/").split("/"))

    def _write_runtime(self, runtime_path, data):
        path = self._mapped(runtime_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def _write_json(self, runtime_path, value):
        self._write_runtime(
            runtime_path,
            (json.dumps(value, sort_keys=True, indent=2) + "\n").encode("utf-8"),
        )
        if os.name == "posix":
            self._mapped(runtime_path).chmod(0o600)

    def _initialize_state(self):
        self._write_json("/var/lib/huangque-release/state.json", {
            "schema_version": 1,
            "environment": "test",
            "host_id": "unit-test-host",
            "deployed_main_commit": BASE,
            "runtime_hashes": {RUNTIME_PATH: state_hash(self.before)},
            "repository_paths": {RUNTIME_PATH: RUNTIME_REPOSITORY_PATH},
            "last_release_id": None,
            "last_successful_release": None,
            "applied_migrations": [],
        })
        if os.name == "posix":
            self._mapped("/var/lib/huangque-release").chmod(0o700)

    def engine(self, **overrides):
        arguments = {
            "repo": self.repo,
            "runner": self.runner,
            "health_getter": lambda _url: 200,
            "environment": {"EXAMPLE_API_KEY": "present-but-never-logged"},
            "sleeper": lambda _seconds: None,
        }
        arguments.update(overrides)
        return release_test.ReleaseEngine(
            self.source, self.runtime, self.catalog, **arguments
        )

    def backup_directories(self):
        root = self._mapped("/var/lib/huangque-release/backups")
        return [] if not root.exists() else list(root.iterdir())

    def load_state(self):
        return json.loads(
            self._mapped("/var/lib/huangque-release/state.json").read_text("utf-8")
        )

    def test_plan_derives_git_preimage_and_does_not_create_backup(self):
        plan = self.engine().build_plan(TARGET)
        self.assertEqual("planned", plan["status"])
        self.assertEqual(state_hash(self.before), plan["files"][0]["before"])
        self.assertEqual(state_hash(self.after), plan["files"][0]["after"])
        self.assertEqual([], self.backup_directories())
        self.assertEqual(1, len(self.runner.commands))

    def test_wrong_host_is_rejected_before_backup_or_checkout(self):
        identity_path = self._mapped("/etc/huangque/release-identity.json")
        identity = json.loads(identity_path.read_text("utf-8"))
        identity["host_id"] = "production"
        identity_path.write_text(json.dumps(identity), encoding="utf-8")
        with self.assertRaisesRegex(release_test.ReleaseError, "identity is wrong"):
            self.engine().apply(TARGET, REVIEWED, "test", verify_live_origin=False)
        self.assertEqual([], self.repo.checkout_calls)
        self.assertEqual([], self.backup_directories())

    @unittest.skipIf(os.name != "posix", "POSIX permission semantics")
    def test_world_readable_identity_is_rejected_before_backup(self):
        self._mapped("/etc/huangque/release-identity.json").chmod(0o644)
        with self.assertRaisesRegex(release_test.ReleaseError, "private"):
            self.engine().apply(TARGET, REVIEWED, "test", verify_live_origin=False)
        self.assertEqual([], self.backup_directories())

    def test_checkout_gate_fails_before_backup(self):
        self.repo.checkout_error = "wrong checkout"
        with self.assertRaisesRegex(release_test.ReleaseError, "wrong checkout"):
            self.engine().apply(TARGET, REVIEWED, "test", verify_live_origin=False)
        self.assertEqual([], self.backup_directories())

    def test_runtime_drift_fails_before_backup(self):
        self._write_runtime(RUNTIME_PATH, b"manual drift")
        with self.assertRaisesRegex(release_test.ReleaseError, "drifted"):
            self.engine().apply(TARGET, REVIEWED, "test", verify_live_origin=False)
        self.assertEqual([], self.backup_directories())

    def test_missing_environment_name_is_reported_without_values(self):
        with self.assertRaisesRegex(release_test.ReleaseError, "EXAMPLE_API_KEY") as caught:
            self.engine(environment={}).build_plan(TARGET)
        self.assertNotIn("present-but-never-logged", str(caught.exception))
        self.assertEqual([], self.backup_directories())

    def test_missing_tool_preflight_fails_before_backup(self):
        self.runner.validate_error = "deployment tool is unavailable"
        with self.assertRaisesRegex(release_test.ReleaseError, "unavailable"):
            self.engine().apply(TARGET, REVIEWED, "test", verify_live_origin=False)
        self.assertEqual([], self.backup_directories())

    def test_external_check_failure_fails_before_backup(self):
        self.runner.run_error = "offline dependency missing"
        with self.assertRaisesRegex(release_test.ReleaseError, "offline dependency"):
            self.engine().apply(TARGET, REVIEWED, "test", verify_live_origin=False)
        self.assertEqual([], self.backup_directories())

    def test_apply_updates_file_and_ledger_then_is_idempotent(self):
        engine = self.engine()
        result = engine.apply(TARGET, REVIEWED, "test", verify_live_origin=False)
        self.assertEqual("deployed", result["status"])
        self.assertEqual(self.after, self._mapped(RUNTIME_PATH).read_bytes())
        self.assertEqual(TARGET, self.load_state()["deployed_main_commit"])
        backups = list(self.backup_directories())
        services = list(self.runner.services)
        second = engine.apply(TARGET, REVIEWED, "test", verify_live_origin=False)
        self.assertEqual("already_deployed", second["status"])
        self.assertEqual(backups, self.backup_directories())
        self.assertEqual(services, self.runner.services)
        self.assertEqual(0, second["restart_count"])

    def test_health_failure_restores_file_and_ledger(self):
        statuses = iter([200, 503, 200])
        engine = self.engine(
            health_getter=lambda _url: next(statuses),
            clock=IncrementingClock(),
        )
        with self.assertRaisesRegex(release_test.ReleaseError, "rolled back"):
            engine.apply(TARGET, REVIEWED, "test", verify_live_origin=False)
        self.assertEqual(self.before, self._mapped(RUNTIME_PATH).read_bytes())
        self.assertEqual(BASE, self.load_state()["deployed_main_commit"])
        self.assertFalse(self._mapped("/var/lib/huangque-release/release.lock").exists())

    def test_explicit_rollback_restores_current_release(self):
        engine = self.engine()
        deployed = engine.apply(TARGET, REVIEWED, "test", verify_live_origin=False)
        rolled_back = engine.rollback(deployed["release_id"], "test")
        self.assertEqual("rolled_back", rolled_back["status"])
        self.assertEqual(self.before, self._mapped(RUNTIME_PATH).read_bytes())
        self.assertEqual(BASE, self.load_state()["deployed_main_commit"])

    def test_existing_lock_rejects_concurrent_apply_without_backup(self):
        lock = self._mapped("/var/lib/huangque-release/release.lock")
        lock.write_text("busy", encoding="utf-8")
        with self.assertRaisesRegex(release_test.ReleaseError, "already running"):
            self.engine().apply(TARGET, REVIEWED, "test", verify_live_origin=False)
        self.assertEqual([], self.backup_directories())
        self.assertEqual("busy", lock.read_text("utf-8"))

    def test_status_reports_tracked_runtime_drift(self):
        self._write_runtime(RUNTIME_PATH, b"drift")
        result = self.engine().status()
        self.assertEqual("drifted", result["status"])
        self.assertEqual([RUNTIME_PATH], result["drifted_paths"])

    def test_multi_impact_release_requires_distinct_reviewed_heads(self):
        engine = self.engine()
        with self.assertRaisesRegex(release_test.ReleaseError, "every release impact"):
            engine._normalize_reviewed_heads(
                REVIEWED, ["pr-1-example", "pr-2-example"], TARGET
            )
        with self.assertRaisesRegex(release_test.ReleaseError, "distinct"):
            engine._normalize_reviewed_heads({
                "pr-1-example": REVIEWED,
                "pr-2-example": REVIEWED,
            }, ["pr-1-example", "pr-2-example"], TARGET)

    def test_inactive_service_fails_before_backup(self):
        self.runner.active = False
        with self.assertRaisesRegex(release_test.ReleaseError, "not active"):
            self.engine().apply(TARGET, REVIEWED, "test", verify_live_origin=False)
        self.assertEqual([], self.backup_directories())

    def test_initialize_requires_full_commit_match_and_writes_only_ledger(self):
        self._mapped("/var/lib/huangque-release/state.json").unlink()
        self.repo.commits[BASE]["server/not-yet-catalogued.py"] = b"not-managed"
        initialized = self.engine().initialize(BASE, "test", verify_live_origin=False)
        self.assertEqual("initialized", initialized["status"])
        self.assertEqual(1, initialized["tracked_runtime_paths"])
        self.assertEqual(BASE, self.load_state()["deployed_main_commit"])
        self.assertEqual([], self.runner.services)

    @unittest.skipIf(os.name != "posix", "POSIX symlink semantics")
    def test_rollback_rejects_replaced_backup_symlink(self):
        engine = self.engine()
        deployed = engine.apply(TARGET, REVIEWED, "test", verify_live_origin=False)
        backup = Path(deployed["backup"])
        backup_file = backup / "file-0000.bin"
        external = self.root / "external"
        external.write_bytes(b"attacker")
        backup_file.unlink()
        backup_file.symlink_to(external)
        with self.assertRaisesRegex(release_test.RollbackError, "incomplete"):
            engine.rollback(deployed["release_id"], "test")
        self.assertEqual(self.after, self._mapped(RUNTIME_PATH).read_bytes())

    def test_initialize_rejects_mixed_runtime(self):
        self._mapped("/var/lib/huangque-release/state.json").unlink()
        self._write_runtime(RUNTIME_PATH, b"mixed")
        with self.assertRaisesRegex(release_test.ReleaseError, "does not match"):
            self.engine().initialize(BASE, "test", verify_live_origin=False)
        self.assertFalse(self._mapped("/var/lib/huangque-release/state.json").exists())

    def test_sqlite_migration_failure_restores_exact_snapshot(self):
        database_path = "/home/ubuntu/content-api/example.db"
        database = self._mapped(database_path)
        database.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("CREATE TABLE example(value TEXT)")
            connection.execute("INSERT INTO example VALUES ('before')")
        migration = {
            "id": "migration-example",
            "database_path": database_path,
            "rollback": "restore_sqlite_snapshot",
            "up": {"argv": ["/usr/bin/python3", "-c", "up"], "cwd": "{source}"},
            "verify": {"argv": ["/usr/bin/python3", "-c", "verify"], "cwd": "{source}"},
        }
        changed_impact = impact_data(migrations=[migration])
        self.repo.commits[TARGET][IMPACT_PATH] = json.dumps(changed_impact).encode("utf-8")

        def mutate(command, _runtime_root):
            if command["argv"][-1] == "up":
                with closing(sqlite3.connect(database)) as connection, connection:
                    connection.execute("UPDATE example SET value='after'")

        self.runner.run_callback = mutate
        statuses = iter([200, 503, 200])
        engine = self.engine(
            health_getter=lambda _url: next(statuses),
            clock=IncrementingClock(),
        )
        with self.assertRaisesRegex(release_test.ReleaseError, "rolled back"):
            engine.apply(TARGET, REVIEWED, "test", verify_live_origin=False)
        backup = self.backup_directories()[0]
        self.assertEqual((backup / "database-0000.sqlite").read_bytes(), database.read_bytes())
        with closing(sqlite3.connect(database)) as connection:
            self.assertEqual("ok", connection.execute("PRAGMA integrity_check").fetchone()[0])
            self.assertEqual(
                "before", connection.execute("SELECT value FROM example").fetchone()[0]
            )
        self.assertIn(("stop", "huangque-content.service"), self.runner.services)


class ContractValidationTests(unittest.TestCase):
    def setUp(self):
        self.catalog = release_test.RuntimeCatalog(catalog_data())

    def repository(self, impacts):
        base = {RUNTIME_REPOSITORY_PATH: b"before"}
        target = {RUNTIME_REPOSITORY_PATH: b"after"}
        changes = [("M", RUNTIME_REPOSITORY_PATH)]
        for index, impact in enumerate(impacts):
            path = "deploy/test-release/impacts/%02d.json" % index
            target[path] = json.dumps(impact).encode("utf-8")
            changes.append(("A", path))
        return FakeRepository(base, target, changes)

    def test_runtime_change_without_impact_is_blocked(self):
        with self.assertRaisesRegex(release_test.ReleaseError, "lack release impact"):
            release_test.collect_release_impact(
                self.repository([]), self.catalog, BASE, TARGET
            )

    def test_unmapped_runtime_candidate_is_blocked(self):
        repo = FakeRepository(
            {"server/unknown.py": b"a"},
            {"server/unknown.py": b"b"},
            [("M", "server/unknown.py")],
        )
        with self.assertRaisesRegex(release_test.ReleaseError, "no catalog mapping"):
            release_test.collect_release_impact(repo, self.catalog, BASE, TARGET)

    def test_git_symlink_runtime_source_is_blocked_in_ci_contract(self):
        repo = self.repository([impact_data()])
        repo.modes[(TARGET, RUNTIME_REPOSITORY_PATH)] = "120000"
        with self.assertRaisesRegex(release_test.ReleaseError, "regular Git blob"):
            release_test.collect_release_impact(repo, self.catalog, BASE, TARGET)

    def test_ignored_non_runtime_file_needs_no_impact(self):
        repo = FakeRepository(
            {"server/test_example.py": b"a"},
            {"server/test_example.py": b"b"},
            [("M", "server/test_example.py")],
        )
        result = release_test.collect_release_impact(repo, self.catalog, BASE, TARGET)
        self.assertEqual([], result["runtime_changes"])

    def test_public_health_check_is_rejected(self):
        impact = impact_data(health_checks=[{
            "url": "https://yuelei.huangquechuanmei.com/health",
            "expected_statuses": [200],
        }])
        with self.assertRaisesRegex(release_test.ReleaseError, "loopback"):
            release_test.validate_impact(impact, self.catalog)

    def test_chargeable_external_check_is_rejected(self):
        impact = impact_data(external_checks=[{
            "name": "paid", "no_charge": False,
            "argv": ["/usr/bin/python3", "paid.py"],
        }])
        with self.assertRaisesRegex(release_test.ReleaseError, "no-charge"):
            release_test.validate_impact(impact, self.catalog)

    def test_env_cannot_hide_an_undeclared_tool(self):
        impact = impact_data(external_checks=[{
            "name": "hidden-shell", "no_charge": True,
            "argv": ["/usr/bin/env", "SAFE=1", "/bin/sh", "-c", "true"],
        }])
        with self.assertRaisesRegex(release_test.ReleaseError, "behind env"):
            release_test.validate_impact(impact, self.catalog)

    def test_duplicate_migration_ids_across_impacts_are_blocked(self):
        migration = {
            "id": "same-migration",
            "database_path": "/tmp/example.db",
            "rollback": "restore_sqlite_snapshot",
            "up": {"argv": ["/usr/bin/python3", "-c", "up"]},
            "verify": {"argv": ["/usr/bin/python3", "-c", "verify"]},
        }
        first = impact_data(migrations=[migration])
        second = impact_data(release_id="pr-1000-example", migrations=[migration])
        repo = self.repository([first, second])
        engine = release_test.ReleaseEngine.__new__(release_test.ReleaseEngine)
        engine.repo = repo
        engine.catalog = self.catalog
        with self.assertRaisesRegex(release_test.ReleaseError, "migration ids"):
            engine._aggregate_impact(BASE, TARGET)

    def test_impact_cannot_invoke_systemctl_directly(self):
        impact = impact_data(external_checks=[{
            "name": "restart", "no_charge": True,
            "argv": ["/usr/bin/systemctl", "restart", "anything.service"],
        }])
        with self.assertRaisesRegex(release_test.ReleaseError, "cannot invoke systemctl"):
            release_test.validate_impact(impact, self.catalog)

    def test_same_runtime_path_can_be_declared_by_sequential_impacts(self):
        second = impact_data(release_id="pr-1000-example")
        result = release_test.collect_release_impact(
            self.repository([impact_data(), second]), self.catalog, BASE, TARGET
        )
        self.assertEqual(2, len(result["impacts"]))

    def test_existing_release_impact_is_immutable(self):
        repo = FakeRepository(
            {}, {IMPACT_PATH: json.dumps(impact_data()).encode("utf-8")},
            [("M", IMPACT_PATH)],
        )
        with self.assertRaisesRegex(release_test.ReleaseError, "immutable"):
            release_test.collect_release_impact(repo, self.catalog, BASE, TARGET)

    def test_systemd_dropin_derives_service_and_daemon_reload(self):
        mapping = self.catalog.map(
            "deploy/systemd/huangque-content.service.d/example.conf"
        )
        self.assertEqual("huangque-content.service", mapping["service"])
        self.assertTrue(mapping["daemon_reload"])

    def test_service_change_requires_pre_release_health(self):
        impact = impact_data(pre_health_checks=[])
        with self.assertRaisesRegex(release_test.ReleaseError, "pre-release"):
            release_test.validate_impact(impact, self.catalog)

    @unittest.skipIf(os.name != "posix", "POSIX symlink semantics")
    def test_symlink_tool_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tool = root / "usr/bin/python3"
            tool.parent.mkdir(parents=True)
            real = root / "real-python"
            real.write_text("#!/bin/sh\n", encoding="utf-8")
            real.chmod(0o755)
            tool.symlink_to(real)
            runner = release_test.CommandRunner(tool_root=root)
            with self.assertRaisesRegex(release_test.ReleaseError, "unsafe"):
                runner.validate({
                    "argv": ["/usr/bin/python3"], "cwd": "{source}",
                    "timeout_seconds": 1,
                }, self.catalog)

    @unittest.skipIf(os.name != "posix", "POSIX symlink semantics")
    def test_in_directory_tool_symlink_is_allowed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tools = root / "usr/bin"
            tools.mkdir(parents=True)
            real = tools / "python3.12"
            real.write_text("#!/bin/sh\n", encoding="utf-8")
            real.chmod(0o755)
            (tools / "python3").symlink_to("python3.12")
            runner = release_test.CommandRunner(tool_root=root)
            runner.validate({
                "argv": ["/usr/bin/python3"], "cwd": "{source}",
                "timeout_seconds": 1,
            }, self.catalog)


class RepositoryContractTests(unittest.TestCase):
    def test_catalog_and_schema_are_json_objects(self):
        for relative in (
            "deploy/test-release/runtime-catalog.json",
            "deploy/test-release/release-impact.schema.json",
            "deploy/test-release/release-impact.example.json",
        ):
            value = json.loads((ROOT / relative).read_text("utf-8"))
            self.assertIsInstance(value, dict)

    def test_checked_in_example_passes_runtime_contract_validation(self):
        catalog = release_test.RuntimeCatalog.load(
            ROOT, "deploy/test-release/runtime-catalog.json"
        )
        example = json.loads(
            (ROOT / "deploy/test-release/release-impact.example.json").read_text("utf-8")
        )
        validated = release_test.validate_impact(example, catalog)
        self.assertEqual("pr-000-example", validated["release_id"])

    def test_ci_runs_pull_request_impact_gate(self):
        workflow = (ROOT / ".github/workflows/ci.yml").read_text("utf-8")
        self.assertIn("scripts/release_test.py check-impact", workflow)
        self.assertIn("github.event.pull_request.base.sha", workflow)
        self.assertIn("github.event.pull_request.head.sha", workflow)

    def test_reviewed_head_cli_requires_release_id_mapping(self):
        parsed = release_test._parse_reviewed_head_arguments([
            "pr-123-example=" + REVIEWED,
        ])
        self.assertEqual({"pr-123-example": REVIEWED}, parsed)
        with self.assertRaises(release_test.ReleaseError):
            release_test._parse_reviewed_head_arguments([REVIEWED])


if __name__ == "__main__":
    unittest.main()
