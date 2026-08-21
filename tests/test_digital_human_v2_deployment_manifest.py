# -*- coding: utf-8 -*-
import hashlib
import json
import pathlib
import subprocess
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFEST_PATH = (
    ROOT / "deploy" / "test-runtime" /
    "digital-human-material-seedream-v3-20260821.json"
)
HISTORICAL_MANIFEST_PATH = (
    ROOT / "deploy" / "test-runtime" / "digital-human-material-v2-20260818.json"
)
HISTORICAL_MANIFEST_BLOB = "e7d139001c065f6a3e8ec7137057abdc86bf5d06"
HISTORICAL_MANIFEST_SHA256 = (
    "44006751e15f56c946d8dfadeb4ad74f497dab44ceda66512bdb9603b919e453"
)
CI_PATH = ROOT / ".github" / "workflows" / "ci.yml"
EXPECTED_SCOPE = {
    "server/content_domains/script_to_video.py",
    "server/content_domains/core.py",
    "server/content_domains/digital_human_oneclick.py",
    "server/content_domains/points.py",
    "server/content_domains/digital_human_timeline.py",
    "server/content_domains/digital_human_v2.py",
    "server/content_domains/audio.py",
    "server/content_domains/cosyvoice.py",
    "server/content_domains/video.py",
    "site/workbench/digital-human-oneclick.html",
}
LOCKED_PREIMAGES = {
    "server/content_domains/script_to_video.py": (
        "file", "6b3f8b8c9068705debbd7959406362f19e821ba0",
        "a32785c2c8ead5d366c431f5c405a24da9e0e69c2296d6ccc7473028aba3389d",
    ),
    "server/content_domains/core.py": (
        "file", "db05b6d0c122186798d8ef80bff77648eeb12711",
        "f8634b3fe5d601587448a4ddd4e27f1ebca99f97618241920afb46065b48a425",
    ),
    "server/content_domains/digital_human_oneclick.py": (
        "file", "351d278b5c3bd243974fc0f235b076e8723f0b85",
        "55c03a5e774d8fc83da6496decdbea604df8ca6880736153be4c5df888cc45fc",
    ),
    "server/content_domains/points.py": (
        "file", "9e4e52f7d1a21ce9e85af2a9c9b74055e5724dff",
        "7080c054b25a0b17dd6b1d1ec194dc3e1db211a011e4448e3208dbb553d3a414",
    ),
    "server/content_domains/digital_human_timeline.py": (
        "file", "7a2d26eb72af41624e45c5daa273d8b67acb3f23",
        "ed07bbdb1be7e5281dfab9541e414a5a4b5c02a9e39bb26c3dd262c1d071b19e",
    ),
    "server/content_domains/digital_human_v2.py": (
        "file", "383626daf4403ff54ffa18b7c3a5e55bc0094d81",
        "bb0b1ff6909ec7f1785b8bf3b7a81e248919733d4eb4ae0efa061d996b1c3c68",
    ),
    "server/content_domains/audio.py": (
        "file", "32f948f451d0f527d992425ae1eaa8bc28583c6f",
        "1482e90c5cba03778a5c00b53eea21a9018fb4683c6b31eb329c54d12b012651",
    ),
    "server/content_domains/cosyvoice.py": (
        "file", "a23eb651c1ea0d0ec6cab7ae44bd561ed00436fd",
        "ad29df5e53990880941f57d32fe3355393c870ba62bb3485086febb4065358b8",
    ),
    "server/content_domains/video.py": (
        "file", "ed6ad03b72fedd4f7e4e388cfdab9ad6c2392ddf",
        "ce3153d484729b8b7dc226f8fa82ca6a8ee8afa50ebf5b6646c458d92303cfeb",
    ),
    "site/workbench/digital-human-oneclick.html": (
        "file", "cdb4b99cc44302602c92c9c26b906ffe766438cc",
        "625aa315e33ea599a5dbfb4f1e5e7f8b55e0d3665a32d2a6f348e762d079d5cf",
    ),
}


