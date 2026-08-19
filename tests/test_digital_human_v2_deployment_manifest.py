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
    "server/content_domains/core.py",
    "server/content_domains/digital_human_oneclick.py",
    "server/content_domains/points.py",
    "server/content_domains/digital_human_timeline.py",
    "server/content_domains/digital_human_v2.py",
    "site/workbench/digital-human-oneclick.html",
}
LOCKED_PREIMAGES = {
    "server/content_domains/script_to_video.py": (
        "file", "6b3f8b8c9068705debbd7959406362f19e821ba0",
        "a32785c2c8ead5d366c431f5c405a24da9e0e69c2296d6ccc7473028aba3389d",
    ),
    "server/content_domains/core.py": (
        "file", "63cd17054f33e872b07f81b066108089323ef762",
        "5587befc8f1aba6ea289b49648589dd11ed58f004904ab291d04cfea4280245e",
    ),
    "server/content_domains/digital_human_oneclick.py": (
        "file", "6f9e460e8c7cee91042028eda69d4a7a7e939b28",
        "c4068c60d83be064ad08d53bf5436502cbafbd8135f5389ee8645348db559119",
    ),
    "server/content_domains/points.py": (
        "file", "9e4e52f7d1a21ce9e85af2a9c9b74055e5724dff",
        "7080c054b25a0b17dd6b1d1ec194dc3e1db211a011e4448e3208dbb553d3a414",
    ),
    "server/content_domains/digital_human_timeline.py": (
        "file", "c091ef9592409db774c72ec6a53f816240afdae5",
        "f81f0f5ad61587d7b32930e198c61325f18496e75fdb7abec77f19d69c6b4d08",
    ),
    "server/content_domains/digital_human_v2.py": (
        "file", "a0c26b4617108f5e85b1c79bbf491c57436db083",
        "330891a41ea051ae9cfd9013b340446ce116a1c4c3b9bae051373dc7bbdcc127",
    ),
    "site/workbench/digital-human-oneclick.html": (
        "file", "9b8f5e993584dc07abdd6213fd369213b812cbb6",
        "79862cca18453a812517df7b4c108fb87e3e4740b0c2b244b6e62c0f791db83d",
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
        self.assertEqual(len(files), 7)
        self.assertEqual(len({entry["runtime_path"] for entry in files}), 7)
        self.assertEqual(len(self.verify.verify_sources(self.manifest, ROOT)), 7)
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
        self.assertEqual(
            observation["captured_at"], "2026-08-19T09:16:28Z",
        )
        self.assertEqual(
            observation["repository_main_commit"],
            "e99ea276d1b04df7e7278378ee0efcbadcd826f2",
        )
        self.assertEqual(observation["service_state"], "active")
        self.assertEqual(observation["health_status"], 200)
        self.assertEqual(observation["files"], 7)

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
        self.assertEqual(absent, [])

    def test_feishu_credentials_are_named_but_never_committed(self):
        feishu = self.manifest["configuration_requirements"]["feishu"]
        self.assertTrue(feishu["required_for_real_priority_validation"])
        self.assertEqual(
            feishu["secret_environment_names"],
            ["FEISHU_APP_ID", "FEISHU_APP_SECRET"],
        )
        self.assertEqual(
            feishu["app_token_environment_name"],
            "DIGITAL_HUMAN_FEISHU_APP_TOKEN",
        )
        self.assertTrue(feishu["credentials_must_not_be_committed"])
        serialized = json.dumps(feishu, ensure_ascii=False)
        self.assertNotIn("app_secret=", serialized.lower())

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
        font_command = commands["font"][0]["argv"][-1]
        self.assertIn("result=subtitle_runtime_preflight()", font_command)
        self.assertIn("result.get('no_charge') is True", font_command)
        self.assertNotIn("get('font')", font_command)
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
