import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from content_domains import jobs_store  # noqa: E402


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class RuntimeSourceAndExclusionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads(
            (ROOT / "deploy/runtime-manifest.json").read_text(encoding="utf-8")
        )

    def test_runtime_identity_and_reviewed_overlay_sources_are_pinned(self):
        self.assertEqual(self.manifest["server_role"], "test-only")
        self.assertFalse(self.manifest["production_connected"])
        classes = {}
        for entry in self.manifest["files"]:
            classes[entry["classification"]] = classes.get(
                entry["classification"], 0
            ) + 1
        self.assertEqual(
            classes,
            {
                "production_baseline_snapshot": 296,
                "overlay_pr141": 3,
                "overlay_pr142": 1,
                "runtime_only_preserved": 5,
            },
        )
        by_path = {item["git_path"]: item for item in self.manifest["files"]}
        for path in (
            "server/content_domains/breakdown.py",
            "server/content_domains/egress.py",
            "server/tikhub.py",
        ):
            self.assertEqual(by_path[path]["classification"], "overlay_pr141")
        self.assertEqual(
            by_path["site/workbench/script.html"]["classification"],
            "overlay_pr142",
        )

    def test_env_contract_contains_keys_only_and_no_secret_values(self):
        contract = json.loads(
            (ROOT / "deploy/runtime-test/env-contract.json").read_text(
                encoding="utf-8"
            )
        )
        rendered = json.dumps(contract, ensure_ascii=False)
        self.assertNotRegex(rendered, r"(?i)(api[_-]?key|secret|password|token)\s*[:=]\s*[^\"{\[]")
        self.assertNotIn("-----BEGIN", rendered)
        self.assertNotIn("sk-", rendered)

    def test_manifest_excludes_every_external_state_class(self):
        exclusions = "\n".join(self.manifest["excluded_external_state"]).lower()
        for value in (
            "env",
            "password",
            "api keys",
            "model credentials",
            "tls",
            "db",
            "content_out",
            "uploads",
            "generated",
            "logs",
            "caches",
            "user",
            "transaction",
        ):
            self.assertIn(value, exclusions)

    def test_service_and_nginx_contracts_are_sanitized_regular_files(self):
        files = sorted((ROOT / "deploy/runtime-test/systemd").glob("*.conf"))
        files += sorted((ROOT / "deploy/runtime-test/nginx").glob("*.conf"))
        self.assertEqual(len(files), 8)
        for path in files:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("-----BEGIN", text, path.name)
            self.assertNotRegex(text, r"(?i)(password|api[_-]?key|secret|token)=\S+")
            self.assertTrue(os.path.isfile(path))
            self.assertFalse(os.stat(path).st_mode & 0o111, path.name)


class ScriptStampSemanticsTests(unittest.TestCase):
    def test_only_cache_stamp_changed_and_manifest_tracks_new_bytes(self):
        evidence = json.loads(
            (ROOT / "deploy/runtime-test/script-stamp-evidence.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(evidence["semantic_diff"], 0)
        self.assertEqual(
            evidence["before_normalized_sha256"],
            evidence["after_normalized_sha256"],
        )
        script = ROOT / evidence["file"]
        self.assertEqual(sha256(script), evidence["after_sha256"])
        text = script.read_text(encoding="utf-8")
        self.assertIn(f"cloud-shell.js?v={evidence['new_stamp']}", text)
        manifest = json.loads(
            (ROOT / "deploy/runtime-manifest.json").read_text(encoding="utf-8")
        )
        entry = next(
            item for item in manifest["files"] if item["git_path"] == evidence["file"]
        )
        self.assertEqual(entry["sha256"], evidence["after_sha256"])


class JobsRefundRuntimeContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "jobs.db"
        with closing(self.db()) as conn:
            conn.execute(
                """CREATE TABLE jobs(
                   id INTEGER PRIMARY KEY,kind TEXT,username TEXT,cost INTEGER,
                   status TEXT,result TEXT,error TEXT,created_at INTEGER,
                   updated_at INTEGER,deleted INTEGER DEFAULT 0,
                   refunded INTEGER DEFAULT 0,owner TEXT)"""
            )
            conn.execute(
                """INSERT INTO jobs(
                   id,kind,username,cost,status,created_at,updated_at,refunded)
                   VALUES(1,'breakdown','tester',20,'running',1,1,0)"""
            )
            conn.commit()

    def tearDown(self):
        self.temp.cleanup()

    def db(self):
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def refunded(self):
        with closing(self.db()) as conn:
            return conn.execute(
                "SELECT refunded FROM jobs WHERE id=1"
            ).fetchone()["refunded"]

    def test_error_refund_state_is_exactly_zero_to_two_to_one(self):
        self.assertEqual(self.refunded(), 0)
        self.assertTrue(
            jobs_store.set_terminal(
                self.db, 1, "error", error="test", from_states=("running",)
            )
        )
        self.assertEqual(self.refunded(), 2)
        self.assertTrue(jobs_store.refund_once(self.db, 1, "tester", 20, lambda *_: True))
        self.assertEqual(self.refunded(), 1)

    def test_concurrent_refund_has_one_financial_effect_and_final_state_one(self):
        jobs_store.set_terminal(
            self.db, 1, "error", error="test", from_states=("running",)
        )
        lock = threading.Lock()
        financial_effects = set()

        def idempotent_auth_refund(username, cost):
            with lock:
                financial_effects.add(("job-refund:tester:1", username, cost))
            return True

        threads = [
            threading.Thread(
                target=jobs_store.refund_once,
                args=(self.db, 1, "tester", 20, idempotent_auth_refund),
            )
            for _ in range(8)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(financial_effects, {("job-refund:tester:1", "tester", 20)})
        self.assertEqual(self.refunded(), 1)


class RuntimeWorkflowRoutingTests(unittest.TestCase):
    def test_main_ci_is_unchanged_except_runtime_test_job_exclusion(self):
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertIn(
            "github.event_name != 'pull_request' || github.base_ref != 'runtime/test'",
            workflow,
        )
        self.assertIn("python -m unittest discover -s tests -v", workflow)
        self.assertIn("push:\n    branches: [main]", workflow)

    def test_runtime_workflow_is_scoped_only_to_runtime_test(self):
        workflow = (
            ROOT / ".github/workflows/runtime-test-baseline.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("branches: [runtime/test]", workflow)
        self.assertNotIn("branches: [main]", workflow)
        self.assertIn("blocking-targets.txt", workflow)
        self.assertIn("full-diagnostic-targets.txt", workflow)
        self.assertIn("--discover", workflow)
        self.assertIn("--exclude-prefix tests.test_runtime_test_gate", workflow)
        self.assertIn("--exclude-prefix test_runtime_test_gate", workflow)
        self.assertIn("--non-blocking", workflow)


if __name__ == "__main__":
    unittest.main()
