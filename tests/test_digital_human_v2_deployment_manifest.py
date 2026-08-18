# -*- coding: utf-8 -*-
import hashlib
import importlib.util
import json
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFEST_PATH = (
    ROOT / "deploy" / "test-runtime" / "digital-human-material-v2-20260818.json"
)
VERIFY_PATH = ROOT / "scripts" / "verify_content_whisper_deployment.py"
EXPECTED_SCOPE = {
    "server/content_domains/script_to_video.py",
    "server/content_domains/digital_human_oneclick.py",
    "server/content_domains/points.py",
    "server/content_domains/digital_human_timeline.py",
    "server/content_domains/digital_human_v2.py",
    "site/workbench/digital-human-oneclick.html",
}
LOCKED_PREIMAGES = {
    "server/content_domains/script_to_video.py": (
        "file", "6f264a095fdd0bb0d4d8fb17a34b9c50bf72ef06",
        "955c646c462495ee51f0c074eeb628be421c25dda1037010906ff76e2e0ea680",
    ),
    "server/content_domains/digital_human_oneclick.py": (
        "file", "86d02f30c4b1ab9dc426e926c1e7b7d963bdd2a2",
        "91848d5bfb3b129e2d08722f5d39119c7c0d6f033178eea1251e04358d2536a5",
    ),
    "server/content_domains/points.py": (
        "file", "3f273f133fa22a544d4955d8a9bc4b0b34584fae",
        "ffde6ee4e86bfb292809469ea53c0d39134c9c900506c46295dbecc5f2fed294",
    ),
    "server/content_domains/digital_human_timeline.py": (
        "absent", None,
        "ede4663fa9a5e8031704705268edde90aae07273f42b7b7274db7b317a40b001",
    ),
    "server/content_domains/digital_human_v2.py": (
        "absent", None,
        "ede4663fa9a5e8031704705268edde90aae07273f42b7b7274db7b317a40b001",
    ),
    "site/workbench/digital-human-oneclick.html": (
        "file", "5b87aab8858b36e813203daf07f18ff6eb30d8bc",
        "e65e87f59afd672d76390bdf7b65e06dcda3d9495f767d4feeb70e8281ef2ff4",
    ),
}


def _load_verifier():
    spec = importlib.util.spec_from_file_location("digital_human_v2_verify", VERIFY_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DigitalHumanV2DeploymentManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        cls.verify = _load_verifier()

    def test_scope_and_source_locks_are_exact(self):
        files = self.manifest["files"]
        self.assertEqual({entry["repository_path"] for entry in files}, EXPECTED_SCOPE)
        self.assertEqual(len(files), 6)
        self.assertEqual(len({entry["runtime_path"] for entry in files}), 6)
        self.assertEqual(len(self.verify.verify_sources(self.manifest, ROOT)), 6)
        for entry in files:
            data = (ROOT / entry["repository_path"]).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), entry["source_sha256"])
            self.assertEqual(self.verify._blob_id(data), entry["source_blob"])
            self.assertEqual(entry["source_sha256"], entry["expected_postimage_sha256"])
            self.assertEqual(entry["source_blob"], entry["expected_postimage_blob"])

    def test_read_only_test_preimages_are_exact(self):
        actual = {
            entry["repository_path"]: (
                entry["target_preimage_state"], entry["target_preimage_blob"],
                entry["target_preimage_sha256"],
            )
            for entry in self.manifest["files"]
        }
        self.assertEqual(actual, LOCKED_PREIMAGES)
        observation = self.manifest["preimage_observation"]
        self.assertEqual(observation["target"], "test@8.148.158.106")
        self.assertIn("read-only SSH", observation["capture_method"])
        self.assertEqual(observation["files"], 6)

    def test_policy_is_test_only_atomic_and_removes_only_new_targets_on_rollback(self):
        policy = self.manifest["deployment_policy"]
        self.assertEqual(self.manifest["target"]["role"], "test")
        self.assertEqual(self.manifest["target"]["host"], "8.148.158.106")
        self.assertTrue(policy["fail_closed_on_source_mismatch"])
        self.assertTrue(policy["fail_closed_on_preimage_mismatch"])
        self.assertTrue(policy["backup_all_targets_before_first_write"])
        self.assertTrue(policy["atomic_replace"])
        self.assertTrue(policy["rollback_all_targets_as_one_unit"])
        self.assertTrue(policy["remove_only_targets_recorded_missing_during_rollback"])
        self.assertFalse(policy["copy_environment_database_or_user_data"])
        self.assertFalse(policy["production_server_write_allowed"])
        self.assertTrue(self.manifest["rollback"]["new_files_removed_only_if_preimage_was_absent"])
        absent = [entry["repository_path"] for entry in self.manifest["files"]
                  if entry["target_preimage_state"] == "absent"]
        self.assertEqual(absent, [
            "server/content_domains/digital_human_timeline.py",
            "server/content_domains/digital_human_v2.py",
        ])

    def test_release_tools_and_contract_tests_are_content_locked(self):
        executor = self.manifest["executor"]
        self.assertEqual(executor["confirm_target"], "test@8.148.158.106")
        self.assertFalse(executor["remote_connection_capability"])
        for tool in (executor, executor["verifier"], executor["requirements_verifier"]):
            data = (ROOT / tool["repository_path"]).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), tool["source_sha256"])
            self.assertEqual(self.verify._blob_id(data), tool["source_blob"])
        for contract in self.manifest["release_contract_sources"]:
            data = (ROOT / contract["repository_path"]).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), contract["source_sha256"])
            self.assertEqual(self.verify._blob_id(data), contract["source_blob"])

    def test_all_no_charge_checks_run_before_the_single_restart(self):
        commands = self.manifest["release_commands"]
        for stage in ("dependencies", "cache", "offline", "font", "no_charge"):
            self.assertTrue(commands[stage])
        self.assertEqual(len(commands["restart"]), 1)
        self.assertEqual(len(commands["rollback_restart"]), 1)
        rendered = json.dumps(commands["no_charge"], ensure_ascii=False)
        self.assertIn("tests.test_digital_human_timeline", rendered)
        self.assertIn("tests.test_digital_human_v2", rendered)
        self.assertIn("tests.test_digital_human_v2_ui", rendered)
        self.assertIn("tests.test_digital_human_v2_compose", rendered)
        self.assertNotIn("heygen", rendered.lower())
        self.assertEqual(
            {200, 401},
            {int(item["expected_status"]) for item in self.manifest["health_checks"]},
        )


if __name__ == "__main__":
    unittest.main()
