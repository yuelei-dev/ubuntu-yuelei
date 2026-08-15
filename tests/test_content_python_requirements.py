# -*- coding: utf-8 -*-
import importlib.util
import pathlib
import subprocess
import tempfile
import types
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify_content_python_requirements.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("content_requirements", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ContentPythonRequirementsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load_module()

    def _requirements(self, content):
        temporary = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".txt", delete=False,
        )
        self.addCleanup(pathlib.Path(temporary.name).unlink, missing_ok=True)
        with temporary:
            temporary.write(content)
        return temporary.name

    def test_exact_pins_and_consistent_environment_pass_read_only(self):
        path = self._requirements("# locked\nAlpha_Pkg==1.2.3\nbeta-pkg==4.5.6\n")
        versions = {"Alpha_Pkg": "1.2.3", "beta-pkg": "4.5.6"}
        with mock.patch.object(
                self.module.importlib.metadata, "version",
                side_effect=lambda name: versions[name]), \
             mock.patch.object(
                 self.module.subprocess, "run",
                 return_value=types.SimpleNamespace(returncode=0)) as run:
            self.assertEqual(2, self.module.verify_installed(path))
        argv = run.call_args.args[0]
        self.assertEqual([self.module.sys.executable, "-m", "pip", "check"], argv)
        self.assertNotIn("install", argv)

    def test_missing_or_wrong_version_fails_before_pip_check(self):
        path = self._requirements("alpha==1.0\nbeta==2.0\n")

        def version(name):
            if name == "alpha":
                return "0.9"
            raise self.module.importlib.metadata.PackageNotFoundError(name)

        with mock.patch.object(
                self.module.importlib.metadata, "version", side_effect=version), \
             mock.patch.object(self.module.subprocess, "run") as run:
            with self.assertRaisesRegex(
                    RuntimeError, "alpha is 0.9.*beta is missing"):
                self.module.verify_installed(path)
        run.assert_not_called()

    def test_unpinned_duplicate_and_empty_requirements_fail_closed(self):
        for content in (
                "alpha>=1.0\n", "alpha==1.0\nAlpha==1.0\n", "# only comments\n"):
            with self.subTest(content=content):
                with self.assertRaises(RuntimeError):
                    self.module.read_exact_pins(self._requirements(content))

    def test_inconsistent_transitive_environment_fails(self):
        path = self._requirements("alpha==1.0\n")
        with mock.patch.object(
                self.module.importlib.metadata, "version", return_value="1.0"), \
             mock.patch.object(
                 self.module.subprocess, "run",
                 return_value=subprocess.CompletedProcess([], 1)):
            with self.assertRaisesRegex(RuntimeError, "inconsistent environment"):
                self.module.verify_installed(path)


if __name__ == "__main__":
    unittest.main()
