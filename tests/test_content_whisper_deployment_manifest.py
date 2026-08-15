# -*- coding: utf-8 -*-
import copy
import hashlib
import importlib.util
import json
import os
import pathlib
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFEST_PATH = (
    ROOT / "deploy" / "test-runtime" / "digital-human-whisper-runtime-20260815.json"
)
VERIFY_PATH = ROOT / "scripts" / "verify_content_whisper_deployment.py"
EXPECTED_SCOPE = {
    "server/content_domains/video.py",
    "server/content_domains/script_to_video.py",
    "server/content_domains/digital_human_oneclick.py",
    "deploy/systemd/huangque-content.service.d/whisper.conf",
    "deploy/requirements-content.txt",
    "scripts/prepare_content_whisper.py",
    "scripts/prepare_content_whisper_runtime.sh",
}
LOCKED_PREIMAGES = {
    "server/content_domains/video.py": (
        "file",
        "935bede1740e52e549ea389f4b76ddf3d4ba66d3",
        "3b25b9eba89eccf5ba0212b2916a6d6e9679528b80a6d652ee7cac204b00b583",
    ),
    "server/content_domains/script_to_video.py": (
        "file",
        "c7d9c8dbba07d2504cc856695612b1a43433dac4",
        "7154a39a8c66f44f7638c665c551afc2f7a5b0e52344b667b1b5bb5d6ad598a9",
    ),
    "server/content_domains/digital_human_oneclick.py": (
        "file",
        "aa6bc0bfe2bcf68e5a131cb53309053776a85447",
        "2f73895135b07dc346749813842d692f739337883b183794ecac5b53378c0a95",
    ),
    "deploy/systemd/huangque-content.service.d/whisper.conf": (
        "file",
        "7850c33c09e60a888f641c99d816ded675a249d3",
        "a8140bb6c63dd9a93cac9a2627d76a96f1b11e02356a63d0d34079c472a469ec",
    ),
    "deploy/requirements-content.txt": (
        "absent",
        None,
        "ede4663fa9a5e8031704705268edde90aae07273f42b7b7274db7b317a40b001",
    ),
    "scripts/prepare_content_whisper.py": (
        "absent",
        None,
        "ede4663fa9a5e8031704705268edde90aae07273f42b7b7274db7b317a40b001",
    ),
    "scripts/prepare_content_whisper_runtime.sh": (
        "absent",
        None,
        "ede4663fa9a5e8031704705268edde90aae07273f42b7b7274db7b317a40b001",
    ),
}


