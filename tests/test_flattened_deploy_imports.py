import os
import pathlib
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class FlattenedDeployImportTests(unittest.TestCase):
    def test_sound_modules_import_with_only_deployed_server_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "server")
            env["CONTENT_OUT"] = str(pathlib.Path(tmp) / "content_out")
            proc = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "from content_domains import "
                        "short_drama_sound_design, short_drama_sound_effect; "
                        "from providers import sound_effects"
                    ),
                ],
                cwd=tmp,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
