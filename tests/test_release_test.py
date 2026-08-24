import ast
import hashlib
import importlib.util
import contextlib
import io
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "release_test", ROOT / "scripts" / "release_test.py"
)
release_test = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_test)


BASE = "1" * 40
HEAD = "2" * 40
MERGE = "3" * 40
UNRELATED = "4" * 40
RUNTIME_REPOSITORY_PATH = "server/content_domains/example.py"
RUNTIME_PATH = "/home/ubuntu/content-api/content_domains/example.py"
IMPACT_PATH = "deploy/test-release/impacts/pr-999.json"
CATALOG_PATH = "deploy/test-release/runtime-catalog.json"


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def state_hash(data):
    return {"state": "file", "sha256": sha256(data)}


def catalog_data():
    return {
        "schema_version": 1,
        "target": {
            "environment": "test",
            "host_id": "unit-test-host",
            "origin_url": "https://github.com/yuelei-dev/ubuntu-yuelei.git",
        },
        "impact_prefix": "deploy/test-release/impacts/",
        "runtime_candidate_prefixes": ["server/", "site/", "deploy/systemd/"],
        "runtime_candidate_paths": [],
        "ignored_repository_paths": ["server/test_example.py"],
        "ignored_repository_prefixes": [],
        "ignored_runtime_directory_names": ["__pycache__"],
        "inventory_roots": [
            "/home/ubuntu/content-api/",
            "/var/www/huangque/",
        ],
        "unmanaged_runtime_paths": [],
        "unmanaged_runtime_prefixes": [],
        "runtime_owner_rules": [
            {"runtime_prefix": "/etc/", "owner": "test-owner", "group": "test-group"},
            {"runtime_prefix": "/home/ubuntu/", "owner": "test-owner", "group": "test-group"},
            {"runtime_prefix": "/var/www/", "owner": "test-owner", "group": "test-group"},
        ],
        "health_probes": {
            "content-health": {"description": "approved content health contract"},
            "site-health": {"description": "approved site health contract"},
            "timer-active": {"description": "approved timer active contract"},
        },
        "service_health_probes": {
            "huangque-content.service": "content-health",
            "example.timer": "timer-active",
        },
        "allowed_tools": ["/usr/bin/git", "/usr/bin/systemctl"],
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
                "allow_unmanaged_runtime": False,
            },
            {
                "kind": "prefix",
                "repository": "site/",
                "runtime": "/var/www/huangque/",
                "service": None,
                "health_probe": "site-health",
                "mode": "0644",
                "delete_allowed": True,
                "allow_unmanaged_runtime": False,
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
                "allow_unmanaged_runtime": True,
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
        "pre_health_checks": ["content-health"],
        "health_checks": ["content-health"],
        "external_checks": [],
        "migrations": [],
    }
    value.update(overrides)
    return value


class FakeRepository:
    def __init__(self, commits, parents=None):
        self.commits = {key: dict(value) for key, value in commits.items()}
        self.parent_map = dict(parents or {})
        self.modes = {}
        self.checkout_calls = []
        self.checkout_error = None
        self.checkout_callback = None

    def require_commit(self, commit):
        if commit not in self.commits:
            raise release_test.ReleaseError("unknown commit")

    def _ancestors(self, commit):
        found = {commit}
        stack = [commit]
        while stack:
            current = stack.pop()
            for parent in self.parent_map.get(current, []):
                if parent not in found:
                    found.add(parent)
                    stack.append(parent)
        return found

    def require_ancestor(self, older, newer):
        self.require_commit(older)
        self.require_commit(newer)
        if older not in self._ancestors(newer):
            raise release_test.ReleaseError("required Git ancestry relation is absent")

    def changed_paths(self, older, newer):
        self.require_ancestor(older, newer)
        before = self.commits[older]
        after = self.commits[newer]
        result = []
        for path in sorted(set(before) | set(after)):
            if path not in before:
                result.append(("A", path))
            elif path not in after:
                result.append(("D", path))
            elif before[path] != after[path] or self.file_mode_at(older, path) != self.file_mode_at(newer, path):
                result.append(("M", path))
        return result

    def file_at(self, commit, path):
        self.require_commit(commit)
        return self.commits[commit].get(path)

    def file_mode_at(self, commit, path):
        self.require_commit(commit)
        if path not in self.commits[commit]:
            return None
        return self.modes.get((commit, path), "100644")

    def file_oid_at(self, commit, path):
        value = self.file_at(commit, path)
        if value is None:
            return None
        header = ("blob %d\0" % len(value)).encode("ascii")
        return hashlib.sha1(header + value).hexdigest()

    def files_at(self, commit):
        self.require_commit(commit)
        return sorted(self.commits[commit])

    def parents(self, commit):
        self.require_commit(commit)
        return list(self.parent_map.get(commit, []))

    def merge_base(self, left, right):
        self.require_ancestor(BASE, left)
        self.require_ancestor(BASE, right)
        return BASE

    def first_parent_commits(self, older, newer):
        self.require_ancestor(older, newer)
        return [MERGE] if newer == MERGE and older == BASE else []

    def verify_checkout(self, target, expected_origin_url, verify_live_origin=True):
        if self.checkout_error:
            raise release_test.ReleaseError(self.checkout_error)
        self.checkout_calls.append((target, expected_origin_url, verify_live_origin))
        if self.checkout_callback:
            self.checkout_callback(len(self.checkout_calls))


class FakeInspector:
    def __init__(self):
        self.active = True
        self.tools = []

    def validate_tool(self, tool, catalog):
        self.tools.append(tool)

    def is_active(self, service):
        return self.active


def review_repository(*, target_impact=None):
    before = b"before\n"
    after = b"after\n"
    raw_impact = json.dumps(target_impact or impact_data(), sort_keys=True).encode("utf-8")
    raw_catalog = release_test._json_bytes(catalog_data())
    base_files = {RUNTIME_REPOSITORY_PATH: before, CATALOG_PATH: raw_catalog}
    head_files = {
        RUNTIME_REPOSITORY_PATH: after,
        IMPACT_PATH: raw_impact,
        CATALOG_PATH: raw_catalog,
    }
    return FakeRepository(
        {
            BASE: base_files,
            HEAD: head_files,
            MERGE: head_files,
            UNRELATED: base_files,
        },
        {
            HEAD: [BASE],
            MERGE: [BASE, HEAD],
            UNRELATED: [BASE],
        },
    )


