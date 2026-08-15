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

    def test_exact_unrelated_pip_conflict_may_be_declared_without_hiding_it(self):
        path = self._requirements("alpha==1.0\n")
        versions = {"alpha": "1.0", "PyGObject": "3.42.1"}
        result = subprocess.CompletedProcess(
            [], 1,
            stdout=(
                "pygobject 3.42.1 requires pycairo, which is not installed.\n"
            ),
            stderr="",
        )
        with mock.patch.object(
                self.module.importlib.metadata, "version",
                side_effect=lambda name: versions[name]), \
             mock.patch.object(
                 self.module.subprocess, "run", return_value=result):
            self.assertEqual(
                1,
                self.module.verify_installed(
                    path,
                    allowed_broken=["PyGObject==3.42.1:pycairo"],
                ),
            )

    def test_allowed_conflict_is_exact_and_never_covers_content_pins(self):
        path = self._requirements("alpha==1.0\n")
        versions = {"alpha": "1.0", "PyGObject": "3.42.1"}
        with mock.patch.object(
                self.module.importlib.metadata, "version",
                side_effect=lambda name: versions[name]):
            for allowed in (
                    ["not-a-contract"],
                    ["alpha==1.0:missing"],
                    ["PyGObject==9.9:pycairo"]):
                with self.subTest(allowed=allowed), self.assertRaises(RuntimeError):
                    self.module.verify_installed(path, allowed_broken=allowed)

    def test_allowed_conflict_does_not_hide_any_other_pip_problem(self):
        path = self._requirements("alpha==1.0\n")
        versions = {"alpha": "1.0", "PyGObject": "3.42.1"}
        result = subprocess.CompletedProcess(
            [], 1,
            stdout=(
                "pygobject 3.42.1 requires pycairo, which is not installed.\n"
                "beta 2.0 requires gamma, which is not installed.\n"
            ),
            stderr="",
        )
        with mock.patch.object(
                self.module.importlib.metadata, "version",
                side_effect=lambda name: versions[name]), \
             mock.patch.object(
                 self.module.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(RuntimeError, "beta 2.0 requires gamma"):
                self.module.verify_installed(
                    path,
                    allowed_broken=["PyGObject==3.42.1:pycairo"],
                )


if __name__ == "__main__":
    unittest.main()
