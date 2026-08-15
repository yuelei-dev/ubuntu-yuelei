# -*- coding: utf-8 -*-
import hashlib
import json
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "deploy" / "test-runtime" / "ip12-openai-egress-20260815.json"
LOCKED_PREIMAGES = {
    "server/content_domains/digital_ip.py": (
        "814999a3654df59b41e889c53237adca99c9471a",
        "45cc5ad234e3fc95f9e77fd0796e1ae9ca1a133fbbb3cee0fc0d01df3c111618",
    ),
    "server/content_domains/egress.py": (
        "bcedb7c52101b95acc238031808100b22385874e",
        "102798fbdfc0e581bce1c254b426d6c78d1f2d77654a07a9812da69fe5dd2e79",
    ),
}


def _blob_id(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


class IP12OpenAIEgressReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    def test_manifest_is_test_only_and_never_copies_secrets_or_state(self):
        self.assertEqual(self.manifest["target"]["role"], "test")
        self.assertEqual(self.manifest["target"]["host"], "8.148.158.106")
        policy = self.manifest["deployment_policy"]
        self.assertFalse(policy["production_server_write_allowed"])
        self.assertFalse(policy["copy_environment_or_database"])

    def test_file_scope_preimages_and_current_postimages_are_exact(self):
        entries = self.manifest["files"]
        self.assertEqual(
            {entry["repository_path"] for entry in entries}, set(LOCKED_PREIMAGES)
        )
        actual_preimages = {}
        for entry in entries:
            data = (ROOT / entry["repository_path"]).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), entry["postimage_sha256"])
            self.assertEqual(_blob_id(data), entry["postimage_blob"])
            actual_preimages[entry["repository_path"]] = (
                entry["preimage_blob"], entry["preimage_sha256"],
            )
        self.assertEqual(actual_preimages, LOCKED_PREIMAGES)

    def test_deployment_fails_closed_and_rollback_restores_both_files(self):
        policy = self.manifest["deployment_policy"]
        self.assertTrue(policy["fail_closed_on_preimage_mismatch"])
        self.assertTrue(policy["fail_closed_on_source_blob_mismatch"])
        self.assertTrue(policy["stage_and_validate_all_candidates_before_first_write"])
        self.assertTrue(policy["rollback_both_files_as_one_unit"])
        checks = "\n".join(self.manifest["pre_write_checks"])
        self.assertIn("both runtime files", checks)
        self.assertIn("py_compile", checks)
        self.assertIn("isolated import", checks)
        rollback = json.dumps(self.manifest["rollback"], ensure_ascii=False)
        self.assertIn("both", rollback)
        self.assertIn("preimage", rollback)
        self.assertIn("HTTP 200", rollback)
        self.assertIn("HTTP 401", rollback)

    def test_deployment_and_rollback_probes_cannot_issue_paid_requests(self):
        deploy_checks = "\n".join(self.manifest["post_deploy_checks"])
        rollback_checks = "\n".join(self.manifest["rollback"]["verify"])
        for checks in (deploy_checks, rollback_checks):
            self.assertIn("unauthenticated POST", checks)
            self.assertIn("outbound model request", checks)


if __name__ == "__main__":
    unittest.main()
