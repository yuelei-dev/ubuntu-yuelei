import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RuntimeSixServiceSmokeFixtureTests(unittest.TestCase):
    def test_workflow_installs_required_import_dependency(self):
        workflow = (
            ROOT / ".github/workflows/runtime-test-baseline.yml"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "python -m pip install cryptography==45.0.7",
            workflow,
        )

    def test_generated_output_is_redirected_to_disposable_state(self):
        harness = (ROOT / "scripts/runtime_six_service_smoke.py").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            '"CONTENT_OUT": str(state / "content_out")',
            harness,
        )
        self.assertNotIn(
            '"CONTENT_OUT": "/home/ubuntu/content-api/content_out"',
            harness,
        )
        self.assertIn(
            '"CONTENT_JOB_DB": str(state / "content_jobs.db")',
            harness,
        )
        self.assertNotIn(
            '"CONTENT_JOB_DB": str(server / "content_jobs.db")',
            harness,
        )

    def test_smoke_uses_writable_instance_without_mutating_release(self):
        harness = (ROOT / "scripts/runtime_six_service_smoke.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("release_before = tree_fingerprint(release)", harness)
        self.assertIn("shutil.copytree(release, runtime)", harness)
        self.assertIn("release_after = tree_fingerprint(release)", harness)
        self.assertIn('"source_release_unchanged": source_release_unchanged', harness)
        self.assertIn('and payload["source_release_unchanged"]', harness)


if __name__ == "__main__":
    unittest.main()
