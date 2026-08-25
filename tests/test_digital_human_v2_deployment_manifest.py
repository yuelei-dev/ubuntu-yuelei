# -*- coding: utf-8 -*-
import hashlib
import json
import pathlib
import subprocess
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFEST_PATH = (
    ROOT / "docs" / "release-manifests" /
    "digital-human-material-feishu-priority-20260823.json"
)
CATALOG_PATH = ROOT / "deploy" / "test-release" / "runtime-catalog.json"
HISTORICAL_MANIFEST_PATH = (
    ROOT / "deploy" / "test-runtime" /
    "digital-human-material-seedream-v3-20260821.json"
)
HISTORICAL_MANIFEST_BLOB = "1a1bef4ec64323208fe0212961e72e38206a69bf"
HISTORICAL_MANIFEST_SHA256 = (
    "1cb48ad76a69d05ea39d96f3bc0607ee18d2566354d7317e5bdb0dd6308d8578"
)
CI_PATH = ROOT / ".github" / "workflows" / "ci.yml"
EXPECTED_SCOPE = {
    "server/content_domains/script_to_video.py",
    "server/content_domains/digital_human_v2.py",
    "site/workbench/digital-human-oneclick.html",
}
LOCKED_PREIMAGES = {
    "server/content_domains/script_to_video.py": (
        "file", "6b3f8b8c9068705debbd7959406362f19e821ba0",
        "a32785c2c8ead5d366c431f5c405a24da9e0e69c2296d6ccc7473028aba3389d",
    ),
    "server/content_domains/digital_human_v2.py": (
        "file", "c12813c12d36ca93f9083dba4767fa759f81ab32",
        "e99c99f5ab8ba287b55f27a5e05c146f06b1dad9d5d7612df1c7b48611400214",
    ),
    "site/workbench/digital-human-oneclick.html": (
        "file", "289e095bb6869337b185a8ab3ee7eff153543f08",
        "e2571f4bd310b74da5d4bae60ba368c79b9abd881bc42e6fd2ad5c658140aaae",
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


def _git_blob(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _verify_manifest_relock(repository, manifest_path, head="HEAD"):
    relative_path = manifest_path.relative_to(repository).as_posix()
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    expected_parent = manifest["source"]["code_source_commit"]
    expected_blob = _git_blob(manifest_bytes)
    history = subprocess.run(
        ["git", "rev-list", head, "--", relative_path], cwd=repository,
        check=True, text=True, stdout=subprocess.PIPE,
    ).stdout.splitlines()
    candidates = []
    for commit in history:
        blob = subprocess.run(
            ["git", "rev-parse", "%s:%s" % (commit, relative_path)],
            cwd=repository, check=True, text=True, stdout=subprocess.PIPE,
        ).stdout.strip()
        if blob == expected_blob:
            candidates.append(commit)
    if not candidates:
        raise AssertionError("current manifest bytes have no reachable locked commit")
    locked_commit = candidates[0]
    parents = subprocess.run(
        ["git", "rev-list", "--parents", "-n", "1", locked_commit],
        cwd=repository, check=True, text=True, stdout=subprocess.PIPE,
    ).stdout.split()
    if len(parents) != 2 or parents[1] != expected_parent:
        raise AssertionError("locked manifest parent does not match code source")
    changed = set(filter(None, subprocess.run(
        ["git", "diff", "--name-only", parents[1], locked_commit],
        cwd=repository, check=True, text=True, stdout=subprocess.PIPE,
    ).stdout.splitlines()))
    if changed != {relative_path}:
        raise AssertionError("locked manifest commit is not manifest-only")
    locked_bytes = subprocess.run(
        ["git", "cat-file", "blob", "%s:%s" % (locked_commit, relative_path)],
        cwd=repository, check=True, stdout=subprocess.PIPE,
    ).stdout
    if locked_bytes != manifest_bytes or _git_blob(locked_bytes) != expected_blob:
        raise AssertionError("current manifest bytes do not match locked blob")
    return locked_commit


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

    def test_successor_docs_manifest_is_not_runtime_candidate(self):
        catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))

        def is_candidate(path):
            return (
                path in catalog["runtime_candidate_paths"]
                or any(
                    path.startswith(prefix.rstrip("/") + "/")
                    for prefix in catalog["runtime_candidate_prefixes"]
                )
            )

        def is_ignored(path):
            return (
                path in catalog["ignored_repository_paths"]
                or any(
                    path.startswith(prefix.rstrip("/") + "/")
                    for prefix in catalog["ignored_repository_prefixes"]
                )
            )

        successor = MANIFEST_PATH.relative_to(ROOT).as_posix()
        future_runtime_manifest = (
            "deploy/test-runtime/"
            "digital-human-material-feishu-priority-v2-future.json"
        )
        self.assertFalse(is_candidate(successor))
        self.assertTrue(is_candidate(future_runtime_manifest))
        self.assertFalse(is_ignored(future_runtime_manifest))
        self.assertEqual(".json", MANIFEST_PATH.suffix)
        self.assertTrue(
            self.manifest["executor"]["repository_path"].startswith("scripts/")
        )

    def test_scope_and_historical_source_locks_are_exact(self):
        files = self.manifest["files"]
        self.assertEqual({entry["repository_path"] for entry in files}, EXPECTED_SCOPE)
        self.assertEqual(len(files), 3)
        self.assertEqual(len({entry["runtime_path"] for entry in files}), 3)
        for entry in files:
            self._assert_historical_content_lock(entry)
            self.assertEqual(entry["source_sha256"], entry["expected_postimage_sha256"])
            self.assertEqual(entry["source_blob"], entry["expected_postimage_blob"])

    def test_successor_manifest_is_manifest_only_child_of_locked_code_source(self):
        locked_commit = _verify_manifest_relock(ROOT, MANIFEST_PATH)
        self.assertEqual(
            self.manifest["source"]["code_source_commit"],
            subprocess.run(
                ["git", "rev-parse", locked_commit + "^"], cwd=ROOT,
                check=True, text=True, stdout=subprocess.PIPE,
            ).stdout.strip(),
        )
        code_source = self.manifest["source"]["code_source_commit"]
        locks = (
            self.manifest["files"]
            + [self.manifest["executor"]]
            + [self.manifest["executor"]["verifier"]]
            + [self.manifest["executor"]["requirements_verifier"]]
            + self.manifest["release_contract_sources"]
        )
        for lock in locks:
            with self.subTest(repository_path=lock["repository_path"]):
                actual_blob = subprocess.run(
                    ["git", "rev-parse", "%s:%s" % (
                        code_source, lock["repository_path"],
                    )],
                    cwd=ROOT, check=True, text=True, stdout=subprocess.PIPE,
                ).stdout.strip()
                self.assertEqual(lock["source_blob"], actual_blob)

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
        self.assertIn("read-only", observation["capture_method"])
        self.assertEqual(
            observation["captured_at"],
            "2026-08-25 (user-provided read-only pre-deployment evidence; no server access in this task)",
        )
        self.assertEqual(
            observation["repository_main_commit"],
            "864f557f6003f92083a8e2d366800d63d041b7ce",
        )
        self.assertIn("no server access", observation["repository_git_metadata"])
        self.assertEqual(
            self.manifest["source"]["base_main_commit"],
            "864f557f6003f92083a8e2d366800d63d041b7ce",
        )
        self.assertEqual(observation["service_state"], "active")
        self.assertEqual(observation["health_statuses"], {
            "http://127.0.0.1:8096/api/gen/health": 200,
            "http://127.0.0.1:8096/api/gen/history": 401,
            "http://127.0.0.1:8096/api/gen/digital-human-v2/history": 404,
        })
        self.assertFalse(observation["backup_started"])
        self.assertFalse(observation["write_started"])
        self.assertFalse(observation["restart_started"])
        self.assertEqual(observation["files"], 3)

    def test_tampered_successor_preimage_lock_is_rejected(self):
        entry = next(
            item for item in self.manifest["files"]
            if item["repository_path"]
            == "server/content_domains/digital_human_v2.py"
        )
        with self.assertRaises(AssertionError):
            self._assert_blob_and_sha256(
                entry["target_preimage_blob"], "0" * 64,
            )

    def test_successor_changes_only_material_history_runtime_files(self):
        changed_paths = {
            entry["repository_path"]
            for entry in self.manifest["files"]
            if entry["target_preimage_blob"] != entry["expected_postimage_blob"]
        }
        self.assertEqual(
            changed_paths,
            {
                "server/content_domains/script_to_video.py",
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
        self.assertEqual(self.manifest["rollback"]["scope"], "all three manifest targets as one unit")
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
            "DIGITAL_HUMAN_MATERIAL_LIBRARY_APP_TOKEN",
        )
        self.assertEqual(feishu["app_token_default"], "TYqUb6KaQaLPQ2sLrLFcdsWanPZ")
        self.assertEqual(feishu["table_environment_name"],
                         "DIGITAL_HUMAN_MATERIAL_LIBRARY_TABLE_1")
        self.assertEqual(feishu["table_default"], "tbl58c0UkQ5ZaR2z")
        self.assertEqual(feishu["view_environment_name"],
                         "DIGITAL_HUMAN_MATERIAL_LIBRARY_VIEW_1")
        self.assertEqual(feishu["view_default"], "vewa9ZW0Og")
        self.assertTrue(feishu["operational_probe_required"])
        self.assertTrue(feishu["probe_all_pages"])
        self.assertTrue(feishu["probe_attachment_download_and_mime"])
        runtime = self.manifest["configuration_requirements"]["service_runtime"]
        self.assertEqual(runtime["user"], "ubuntu")
        self.assertEqual(
            runtime["environment_file"],
            "/home/ubuntu/content-api/content.env",
        )
        self.assertTrue(runtime["inspect_active_process_environment"])
        self.assertEqual(
            self.manifest["configuration_requirements"]["material_priority"],
            ["customer_upload_required", "feishu", "ai_optional"],
        )
        self.assertFalse(
            self.manifest["configuration_requirements"]["public_web_materials_enabled"]
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
        self.assertIn("content_domains/script_to_video.py", dependencies)
        self.assertIn("content_domains/digital_human_v2.py", dependencies)
        self.assertEqual(len(commands["restart"]), 1)
        self.assertEqual(len(commands["rollback_restart"]), 1)
        rendered = json.dumps(commands["no_charge"], ensure_ascii=False)
        self.assertIn("tests.test_digital_human_timeline", rendered)
        self.assertIn("tests.test_digital_human_v2", rendered)
        self.assertIn("tests.test_digital_human_oneclick", rendered)
        self.assertIn("tests.test_digital_human_v2_ui", rendered)
        self.assertIn("tests.test_digital_human_v2_compose", rendered)
        self.assertIn("tests.test_seedream_v3_release_executor", rendered)
        self.assertIn("tests.test_unified_voice_v6_release", rendered)
        self.assertNotIn("/usr/bin/node", rendered)
        self.assertNotIn("tests/test_digital_human_voice_state.js", rendered)
        self.assertNotIn("tests.test_cosyvoice", rendered)
        self.assertNotIn("tests.test_heygen_mcp_oauth", rendered)
        expected_health = {
            "http://127.0.0.1:8096/api/gen/health": {
                "pre_expected_statuses": [200],
                "post_expected_status": 200,
                "rollback_expected_statuses": [200],
            },
            "http://127.0.0.1:8096/api/gen/history": {
                "pre_expected_statuses": [401],
                "post_expected_status": 401,
                "rollback_expected_statuses": [401],
            },
            "http://127.0.0.1:8096/api/gen/digital-human-v2/history": {
                "pre_expected_statuses": [404, 401],
                "post_expected_status": 401,
                "rollback_expected_statuses": [404, 401],
            },
        }
        actual_health = {
            item["url"]: {
                key: value for key, value in item.items() if key != "url"
            }
            for item in self.manifest["health_checks"]
        }
        self.assertEqual(expected_health, actual_health)
        history = next(
            item for item in self.manifest["health_checks"]
            if item["url"].endswith("/api/gen/digital-human-v2/history")
        )
        self.assertEqual(401, history["post_expected_status"])
        self.assertNotIn(200, history["pre_expected_statuses"])
        self.assertNotIn(200, history["rollback_expected_statuses"])

    def test_voice_state_node_test_is_ci_only_and_content_locked(self):
        ci = CI_PATH.read_text(encoding="utf-8")
        self.assertIn(
            "uses: actions/setup-node@249970729cb0ef3589644e2896645e5dc5ba9c38 # v6",
            ci,
        )
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
