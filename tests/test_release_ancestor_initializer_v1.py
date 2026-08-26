import contextlib
import hashlib
import importlib.util
import io
import json
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BASE = "709311d5b92a559da50c4284321c5aa4504b9002"
OLD = "1" * 40
LATEST = "2" * 40
OTHER = "3" * 40
CATALOG = b'{"schema_version":1}\n'
RUNTIME_PATH = "/srv/example.py"

SPEC = importlib.util.spec_from_file_location(
    "release_ancestor_initializer_v1",
    ROOT / "tools/test_release_ancestor_initializer_v1.py",
)
initializer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(initializer)

PHASE_SPEC = importlib.util.spec_from_file_location(
    "locked_phase_one_compatibility",
    ROOT / "scripts/release_test.py",
)
phase_one = importlib.util.module_from_spec(PHASE_SPEC)
PHASE_SPEC.loader.exec_module(phase_one)

TRANSACTION_SPEC = importlib.util.spec_from_file_location(
    "locked_transaction_compatibility",
    ROOT / "tools/test_release_transaction.py",
)
transaction = importlib.util.module_from_spec(TRANSACTION_SPEC)
TRANSACTION_SPEC.loader.exec_module(transaction)


class FakeCatalog:
    target = {
        "origin_url": "https://github.com/yuelei-dev/ubuntu-yuelei.git",
        "environment": "test",
        "host_id": "test-01",
    }
    impact_prefix = "deploy/test-release/impacts/"
    min_free_bytes = 0


class FakeRepo:
    def __init__(self):
        self.commits = {OLD: {"catalog": CATALOG}, LATEST: {"catalog": CATALOG}, OTHER: {}}
        self.parents = {LATEST: [OLD]}
        self.head = LATEST
        self.local_main = LATEST
        self.live_main = LATEST
        self.live_calls = 0
        self.after_live = None

    def output(self, arguments):
        values = {
            ("status", "--porcelain", "--untracked-files=normal"): "",
            ("symbolic-ref", "--short", "HEAD"): "main",
            ("remote", "get-url", "origin"): FakeCatalog.target["origin_url"],
            ("rev-parse", "HEAD"): self.head,
            ("rev-parse", "refs/remotes/origin/main"): self.local_main,
        }
        return values[tuple(arguments)]

    def remote_main_resolver(self, _origin):
        self.live_calls += 1
        value = self.live_main
        if self.after_live:
            self.after_live(self.live_calls)
        return value

    def require_commit(self, commit):
        if commit not in self.commits:
            raise RuntimeError("commit missing")

    def require_ancestor(self, older, newer):
        self.require_commit(older)
        self.require_commit(newer)
        current = {newer}
        stack = [newer]
        while stack:
            for parent in self.parents.get(stack.pop(), []):
                if parent not in current:
                    current.add(parent)
                    stack.append(parent)
        if older not in current:
            raise RuntimeError("required Git ancestry relation is absent")


class FakePhase:
    SCHEMA_VERSION = 1

    @staticmethod
    def validate_catalog_coverage(_repo, _catalog, _commit):
        return None

    @staticmethod
    def collect_impact_index(_repo, _catalog, commit):
        return {} if commit == OLD else {"new": {"sha256": "4" * 64}}

    @staticmethod
    def _read_regular(runtime_root, _path):
        return runtime_root.state