class CatalogAndImpactTests(unittest.TestCase):
    def setUp(self):
        self.catalog = release_test.RuntimeCatalog(catalog_data())

    def test_runtime_change_without_impact_is_blocked(self):
        repo = review_repository()
        del repo.commits[HEAD][IMPACT_PATH]
        with self.assertRaisesRegex(release_test.ReleaseError, "lack release impact"):
            release_test.collect_release_impact(repo, self.catalog, BASE, HEAD)

    def test_unmapped_runtime_candidate_is_blocked(self):
        repo = FakeRepository(
            {BASE: {"server/unknown.py": b"a"}, HEAD: {"server/unknown.py": b"b"}},
            {HEAD: [BASE]},
        )
        with self.assertRaisesRegex(release_test.ReleaseError, "no catalog mapping"):
            release_test.collect_release_impact(repo, self.catalog, BASE, HEAD)

    def test_catalog_coverage_blocks_unchanged_unclassified_candidate(self):
        repo = FakeRepository(
            {BASE: {"server/unknown.py": b"a"}, HEAD: {"server/unknown.py": b"a"}},
            {HEAD: [BASE]},
        )
        with self.assertRaisesRegex(release_test.ReleaseError, "unclassified"):
            release_test.validate_catalog_coverage(repo, self.catalog, HEAD)

    def test_ignored_repository_file_needs_no_impact(self):
        repo = FakeRepository(
            {BASE: {"server/test_example.py": b"a"}, HEAD: {"server/test_example.py": b"b"}},
            {HEAD: [BASE]},
        )
        result = release_test.collect_release_impact(repo, self.catalog, BASE, HEAD)
        self.assertEqual([], result["runtime_changes"])

    def test_target_catalog_cannot_hide_base_runtime_mapping(self):
        target_data = catalog_data()
        target_data["ignored_repository_paths"].append(RUNTIME_REPOSITORY_PATH)
        target_catalog = release_test.RuntimeCatalog(target_data)
        repo = review_repository()
        del repo.commits[HEAD][IMPACT_PATH]
        with self.assertRaisesRegex(release_test.ReleaseError, "lack release impact"):
            release_test.collect_release_impact(
                repo, target_catalog, BASE, HEAD, base_catalog=self.catalog,
            )

    def test_catalog_change_cannot_share_pr_with_runtime_or_impact(self):
        repo = review_repository()
        repo.commits[BASE][CATALOG_PATH] = json.dumps(catalog_data()).encode("utf-8")
        changed = catalog_data()
        changed["ignored_repository_paths"].append(RUNTIME_REPOSITORY_PATH)
        repo.commits[HEAD][CATALOG_PATH] = json.dumps(changed).encode("utf-8")
        with self.assertRaisesRegex(release_test.ReleaseError, "must be isolated"):
            release_test.collect_release_impact(
                repo, release_test.RuntimeCatalog(changed), BASE, HEAD,
                base_catalog=self.catalog, enforce_catalog_isolation=True,
            )

    def test_catalog_is_immutable_after_bootstrap(self):
        base_files = {CATALOG_PATH: json.dumps(catalog_data()).encode("utf-8")}
        changed = catalog_data()
        changed["min_free_bytes"] = 2
        repo = FakeRepository(
            {
                BASE: base_files,
                HEAD: {CATALOG_PATH: json.dumps(changed).encode("utf-8")},
            },
            {HEAD: [BASE]},
        )
        with self.assertRaisesRegex(release_test.ReleaseError, "immutable"):
            release_test.collect_release_impact(
                repo, release_test.RuntimeCatalog(changed), BASE, HEAD,
                base_catalog=self.catalog, enforce_catalog_isolation=True,
            )

    def test_catalog_rejects_boolean_integer_and_non_boolean_rule_values(self):
        invalid_catalogs = []
        for value in (True, False, "1024", 1.5):
            data = catalog_data()
            data["min_free_bytes"] = value
            invalid_catalogs.append(data)
        for field, value in (
                ("delete_allowed", 1),
                ("allow_unmanaged_runtime", "false"),
                ("daemon_reload", 0),
                ("service_from_repository", "true")):
            data = catalog_data()
            data["rules"][-1][field] = value
            invalid_catalogs.append(data)
        for data in invalid_catalogs:
            with self.subTest(value=data), self.assertRaises(release_test.ReleaseError):
                release_test.RuntimeCatalog(data)

    def test_future_pr_cannot_modify_the_base_owned_verifier(self):
        raw_catalog = release_test._json_bytes(catalog_data())
        repo = FakeRepository(
            {
                BASE: {CATALOG_PATH: raw_catalog, "scripts/release_test.py": b"safe"},
                HEAD: {CATALOG_PATH: raw_catalog, "scripts/release_test.py": b"return 0"},
            },
            {HEAD: [BASE]},
        )
        with self.assertRaisesRegex(release_test.ReleaseError, "trust roots"):
            release_test.collect_release_impact(
                repo, self.catalog, BASE, HEAD,
                base_catalog=self.catalog, enforce_catalog_isolation=True,
            )

    def test_future_pr_cannot_add_a_same_named_spoof_workflow(self):
        raw_catalog = release_test._json_bytes(catalog_data())
        spoof = ".github/workflows/spoof-required-check.yml"
        repo = FakeRepository(
            {
                BASE: {CATALOG_PATH: raw_catalog},
                HEAD: {
                    CATALOG_PATH: raw_catalog,
                    spoof: b"jobs:\n  spoof:\n    name: Base-owned test release impact gate\n",
                },
            },
            {HEAD: [BASE]},
        )
        with self.assertRaisesRegex(release_test.ReleaseError, "trust roots"):
            release_test.collect_release_impact(
                repo, self.catalog, BASE, HEAD,
                base_catalog=self.catalog, enforce_catalog_isolation=True,
            )

    def test_future_pr_cannot_delete_any_workflow(self):
        raw_catalog = release_test._json_bytes(catalog_data())
        workflow = ".github/workflows/ordinary.yml"
        repo = FakeRepository(
            {
                BASE: {CATALOG_PATH: raw_catalog, workflow: b"jobs: {}\n"},
                HEAD: {CATALOG_PATH: raw_catalog},
            },
            {HEAD: [BASE]},
        )
        with self.assertRaisesRegex(release_test.ReleaseError, "trust roots"):
            release_test.collect_release_impact(
                repo, self.catalog, BASE, HEAD,
                base_catalog=self.catalog, enforce_catalog_isolation=True,
            )

    def test_malicious_head_cannot_replace_gate_verifier_catalog_and_runtime(self):
        raw_catalog = release_test._json_bytes(catalog_data())
        base_files = {
            CATALOG_PATH: raw_catalog,
            "scripts/release_test.py": b"safe verifier",
            ".github/workflows/ci.yml": b"safe ci",
            ".github/workflows/release-impact-gate.yml": b"safe base gate",
            RUNTIME_REPOSITORY_PATH: b"before",
        }
        head_files = dict(base_files)
        head_files.update({
            "scripts/release_test.py": b"raise SystemExit(0)",
            ".github/workflows/ci.yml": b"jobs: {}",
            ".github/workflows/release-impact-gate.yml": b"jobs: {}",
            CATALOG_PATH: release_test._json_bytes({**catalog_data(), "min_free_bytes": 2}),
            RUNTIME_REPOSITORY_PATH: b"malicious runtime",
        })
        repo = FakeRepository({BASE: base_files, HEAD: head_files}, {HEAD: [BASE]})
        with self.assertRaisesRegex(
                release_test.ReleaseError, "trust roots|must be isolated"):
            release_test.collect_release_impact(
                repo, self.catalog, BASE, HEAD,
                base_catalog=self.catalog, enforce_catalog_isolation=True,
            )

    def test_runtime_git_symlink_is_blocked(self):
        repo = review_repository()
        repo.modes[(HEAD, RUNTIME_REPOSITORY_PATH)] = "120000"
        with self.assertRaisesRegex(release_test.ReleaseError, "regular Git blob"):
            release_test.collect_release_impact(repo, self.catalog, BASE, HEAD)

    def test_existing_impact_is_immutable(self):
        first = json.dumps(impact_data()).encode("utf-8")
        second = json.dumps(impact_data(required_env=[])).encode("utf-8")
        repo = FakeRepository(
            {BASE: {IMPACT_PATH: first}, HEAD: {IMPACT_PATH: second}}, {HEAD: [BASE]},
        )
        with self.assertRaisesRegex(release_test.ReleaseError, "immutable"):
            release_test.collect_release_impact(repo, self.catalog, BASE, HEAD)

    def test_release_ids_are_globally_unique_at_target_commit(self):
        repo = review_repository()
        repo.commits[HEAD]["deploy/test-release/impacts/duplicate.json"] = (
            repo.commits[HEAD][IMPACT_PATH]
        )
        with self.assertRaisesRegex(release_test.ReleaseError, "globally unique"):
            release_test.collect_release_impact(repo, self.catalog, BASE, HEAD)

    def test_arbitrary_external_command_is_fail_closed(self):
        impact = impact_data(external_checks=[{
            "name": "claimed-no-charge",
            "no_charge": True,
            "argv": ["/usr/bin/python3", "-c", "print('secret')"],
        }])
        with self.assertRaisesRegex(release_test.ReleaseError, "forbid executable"):
            release_test.validate_impact(impact, self.catalog)

    def test_migration_command_is_fail_closed(self):
        impact = impact_data(migrations=[{
            "id": "unsafe-migration",
            "database_path": "/tmp/example.db",
            "up": {"argv": ["/usr/bin/python3", "-c", "print('write')"]},
            "verify": {"argv": ["/usr/bin/python3", "-c", "print('read')"]},
            "rollback": "restore_sqlite_snapshot",
        }])
        with self.assertRaisesRegex(release_test.ReleaseError, "forbid database"):
            release_test.validate_impact(impact, self.catalog)

    def test_feature_pr_cannot_supply_an_unregistered_health_target(self):
        impact = impact_data(health_checks=["unregistered-action-route"])
        with self.assertRaisesRegex(release_test.ReleaseError, "probe ids"):
            release_test.validate_impact(impact, self.catalog)

    def test_impact_fields_and_empty_execution_lists_are_strict(self):
        with self.assertRaisesRegex(release_test.ReleaseError, "fields"):
            release_test.validate_impact(
                dict(impact_data(), unexpected=True), self.catalog,
            )
        with self.assertRaisesRegex(release_test.ReleaseError, "executable"):
            release_test.validate_impact(
                impact_data(external_checks={}), self.catalog,
            )

    def test_boolean_schema_version_and_numeric_release_id_are_rejected(self):
        with self.assertRaisesRegex(release_test.ReleaseError, "schema version"):
            release_test.validate_impact(
                impact_data(schema_version=True), self.catalog,
            )
        with self.assertRaisesRegex(release_test.ReleaseError, "id is invalid"):
            release_test.validate_impact(
                impact_data(release_id=123), self.catalog,
            )

    def test_systemd_dropin_derives_allowlisted_service(self):
        mapping = self.catalog.map(
            "deploy/systemd/huangque-content.service.d/example.conf"
        )
        self.assertEqual("huangque-content.service", mapping["service"])


class ReviewEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.catalog = release_test.RuntimeCatalog(catalog_data())
        self.repo = review_repository()
        self.collected = release_test.collect_release_impact(
            self.repo, self.catalog, BASE, MERGE,
        )

    def test_exact_merge_base_head_range_binds_impact_blob(self):
        result = release_test.verify_review_evidence(
            self.repo, self.catalog, BASE, MERGE, self.collected["impacts"],
            {"pr-999-example": {"base": BASE, "head": HEAD}},
        )
        self.assertEqual(MERGE, result["pr-999-example"]["merge_commit"])
        self.assertEqual(HEAD, result["pr-999-example"]["head"])

    def test_arbitrary_historical_ancestor_cannot_masquerade_as_reviewed_head(self):
        with self.assertRaisesRegex(release_test.ReleaseError, "second parent"):
            release_test.verify_review_evidence(
                self.repo, self.catalog, BASE, MERGE, self.collected["impacts"],
                {"pr-999-example": {"base": BASE, "head": BASE}},
            )

    def test_wrong_release_id_mapping_is_rejected(self):
        with self.assertRaisesRegex(release_test.ReleaseError, "exactly cover"):
            release_test.verify_review_evidence(
                self.repo, self.catalog, BASE, MERGE, self.collected["impacts"],
                {"pr-wrong": {"base": BASE, "head": HEAD}},
            )

    def test_merge_runtime_blob_must_equal_reviewed_head(self):
        self.repo.commits[MERGE][RUNTIME_REPOSITORY_PATH] = b"unreviewed\n"
        collected = release_test.collect_release_impact(
            self.repo, self.catalog, BASE, MERGE,
        )
        with self.assertRaisesRegex(release_test.ReleaseError, "differs"):
            release_test.verify_review_evidence(
                self.repo, self.catalog, BASE, MERGE, collected["impacts"],
                {"pr-999-example": {"base": BASE, "head": HEAD}},
            )

    def test_review_range_parser_requires_merge_base_and_head(self):
        parsed = release_test._parse_reviewed_head_arguments([
            "pr-999-example=%s..%s" % (BASE, HEAD),
        ])
        self.assertEqual(BASE, parsed["pr-999-example"]["base"])
        self.assertEqual(HEAD, parsed["pr-999-example"]["head"])
        with self.assertRaises(release_test.ReleaseError):
            release_test._parse_reviewed_head_arguments([
                "pr-999-example=" + HEAD,
            ])


class ReleaseEngineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.runtime = self.root / "runtime"
        self.source.mkdir()
        self.runtime.mkdir()
        self.before = b"before\n"
        self.after = b"after\n"
        self.catalog = release_test.RuntimeCatalog(catalog_data())
        (self.source / CATALOG_PATH).parent.mkdir(parents=True)
        (self.source / CATALOG_PATH).write_bytes(
            release_test._json_bytes(catalog_data())
        )
        self.repo = review_repository()
        self.inspector = FakeInspector()
        self._write_runtime("/etc/hostname", b"test-host\n")
        self._write_runtime("/etc/machine-id", b"machine-id\n")
        self._write_json("/etc/huangque/release-identity.json", {
            "schema_version": 1,
            "environment": "test",
            "host_id": "unit-test-host",
            "hostname": "test-host",
            "machine_id_sha256": sha256(b"machine-id"),
        })
        self._write_runtime(RUNTIME_PATH, self.before)

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
            runtime_path, (json.dumps(value, sort_keys=True) + "\n").encode("utf-8"),
        )
        if os.name == "posix":
            self._mapped(runtime_path).chmod(0o600)

    def engine(self, **overrides):
        arguments = {
            "repo": self.repo,
            "inspector": self.inspector,
            "environment": {"EXAMPLE_API_KEY": "present-never-logged"},
            "owner_resolver": lambda _owner: getattr(os, "geteuid", lambda: 0)(),
            "group_resolver": lambda _group: getattr(os, "getegid", lambda: 0)(),
        }
        arguments.update(overrides)
        return release_test.ReleaseEngine(
            self.source, self.runtime, self.catalog, **arguments,
        )

    def initialize(self):
        return self.engine().initialize(BASE, "test", verify_live_origin=False)

    def test_initialize_records_exact_commit_and_complete_inventory(self):
        result = self.initialize()
        self.assertEqual("initialized", result["status"])
        state = json.loads(self._mapped(
            "/var/lib/huangque-release/state.json"
        ).read_text("utf-8"))
        self.assertEqual(BASE, state["deployed_main_commit"])
        self.assertEqual([RUNTIME_PATH], state["managed_runtime_paths"])
        self.assertEqual(RUNTIME_REPOSITORY_PATH, state["repository_paths"][RUNTIME_PATH])
        self.assertEqual(CATALOG_PATH, state["runtime_catalog"]["repository_path"])
        self.assertEqual(0o644, state["runtime_metadata"][RUNTIME_PATH]["mode"])
        self.assertEqual("test-owner", state["runtime_metadata"][RUNTIME_PATH]["owner"])
        self.assertEqual("test-group", state["runtime_metadata"][RUNTIME_PATH]["group"])

    def test_wrong_host_is_rejected(self):
        identity_path = self._mapped("/etc/huangque/release-identity.json")
        identity = json.loads(identity_path.read_text("utf-8"))
        identity["host_id"] = "production"
        identity_path.write_text(json.dumps(identity), encoding="utf-8")
        with self.assertRaisesRegex(release_test.ReleaseError, "identity is wrong"):
            self.initialize()
        self.assertFalse(self._mapped("/var/lib/huangque-release/release.lock").exists())

    def test_identity_schema_rejects_json_boolean(self):
        identity_path = self._mapped("/etc/huangque/release-identity.json")
        identity = json.loads(identity_path.read_text("utf-8"))
        identity["schema_version"] = True
        self._write_json("/etc/huangque/release-identity.json", identity)
        with self.assertRaisesRegex(release_test.ReleaseError, "identity is wrong"):
            self.initialize()

    def test_wrong_host_does_not_change_an_existing_lock_file(self):
        lock_path = self._mapped("/var/lib/huangque-release/release.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_bytes(b"existing-lock\n")
        before_bytes = lock_path.read_bytes()
        before_mtime = lock_path.stat().st_mtime_ns
        identity_path = self._mapped("/etc/huangque/release-identity.json")
        identity = json.loads(identity_path.read_text("utf-8"))
        identity["host_id"] = "production"
        identity_path.write_text(json.dumps(identity), encoding="utf-8")
        with self.assertRaisesRegex(release_test.ReleaseError, "identity is wrong"):
            self.initialize()
        self.assertEqual(before_bytes, lock_path.read_bytes())
        self.assertEqual(before_mtime, lock_path.stat().st_mtime_ns)

    def test_mixed_runtime_is_rejected_before_ledger_write(self):
        self._write_runtime(RUNTIME_PATH, b"mixed")
        with self.assertRaisesRegex(release_test.ReleaseError, "does not exactly match"):
            self.initialize()
        self.assertFalse(self._mapped("/var/lib/huangque-release/state.json").exists())

    def test_extra_managed_runtime_file_blocks_initialize(self):
        self._write_runtime(
            "/home/ubuntu/content-api/content_domains/old.py", b"old",
        )
        with self.assertRaisesRegex(release_test.ReleaseError, "does not exactly match"):
            self.initialize()

    def test_python_bytecode_cache_is_not_runtime_drift(self):
        self._write_runtime(
            "/home/ubuntu/content-api/content_domains/__pycache__/core.pyc", b"pyc",
        )
        self.assertEqual("initialized", self.initialize()["status"])
        self.assertEqual("deployed", self.engine().status()["status"])

    @unittest.skipIf(os.name != "posix", "POSIX mode semantics")
    def test_wrong_runtime_mode_blocks_initialize(self):
        self._mapped(RUNTIME_PATH).chmod(0o600)
        with self.assertRaisesRegex(release_test.ReleaseError, "does not exactly match"):
            self.initialize()

    @unittest.skipIf(os.name != "posix", "POSIX owner semantics")
    def test_wrong_runtime_owner_blocks_initialize(self):
        wrong_uid = os.geteuid() + 1
        with self.assertRaisesRegex(release_test.ReleaseError, "does not exactly match"):
            self.engine(owner_resolver=lambda _owner: wrong_uid).initialize(
                BASE, "test", verify_live_origin=False,
            )

    @unittest.skipIf(os.name != "posix", "POSIX group semantics")
    def test_wrong_runtime_group_blocks_initialize(self):
        wrong_gid = os.getegid() + 1
        with self.assertRaisesRegex(release_test.ReleaseError, "does not exactly match"):
            self.engine(group_resolver=lambda _group: wrong_gid).initialize(
                BASE, "test", verify_live_origin=False,
            )

    def test_status_reports_hash_drift(self):
        self.initialize()
        self._write_runtime(RUNTIME_PATH, b"drift")
        result = self.engine().status()
        self.assertEqual("drifted", result["status"])
        self.assertEqual([RUNTIME_PATH], result["drifted_paths"])

    def test_status_reports_extra_file_drift(self):
        self.initialize()
        extra = "/home/ubuntu/content-api/content_domains/old.py"
        self._write_runtime(extra, b"old")
        result = self.engine().status()
        self.assertEqual([extra], result["unexpected_runtime_paths"])

    def test_read_only_plan_binds_review_and_reports_files(self):
        self.initialize()
        result = self.engine().build_plan(
            MERGE,
            {"pr-999-example": {"base": BASE, "head": HEAD}},
        )
        self.assertEqual("planned_read_only", result["status"])
        self.assertEqual("planning_only_no_apply", result["phase"])
        self.assertEqual(RUNTIME_PATH, result["files"][0]["runtime_path"])
        self.assertEqual("test-owner", result["files"][0]["owner"])
        self.assertEqual("test-group", result["files"][0]["group"])
        self.assertEqual(HEAD, result["review_evidence"]["pr-999-example"]["head"])

    def test_plan_is_idempotent_when_target_equals_ledger(self):
        self.initialize()
        result = self.engine().build_plan(BASE, {})
        self.assertEqual("already_deployed", result["status"])
        self.assertEqual([], result["files"])

    def test_idempotent_plan_still_requires_canonical_checkout(self):
        self.initialize()
        self.repo.checkout_error = "wrong checkout"
        with self.assertRaisesRegex(release_test.ReleaseError, "wrong checkout"):
            self.engine().build_plan(BASE, {})

    def test_dirty_worktree_catalog_is_rejected(self):
        self.initialize()
        (self.source / CATALOG_PATH).write_bytes(b"{}\n")
        with self.assertRaisesRegex(release_test.ReleaseError, "differs"):
            self.engine().status()

    def test_ledger_catalog_hash_mismatch_is_rejected(self):
        self.initialize()
        state_path = self._mapped("/var/lib/huangque-release/state.json")
        state = json.loads(state_path.read_text("utf-8"))
        state["runtime_catalog"]["sha256"] = "0" * 64
        self._write_json("/var/lib/huangque-release/state.json", state)
        with self.assertRaisesRegex(release_test.ReleaseError, "does not match Git"):
            self.engine().status()

    def test_ledger_schema_values_reject_json_booleans(self):
        self.initialize()
        state_path = self._mapped("/var/lib/huangque-release/state.json")
        original = json.loads(state_path.read_text("utf-8"))
        for field_path in (("schema_version",), ("runtime_catalog", "schema_version")):
            state = json.loads(json.dumps(original))
            if len(field_path) == 1:
                state[field_path[0]] = True
            else:
                state[field_path[0]][field_path[1]] = True
            self._write_json("/var/lib/huangque-release/state.json", state)
            with self.subTest(field_path=field_path), self.assertRaisesRegex(
                    release_test.ReleaseError, "ledger"):
                self.engine().status()

    def test_initialize_rechecks_runtime_inside_lock_before_ledger_write(self):
        def mutate_after_first_checkout(call_number):
            if call_number == 1:
                self._write_runtime(RUNTIME_PATH, b"changed-during-init")
        self.repo.checkout_callback = mutate_after_first_checkout
        with self.assertRaisesRegex(release_test.ReleaseError, "does not exactly match"):
            self.initialize()
        self.assertFalse(self._mapped("/var/lib/huangque-release/state.json").exists())

    def test_initialize_rechecks_identity_before_ledger_write(self):
        engine = self.engine()
        original = engine.verify_identity
        calls = 0

        def mutate_after_first_identity_read():
            nonlocal calls
            calls += 1
            result = original()
            if calls == 1:
                identity = dict(result)
                identity["host_id"] = "changed-during-init"
                self._write_json("/etc/huangque/release-identity.json", identity)
            return result

        engine.verify_identity = mutate_after_first_identity_read
        with self.assertRaisesRegex(release_test.ReleaseError, "identity is wrong"):
            engine.initialize(BASE, "test", verify_live_origin=False)
        self.assertFalse(self._mapped("/var/lib/huangque-release/state.json").exists())

    def test_initialize_rechecks_checkout_before_ledger_write(self):
        def invalidate_after_first_checkout(call_number):
            if call_number == 1:
                self.repo.checkout_error = "checkout changed during initialization"
        self.repo.checkout_callback = invalidate_after_first_checkout
        with self.assertRaisesRegex(release_test.ReleaseError, "checkout changed"):
            self.initialize()
        self.assertFalse(self._mapped("/var/lib/huangque-release/state.json").exists())

    def test_initialize_rechecks_extra_inventory_before_ledger_write(self):
        engine = self.engine()
        original = engine._inventory_drift
        calls = 0

        def add_extra_after_first_inventory(expected_paths):
            nonlocal calls
            calls += 1
            result = original(expected_paths)
            if calls == 1:
                self._write_runtime(
                    "/home/ubuntu/content-api/content_domains/late.py", b"late",
                )
            return result

        engine._inventory_drift = add_extra_after_first_inventory
        with self.assertRaisesRegex(release_test.ReleaseError, "changed during"):
            engine.initialize(BASE, "test", verify_live_origin=False)
        self.assertFalse(self._mapped("/var/lib/huangque-release/state.json").exists())

    def test_plan_fails_when_required_environment_name_is_missing(self):
        self.initialize()
        with self.assertRaisesRegex(release_test.ReleaseError, "EXAMPLE_API_KEY") as caught:
            self.engine(environment={}).build_plan(
                MERGE, {"pr-999-example": {"base": BASE, "head": HEAD}},
            )
        self.assertNotIn("present-never-logged", str(caught.exception))

    def test_plan_fails_when_service_is_inactive(self):
        self.initialize()
        self.inspector.active = False
        with self.assertRaisesRegex(release_test.ReleaseError, "not active"):
            self.engine().build_plan(
                MERGE, {"pr-999-example": {"base": BASE, "head": HEAD}},
            )

    def test_plan_only_reports_named_health_probes_without_calling_routes(self):
        self.initialize()
        result = self.engine().build_plan(
            MERGE, {"pr-999-example": {"base": BASE, "head": HEAD}},
        )
        self.assertEqual(["content-health"], result["pre_health_probes"])
        self.assertEqual(["content-health"], result["post_health_probes"])

    def test_plan_reports_daemon_reload_for_systemd_change(self):
        repository_path = "deploy/systemd/example.timer"
        runtime_path = "/etc/systemd/system/example.timer"
        impact = impact_data(
            runtime_changes=[repository_path],
            restart_services=["example.timer"],
            required_env=[],
            pre_health_checks=["timer-active"],
            health_checks=["timer-active"],
        )
        raw_catalog = release_test._json_bytes(catalog_data())
        raw_impact = json.dumps(impact, sort_keys=True).encode("utf-8")
        repo = FakeRepository(
            {
                BASE: {CATALOG_PATH: raw_catalog, repository_path: b"before\n"},
                HEAD: {
                    CATALOG_PATH: raw_catalog,
                    repository_path: b"after\n",
                    IMPACT_PATH: raw_impact,
                },
                MERGE: {
                    CATALOG_PATH: raw_catalog,
                    repository_path: b"after\n",
                    IMPACT_PATH: raw_impact,
                },
            },
            {HEAD: [BASE], MERGE: [BASE, HEAD]},
        )
        self._mapped(RUNTIME_PATH).unlink()
        self._write_runtime(runtime_path, b"before\n")
        engine = self.engine(repo=repo, environment={})
        engine.initialize(BASE, "test", verify_live_origin=False)
        result = engine.build_plan(
            MERGE, {"pr-999-example": {"base": BASE, "head": HEAD}},
        )
        self.assertTrue(result["daemon_reload_required"])
        self.assertTrue(result["files"][0]["daemon_reload"])

    def test_nginx_change_is_explicitly_not_releasable_in_phase_one(self):
        repository_path = "deploy/nginx-huangquechuanmei.conf"
        runtime_path = "/etc/nginx/sites-available/huangquechuanmei"
        data = catalog_data()
        data["runtime_candidate_paths"] = [repository_path]
        data["rules"].append({
            "kind": "exact",
            "repository": repository_path,
            "runtime": runtime_path,
            "service": None,
            "health_probe": "site-health",
            "planning_blocker": (
                "nginx syntax validation and transactional reload are not supported"
            ),
            "mode": "0644",
            "delete_allowed": False,
            "allow_unmanaged_runtime": False,
        })
        self.catalog = release_test.RuntimeCatalog(data)
        (self.source / CATALOG_PATH).write_bytes(release_test._json_bytes(data))
        impact = impact_data(
            runtime_changes=[repository_path], restart_services=[], required_env=[],
            pre_health_checks=["site-health"], health_checks=["site-health"],
        )
        raw_catalog = release_test._json_bytes(data)
        raw_impact = json.dumps(impact, sort_keys=True).encode("utf-8")
        repo = FakeRepository(
            {
                BASE: {CATALOG_PATH: raw_catalog, repository_path: b"before\n"},
                HEAD: {
                    CATALOG_PATH: raw_catalog, repository_path: b"after\n",
                    IMPACT_PATH: raw_impact,
                },
                MERGE: {
                    CATALOG_PATH: raw_catalog, repository_path: b"after\n",
                    IMPACT_PATH: raw_impact,
                },
            },
            {HEAD: [BASE], MERGE: [BASE, HEAD]},
        )
        self._mapped(RUNTIME_PATH).unlink()
        self._write_runtime(runtime_path, b"before\n")
        engine = self.engine(repo=repo, environment={})
        engine.initialize(BASE, "test", verify_live_origin=False)
        with self.assertRaisesRegex(release_test.ReleaseError, "merged_not_releasable"):
            engine.build_plan(
                MERGE, {"pr-999-example": {"base": BASE, "head": HEAD}},
            )

    def test_stale_lock_file_does_not_block_initialization(self):
        lock = self._mapped("/var/lib/huangque-release/release.lock")
        lock.parent.mkdir(parents=True)
        lock.write_text("stale", encoding="utf-8")
        if os.name == "posix":
            lock.parent.chmod(0o700)
            lock.chmod(0o600)
        self.assertEqual("initialized", self.initialize()["status"])

    @unittest.skipIf(os.name != "posix", "POSIX flock semantics")
    def test_live_kernel_lock_rejects_concurrent_planner(self):
        import fcntl
        lock = self._mapped("/var/lib/huangque-release/release.lock")
        lock.parent.mkdir(parents=True)
        lock.parent.chmod(0o700)
        with lock.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(release_test.ReleaseError, "already running"):
                with self.engine()._release_lock():
                    pass


class RepositoryContractTests(unittest.TestCase):
    def test_checked_in_json_contracts_are_objects(self):
        for relative in (
            "deploy/test-release/runtime-catalog.json",
            "deploy/test-release/release-impact.schema.json",
            "deploy/test-release/release-impact.example.json",
        ):
            self.assertIsInstance(json.loads((ROOT / relative).read_text("utf-8")), dict)

    def test_checked_in_example_passes_fail_closed_phase_one_contract(self):
        catalog = release_test.RuntimeCatalog.load(
            ROOT, "deploy/test-release/runtime-catalog.json",
        )
        example = json.loads(
            (ROOT / "deploy/test-release/release-impact.example.json").read_text("utf-8")
        )
        validated = release_test.validate_impact(example, catalog)
        self.assertEqual("pr-000-example", validated["release_id"])
        self.assertEqual([], validated["external_checks"])
        self.assertEqual([], validated["migrations"])

    def test_ci_checks_out_and_asserts_exact_pull_request_head(self):
        workflow = (ROOT / ".github/workflows/ci.yml").read_text("utf-8")
        self.assertIn("ref: ${{ github.event_name == 'pull_request'", workflow)
        self.assertIn("github.event.pull_request.head.sha", workflow)
        self.assertIn("git rev-parse HEAD", workflow)
        self.assertIn("scripts/release_test.py check-impact", workflow)

    def test_release_impact_gate_executes_only_the_base_owned_verifier(self):
        workflow = (
            ROOT / ".github/workflows/release-impact-gate.yml"
        ).read_text("utf-8")
        action_pin = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
        self.assertIn("pull_request_target:", workflow)
        self.assertRegex(
            workflow,
            r"(?ms)^permissions:\n  contents: read\n  pull-requests: read\n\n",
        )
        self.assertNotRegex(workflow, r"(?m)^\s*[^#\n]*:\s*write\s*$")
        self.assertNotIn("secrets", workflow.lower())
        self.assertEqual(1, len(re.findall(r"(?m)^\s*uses:\s*", workflow)))
        self.assertEqual(1, workflow.count(action_pin))
        self.assertIn("github.event.pull_request.base.sha", workflow)
        self.assertIn("refs/pull/$PR_NUMBER/head", workflow)
        self.assertIn("Execute only the trusted base verifier", workflow)
        self.assertIn("persist-credentials: false", workflow)
        self.assertNotIn("head.repo.clone_url", workflow)
        self.assertNotIn("ref: ${{ github.event.pull_request.head.sha", workflow)
        self.assertIn('path.startswith(".github/workflows/")', (
            ROOT / "scripts/release_test.py"
        ).read_text("utf-8"))

    def test_checked_in_bootstrap_manifest_binds_the_exact_installed_entrypoint(self):
        manifest = json.loads((
            ROOT / "deploy/test-release/bootstrap.example.json"
        ).read_text("utf-8"))
        entrypoint = (ROOT / "scripts/release_test.py").read_bytes()
        self.assertEqual({
            "schema_version": 1,
            "entrypoint": "/usr/local/libexec/huangque-release/release_test.py",
            "entrypoint_sha256": hashlib.sha256(entrypoint).hexdigest(),
            "source_root": "/opt/huangque-test-release",
        }, manifest)
        readme = (ROOT / "deploy/test-release/README.md").read_text("utf-8")
        self.assertNotRegex(
            readme,
            r"sudo\s+/usr/bin/python3\s+scripts/release_test\.py",
        )

    @unittest.skipIf(os.name != "posix", "Installed entrypoint trust is Linux-only")
    def test_runtime_commands_reject_a_deployment_user_worktree_entrypoint(self):
        with tempfile.TemporaryDirectory() as directory:
            with contextlib.redirect_stdout(io.StringIO()) as output:
                result = release_test.main(["status", "--source-root", directory])
        self.assertEqual(2, result)
        self.assertIn("root-owned installed entrypoint", output.getvalue())

    @unittest.skipIf(os.name != "posix", "Installed entrypoint trust is Linux-only")
    def test_runtime_entrypoint_requires_root_owned_exact_mode_manifest_and_hash(self):
        script_bytes = (ROOT / "scripts/release_test.py").read_bytes()
        manifest = release_test._json_bytes({
            "schema_version": 1,
            "entrypoint": release_test.RUNTIME_ENTRYPOINT,
            "entrypoint_sha256": hashlib.sha256(script_bytes).hexdigest(),
            "source_root": release_test.DEFAULT_SOURCE_ROOT,
        })
        manifest_info = mock.Mock(st_uid=0, st_mode=0o100600)
        script_info = mock.Mock(st_uid=0, st_mode=0o100755)
        records = {
            release_test.RUNTIME_BOOTSTRAP_MANIFEST: (manifest, manifest_info),
            release_test.RUNTIME_ENTRYPOINT: (script_bytes, script_info),
        }
        with mock.patch.object(release_test.os, "geteuid", return_value=0), \
                mock.patch.object(
                    release_test, "__file__", release_test.RUNTIME_ENTRYPOINT,
                ), mock.patch.object(
                    release_test, "_read_regular_record",
                    side_effect=lambda _root, path: records.get(path),
                ):
            release_test._verify_runtime_entrypoint(release_test.DEFAULT_SOURCE_ROOT)
            manifest_info.st_mode = 0o100644
            with self.assertRaisesRegex(release_test.ReleaseError, "manifest is not trusted"):
                release_test._verify_runtime_entrypoint(release_test.DEFAULT_SOURCE_ROOT)

    def test_checked_in_catalog_matches_existing_authoritative_runtime_maps(self):
        catalog = release_test.RuntimeCatalog.load(
            ROOT, "deploy/test-release/runtime-catalog.json",
        )
        tree = ast.parse((ROOT / "scripts/drift_sentinel.py").read_text("utf-8"))
        assignments = {}
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in {
                            "BACKEND_RUNTIME", "AUTH_SHARED_RUNTIME"}:
                        assignments[target.id] = ast.literal_eval(node.value)
        self.assertEqual({"BACKEND_RUNTIME", "AUTH_SHARED_RUNTIME"}, set(assignments))
        for repository_path, runtime_path in {
                **assignments["BACKEND_RUNTIME"],
                **assignments["AUTH_SHARED_RUNTIME"],
        }.items():
            with self.subTest(repository_path=repository_path, runtime_path=runtime_path):
                self.assertTrue(catalog.is_candidate(repository_path))
                self.assertIn(
                    runtime_path,
                    [item["runtime_path"] for item in catalog.mappings(repository_path)],
                )
        self.assertEqual(
            {
                "/home/ubuntu/leadgen-A/pool_health.py",
                "/home/ubuntu/leadgen-B/pool_health.py",
            },
            {
                item["runtime_path"]
                for item in catalog.mappings("scripts/pool_health.py")
            },
        )
        for repository_path in (
                "server/hermes_ip12/README.md",
                "server/hermes_ip12/prompt.md"):
            with self.subTest(repository_path=repository_path):
                self.assertFalse(catalog.is_ignored(repository_path))
                self.assertEqual(
                    "/home/ubuntu/hermes-web/" + repository_path.rsplit("/", 1)[1],
                    catalog.map(repository_path)["runtime_path"],
                )
        migration = catalog.map("scripts/migrate_hermes_artifacts.py")
        self.assertEqual(
            "/home/ubuntu/hermes-web/scripts/migrate_hermes_artifacts.py",
            migration["runtime_path"],
        )
        self.assertEqual(0o755, migration["mode"])
        release_script = (ROOT / "deploy/hermes-ip12-release.sh").read_text("utf-8")
        self.assertIn('"$HERMES_RELEASE_DIR/server/hermes_ip12/" "$APP_DIR/"', release_script)
        self.assertIn('"$APP_DIR/scripts/migrate_hermes_artifacts.py"', release_script)

    @unittest.skipIf(os.name != "posix", "Git metadata trust is Linux-only")
    def test_untrusted_git_fsmonitor_config_is_rejected_without_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            subprocess.run(
                ["/usr/bin/git", "init", "-b", "main", str(root)],
                check=True, capture_output=True,
            )
            marker = Path(directory) / "fsmonitor-ran"
            payload = Path(directory) / "payload.sh"
            payload.write_text(
                "#!/bin/sh\ntouch '%s'\n" % marker.as_posix(), encoding="utf-8",
            )
            payload.chmod(0o755)
            subprocess.run(
                ["/usr/bin/git", "-C", str(root), "config", "core.fsmonitor", str(payload)],
                check=True,
            )
            with self.assertRaisesRegex(release_test.ReleaseError, "forbidden key"):
                release_test.GitRepository(root)
            self.assertFalse(marker.exists())

    @unittest.skipIf(os.name != "posix", "Git metadata trust is Linux-only")
    def test_untrusted_git_url_rewrite_is_rejected_before_live_origin(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            subprocess.run(
                ["/usr/bin/git", "init", "-b", "main", str(root)],
                check=True, capture_output=True,
            )
            subprocess.run(
                [
                    "/usr/bin/git", "-C", str(root), "config",
                    "url.file:///tmp/forged/.insteadOf",
                    "https://github.com/yuelei-dev/ubuntu-yuelei.git",
                ],
                check=True,
            )
            with self.assertRaisesRegex(release_test.ReleaseError, "forbidden section"):
                release_test.GitRepository(root)

    @unittest.skipIf(os.name != "posix", "Git metadata trust is Linux-only")
    def test_git_history_replacement_metadata_and_writable_metadata_are_rejected(self):
        cases = {
            "objects/info/alternates": "/tmp/alternate-objects\n",
            "info/grafts": "%s %s\n" % ("1" * 40, "2" * 40),
            "refs/replace/" + "1" * 40: "2" * 40 + "\n",
            "shallow": "1" * 40 + "\n",
        }
        for relative, content in cases.items():
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "repo"
                subprocess.run(
                    ["/usr/bin/git", "init", "-b", "main", str(root)],
                    check=True, capture_output=True,
                )
                target = root / ".git" / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                with self.assertRaisesRegex(
                        release_test.ReleaseError,
                        "replacement, graft, alternate, or shallow"):
                    release_test.GitRepository(root)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            subprocess.run(
                ["/usr/bin/git", "init", "-b", "main", str(root)],
                check=True, capture_output=True,
            )
            config = root / ".git" / "config"
            config.chmod(0o666)
            with self.assertRaisesRegex(release_test.ReleaseError, "repository files"):
                release_test.GitRepository(root)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            subprocess.run(
                ["/usr/bin/git", "init", "-b", "main", str(root)],
                check=True, capture_output=True,
            )
            untrusted = root / "deploy" / "test-release" / "runtime-catalog.json"
            untrusted.parent.mkdir(parents=True)
            untrusted.write_text("{}\n", encoding="utf-8")
            untrusted.chmod(0o666)
            with self.assertRaisesRegex(release_test.ReleaseError, "repository files"):
                release_test.GitRepository(root)

    @unittest.skipIf(os.name != "posix", "Git metadata trust is Linux-only")
    def test_packed_replacement_ref_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            subprocess.run(
                ["/usr/bin/git", "init", "-b", "main", str(root)],
                check=True, capture_output=True,
            )
            (root / ".git" / "packed-refs").write_text(
                "%s refs/replace/%s\n" % ("2" * 40, "1" * 40),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(release_test.ReleaseError, "replacement refs"):
                release_test.GitRepository(root)

    def test_git_subprocess_environment_disables_replace_and_optional_locks(self):
        environment = release_test._minimal_subprocess_environment()
        self.assertEqual("1", environment["GIT_NO_REPLACE_OBJECTS"])
        self.assertEqual("0", environment["GIT_OPTIONAL_LOCKS"])
        self.assertEqual("/dev/null", environment["GIT_CONFIG_GLOBAL"])

    def test_live_origin_verification_does_not_use_git_remote_transport(self):
        script = (ROOT / "scripts/release_test.py").read_text("utf-8")
        self.assertNotIn('"ls-remote"', script)
        self.assertIn("urllib.request.ProxyHandler({})", script)
        self.assertIn("ssl.create_default_context()", script)

    def test_checked_in_script_runtime_change_requires_an_impact_contract(self):
        catalog = release_test.RuntimeCatalog.load(
            ROOT, "deploy/test-release/runtime-catalog.json",
        )
        path = "scripts/process_invite_reward_claims.py"
        repo = FakeRepository(
            {BASE: {path: b"before"}, HEAD: {path: b"after"}}, {HEAD: [BASE]},
        )
        with self.assertRaisesRegex(release_test.ReleaseError, "lack release impact"):
            release_test.collect_release_impact(repo, catalog, BASE, HEAD)

    def test_phase_one_exposes_no_mutating_release_subcommands(self):
        script = (ROOT / "scripts/release_test.py").read_text("utf-8")
        self.assertNotIn('add_parser("apply")', script)
        self.assertNotIn('add_parser("rollback")', script)
        self.assertNotIn('add_parser("recover")', script)
        self.assertIn("planning_only_no_apply", script)

    def test_runtime_cli_rejects_alternate_trust_roots(self):
        for flag, value in (
            ("--catalog", "alternate.json"),
            ("--identity-file", "/tmp/identity.json"),
            ("--state-root", "/tmp/state"),
            ("--runtime-root", "/tmp/runtime"),
        ):
            with self.subTest(flag=flag), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    release_test.main(["status", flag, value])


if __name__ == "__main__":
    unittest.main()
