# -*- coding: utf-8 -*-
import hashlib
import json
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "deploy" / "test-runtime" / "director-production-parity-20260810.json"
LOCKED_PREIMAGES = {
    "server/content_domains/breakdown.py": (
        "78a1d22c481266ca04d81f5f3ab9fcc06af3a88b",
        "950e3171523f0267084e9fb581d05088f00bd7646870914c7bfec2723204f263",
    ),
    "server/content_domains/gemini_reverse.py": (
        "b36c247fa09281378821119746622f7a382ecfff",
        "f8df21e8883e79fa749052fa336012fee8ca7202af4e1b12801a82cdd7c5768a",
    ),
    "server/tikhub.py": (
        "8023977fc79118759c09eb1c6122b39d5aa96015",
        "223875db6e1f10f15f88f63f3deab8f699c78912d77bb547e31d961074656343",
    ),
    "server/content_domains/core.py": (
        "32ce1cbb762531c8ccfc9ff7057f9b15109d06fa",
        "bb12e588c76c3f2659782c694a37f2f5d6d6ae0d375df6b508e6f62f984246a0",
    ),
    "site/workbench/script.html": (
        "06715a917bf934f02d802fafff91a98949e92a9d",
        "cb425ccfb266e4d1cd05d4413bcab0f6c2e1f772256746862c266f03f569bfc0",
    ),
}


def _blob_id(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


class DirectorProductionParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    def test_manifest_is_test_only_and_production_remains_read_only(self):
        self.assertEqual(self.manifest["target"]["role"], "test")
        self.assertEqual(self.manifest["target"]["host"], "8.148.158.106")
        self.assertFalse(
            self.manifest["deployment_policy"]["production_server_write_allowed"]
        )
        self.assertFalse(
            self.manifest["deployment_policy"]["copy_environment_or_database"]
        )

    def test_runtime_file_scope_and_postimages_are_exact(self):
        entries = self.manifest["files"]
        self.assertEqual(
            {entry["repository_path"] for entry in entries},
            {
                "server/content_domains/breakdown.py",
                "server/content_domains/gemini_reverse.py",
                "server/content_domains/core.py",
                "server/tikhub.py",
                "site/workbench/script.html",
            },
        )
        for entry in entries:
            data = (ROOT / entry["repository_path"]).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), entry["postimage_sha256"])
            self.assertEqual(_blob_id(data), entry["postimage_blob"])

    def test_preimages_are_the_reviewed_locked_values(self):
        actual = {
            entry["repository_path"]: (
                entry["preimage_blob"],
                entry["preimage_sha256"],
            )
            for entry in self.manifest["files"]
        }
        self.assertEqual(actual, LOCKED_PREIMAGES)

    def test_exact_source_files_keep_the_locked_production_blobs(self):
        exact = [
            entry for entry in self.manifest["files"]
            if entry["strategy"] == "production_exact_file"
        ]
        self.assertEqual(len(exact), 2)
        for entry in exact:
            self.assertEqual(entry["postimage_blob"], entry["production_blob"])

    def test_yuelei_only_material_and_provider_safety_is_not_removed(self):
        self.assertEqual(
            self.manifest["preserved_yuelei_safety_files"],
            [
                "server/content_domains/script_to_video.py",
                "server/content_domains/video.py",
                "server/content_domains/feature_flags.py",
            ],
        )
        script_to_video = (ROOT / "server/content_domains/script_to_video.py").read_text(
            encoding="utf-8"
        )
        video = (ROOT / "server/content_domains/video.py").read_text(encoding="utf-8")
        feature_flags = (ROOT / "server/content_domains/feature_flags.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("def _prepare_frozen_materials", script_to_video)
        self.assertIn("def recover_paid_job_error", script_to_video)
        self.assertIn('phase == "provider_submitted"', script_to_video)
        self.assertIn("def stage_seedance_references", video)
        self.assertIn("def cleanup_staged_seedance_references", video)
        self.assertIn('"key": "seedance_video"', feature_flags)

    def test_director_submission_contract_matches_production_behavior(self):
        page = (ROOT / "site/workbench/script.html").read_text(encoding="utf-8")
        core = (ROOT / "server/content_domains/core.py").read_text(encoding="utf-8")
        self.assertIn("'Idempotency-Key':copyPending.key", page)
        self.assertIn("'Idempotency-Key':breakdownPending.key", page)
        self.assertIn("'Idempotency-Key':imagePending.key", page)
        self.assertIn("requestHeaders['Idempotency-Key']=videoPending.key", page)
        self.assertIn("source_page:'script'", page)
        self.assertIn('"script_to_video", "breakdown", "copy"}', core)


if __name__ == "__main__":
    unittest.main()