class FakeEngine:
    def __init__(self):
        self.repo = FakeRepo()
        self.catalog = FakeCatalog()
        self.runtime_root = types.SimpleNamespace(state=None)
        self.identity = {"environment": "test", "host_id": "test-01"}
        self.worktree_catalog = CATALOG
        self.inventory_drift = []
        self.snapshot_calls = 0
        self.on_inventory = None
        self.written_state = None
        self.external_boundaries = [{"runtime_path": "/external", "token": "stable"}]
        self.external_calls = 0
        self.on_external = None

    def verify_identity(self):
        return dict(self.identity)

    def _catalog_record(self, commit):
        raw = self.repo.commits[commit].get("catalog")
        if raw != self.worktree_catalog:
            raise RuntimeError("working tree runtime catalog differs from locked Git blob")
        return {
            "repository_path": "deploy/test-release/runtime-catalog.json",
            "blob_oid": "5" * 40,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "schema_version": 1,
        }

    @staticmethod
    def _expected_runtime(commit):
        if commit != OLD:
            raise RuntimeError("wrong inventory commit")
        return (
            {RUNTIME_PATH: {"state": "file", "sha256": "6" * 64}},
            {RUNTIME_PATH: "server/example.py"},
            {RUNTIME_PATH: {"mode": 0o644, "owner": "root", "group": "root"}},
        )

    @staticmethod
    def _runtime_matches(_path, _expected, _metadata):
        return True

    def _inventory_drift(self, _paths):
        self.snapshot_calls += 1
        result = list(self.inventory_drift)
        if self.on_inventory:
            self.on_inventory(self.snapshot_calls)
        return result

    def external_boundary_snapshot(self):
        self.external_calls += 1
        result = list(self.external_boundaries)
        if self.on_external:
            self.on_external(self.external_calls)
        return result

    @contextlib.contextmanager
    def _release_lock(self):
        yield

    @staticmethod
    def _state_runtime_path(_leaf):
        return "/var/lib/huangque-release/state.json"

    def _write_state(self, state):
        self.written_state = state
        self.runtime_root.state = json.dumps(state).encode("utf-8")


class CompatibleConsumerEngine(FakeEngine):
    """Small harness that invokes the immutable consumers' real methods."""

    def _verify_private_runtime_file(self, _runtime_path, _label):
        return None

    def _runtime_json(self, _runtime_path, _label):
        return json.loads(self.runtime_root.state.decode("utf-8"))

    state_root = ROOT

    def load_state(self):
        return phase_one.ReleaseEngine.load_state(self)

    def status(self):
        return phase_one.ReleaseEngine.status(self)

    def _catalog_record(self, _commit, *, expected=None):
        record = {
            "repository_path": phase_one.DEFAULT_CATALOG,
            "blob_oid": "5" * 40,
            "sha256": hashlib.sha256(CATALOG).hexdigest(),
            "schema_version": 1,
        }
        if expected is not None and record != expected:
            raise RuntimeError("catalog record mismatch")
        return record

    @staticmethod
    def _runtime_matches(_path, _expected, _metadata):
        return True

    @staticmethod
    def _inventory_drift(_paths):
        return []

    @staticmethod
    def _verify_service_preconditions(_services):
        return None

    def build_plan(self, target_commit, reviewed_evidence=None):
        return phase_one.ReleaseEngine.build_plan(self, target_commit, reviewed_evidence)

    def _verify_planning_snapshot(self, identity, state, target_commit, services):
        return phase_one.ReleaseEngine._verify_planning_snapshot(
            self, identity, state, target_commit, services,
        )

    def verify_external_boundary_snapshot(self, expected):
        if expected != self.external_boundaries:
            raise RuntimeError("external boundary drift")
        return expected