def _load_module():
    spec = importlib.util.spec_from_file_location("whisper_manifest_verify", VERIFY_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ContentWhisperDeploymentManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        cls.verify = _load_module()

    def test_manifest_scope_sources_and_postimages_are_exact(self):
        entries = self.manifest["files"]
        self.assertEqual({entry["repository_path"] for entry in entries}, EXPECTED_SCOPE)
        self.assertEqual(len(entries), 7)
        self.assertEqual(len({entry["runtime_path"] for entry in entries}), 7)
        self.assertEqual(len(self.verify.verify_sources(self.manifest, ROOT)), 7)
        for entry in entries:
            data = (ROOT / entry["repository_path"]).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), entry["source_sha256"])
            self.assertEqual(entry["source_sha256"], entry["expected_postimage_sha256"])
            self.assertEqual(entry["source_blob"], entry["expected_postimage_blob"])

    def test_locked_preimages_include_deterministic_absent_digest(self):
        actual = {
            entry["repository_path"]: (
                entry["target_preimage_state"],
                entry["target_preimage_blob"],
                entry["target_preimage_sha256"],
            )
            for entry in self.manifest["files"]
        }
        self.assertEqual(actual, LOCKED_PREIMAGES)
        absent = self.manifest["absent_target_digest"]
        self.assertEqual(absent["canonical_bytes"], "ABSENT\n")
        self.assertEqual(
            hashlib.sha256(b"ABSENT\n").hexdigest(), absent["sha256"]
        )

    def test_policy_is_test_only_atomic_and_whole_unit_rollback(self):
        policy = self.manifest["deployment_policy"]
        self.assertEqual(self.manifest["target"]["role"], "test")
        self.assertEqual(self.manifest["target"]["host"], "8.148.158.106")
        self.assertTrue(policy["fail_closed_on_source_mismatch"])
        self.assertTrue(policy["fail_closed_on_preimage_mismatch"])
        self.assertTrue(policy["backup_all_targets_before_first_write"])
        self.assertTrue(policy["atomic_replace"])
        self.assertTrue(policy["rollback_all_targets_as_one_unit"])
        self.assertFalse(policy["copy_environment_database_or_user_data"])
        self.assertFalse(policy["production_server_write_allowed"])
        self.assertEqual(
            "scripts/deploy_content_whisper_runtime.py",
            self.manifest["executor"]["repository_path"],
        )
        for tool in (
                self.manifest["executor"],
                self.manifest["executor"]["verifier"],
                self.manifest["executor"]["requirements_verifier"]):
            data = (ROOT / tool["repository_path"]).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), tool["source_sha256"])
            self.assertEqual(self.verify._blob_id(data), tool["source_blob"])
        source_gate = self.manifest["executor"]["source_checkout_gate"]
        self.assertTrue(source_gate["clean_worktree_required"])
        self.assertEqual("main", source_gate["branch"])
        self.assertTrue(
            source_gate["head_local_tracking_live_remote_and_reviewed_main_must_match"]
        )
        self.assertTrue(source_gate["reviewed_source_commit_must_be_ancestor"])
        self.assertTrue(source_gate["network_or_missing_history_fails_closed"])
        self.assertFalse(self.manifest["executor"]["remote_connection_capability"])
        self.assertEqual(self.manifest["rollback"]["scope"], "all seven manifest targets as one unit")

    def test_release_order_preflights_before_the_only_restart(self):
        steps = self.manifest["ordered_release_steps"]
        restart = steps.index("restart_huangque_content_once")
        required_before_restart = {
            "verify_exact_existing_content_dependencies_without_mutation",
            "run_staged_whisper_cache_prepare_and_online_model_preload",
            "run_staged_whisper_verify_only_with_HF_HUB_OFFLINE_1",
            "run_candidate_subtitle_runtime_preflight_for_model_and_cjk_font",
            "run_no_charge_page_talking_and_mcp_asset_contracts",
        }
        self.assertTrue(required_before_restart.issubset(set(steps[:restart])))
        self.assertEqual(steps.count("restart_huangque_content_once"), 1)
        self.assertLess(
            steps.index("verify_all_target_preimages_before_first_write"),
            steps.index("backup_all_seven_targets_and_record_present_or_missing"),
        )
        self.assertLess(
            steps.index("backup_all_seven_targets_and_record_present_or_missing"),
            steps.index("atomically_install_all_seven_targets"),
        )

    def test_no_charge_contract_is_before_any_paid_heygen_create(self):
        contract = self.manifest["paid_submission_contract"]
        self.assertIn("503", contract["page_preflight_failure"])
        self.assertIn("no_charge=true", contract["page_preflight_failure"])
        self.assertIn("before", contract["page_preflight_failure"])
        self.assertIn("503", contract["talking_subtask_preflight_failure"])
        self.assertIn("before", contract["talking_subtask_preflight_failure"])
        self.assertIn("before asset upload", contract["mcp_asset_contract_failure"])
        self.assertIn("before a paid HeyGen create", contract["mcp_asset_contract_failure"])
        self.assertFalse(contract["automatic_paid_retry_allowed"])
        script_source = (ROOT / "server/content_domains/script_to_video.py").read_text(
            encoding="utf-8"
        )
        page_block = script_source.index("def dispatch_http")
        subtitle_call = script_source.index(
            "subtitle = video_domain.subtitle_runtime_preflight()", page_block
        )
        heygen_call = script_source.index(
            "result = dict(video_domain.heygen_upload_preflight())", subtitle_call
        )
        no_charge = script_source.index('"no_charge": True', heygen_call)
        self.assertLess(subtitle_call, heygen_call)
        self.assertLess(heygen_call, no_charge)
        oneclick = (ROOT / "server/content_domains/digital_human_oneclick.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("still before core creates/charges the video child job", oneclick)
        self.assertIn("video_domain.subtitle_runtime_preflight()", oneclick)

        release_tests = set(self.manifest["release_gate_tests"])
        self.assertIn(
            "tests.test_heygen_mcp_oauth.HeyGenMcpOAuthTests."
            "test_mcp_asset_contract_uses_live_tools_list_schema",
            release_tests,
        )
        self.assertIn(
            "tests.test_heygen_mcp_oauth.HeyGenMcpOAuthTests."
            "test_mcp_asset_validation_error_is_redacted_and_pre_billing",
            release_tests,
        )
        command_patterns = {
            command["argv"][7]
            for command in self.manifest["release_commands"]["no_charge"]
        }
        self.assertEqual(
            command_patterns,
            {
                "test_script_to_video.py",
                "test_digital_human_oneclick.py",
                "test_heygen_mcp_oauth.py",
            },
        )

    def test_verifier_fails_closed_on_drift_and_accepts_exact_images(self):
        synthetic = copy.deepcopy(self.manifest)
        with tempfile.TemporaryDirectory() as directory:
            runtime_root = pathlib.Path(directory)
            for entry in synthetic["files"]:
                target = self.verify._safe_runtime_path(
                    runtime_root, entry["runtime_path"]
                )
                if entry["target_preimage_state"] == "file":
                    data = ("preimage:" + entry["repository_path"]).encode("utf-8")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                    entry["target_preimage_sha256"] = hashlib.sha256(data).hexdigest()
                    entry["target_preimage_blob"] = self.verify._blob_id(data)
            self.assertEqual(
                len(self.verify.verify_targets(synthetic, runtime_root, "preimage")), 7
            )
            first = synthetic["files"][0]
            first_target = self.verify._safe_runtime_path(runtime_root, first["runtime_path"])
            first_target.write_bytes(first_target.read_bytes() + b"drift")
            with self.assertRaisesRegex(RuntimeError, "preimage mismatch"):
                self.verify.verify_targets(synthetic, runtime_root, "preimage")

            for entry in synthetic["files"]:
                target = self.verify._safe_runtime_path(runtime_root, entry["runtime_path"])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((ROOT / entry["repository_path"]).read_bytes())
            self.assertEqual(
                len(self.verify.verify_targets(self.manifest, runtime_root, "postimage")), 7
            )

    def test_verifier_rejects_unexpected_file_for_absent_preimage(self):
        synthetic = copy.deepcopy(self.manifest)
        with tempfile.TemporaryDirectory() as directory:
            runtime_root = pathlib.Path(directory)
            for entry in synthetic["files"]:
                target = self.verify._safe_runtime_path(runtime_root, entry["runtime_path"])
                if entry["target_preimage_state"] == "file":
                    data = ("preimage:" + entry["repository_path"]).encode("utf-8")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                    entry["target_preimage_sha256"] = hashlib.sha256(data).hexdigest()
                    entry["target_preimage_blob"] = self.verify._blob_id(data)
            absent_entry = next(
                entry for entry in synthetic["files"]
                if entry["target_preimage_state"] == "absent"
            )
            unexpected = self.verify._safe_runtime_path(
                runtime_root, absent_entry["runtime_path"]
            )
            unexpected.parent.mkdir(parents=True, exist_ok=True)
            unexpected.write_text("unexpected", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "preimage mismatch"):
                self.verify.verify_targets(synthetic, runtime_root, "preimage")

    def test_verifier_rejects_escaping_paths(self):
        with self.assertRaises(ValueError):
            self.verify._safe_source_path(ROOT, "../outside")
        with self.assertRaises(ValueError):
            self.verify._safe_runtime_path(ROOT, "relative/path")

    @unittest.skipUnless(os.name == "posix", "POSIX no-follow regression")
    def test_verifier_rejects_final_parent_broken_and_absent_symlinks(self):
        def regular_preimages(manifest, runtime_root, count=4):
            for entry in manifest["files"][:count]:
                target = self.verify._safe_runtime_path(
                    runtime_root, entry["runtime_path"]
                )
                target.parent.mkdir(parents=True, exist_ok=True)
                data = ("old:" + entry["repository_path"]).encode("utf-8")
                target.write_bytes(data)
                entry["target_preimage_state"] = "file"
                entry["target_preimage_sha256"] = hashlib.sha256(data).hexdigest()
                entry["target_preimage_blob"] = self.verify._blob_id(data)

        for destination_kind in ("inside", "outside", "broken"):
            with self.subTest(final_symlink=destination_kind), \
                 tempfile.TemporaryDirectory() as directory, \
                 tempfile.TemporaryDirectory() as outside:
                runtime_root = pathlib.Path(directory)
                manifest = copy.deepcopy(self.manifest)
                victim = self.verify._safe_runtime_path(
                    runtime_root, manifest["files"][0]["runtime_path"]
                )
                victim.parent.mkdir(parents=True, exist_ok=True)
                if destination_kind == "inside":
                    destination = runtime_root / "inside.py"
                    destination.write_text("inside", encoding="utf-8")
                elif destination_kind == "outside":
                    destination = pathlib.Path(outside) / "outside.py"
                    destination.write_text("outside", encoding="utf-8")
                else:
                    destination = pathlib.Path(outside) / "missing.py"
                victim.symlink_to(destination)
                with self.assertRaisesRegex(RuntimeError, "symbolic link"):
                    self.verify.verify_targets(manifest, runtime_root, "preimage")

        for destination_kind in ("inside", "outside"):
            with self.subTest(parent_symlink=destination_kind), \
                 tempfile.TemporaryDirectory() as directory, \
                 tempfile.TemporaryDirectory() as outside:
                runtime_root = pathlib.Path(directory)
                manifest = copy.deepcopy(self.manifest)
                victim = self.verify._safe_runtime_path(
                    runtime_root, manifest["files"][0]["runtime_path"]
                )
                victim.parent.parent.mkdir(parents=True, exist_ok=True)
                if destination_kind == "inside":
                    destination = runtime_root / "linked-content-domains"
                else:
                    destination = pathlib.Path(outside) / "linked-content-domains"
                destination.mkdir(parents=True)
                (destination / victim.name).write_text("linked", encoding="utf-8")
                victim.parent.symlink_to(destination, target_is_directory=True)
                with self.assertRaisesRegex(RuntimeError, "symbolic link"):
                    self.verify.verify_targets(manifest, runtime_root, "preimage")

        with tempfile.TemporaryDirectory() as directory, \
             tempfile.TemporaryDirectory() as outside:
            runtime_root = pathlib.Path(directory)
            manifest = copy.deepcopy(self.manifest)
            regular_preimages(manifest, runtime_root)
            absent = manifest["files"][4]
            victim = self.verify._safe_runtime_path(
                runtime_root, absent["runtime_path"]
            )
            victim.parent.mkdir(parents=True, exist_ok=True)
            victim.symlink_to(pathlib.Path(outside) / "missing-input")
            with self.assertRaisesRegex(RuntimeError, "symbolic link"):
                self.verify.verify_targets(manifest, runtime_root, "preimage")


if __name__ == "__main__":
    unittest.main()
