import copy
import hashlib
import importlib.util
import json
import pathlib
import subprocess
import sys
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = "b931da6581f263f720deb9c0f9b767a52aaec421"
OLD_MANIFEST = (
    ROOT / "release-manifests" / "test-runtime" /
    "expired-audio-reupload-v1-20260826.json"
)
OLD_EXECUTOR = (
    ROOT / "release-manifests" / "tools" /
    "deploy_expired_audio_reupload_locked_manifest.py"
)
MANIFEST = (
    ROOT / "release-manifests" / "test-runtime" /
    "expired-audio-reupload-runuser-v2-20260826.json"
)
EXECUTOR = (
    ROOT / "release-manifests" / "tools" /
    "deploy_expired_audio_reupload_runuser_v2_locked_manifest.py"
)
IMPACT = (
    ROOT / "release-manifests" / "test-runtime" /
    "expired-audio-reupload-runuser-v2-impact-20260826.json"
)
SERVICE_USER_TEST = "tests.test_digital_human_local_material_library"
LOCKED_SERVICE_USER_ARGV = [
    "/usr/sbin/runuser", "-u", "ubuntu", "--", "/usr/bin/python3",
    "-m", "unittest", SERVICE_USER_TEST, "-v",
]


def _blob(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _load_executor():
    scripts = ROOT / "scripts"
    sys.path.insert(0, str(scripts))
    try:
        sys.modules.pop("verify_content_whisper_deployment", None)
        spec = importlib.util.spec_from_file_location(
            "expired_audio_runuser_v2_release_test", EXECUTOR,
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(scripts))


class ExpiredAudioRunuserV2ReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        cls.old_manifest = json.loads(OLD_MANIFEST.read_text(encoding="utf-8"))
        cls.impact = json.loads(IMPACT.read_text(encoding="utf-8"))
        cls.module = _load_executor()

    def test_historical_manifest_and_executor_bytes_remain_exact(self):
        for path in (OLD_MANIFEST, OLD_EXECUTOR):
            relative = path.relative_to(ROOT).as_posix()
            expected = subprocess.run(
                ["git", "cat-file", "blob", BASE + ":" + relative],
                cwd=ROOT, check=True, stdout=subprocess.PIPE,
            ).stdout
            self.assertEqual(expected, path.read_bytes())

    def test_successor_preserves_exact_three_target_payload_and_impact(self):
        self.assertEqual(self.old_manifest["files"], self.manifest["files"])
        self.assertEqual(
            self.impact["runtime_targets"],
            [item["repository_path"] for item in self.manifest["files"]],
        )
        self.assertFalse(self.impact["business_payload_changed"])
        self.assertEqual(
            self.impact["supersedes_manifest"],
            OLD_MANIFEST.relative_to(ROOT).as_posix(),
        )
        self.assertEqual(self.impact["preflight_change"], {
            "test_module": SERVICE_USER_TEST,
            "required_user": "ubuntu",
            "launcher": "/usr/sbin/runuser",
            "nested_tool": "/usr/bin/python3",
        })

    def test_successor_manifest_is_manifest_only_child_of_code_source(self):
        relative = MANIFEST.relative_to(ROOT).as_posix()
        current_blob = _blob(MANIFEST.read_bytes())
        commits = subprocess.run(
            ["git", "rev-list", "HEAD", "--", relative], cwd=ROOT,
            check=True, text=True, stdout=subprocess.PIPE,
        ).stdout.splitlines()
        locked = next(commit for commit in commits if subprocess.run(
            ["git", "rev-parse", commit + ":" + relative], cwd=ROOT,
            check=True, text=True, stdout=subprocess.PIPE,
        ).stdout.strip() == current_blob)
        parent = subprocess.run(
            ["git", "rev-parse", locked + "^"], cwd=ROOT, check=True,
            text=True, stdout=subprocess.PIPE,
        ).stdout.strip()
        self.assertEqual(self.manifest["source"]["code_source_commit"], parent)
        changed = subprocess.run(
            ["git", "diff", "--name-only", parent, locked], cwd=ROOT,
            check=True, text=True, stdout=subprocess.PIPE,
        ).stdout.splitlines()
        self.assertEqual([relative], changed)

    def test_only_permission_test_runs_as_ubuntu_and_all_tests_remain(self):
        old = self.old_manifest["release_commands"]["no_charge"]
        successor = self.manifest["release_commands"]["no_charge"]
        self.assertEqual(len(old), len(successor))
        old_modules = [item["argv"][-2] for item in old]
        successor_modules = [item["argv"][-2] for item in successor]
        self.assertEqual(old_modules, successor_modules)
        commands = {item["argv"][-2]: item["argv"] for item in successor}
        self.assertEqual(commands[SERVICE_USER_TEST], LOCKED_SERVICE_USER_ARGV)
        for module_name, argv in commands.items():
            self.assertEqual(argv[-1], "-v")
            if module_name != SERVICE_USER_TEST:
                self.assertEqual(argv[0], "/usr/bin/python3")

    def test_root_executor_spawns_permission_test_through_runuser(self):
        runner = self.module.CommandRunner()
        calls = []

        def capture(argv, **kwargs):
            calls.append((argv, kwargs))
            return mock.Mock(returncode=0)

        with mock.patch.object(self.module.subprocess, "run", side_effect=capture):
            with mock.patch.object(
                    self.module.os, "geteuid", return_value=0, create=True):
                runner.run(
                    "no_charge", self.manifest["release_commands"]["no_charge"],
                    source_root=ROOT, runtime_root=ROOT,
                )
        permission_calls = [item for item in calls if SERVICE_USER_TEST in item[0]]
        self.assertEqual(len(permission_calls), 1)
        self.assertEqual(permission_calls[0][0], LOCKED_SERVICE_USER_ARGV)
        self.assertTrue(permission_calls[0][1]["check"])

    def _preflight(self, manifest):
        release = object.__new__(self.module.ContentWhisperRelease)
        release.manifest = manifest
        release._validate_deployment_tool = mock.Mock()
        release.checkpoint = mock.Mock()
        release._preflight_release_commands()
        return release

    def test_preflight_validates_runuser_and_nested_absolute_tool(self):
        release = self._preflight(copy.deepcopy(self.manifest))
        checked = [call.args[0] for call in release._validate_deployment_tool.call_args_list]
        self.assertIn("/usr/sbin/runuser", checked)
        self.assertIn("/usr/bin/python3", checked)

    def test_tampered_runuser_user_tool_or_parameters_fail_closed(self):
        mutations = (
            (1, "--user"),
            (2, "root"),
            (3, "-c"),
            (4, "python3"),
            (4, "/usr/bin/env"),
            (5, "-I"),
            (7, "tests.test_digital_human_v2"),
            (8, "--failfast"),
        )
        for index, value in mutations:
            with self.subTest(index=index, value=value):
                manifest = copy.deepcopy(self.manifest)
                command = manifest["release_commands"]["no_charge"][1]
                command["argv"][index] = value
                with self.assertRaisesRegex(
                        self.module.ReleaseError,
                        "locked service-user test|must run as the locked service user|exactly once"):
                    self._preflight(manifest)

    def test_removing_runuser_or_permission_test_fails_closed(self):
        direct = copy.deepcopy(self.manifest)
        direct["release_commands"]["no_charge"][1]["argv"] = [
            "/usr/bin/python3", "-m", "unittest", SERVICE_USER_TEST, "-v",
        ]
        with self.assertRaisesRegex(
                self.module.ReleaseError, "must run as the locked service user"):
            self._preflight(direct)
        missing = copy.deepcopy(self.manifest)
        del missing["release_commands"]["no_charge"][1]
        with self.assertRaisesRegex(self.module.ReleaseError, "exactly once"):
            self._preflight(missing)


if __name__ == "__main__":
    unittest.main()