class AncestorInitializerTests(unittest.TestCase):
    def setUp(self):
        self.engine = FakeEngine()
        self.subject = initializer.AncestorInitializer(FakePhase, self.engine)

    def test_older_deployed_ancestor_initializes_compatible_ledger(self):
        result = self.subject.initialize(OLD, "test")
        self.assertEqual("initialized", result["status"])
        self.assertEqual(OLD, self.engine.written_state["deployed_main_commit"])
        self.assertEqual({}, self.engine.written_state["accepted_impacts"])
        self.assertEqual([RUNTIME_PATH], self.engine.written_state["managed_runtime_paths"])
        self.assertEqual({
            "schema_version", "environment", "host_id", "deployed_main_commit",
            "runtime_catalog", "accepted_impacts", "runtime_hashes",
            "repository_paths", "runtime_metadata", "managed_runtime_paths",
            "last_release_id", "last_successful_release",
        }, set(self.engine.written_state))
        self.assertEqual(3, self.engine.repo.live_calls)

    def test_immutable_phase_one_and_transaction_consume_ledger_without_migration(self):
        producer = FakeEngine()
        initializer.AncestorInitializer(FakePhase, producer).initialize(OLD, "test")
        consumer = CompatibleConsumerEngine()
        consumer.runtime_root.state = producer.runtime_root.state
        consumer.repo.commits[OLD]["catalog"] = CATALOG
        consumer.repo.verify_checkout = mock.Mock()
        release_collection = {"impacts": [], "changed_paths": []}
        with mock.patch.object(phase_one, "collect_impact_index", return_value={}), \
                mock.patch.object(phase_one, "validate_catalog_coverage"), \
                mock.patch.object(
                    phase_one, "collect_release_impact", return_value=release_collection,
                ), mock.patch.object(phase_one, "verify_review_evidence", return_value={}):
            loaded = consumer.load_state()
            status = consumer.status()
            plan = consumer.build_plan(LATEST)
            adapter = transaction.TrustedPlannerAdapter(phase_one, consumer)
            transaction_plan = adapter.build_plan(LATEST, {})
        self.assertEqual(OLD, loaded["deployed_main_commit"])
        self.assertEqual("deployed", status["status"])
        self.assertEqual("planned_read_only", plan["status"])
        self.assertEqual(OLD, plan["from_commit"])
        self.assertEqual(LATEST, plan["target_commit"])
        self.assertEqual("planned_read_only", transaction_plan["status"])
        self.assertEqual(loaded, adapter.load_state())
        consumer.repo.verify_checkout.assert_called_with(
            LATEST, FakeCatalog.target["origin_url"], verify_live_origin=True,
        )

    def test_phase_one_runtime_trust_check_precedes_catalog_and_engine(self):
        calls = []

        class RuntimeCatalog:
            @staticmethod
            def load(source_root, repository_path):
                calls.append(("catalog", source_root, repository_path))
                return object()

        class ReleaseEngine:
            def __init__(self, source_root, runtime_root, _catalog):
                calls.append(("engine", source_root, runtime_root))

            @staticmethod
            def initialize(deployed_commit, confirmation):
                calls.append(("initialize", deployed_commit, confirmation))
                return {"ok": True}

        delegated = types.SimpleNamespace(
            DEFAULT_CATALOG="deploy/test-release/runtime-catalog.json",
            RuntimeCatalog=RuntimeCatalog,
            ReleaseEngine=ReleaseEngine,
            _verify_runtime_entrypoint=lambda source_root: calls.append(
                ("trust", source_root),
            ),
        )
        delegated._external_boundary_contract = {}
        delegated._external_boundary_module = types.SimpleNamespace(
            install=lambda _phase, catalog, _contract: (catalog, ReleaseEngine, object()),
        )
        with mock.patch.object(initializer, "_load_verified_phase_one", return_value=delegated), \
                mock.patch.object(initializer, "AncestorInitializer") as subject, \
                contextlib.redirect_stdout(io.StringIO()):
            subject.return_value.initialize.return_value = {"ok": True}
            result = initializer.main([
                "initialize", "--deployed-commit", OLD,
                "--confirm-environment", "test",
            ])
        self.assertEqual(0, result)
        self.assertEqual(("trust", initializer.SOURCE_ROOT), calls[0])
        self.assertEqual("catalog", calls[1][0])
        self.assertEqual("engine", calls[2][0])

    def test_phase_one_runtime_trust_failure_creates_no_engine_lock_or_state(self):
        for error in (
                "source parent is writable or symlinked",
                "installed phase-one manifest or launcher differs"):
            with self.subTest(error=error):
                catalog_load = mock.Mock()
                engine_constructor = mock.Mock()
                delegated = types.SimpleNamespace(
                    DEFAULT_CATALOG="deploy/test-release/runtime-catalog.json",
                    RuntimeCatalog=types.SimpleNamespace(load=catalog_load),
                    ReleaseEngine=engine_constructor,
                    _verify_runtime_entrypoint=mock.Mock(side_effect=RuntimeError(error)),
                )
                with mock.patch.object(
                        initializer, "_load_verified_phase_one", return_value=delegated,
                    ), contextlib.redirect_stdout(io.StringIO()):
                    result = initializer.main([
                        "initialize", "--deployed-commit", OLD,
                        "--confirm-environment", "test",
                    ])
                self.assertEqual(2, result)
                delegated._verify_runtime_entrypoint.assert_called_once_with(
                    initializer.SOURCE_ROOT,
                )
                catalog_load.assert_not_called()
                engine_constructor.assert_not_called()

    def test_non_ancestor_and_missing_deployed_commit_fail_closed(self):
        with self.assertRaisesRegex(RuntimeError, "ancestry"):
            self.subject.initialize(OTHER, "test")
        with self.assertRaisesRegex(RuntimeError, "commit missing"):
            self.subject.initialize("9" * 40, "test")

    def test_live_head_and_local_main_drift_fail_closed(self):
        cases = (
            ("live_main", OTHER, "live approved"),
            ("head", OTHER, "HEAD and local"),
            ("local_main", OTHER, "HEAD and local"),
        )
        for attribute, value, message in cases:
            with self.subTest(attribute=attribute):
                engine = FakeEngine()
                setattr(engine.repo, attribute, value)
                with self.assertRaisesRegex(initializer.InitializerError, message):
                    initializer.AncestorInitializer(FakePhase, engine).initialize(OLD, "test")
                self.assertIsNone(engine.written_state)

    def test_catalog_evolution_and_inventory_drift_fail_closed(self):
        self.engine.worktree_catalog = b"changed\n"
        with self.assertRaisesRegex(RuntimeError, "catalog differs"):
            self.subject.initialize(OLD, "test")
        engine = FakeEngine()
        engine.inventory_drift = ["/srv/extra.py"]
        with self.assertRaisesRegex(initializer.InitializerError, "inventory"):
            initializer.AncestorInitializer(FakePhase, engine).initialize(OLD, "test")

    def test_source_and_inventory_toctou_fail_before_state_write(self):
        def drift_source(call_number):
            if call_number == 1:
                self.engine.repo.head = OTHER
                self.engine.repo.local_main = OTHER
                self.engine.repo.live_main = OTHER

        self.engine.repo.after_live = drift_source
        with self.assertRaisesRegex(initializer.InitializerError, "captured latest main"):
            self.subject.initialize(OLD, "test")
        self.assertIsNone(self.engine.written_state)

        engine = FakeEngine()

        def drift_inventory(call_number):
            if call_number == 2:
                engine.inventory_drift = ["/srv/late.py"]

        engine.on_inventory = drift_inventory
        with self.assertRaisesRegex(initializer.InitializerError, "inventory"):
            initializer.AncestorInitializer(FakePhase, engine).initialize(OLD, "test")
        self.assertIsNone(engine.written_state)

        engine = FakeEngine()

        def drift_external(call_number):
            if call_number == 1:
                engine.external_boundaries = [
                    {"runtime_path": "/external", "token": "replaced"},
                ]

        engine.on_external = drift_external
        with self.assertRaisesRegex(initializer.InitializerError, "trust snapshot changed"):
            initializer.AncestorInitializer(FakePhase, engine).initialize(OLD, "test")
        self.assertIsNone(engine.written_state)

    def test_existing_state_is_rejected_without_replacement(self):
        self.engine.runtime_root.state = b"existing"
        with self.assertRaisesRegex(initializer.InitializerError, "already initialized"):
            self.subject.initialize(OLD, "test")
        self.assertIsNone(self.engine.written_state)

    def test_cli_exposes_only_initialize_and_no_path_or_live_bypass(self):
        for arguments in (
            ["plan"],
            ["initialize", "--deployed-commit", OLD, "--confirm-environment", "test",
             "--source-root", "/tmp/forged"],
            ["initialize", "--deployed-commit", OLD, "--confirm-environment", "test",
             "--verify-live-origin", "false"],
        ):
            with self.subTest(arguments=arguments), contextlib.redirect_stderr(
                    io.StringIO()), self.assertRaises(SystemExit):
                initializer.main(arguments)

    def test_verified_phase_one_is_compiled_from_the_locked_bytes(self):
        own = b"initializer"
        phase = b"LOCKED_VALUE = 42\n"
        boundary = b"def load_contract(raw):\n return {'locked': raw.decode()}\n"
        boundary_contract = b"{}\n"
        launcher = b"launcher"
        bootstrap = {
            "initializer_sha256": hashlib.sha256(own).hexdigest(),
            "phase_one_sha256": hashlib.sha256(phase).hexdigest(),
            "boundary_sha256": hashlib.sha256(boundary).hexdigest(),
            "boundary_contract_sha256": hashlib.sha256(boundary_contract).hexdigest(),
            "launcher_sha256": hashlib.sha256(launcher).hexdigest(),
        }
        with mock.patch.object(initializer, "_runtime_is_isolated", return_value=True), \
                mock.patch.object(initializer, "_load_bootstrap", return_value=bootstrap), \
                mock.patch.object(
                    initializer, "_locked_regular",
                    side_effect=[own, phase, boundary, boundary_contract, launcher],
                ):
            module = initializer._load_verified_phase_one()
        self.assertEqual(42, module.LOCKED_VALUE)
        self.assertEqual("{}\n", module._external_boundary_contract["locked"])

    def test_hash_mismatch_and_unisolated_runtime_fail_closed(self):
        with mock.patch.object(initializer, "_runtime_is_isolated", return_value=False), \
                self.assertRaisesRegex(initializer.InitializerError, "not isolated"):
            initializer._load_verified_phase_one()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "locked.py"
            path.write_bytes(b"actual")
            with mock.patch.object(initializer, "_validate_root_chain"), \
                    self.assertRaisesRegex(initializer.InitializerError, "SHA-256"):
                initializer._locked_regular(path, "0" * 64, "locked file")

    def test_isolated_runtime_accepts_approved_python_symlink_target(self):
        flags = types.SimpleNamespace(
            isolated=1, ignore_environment=1, no_user_site=1, dont_write_bytecode=1,
        )
        environment = dict(initializer.EXPECTED_ENVIRONMENT)

        def realpath(value):
            value = str(value)
            if value in {"/usr/bin/python3", "/usr/bin/python3.10"}:
                return "/usr/bin/python3.10"
            return value

        with mock.patch.object(initializer.os, "name", "posix"), \
                mock.patch.object(initializer.os, "geteuid", return_value=0, create=True), \
                mock.patch.object(initializer.os.path, "realpath", side_effect=realpath), \
                mock.patch.object(initializer.sys, "executable", "/usr/bin/python3"), \
                mock.patch.object(initializer.sys, "flags", flags), \
                mock.patch.object(initializer, "__file__", initializer.INITIALIZER_ENTRYPOINT), \
                mock.patch.dict(initializer.os.environ, environment, clear=True):
            self.assertTrue(initializer._runtime_is_isolated())

    def test_isolated_runtime_rejects_other_interpreter_flags_environment_and_path(self):
        good_flags = types.SimpleNamespace(
            isolated=1, ignore_environment=1, no_user_site=1, dont_write_bytecode=1,
        )
        cases = (
            ("executable", "/opt/unapproved/python3", good_flags,
             dict(initializer.EXPECTED_ENVIRONMENT), initializer.INITIALIZER_ENTRYPOINT),
            ("flags", "/usr/bin/python3", types.SimpleNamespace(
                isolated=0, ignore_environment=1, no_user_site=1, dont_write_bytecode=1,
            ), dict(initializer.EXPECTED_ENVIRONMENT), initializer.INITIALIZER_ENTRYPOINT),
            ("environment", "/usr/bin/python3", good_flags,
             {**initializer.EXPECTED_ENVIRONMENT, "EXTRA": "1"},
             initializer.INITIALIZER_ENTRYPOINT),
            ("path", "/usr/bin/python3", good_flags,
             dict(initializer.EXPECTED_ENVIRONMENT), "/tmp/replaced.py"),
        )

        def realpath(value):
            value = str(value)
            return "/usr/bin/python3.10" if value == "/usr/bin/python3" else value

        for label, executable, flags, environment, loaded_path in cases:
            with self.subTest(label=label), \
                    mock.patch.object(initializer.os, "name", "posix"), \
                    mock.patch.object(initializer.os, "geteuid", return_value=0, create=True), \
                    mock.patch.object(initializer.os.path, "realpath", side_effect=realpath), \
                    mock.patch.object(initializer.sys, "executable", executable), \
                    mock.patch.object(initializer.sys, "flags", flags), \
                    mock.patch.object(initializer, "__file__", loaded_path), \
                    mock.patch.dict(initializer.os.environ, environment, clear=True):
                self.assertFalse(initializer._runtime_is_isolated())

    def test_checked_in_hashes_launcher_isolation_and_old_trust_roots(self):
        entrypoint = (ROOT / "tools/test_release_ancestor_initializer_v1.py").read_bytes()
        launcher = (ROOT / "tools/test_release_ancestor_initializer_v1_launcher.sh").read_bytes()
        boundary = (ROOT / "tools/test_release_external_boundaries_v1.py").read_bytes()
        boundary_contract = (ROOT / "tools/test_release_external_boundaries_v1.json").read_bytes()
        doc = (ROOT / "docs/test-release-ancestor-initializer-v1.md").read_text("utf-8")
        self.assertIn(hashlib.sha256(entrypoint).hexdigest(), doc)
        self.assertIn(hashlib.sha256(launcher).hexdigest(), doc)
        self.assertIn(hashlib.sha256(boundary).hexdigest(), doc)
        self.assertIn(hashlib.sha256(boundary_contract).hexdigest(), doc)
        self.assertIn(
            "EXPECTED_ENTRYPOINT_SHA256=" + hashlib.sha256(entrypoint).hexdigest(),
            launcher.decode("utf-8"),
        )
        self.assertIn("exec /usr/bin/env -i", launcher.decode("utf-8"))
        self.assertIn("/usr/bin/python3 -I -E -s -B", launcher.decode("utf-8"))
        for path in (
            "deploy/test-release/bootstrap.example.json",
            "scripts/release_test.py",
            "scripts/release_test_launcher.sh",
        ):
            current = (ROOT / path).read_bytes()
            base = subprocess.run(
                ["git", "show", "%s:%s" % (BASE, path)],
                cwd=ROOT, check=True, capture_output=True,
            ).stdout
            self.assertEqual(base, current, path)

        protected_paths = (
            ".github/workflows",
            "deploy/test-release/runtime-catalog.json",
            "server/content_domains/core.py",
            "server/content_domains/private_domain_media.py",
            "site/workbench/private-domain-video.html",
        )
        for path in protected_paths:
            result = subprocess.run(
                ["git", "diff", "--quiet", BASE, "--", path], cwd=ROOT,
            )
            self.assertEqual(0, result.returncode, path)


if __name__ == "__main__":
    unittest.main()
