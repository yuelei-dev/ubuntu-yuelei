import hashlib
import importlib.util
import os
import stat
import tempfile
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "test_release_transaction", ROOT / "tools" / "test_release_transaction.py"
)
transaction = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(transaction)

BASE = "1" * 40
HEAD = "2" * 40
MERGE = "3" * 40
RUNTIME = "/srv/example.py"
SOURCE = "server/example.py"
SECOND_RUNTIME = "/srv/second.py"
SECOND_SOURCE = "server/second.py"
OLD = b"old\n"
NEW = b"new\n"
SECOND_OLD = b"second-old\n"
SECOND_NEW = b"second-new\n"


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


EVIDENCE = {"pr-1": {"base": BASE, "head": HEAD}}
PLANNED_EVIDENCE = {
    "pr-1": {
        "merge_base": BASE,
        "head": HEAD,
        "merge_commit": MERGE,
        "impact_sha256": "4" * 64,
    }
}


class FakeRepo:
    def __init__(self):
        self.files = {(MERGE, SOURCE): NEW}

    def file_at(self, commit, path):
        return self.files.get((commit, path))


class FakeCatalog:
    allowed_units = {"example.service"}
    health_probes = {"example-pre": {}, "example-post": {}}


class FakePlanner:
    def __init__(self, hooks):
        self.repo = FakeRepo()
        self.catalog = FakeCatalog()
        self.hooks = hooks
        self.written_states = []
        self.state = {
            "deployed_main_commit": BASE,
            "accepted_impacts": {},
            "runtime_hashes": {RUNTIME: {"state": "file", "sha256": digest(OLD)}},
            "repository_paths": {RUNTIME: SOURCE},
            "runtime_metadata": {RUNTIME: {"mode": 0o644, "owner": "tester", "group": "tester"}},
            "managed_runtime_paths": [RUNTIME],
            "last_release_id": None,
            "last_successful_release": None,
        }

    def build_plan(self, target, evidence):
        return {
            "ok": True,
            "status": "planned_read_only",
            "environment": "test",
            "host_id": "test-01",
            "from_commit": BASE,
            "target_commit": target,
            "files": [{
                "repository_path": SOURCE,
                "runtime_path": RUNTIME,
                "change": "write",
                "before": {"state": "file", "sha256": digest(OLD)},
                "after": {"state": "file", "sha256": digest(NEW)},
                "services": ["example.service"],
                "mode": 0o640,
                "owner": "tester",
                "group": "tester",
                "daemon_reload": False,
            }],
            "review_evidence": PLANNED_EVIDENCE,
            "restart_services": ["example.service"],
            "pre_health_probes": ["example-pre"],
            "post_health_probes": ["example-post"],
        }

    def load_state(self):
        return dict(self.state)

    def _expected_runtime(self, target):
        assert target == MERGE
        return (
            {RUNTIME: {"state": "file", "sha256": digest(NEW)}},
            {RUNTIME: SOURCE},
            {RUNTIME: {"mode": 0o640, "owner": "tester", "group": "tester"}},
        )

    def collect_impact_index(self, target):
        return {"pr-1": {"repository_path": "deploy/test-release/impacts/pr-1.json", "sha256": "4" * 64}}

    def verify_target_inventory(self, target):
        return target == MERGE

    def _write_state(self, state):
        self.hooks.events.append("ledger")
        self.state = state
        self.written_states.append(state)


