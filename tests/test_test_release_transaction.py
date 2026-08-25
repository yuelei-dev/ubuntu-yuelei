import hashlib
import importlib.util
import json
import os
import shutil
import stat
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


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
    health_probes = {"example-pre": {}, "example-post": {}, "site-loopback": {}, "content-health": {}}


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
                "before_metadata": {"mode": 0o644, "owner": "tester", "group": "tester"},
                "mode": 0o640,
                "owner": "tester",
                "group": "tester",
                "daemon_reload": False,
            }],
            "review_evidence": PLANNED_EVIDENCE,
            "restart_services": ["example.service"],
            "pre_health_probes": ["example-pre"],
            "post_health_probes": ["example-post"],
            "required_free_bytes": 1,
        }

    def verify_identity(self):
        return {
            "schema_version": 1,
            "environment": "test",
            "host_id": "test-01",
            "hostname": "test-host",
            "machine_id_sha256": "5" * 64,
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

    def verify_services(self, services):
        if services != ["example.service"]:
            raise RuntimeError("service mismatch")

    def trusted_transaction_contract(self, older, target, evidence):
        if older != BASE or target != MERGE or evidence != EVIDENCE:
            raise RuntimeError("contract mismatch")
        plan = self.build_plan(target, evidence)
        return {key: plan[key] for key in (
            "files", "restart_services", "pre_health_probes", "post_health_probes",
        )}

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

    @staticmethod
    def owner_group(_info):
        return "tester", "tester"

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
            enforce_root_paths=False,
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
                "before_metadata": {"mode": 0o600, "owner": "tester", "group": "tester"},
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
        self.assertFalse(self.executor.journal_path.exists())
        self.assertEqual([], list(self.executor.transactions.iterdir()))

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
        with mock.patch.object(transaction, "_validate_root_chain"):
            transaction._locked_regular(path, digest(b"reviewed\n"), "entrypoint")
            path.write_bytes(b"tampered\n")
            with self.assertRaisesRegex(transaction.TransactionError, "SHA-256"):
                transaction._locked_regular(path, digest(b"reviewed\n"), "entrypoint")

    def test_real_pr302_impact_named_health_runs_pre_and_post(self):
        impact = json.loads((
            ROOT / "deploy/test-release/impacts/pr-private-domain-skill-ui-v2-20260825.json"
        ).read_text("utf-8"))
        self.assertEqual(3, len(impact["runtime_changes"]))
        self.assertEqual(["content-health", "site-loopback"], impact["pre_health_checks"])
        original = self.planner.build_plan

        def plan(target, evidence):
            value = original(target, evidence)
            value["pre_health_probes"] = list(impact["pre_health_checks"])
            value["post_health_probes"] = list(impact["health_checks"])
            return value

        self.planner.build_plan = plan
        self.executor.apply(MERGE, EVIDENCE)
        for phase in ("pre", "post"):
            self.assertIn("health:%s:content-health" % phase, self.hooks.events)
            self.assertIn("health:%s:site-loopback" % phase, self.hooks.events)

    def test_real_pr302_impact_rollback_reuses_both_named_pre_health_contracts(self):
        impact = json.loads((
            ROOT / "deploy/test-release/impacts/pr-private-domain-skill-ui-v2-20260825.json"
        ).read_text("utf-8"))
        original = self.planner.build_plan

        def plan(target, evidence):
            value = original(target, evidence)
            value["pre_health_probes"] = list(impact["pre_health_checks"])
            value["post_health_probes"] = list(impact["health_checks"])
            return value

        self.planner.build_plan = plan
        self.hooks.fail_post_health = True
        with self.assertRaisesRegex(transaction.TransactionError, "rolled back"):
            self.executor.apply(MERGE, EVIDENCE)
        self.assertIn("health:rollback:content-health", self.hooks.events)
        self.assertIn("health:rollback:site-loopback", self.hooks.events)

    def test_site_loopback_uses_fixed_address_host_path_and_response_bounds(self):
        sent = []

        class Wrapped:
            def sendall(self, raw):
                sent.append(raw)

        class Context:
            def wrap_socket(self, raw, server_hostname):
                self.server_hostname = server_hostname
                return Wrapped()

        class Response:
            status = 200

            def __init__(self, _connection):
                pass

            def begin(self):
                pass

            def getheader(self, name, default=""):
                return {"Content-Type": "text/html; charset=utf-8", "X-Content-Type-Options": "nosniff"}.get(name, default)

            def read(self, _limit):
                return b"<!doctype html><title>private domain</title>"

        context = Context()
        raw_socket = mock.Mock()
        with mock.patch.object(transaction.ssl, "create_default_context", return_value=context), \
                mock.patch.object(transaction.socket, "create_connection", return_value=raw_socket) as connect, \
                mock.patch.object(transaction.http.client, "HTTPResponse", Response):
            transaction.HostHooks().health("site-loopback", "pre")
        connect.assert_called_once_with(("127.0.0.1", 443), timeout=10)
        self.assertEqual("huangquechuanmei.com", context.server_hostname)
        request = sent[0].decode("ascii")
        self.assertIn("GET /workbench/private-domain-video.html HTTP/1.1", request)
        self.assertIn("Host: huangquechuanmei.com", request)

    def test_site_loopback_rejects_redirect_or_non_html_response(self):
        class Context:
            def wrap_socket(self, raw, server_hostname):
                return mock.Mock()

        class BadResponse:
            status = 302

            def __init__(self, _connection):
                pass

            def begin(self):
                pass

            def getheader(self, name, default=""):
                return {"Content-Type": "text/plain", "X-Content-Type-Options": ""}.get(name, default)

            def read(self, _limit):
                return b"redirect"

        with mock.patch.object(transaction.ssl, "create_default_context", return_value=Context()), \
                mock.patch.object(transaction.socket, "create_connection", return_value=mock.Mock()), \
                mock.patch.object(transaction.http.client, "HTTPResponse", BadResponse):
            with self.assertRaisesRegex(transaction.TransactionError, "response contract"):
                transaction.HostHooks().health("site-loopback", "post")

    def test_launcher_requires_empty_environment_and_all_python_isolation_flags(self):
        launcher = (ROOT / "tools/test_release_transaction_launcher.sh").read_text("utf-8")
        self.assertIn("/usr/bin/env -i", launcher)
        self.assertIn("/usr/bin/python3 -I -E -s -B", launcher)
        self.assertIn(transaction.TRANSACTION_ENTRYPOINT, launcher)
        self.assertFalse(transaction._runtime_environment_is_isolated())

    def test_isolated_runtime_rejects_wrong_loaded_script_path(self):
        flags = types.SimpleNamespace(
            isolated=1, ignore_environment=1, no_user_site=1, dont_write_bytecode=1,
        )
        environment = {
            "HOME": "/root", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PATH": "/usr/bin:/bin",
        }
        with mock.patch.object(transaction.os, "name", "posix"), \
                mock.patch.object(transaction.os, "geteuid", return_value=0, create=True), \
                mock.patch.object(transaction.os.path, "realpath", side_effect=lambda value: str(value)), \
                mock.patch.object(transaction.sys, "executable", "/usr/bin/python3"), \
                mock.patch.object(transaction.sys, "flags", flags), \
                mock.patch.object(transaction, "__file__", "/tmp/replaced.py"), \
                mock.patch.dict(transaction.os.environ, environment, clear=True):
            self.assertFalse(transaction._runtime_environment_is_isolated())

    def test_writable_trusted_parent_is_rejected(self):
        fake = types.SimpleNamespace(st_mode=stat.S_IFDIR | 0o777, st_uid=0)
        with mock.patch.object(transaction.os, "lstat", return_value=fake):
            with self.assertRaisesRegex(transaction.TransactionError, "root-owned and immutable"):
                transaction._validate_root_chain("/trusted/file", final_kind="file", private=False)

    def _leave_active(self, point="after-backup"):
        def crash(actual):
            if actual == point:
                raise transaction.CrashInjection()

        self.executor.crash_hook = crash
        with self.assertRaises(transaction.CrashInjection):
            self.executor.apply(MERGE, EVIDENCE)
        self.executor.crash_hook = lambda _point: None
        return json.loads(self.executor.journal_path.read_text("utf-8"))

    def _write_journal(self, journal):
        self.executor.journal_path.write_text(
            json.dumps(journal, ensure_ascii=False, sort_keys=True, indent=2) + "\n", "utf-8",
        )

    def test_malicious_journal_transaction_path_unit_runtime_and_sets_fail_without_mutation(self):
        mutations = [
            lambda value: value.update(transaction_id="../escape"),
            lambda value: value.update(transaction_id="release-%s-%s" % ("a" * 12, "b" * 12)),
            lambda value: value["restart_services"].append("attacker.service"),
            lambda value: value["files"][0]["plan"].update(runtime_path="/etc/shadow"),
            lambda value: value["written"].append("/etc/shadow"),
            lambda value: value["files"][0].update(backup="../../escape"),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                if self.executor.journal_path.exists():
                    self.executor.recover()
                self.hooks.files[RUNTIME] = OLD
                self.planner.state["deployed_main_commit"] = BASE
                journal = self._leave_active()
                mutate(journal)
                self._write_journal(journal)
                before_events = list(self.hooks.events)
                with self.assertRaises(transaction.TransactionError):
                    self.executor.recover()
                self.assertEqual(OLD, self.hooks.files[RUNTIME])
                self.assertEqual(before_events, self.hooks.events)
                self.executor.journal_path.unlink()
                shutil_target = self.executor.transactions / journal.get("transaction_id", "")
                if shutil_target.parent == self.executor.transactions and shutil_target.exists():
                    shutil.rmtree(shutil_target)

    def test_committed_recovery_retains_backup_when_postimage_or_inventory_drifted(self):
        self._leave_active("after-ledger")
        self.hooks.files[RUNTIME] = b"tampered\n"
        with self.assertRaises(transaction.TransactionError):
            self.executor.recover()
        self.assertTrue(self.executor.journal_path.exists())
        self.assertTrue(any(self.executor.transactions.glob("release-*/backups/*.bin")))
        self.hooks.files[RUNTIME] = NEW
        self.planner.verify_target_inventory = lambda _target: False
        with self.assertRaisesRegex(transaction.TransactionError, "inventory"):
            self.executor.recover()
        self.assertTrue(self.executor.journal_path.exists())

    def test_backup_midflight_crash_has_discoverable_journal_and_recovers(self):
        self._leave_active("during-backup")
        self.assertEqual("backing_up", json.loads(self.executor.journal_path.read_text("utf-8"))["status"])
        result = self.executor.recover()
        self.assertEqual("prewrite_recovered", result["status"])
        self.assertEqual(OLD, self.hooks.files[RUNTIME])

    def test_cleanup_crash_leaves_recoverable_journal_without_orphan(self):
        self._leave_active("after-backup-cleanup")
        self.assertTrue(self.executor.journal_path.exists())
        self.assertEqual([], list(self.executor.transactions.glob("release-*")))
        result = self.executor.recover()
        self.assertEqual("committed_recovered", result["status"])
        self.assertFalse(self.executor.journal_path.exists())

    def test_no_journal_orphan_is_cleaned_under_lock(self):
        orphan = self.executor.transactions / ("release-%s-%s" % (MERGE[:12], "a" * 12))
        (orphan / "backups").mkdir(parents=True)
        (orphan / "backups/0000.bin").write_bytes(OLD)
        result = self.executor.recover()
        self.assertEqual("nothing_to_recover", result["status"])
        self.assertFalse(orphan.exists())

    def test_drift_after_backup_is_caught_before_first_write(self):
        def drift(point):
            if point == "during-backup":
                self.hooks.files[RUNTIME] = b"third-party\n"

        self.executor.crash_hook = drift
        with self.assertRaisesRegex(transaction.TransactionError, "locked before"):
            self.executor.apply(MERGE, EVIDENCE)
        self.assertNotIn("write:new", self.hooks.events)
        self.assertTrue(self.executor.journal_path.exists())

    def test_crash_after_write_before_journal_is_still_recovered(self):
        self._leave_active("after-write-before-journal")
        journal = json.loads(self.executor.journal_path.read_text("utf-8"))
        self.assertEqual([], journal["written"])
        self.assertEqual(NEW, self.hooks.files[RUNTIME])
        self.executor.recover()
        self.assertEqual(OLD, self.hooks.files[RUNTIME])

    def test_recovery_refuses_unwritten_or_written_third_party_state_without_overwrite(self):
        journal = self._leave_active("after-backup")
        self.hooks.files[RUNTIME] = b"third-party\n"
        with self.assertRaisesRegex(transaction.TransactionError, "neither-before-nor-after"):
            self.executor.recover()
        self.assertEqual(b"third-party\n", self.hooks.files[RUNTIME])
        self.assertEqual("rollback_failed", json.loads(self.executor.journal_path.read_text("utf-8"))["status"])

    def test_recovery_refuses_third_party_replacement_after_transaction_write(self):
        self._leave_active("after-write-before-journal")
        self.hooks.files[RUNTIME] = b"newer-third-party\n"
        with self.assertRaisesRegex(transaction.TransactionError, "neither-before-nor-after"):
            self.executor.recover()
        self.assertEqual(b"newer-third-party\n", self.hooks.files[RUNTIME])

    def test_unwritten_third_party_file_prevents_any_multi_file_restore(self):
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
                "before_metadata": {"mode": 0o600, "owner": "tester", "group": "tester"},
                "mode": 0o600,
                "owner": "tester",
                "group": "tester",
                "daemon_reload": False,
            })
            return plan

        self.planner.build_plan = two_file_plan
        self._leave_active("after-write")
        self.assertEqual(NEW, self.hooks.files[RUNTIME])
        self.hooks.files[SECOND_RUNTIME] = b"third-party-second\n"
        with self.assertRaisesRegex(transaction.TransactionError, "neither-before-nor-after"):
            self.executor.recover()
        self.assertEqual(NEW, self.hooks.files[RUNTIME])
        self.assertEqual(b"third-party-second\n", self.hooks.files[SECOND_RUNTIME])


if __name__ == "__main__":
    unittest.main()
