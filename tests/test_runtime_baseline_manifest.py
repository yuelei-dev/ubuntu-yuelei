import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts.verify_runtime_baseline import validate_manifest, verify_targets


class RuntimeBaselineManifestTests(unittest.TestCase):
    def _manifest(self, target: str, payload: bytes = b"baseline") -> dict:
        return {
            "synchronized_files": [
                {
                    "source": "content-api/content_domains/example.py",
                    "target": target,
                    "state_before": "different",
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            ]
        }

    def test_rejects_runtime_data_and_secret_files(self):
        for target in (
            "server/content_out/result.json",
            "server/content.env",
            "server/users.db",
            "site/logs/request.log",
        ):
            with self.subTest(target=target):
                self.assertTrue(validate_manifest(self._manifest(target)))

    def test_verifies_an_imported_target_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "server" / "example.py"
            target.parent.mkdir()
            target.write_bytes(b"baseline")
            self.assertEqual(
                verify_targets(root, self._manifest("server/example.py")),
                [],
            )

    def test_detects_drift_without_modifying_the_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "server" / "example.py"
            target.parent.mkdir()
            target.write_bytes(b"changed")
            errors = verify_targets(root, self._manifest("server/example.py"))
            self.assertEqual(errors, ["target hash differs: server/example.py"])


if __name__ == "__main__":
    unittest.main()
