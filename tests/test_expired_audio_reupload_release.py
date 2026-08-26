import hashlib
import importlib.util
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFEST = (
    ROOT / "release-manifests" / "test-runtime" /
    "expired-audio-reupload-v1-20260826.json"
)
EXECUTOR = (
    ROOT / "release-manifests" / "tools" /
    "deploy_expired_audio_reupload_locked_manifest.py"
)
HISTORICAL_MANIFEST = (
    ROOT / "docs" / "release-manifests" /
    "digital-human-material-feishu-priority-20260823.json"
)
HISTORICAL_EXECUTOR = ROOT / "scripts" / "deploy_seedream_v3_locked_manifest.py"
EXPECTED_SCOPE = {
    "server/content_domains/script_to_video.py",
    "server/content_domains/digital_human_v2.py",
    "site/workbench/digital-human-oneclick.html",
}
OLD_PREIMAGES = {
    "server/content_domains/script_to_video.py": (
        "6b3f8b8c9068705debbd7959406362f19e821ba0",
        "a32785c2c8ead5d366c431f5c405a24da9e0e69c2296d6ccc7473028aba3389d",
    ),
    "server/content_domains/digital_human_v2.py": (
        "c12813c12d36ca93f9083dba4767fa759f81ab32",
        "e99c99f5ab8ba287b55f27a5e05c146f06b1dad9d5d7612df1c7b48611400214",
    ),
    "site/workbench/digital-human-oneclick.html": (
        "289e095bb6869337b185a8ab3ee7eff153543f08",
        "e2571f4bd310b74da5d4bae60ba368c79b9abd881bc42e6fd2ad5c658140aaae",
    ),
}


def _blob(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _load(path, name, scripts):
    sys.path.insert(0, str(scripts))
    try:
        sys.modules.pop("verify_content_whisper_deployment", None)
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(scripts))


class FakeGitRunner:
    def run(self, arguments, *, source_root, allow_failure=False):
        latest = "2" * 40
        if arguments[:2] == ["status", "--porcelain"]:
            output = ""
        elif arguments[:3] == ["symbolic-ref", "--short", "HEAD"]:
            output = "main\n"
        elif arguments in (["rev-parse", "HEAD"], ["rev-parse", "refs/remotes/origin/main"]):
            output = latest + "\n"
        elif arguments[:2] == ["ls-remote", "--exit-code"]:
            output = latest + "\trefs/heads/main\n"
        elif arguments[:2] == ["merge-base", "--is-ancestor"]:
            return types.SimpleNamespace(stdout="", returncode=0)
        else:
            raise AssertionError("unexpected git call: %r" % (arguments,))
        return types.SimpleNamespace(stdout=output, returncode=0)


class ExpiredAudioReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.source = self.root / "source"
        self.runtime = self.root / "runtime"
        self.backup = self.root / "backup"
        self.source.mkdir()
        self.runtime.mkdir()
        self.local_manifest = json.loads(json.dumps(self.manifest))
        for lock in (
            self.local_manifest["executor"],
            self.local_manifest["executor"]["verifier"],
            self.local_manifest["executor"]["requirements_verifier"],
        ):
            target = self.source / lock["repository_path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / lock["repository_path"], target)
        target = self.source / MANIFEST.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(MANIFEST, target)
        self.local_manifest["_manifest_path"] = str(target)
        self.module = _load(
            self.source / self.local_manifest["executor"]["repository_path"],
            "expired_audio_release_test", self.source / "scripts",
        )

    def tearDown(self):
        self.temp.cleanup()

    def release(self):
        return self.module.ContentWhisperRelease(
            self.local_manifest, self.source, self.runtime, self.backup,
            git_runner=FakeGitRunner(), reviewed_source_commit="1" * 40,
            reviewed_main_commit="2" * 40,
            service_environment_getter=lambda: {
                "DIGITAL_HUMAN_LOCAL_MATERIAL_LIBRARY_ROOT":
                "/home/ubuntu/material-libraries/huangque-media",
            },
            local_library_probe_runner=lambda _root, _count: {
                "ok": True, "count": 204,
                "types": {"image": 88, "video": 100, "bgm": 16},
            },
        )

    def test_combined_scope_locks_old_preimages_and_current_postimages(self):
        entries = {item["repository_path"]: item for item in self.manifest["files"]}
        self.assertEqual(EXPECTED_SCOPE, set(entries))
        for path, item in entries.items():
            self.assertEqual(OLD_PREIMAGES[path], (
                item["target_preimage_blob"], item["target_preimage_sha256"],
            ))
            data = subprocess.run(
                ["git", "cat-file", "blob", item["expected_postimage_blob"]],
                cwd=ROOT, check=True, stdout=subprocess.PIPE,
            ).stdout
            self.assertEqual(_blob(data), item["expected_postimage_blob"])
            self.assertEqual(hashlib.sha256(data).hexdigest(), item["expected_postimage_sha256"])
            self.assertEqual(item["source_blob"], item["expected_postimage_blob"])

    def test_historical_manifest_and_executor_remain_exact_main_bytes(self):
        for path in (HISTORICAL_MANIFEST, HISTORICAL_EXECUTOR):
            relative = path.relative_to(ROOT).as_posix()
            expected = subprocess.run(
                ["git", "cat-file", "blob", "256b92d99f89833dd4c18e45024a0cc07f4f0734:" + relative],
                cwd=ROOT, check=True, stdout=subprocess.PIPE,
            ).stdout
            self.assertEqual(expected, path.read_bytes())

    def test_manifest_is_reachable_manifest_only_child_of_code_source(self):
        relative = MANIFEST.relative_to(ROOT).as_posix()
        current_blob = _blob(MANIFEST.read_bytes())
        commits = subprocess.run(
            ["git", "rev-list", "HEAD", "--", relative], cwd=ROOT, check=True,
            text=True, stdout=subprocess.PIPE,
        ).stdout.splitlines()
        locked = next(commit for commit in commits if subprocess.run(
            ["git", "rev-parse", commit + ":" + relative], cwd=ROOT, check=True,
            text=True, stdout=subprocess.PIPE,
        ).stdout.strip() == current_blob)
        parent = subprocess.run(
            ["git", "rev-parse", locked + "^"], cwd=ROOT, check=True,
            text=True, stdout=subprocess.PIPE,
        ).stdout.strip()
        self.assertEqual(self.manifest["source"]["code_source_commit"], parent)
        changed = subprocess.run(
            ["git", "diff", "--name-only", parent, locked], cwd=ROOT, check=True,
            text=True, stdout=subprocess.PIPE,
        ).stdout.splitlines()
        self.assertEqual([relative], changed)

    def test_versioned_executor_accepts_only_successor_path(self):
        release = self.release()
        release._verify_release_tools()
        release._verify_source_checkout("1" * 40, "2" * 40)
        release.manifest["_manifest_path"] = str(self.source / HISTORICAL_MANIFEST.relative_to(ROOT))
        with self.assertRaisesRegex(self.module.ReleaseError, "manifest must come"):
            release._verify_source_checkout("1" * 40, "2" * 40)

    def _prepared_execute(self, feishu_effect):
        release = self.release()
        for name in (
            "_validate_target", "_verify_source_checkout", "_verify_release_tools",
            "_preflight_release_commands", "_health_probe_policy", "_validate_health_contract",
            "_run_stage", "_verify_health",
        ):
            setattr(release, name, mock.Mock())
        release._source_payloads = mock.Mock(return_value={})
        release._verify_local_library_operational = mock.Mock(side_effect=feishu_effect)
        release._backup_all = mock.Mock()
        release._install_all = mock.Mock(return_value=3)
        release._restore_all = mock.Mock()
        return release

    def test_local_library_failure_stops_before_backup_or_install(self):
        release = self._prepared_execute([
            self.module.ReleaseError("local material library operational probe failed")
        ])
        with mock.patch.object(self.module.manifest_verify, "classify_start_states", return_value=[]):
            with self.assertRaisesRegex(self.module.ReleaseError, "local material library"):
                release.execute("test@8.148.158.106")
        release._backup_all.assert_not_called()
        release._install_all.assert_not_called()

    def test_post_local_library_failure_rolls_back_all_targets(self):
        release = self._prepared_execute([
            None, self.module.ReleaseError("local material library became unreadable"),
        ])
        with mock.patch.object(self.module.manifest_verify, "classify_start_states", return_value=[]):
            with self.assertRaisesRegex(self.module.ReleaseError, "all manifest targets were restored"):
                release.execute("test@8.148.158.106")
        release._backup_all.assert_called_once_with([])
        release._install_all.assert_called_once()
        release._restore_all.assert_called_once_with()

    def test_local_contract_keeps_fixed_read_only_operational_gate(self):
        local = self.manifest["configuration_requirements"]["local_library"]
        self.assertEqual(
            local["required_root"],
            "/home/ubuntu/material-libraries/huangque-media",
        )
        self.assertEqual(local["expected_count"], 204)
        self.assertEqual(local["expected_type_counts"], {
            "image": 88, "video": 100, "bgm": 16,
        })
        self.assertTrue(local["operational_probe_required"])
        self.assertTrue(local["read_only"])
        self.assertNotIn("feishu", self.manifest["configuration_requirements"])
        executor_source = EXECUTOR.read_text(encoding="utf-8")
        self.assertNotIn("open.feishu.cn", executor_source)
        self.assertNotIn("_verify_feishu_operational", executor_source)
        self.assertTrue(self.manifest["deployment_policy"]["copy_environment_database_or_user_data"] is False)

    def test_service_root_mismatch_and_probe_result_fail_closed(self):
        release = self.release()
        release.service_environment_getter = lambda: {
            "DIGITAL_HUMAN_LOCAL_MATERIAL_LIBRARY_ROOT": "/tmp/unapproved",
        }
        with self.assertRaisesRegex(self.module.ReleaseError, "locked service configuration"):
            release._verify_local_library_operational("pre-deployment")
        release.service_environment_getter = lambda: {
            "DIGITAL_HUMAN_LOCAL_MATERIAL_LIBRARY_ROOT":
            "/home/ubuntu/material-libraries/huangque-media",
        }
        release.local_library_probe_runner = lambda _root, _count: {
            "ok": True, "count": 203,
            "types": {"image": 88, "video": 99, "bgm": 16},
        }
        with self.assertRaisesRegex(self.module.ReleaseError, "result is invalid"):
            release._verify_local_library_operational("post-restart")


if __name__ == "__main__":
    unittest.main()