class FakeHooks:
    def __init__(self):
        self.files = {RUNTIME: OLD}
        self.modes = {RUNTIME: 0o644}
        self.events = []
        self.fail_restart = False
        self.fail_post_health = False
        self.fail_rollback_health = False
        self.fail_restore = False

    @staticmethod
    def _info(mode):
        return types.SimpleNamespace(st_mode=stat.S_IFREG | mode, st_uid=os.getuid() if hasattr(os, "getuid") else 0,
                                     st_gid=os.getgid() if hasattr(os, "getgid") else 0)

    def read(self, path):
        raw = self.files.get(path)
        return None if raw is None else (raw, self._info(self.modes[path]))

    def atomic_write(self, path, raw, mode, owner, group):
        if self.fail_restore and raw == OLD:
            raise OSError("restore failed")
        self.events.append("write:" + raw.decode().strip())
        self.files[path] = raw
        self.modes[path] = mode

    def delete(self, path):
        self.events.append("delete")
        self.files.pop(path, None)
        self.modes.pop(path, None)

    def daemon_reload(self):
        self.events.append("daemon-reload")

    def restart(self, unit):
        self.events.append("restart:" + unit)
        if self.fail_restart:
            self.fail_restart = False
            raise OSError("restart failed")

    def health(self, probe, phase):
        self.events.append("health:%s:%s" % (phase, probe))
        if phase == "post" and self.fail_post_health:
            raise OSError("health failed")
        if phase == "rollback" and self.fail_rollback_health:
            raise OSError("rollback health failed")


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.hooks = FakeHooks()
        self.planner = FakePlanner(self.hooks)
        self.executor = transaction.TransactionExecutor(
            self.planner,
            runtime_root=Path(self.temporary.name) / "runtime",
            state_root=Path(self.temporary.name) / "state",
            hooks=self.hooks,
            clock=lambda: 1234,
        )

    def test_success_backs_up_writes_restarts_health_then_commits_ledger(self):
        result = self.executor.apply(MERGE, EVIDENCE)
        self.assertEqual("deployed", result["status"])
        self.assertEqual(NEW, self.hooks.files[RUNTIME])
        self.assertEqual(0o640, self.hooks.modes[RUNTIME])
        self.assertLess(self.hooks.events.index("health:post:example-post"), self.hooks.events.index("ledger"))
        self.assertEqual(MERGE, self.planner.state["deployed_main_commit"])
        self.assertFalse(self.executor.journal_path.exists())

    def test_partial_write_process_crash_is_recovered_from_persistent_backup(self):
        original_plan = self.planner.build_plan
        self.planner.repo.files[(MERGE, SECOND_SOURCE)] = SECOND_NEW
        self.hooks.files[SECOND_RUNTIME] = SECOND_OLD
        self.hooks.modes[SECOND_RUNTIME] = 0o600

        def two_file_plan(target, evidence):
            plan = original_plan(target, evidence)
            plan["files"].append({
                "repository_path": SECOND_SOURCE,
                "runtime_path": SECOND_RUNTIME,
                "change": "write",
                "before": {"state": "file", "sha256": digest(SECOND_OLD)},
                "after": {"state": "file", "sha256": digest(SECOND_NEW)},
                "services": ["example.service"],
                "mode": 0o600,
                "owner": "tester",
                "group": "tester",
                "daemon_reload": False,
            })
            return plan

        self.planner.build_plan = two_file_plan
        fired = {"value": False}

        def crash(point):
            if point == "after-write" and not fired["value"]:
                fired["value"] = True
                raise transaction.CrashInjection()

        self.executor.crash_hook = crash
        with self.assertRaises(transaction.CrashInjection):
            self.executor.apply(MERGE, EVIDENCE)
        self.assertEqual(NEW, self.hooks.files[RUNTIME])
        self.assertEqual(SECOND_OLD, self.hooks.files[SECOND_RUNTIME])
        self.assertTrue(self.executor.journal_path.exists())
        self.executor.crash_hook = lambda _point: None
        result = self.executor.recover()
        self.assertEqual("rolled_back_recovered", result["status"])
        self.assertEqual(OLD, self.hooks.files[RUNTIME])
        self.assertEqual(SECOND_OLD, self.hooks.files[SECOND_RUNTIME])
        self.assertFalse(self.executor.journal_path.exists())

    def test_restart_failure_rolls_back_every_file_and_keeps_old_ledger(self):
        self.hooks.fail_restart = True
        with self.assertRaisesRegex(transaction.TransactionError, "rolled back"):
            self.executor.apply(MERGE, EVIDENCE)
        self.assertEqual(OLD, self.hooks.files[RUNTIME])
        self.assertEqual(BASE, self.planner.state["deployed_main_commit"])
        self.assertIn("health:rollback:example-pre", self.hooks.events)

    def test_post_health_failure_rolls_back_before_ledger_commit(self):
        self.hooks.fail_post_health = True
        with self.assertRaisesRegex(transaction.TransactionError, "rolled back"):
            self.executor.apply(MERGE, EVIDENCE)
        self.assertEqual(OLD, self.hooks.files[RUNTIME])
        self.assertNotIn("ledger", self.hooks.events)

    def test_complete_inventory_drift_after_health_rolls_back(self):
        self.planner.verify_target_inventory = lambda _target: False
        with self.assertRaisesRegex(transaction.TransactionError, "rolled back"):
            self.executor.apply(MERGE, EVIDENCE)
        self.assertEqual(OLD, self.hooks.files[RUNTIME])
        self.assertNotIn("ledger", self.hooks.events)

    def test_rollback_failure_is_persisted_for_operator_recovery(self):
        self.hooks.fail_post_health = True
        self.hooks.fail_restore = True
        self.hooks.fail_rollback_health = True
        with self.assertRaisesRegex(transaction.TransactionError, "rollback failed"):
            self.executor.apply(MERGE, EVIDENCE)
        journal = self.executor._load_journal()
        self.assertEqual("rollback_failed", journal["status"])
        self.assertEqual(BASE, self.planner.state["deployed_main_commit"])

    def test_pre_health_failure_occurs_before_backup_or_write(self):
        original = self.hooks.health

        def fail_pre(probe, phase):
            original(probe, phase)
            if phase == "pre":
                raise OSError("pre failed")

        self.hooks.health = fail_pre
        with self.assertRaises(OSError):
            self.executor.apply(MERGE, EVIDENCE)
        self.assertEqual(OLD, self.hooks.files[RUNTIME])
        self.assertFalse(self.executor.transactions.exists())

    def test_reviewed_head_evidence_must_match_planner_topology(self):
        with self.assertRaisesRegex(transaction.TransactionError, "reviewed-head topology"):
            self.executor.apply(MERGE, {"pr-1": {"base": "9" * 40, "head": HEAD}})
        self.assertEqual([], self.hooks.events)

    def test_preimage_mismatch_fails_before_journal_and_mutation(self):
        self.hooks.files[RUNTIME] = b"drift\n"
        with self.assertRaisesRegex(transaction.TransactionError, "locked before"):
            self.executor.apply(MERGE, EVIDENCE)
        self.assertFalse(self.executor.journal_path.exists())
        self.assertNotIn("write:new", self.hooks.events)

    def test_non_allowlisted_service_is_rejected(self):
        original = self.planner.build_plan

        def bad_plan(target, evidence):
            plan = original(target, evidence)
            plan["restart_services"] = ["attacker.service"]
            return plan

        self.planner.build_plan = bad_plan
        with self.assertRaisesRegex(transaction.TransactionError, "non-allowlisted"):
            self.executor.apply(MERGE, EVIDENCE)

    def test_crash_after_ledger_commit_recovers_as_committed_without_rollback(self):
        def crash(point):
            if point == "after-ledger":
                raise transaction.CrashInjection()

        self.executor.crash_hook = crash
        with self.assertRaises(transaction.CrashInjection):
            self.executor.apply(MERGE, EVIDENCE)
        self.assertEqual(MERGE, self.planner.state["deployed_main_commit"])
        self.executor.crash_hook = lambda _point: None
        result = self.executor.recover()
        self.assertEqual("committed_recovered", result["status"])
        self.assertEqual(NEW, self.hooks.files[RUNTIME])

    def test_installed_entrypoint_sha_lock_rejects_tampering(self):
        path = Path(self.temporary.name) / "entrypoint.py"
        path.write_bytes(b"reviewed\n")
        transaction._locked_regular(path, digest(b"reviewed\n"), "entrypoint")
        path.write_bytes(b"tampered\n")
        with self.assertRaisesRegex(transaction.TransactionError, "SHA-256"):
            transaction._locked_regular(path, digest(b"reviewed\n"), "entrypoint")


if __name__ == "__main__":
    unittest.main()