def _read_locked_git_blob(blob_id):
    result = subprocess.run(
        [
            "git", "-c", "safe.directory=" + ROOT.as_posix(),
            "cat-file", "blob", blob_id,
        ],
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout


class DigitalHumanV2DeploymentManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    def _assert_historical_content_lock(self, lock):
        data = _read_locked_git_blob(lock["source_blob"])
        actual_blob = hashlib.sha1(
            b"blob %d\0" % len(data) + data
        ).hexdigest()
        self.assertEqual(actual_blob, lock["source_blob"])
        self.assertEqual(hashlib.sha256(data).hexdigest(), lock["source_sha256"])

    def _assert_blob_and_sha256(self, blob_id, sha256):
        data = _read_locked_git_blob(blob_id)
        actual_blob = hashlib.sha1(
            b"blob %d\0" % len(data) + data
        ).hexdigest()
        self.assertEqual(actual_blob, blob_id)
        self.assertEqual(hashlib.sha256(data).hexdigest(), sha256)

    def test_historical_v2_manifest_bytes_remain_exact(self):
        data = HISTORICAL_MANIFEST_PATH.read_bytes()
        actual_blob = hashlib.sha1(
            b"blob %d\0" % len(data) + data
        ).hexdigest()
        self.assertEqual(HISTORICAL_MANIFEST_BLOB, actual_blob)
        self.assertEqual(HISTORICAL_MANIFEST_SHA256, hashlib.sha256(data).hexdigest())

    def test_scope_and_historical_source_locks_are_exact(self):
        files = self.manifest["files"]
        self.assertEqual({entry["repository_path"] for entry in files}, EXPECTED_SCOPE)
        self.assertEqual(len(files), 10)
        self.assertEqual(len({entry["runtime_path"] for entry in files}), 10)
        for entry in files:
            self._assert_historical_content_lock(entry)
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
        for _, blob_id, sha256 in actual.values():
            self.assertEqual(40, len(blob_id))
            self.assertEqual(64, len(sha256))
            self._assert_blob_and_sha256(blob_id, sha256)
        observation = self.manifest["preimage_observation"]
        self.assertEqual(observation["target"], "test@8.148.158.106")
        self.assertIn("user-supplied read-only", observation["capture_method"])
        self.assertEqual(
            observation["captured_at"],
            "2026-08-21 (user-supplied current evidence; exact timestamp not provided)",
        )
        self.assertIsNone(observation["repository_main_commit"])
        self.assertEqual(observation["repository_git_metadata"], "absent")
        self.assertEqual(
            self.manifest["source"]["base_main_commit"],
            "bec6f49c05107b46df358d99207e5f89bea1804d",
        )
        self.assertEqual(observation["service_state"], "active")
        self.assertEqual(observation["health_status"], 200)
        self.assertEqual(observation["files"], 10)

    def test_tampered_successor_preimage_lock_is_rejected(self):
        entry = next(
            item for item in self.manifest["files"]
            if item["repository_path"]
            == "server/content_domains/digital_human_oneclick.py"
        )
        with self.assertRaises(AssertionError):
            self._assert_blob_and_sha256(
                entry["target_preimage_blob"], "0" * 64,
            )

    def test_successor_changes_only_seedream_business_files(self):
        changed_paths = {
            entry["repository_path"]
            for entry in self.manifest["files"]
            if entry["target_preimage_blob"] != entry["expected_postimage_blob"]
        }
        self.assertEqual(
            changed_paths,
            {
                "server/content_domains/digital_human_oneclick.py",
                "server/content_domains/digital_human_v2.py",
                "site/workbench/digital-human-oneclick.html",
            },
        )

    def test_policy_is_test_only_atomic_and_removes_only_new_targets_on_rollback(self):
        policy = self.manifest["deployment_policy"]
        self.assertEqual(self.manifest["target"]["role"], "test")
        self.assertEqual(self.manifest["target"]["host"], "8.148.158.106")
        self.assertTrue(policy["fail_closed_on_source_mismatch"])
        self.assertTrue(policy["fail_closed_on_preimage_mismatch"])
        self.assertTrue(policy["allow_exact_postimage_as_existing"])
        self.assertTrue(policy["backup_all_targets_before_first_write"])
        self.assertTrue(policy["atomic_replace"])
        self.assertTrue(policy["restart_service_at_most_once"])
        self.assertTrue(policy["skip_restart_when_no_files_change"])
        self.assertTrue(policy["rollback_all_targets_as_one_unit"])
        self.assertTrue(policy["remove_only_targets_recorded_missing_during_rollback"])
        self.assertFalse(policy["copy_environment_database_or_user_data"])
        self.assertFalse(policy["production_server_write_allowed"])
        self.assertTrue(self.manifest["rollback"]["new_files_removed_only_if_preimage_was_absent"])
        self.assertEqual(self.manifest["rollback"]["scope"], "all ten manifest targets as one unit")
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

    def test_release_tools_and_contracts_have_historical_content_locks(self):
        executor = self.manifest["executor"]
        self.assertEqual(
            executor["repository_path"],
            "scripts/deploy_seedream_v3_locked_manifest.py",
        )
        self.assertEqual(executor["confirm_target"], "test@8.148.158.106")
        self.assertFalse(executor["remote_connection_capability"])
        for tool in (executor, executor["verifier"], executor["requirements_verifier"]):
            self._assert_historical_content_lock(tool)
        for contract in self.manifest["release_contract_sources"]:
            self._assert_historical_content_lock(contract)

    def test_all_no_charge_checks_run_before_the_single_restart(self):
        commands = self.manifest["release_commands"]
        for stage in ("dependencies", "cache", "offline", "font", "no_charge"):
            self.assertTrue(commands[stage])
        font_command = commands["font"][0]["argv"][-1]
        self.assertIn("result=subtitle_runtime_preflight()", font_command)
        self.assertIn("result.get('no_charge') is True", font_command)
        self.assertNotIn("get('font')", font_command)
        dependencies = json.dumps(commands["dependencies"], ensure_ascii=False)
        self.assertIn("content_domains/audio.py", dependencies)
        self.assertIn("content_domains/cosyvoice.py", dependencies)
        self.assertIn("content_domains/video.py", dependencies)
        self.assertEqual(len(commands["restart"]), 1)
        self.assertEqual(len(commands["rollback_restart"]), 1)
        rendered = json.dumps(commands["no_charge"], ensure_ascii=False)
        self.assertIn("tests.test_digital_human_timeline", rendered)
        self.assertIn("tests.test_digital_human_v2", rendered)
        self.assertIn("tests.test_digital_human_oneclick", rendered)
        self.assertIn("tests.test_digital_human_v2_ui", rendered)
        self.assertIn("tests.test_digital_human_v2_compose", rendered)
        self.assertIn("tests.test_seedream_v3_release_executor", rendered)
        self.assertNotIn("/usr/bin/node", rendered)
        self.assertNotIn("tests/test_digital_human_voice_state.js", rendered)
        self.assertIn("tests.test_cosyvoice", rendered)
        self.assertIn("tests.test_heygen_mcp_oauth", rendered)
        self.assertEqual(
            {200, 401},
            {int(item["expected_status"]) for item in self.manifest["health_checks"]},
        )

    def test_voice_state_node_test_is_ci_only_and_content_locked(self):
        ci = CI_PATH.read_text(encoding="utf-8")
        self.assertIn("uses: actions/setup-node@v6", ci)
        self.assertIn('node-version: "22"', ci)
        self.assertIn("node tests/test_digital_human_voice_state.js", ci)

        contract = next(
            entry for entry in self.manifest["release_contract_sources"]
            if entry["repository_path"] == "tests/test_digital_human_voice_state.js"
        )
        self._assert_historical_content_lock(contract)

        server_no_charge = json.dumps(
            self.manifest["release_commands"]["no_charge"],
            ensure_ascii=False,
        )
        self.assertNotIn("/usr/bin/node", server_no_charge)
        self.assertNotIn(contract["repository_path"], server_no_charge)


if __name__ == "__main__":
    unittest.main()
