import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "deploy" / "runtime-manifest.json"
BUILDER = ROOT / "scripts" / "build_test_runtime_release.py"


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class RuntimeManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    def test_test_only_and_no_unresolved_sensitive_files(self):
        self.assertEqual(self.manifest["server_role"], "test-only")
        self.assertFalse(self.manifest["production_connected"])
        self.assertEqual(self.manifest["sensitive_files_not_written"], [])
        self.assertEqual(self.manifest["git_path_collisions"], [])

    def test_paths_are_unique_safe_and_hash_exact(self):
        seen = set()
        for entry in self.manifest["files"]:
            git_path = entry["git_path"]
            posix = PurePosixPath(git_path)
            self.assertFalse(posix.is_absolute())
            self.assertNotIn("..", posix.parts)
            self.assertTrue(git_path.startswith(("server/", "site/")))
            self.assertNotIn(git_path, seen)
            seen.add(git_path)
            local = ROOT / Path(*posix.parts)
            self.assertTrue(local.is_file(), git_path)
            self.assertEqual(sha256_file(local), entry["sha256"], git_path)
        self.assertEqual(len(seen), self.manifest["included_file_count"])

    def test_external_state_is_excluded(self):
        exclusions = "\n".join(self.manifest["excluded_external_state"]).lower()
        for required in (
            "env",
            "db",
            "content_out",
            "uploads",
            "logs",
            "password",
            "api keys",
            "tls",
            "user",
        ):
            self.assertIn(required, exclusions)

    def test_builder_verifies_and_reproduces_all_manifest_bytes(self):
        verify = subprocess.run(
            [sys.executable, str(BUILDER), "--verify-only"],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        self.assertEqual(verify.returncode, 0, verify.stderr)
        with tempfile.TemporaryDirectory() as temporary:
            build = subprocess.run(
                [sys.executable, str(BUILDER), "--output", temporary],
                cwd=ROOT,
                text=True,
                capture_output=True,
            )
            self.assertEqual(build.returncode, 0, build.stderr)
            release = json.loads(
                (Path(temporary) / "release-manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                release["file_count"], self.manifest["included_file_count"]
            )
            for entry in self.manifest["files"]:
                built = Path(temporary) / Path(
                    *PurePosixPath(entry["git_path"]).parts
                )
                self.assertEqual(sha256_file(built), entry["sha256"])


if __name__ == "__main__":
    unittest.main()
