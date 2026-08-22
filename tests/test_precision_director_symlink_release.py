import hashlib
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "deploy/test-runtime/digital-human-precision-director-v4-20260822.json"
EXECUTOR = ROOT / "scripts/deploy_precision_director_v4_locked_manifest.py"
HISTORICAL_V3 = {
    "scripts/deploy_precision_director_locked_manifest.py": (
        "fcbd6526ae11806ecea19b02c6507fa6345b6c27",
        "da46b99894cde08dbdebe221632d5447d4259e9298678ef71f00411c5d38fbfc",
    ),
    "deploy/test-runtime/digital-human-precision-director-v3-20260822.json": (
        "1155ba8d842d1c2a9a5cd0a9a5955a184f0f5dd2",
        "8417f2ed45316eafdf172a1c24a34eed1dfaaaab68980869ba7633ccc9519a5b",
    ),
}


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def git_blob(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


class FilesystemHooks:
    def __init__(self, executor, fail_probe=False):
        system = executor.SystemHooks()
        self.link_state = system.link_state
        self.replace_symlink = system.replace_symlink
        self.fail_probe = fail_probe
        self.calls = []

    def validate_import(self, python_root, modules):
        self.calls.append(("imports", tuple(modules)))

    def validate_node(self, path):
        self.calls.append(("node", pathlib.Path(path).name))

    def validate_nginx(self):
        self.calls.append(("nginx-test",))

    def validate_nginx_candidate(self, candidate, reviewed_source):
        self.calls.append(("nginx-candidate", pathlib.Path(candidate).read_bytes()))

    def reload_nginx(self):
        self.calls.append(("nginx-reload",))

    def service_active(self, service):
        self.calls.append(("active", service))
        return True

    def restart(self, service):
        self.calls.append(("restart", service))

    def probe(self, url, method, expected_status):
        self.calls.append(("probe", url, method, expected_status))
        if self.fail_probe:
            self.fail_probe = False
            raise RuntimeError("injected forward health failure")

    def probe_static(self, url, expected_status, expected_sha256):
        self.calls.append(("static", url, expected_status, expected_sha256))


class PrecisionDirectorSymlinkReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        specification = importlib.util.spec_from_file_location(
            "precision_symlink_release", EXECUTOR,
        )
        cls.executor = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(cls.executor)
        cls.manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        renderer_spec = importlib.util.spec_from_file_location(
            "precision_symlink_renderer",
            ROOT / "deploy/render_yuelei_test_nginx.py",
        )
        cls.renderer = importlib.util.module_from_spec(renderer_spec)
        renderer_spec.loader.exec_module(cls.renderer)

    def _blob(self, blob_id):
        return subprocess.run(
            ["git", "cat-file", "blob", blob_id], cwd=ROOT, check=True,
            stdout=subprocess.PIPE,
        ).stdout

    def _target_tree(self, root, *, link_target=None):
        root = pathlib.Path(root)
        for item in self.manifest["files"]:
            target = root.joinpath(
                *pathlib.PurePosixPath(item["runtime_path"]).parts[1:]
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            if item["target_preimage_state"] == "file":
                data = self._blob(item["preimage_blob"])
                self.assertEqual(item["preimage_sha256"], sha256(data))
                target.write_bytes(data)
        nginx = self.manifest["nginx_contract"]
        nginx_target = root.joinpath(
            *pathlib.PurePosixPath(nginx["runtime_path"]).parts[1:]
        )
        nginx_target.parent.mkdir(parents=True, exist_ok=True)
        base_source = subprocess.run(
            [
                "git", "show",
                self.manifest["expected_preimage"]["development_base_commit"]
                + ":" + nginx["source_repository_path"],
            ],
            cwd=ROOT, check=True, stdout=subprocess.PIPE,
        ).stdout.decode("utf-8")
        nginx_preimage = self.renderer.render_config(base_source).encode("utf-8")
        self.assertEqual(nginx["preimage_blob"], git_blob(nginx_preimage))
        self.assertEqual(nginx["preimage_sha256"], sha256(nginx_preimage))
        nginx_target.write_bytes(nginx_preimage)
        main_vhost = root / "etc/nginx/sites-available/huangquechuanmei"
        main_vhost.write_bytes(b"protected-main-site-vhost\n")
        enabled = root.joinpath(
            *pathlib.PurePosixPath(nginx["enabled_runtime_path"]).parts[1:]
        )
        enabled.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(link_target or nginx["enabled_preimage_target"], enabled)
        return root, enabled

    def test_historical_v3_manifest_and_executor_are_exact_locked_blobs(self):
        for relative, (blob_id, digest) in HISTORICAL_V3.items():
            data = (ROOT / relative).read_bytes()
            self.assertEqual(blob_id, git_blob(data), relative)
            self.assertEqual(digest, sha256(data), relative)
            self.assertEqual(data, self._blob(blob_id), relative)

    def test_v4_manifest_locks_new_executor_and_real_entry(self):
        loaded = self.executor._load_manifest(MANIFEST)
        release = loaded["release_executor"]
        data = EXECUTOR.read_bytes()
        self.assertEqual("digital_human_precision_director_v4", release["contract"])
        self.assertEqual(release["git_blob"], git_blob(data))
        self.assertEqual(release["sha256"], sha256(data))
        with self.assertRaisesRegex(self.executor.ReleaseError, "other release contract"):
            self.executor._load_manifest(
                ROOT / "deploy/test-runtime/digital-human-precision-director-v3-20260822.json"
            )
        with tempfile.TemporaryDirectory() as directory:
            wrong = pathlib.Path(directory) / MANIFEST.name
            shutil.copy2(MANIFEST, wrong)
            with self.assertRaisesRegex(self.executor.ReleaseError, "locked source path"):
                self.executor._load_manifest(wrong)

    def test_v4_source_binding_requires_exact_reviewed_parent(self):
        manifest = json.loads(json.dumps(self.manifest))
        parent = "a" * 40
        reviewed = "b" * 40
        merged = "c" * 40
        manifest["source"]["code_source_commit"] = parent

        def git_result(command, cwd=None, env=None):
            if command[:2] == ["git", "rev-parse"]:
                self.assertEqual(reviewed + "^", command[2])
                return parent
            if command[:3] == ["git", "diff", "--name-only"]:
                return MANIFEST.relative_to(ROOT).as_posix()
            raise AssertionError(command)

        with mock.patch.object(
                self.executor, "_verify_director_checkout", return_value=merged), \
             mock.patch.object(self.executor, "_run", side_effect=git_result):
            self.assertEqual(
                merged,
                self.executor._verify_precision_checkout(
                    ROOT, manifest, reviewed, merged,
                ),
            )
            manifest["source"]["code_source_commit"] = "d" * 40
            with self.assertRaisesRegex(self.executor.ReleaseError, "exact parent"):
                self.executor._verify_precision_checkout(
                    ROOT, manifest, reviewed, merged,
                )

    def test_symlink_node_mapping_does_not_follow_final_component(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            available = root / "etc/nginx/sites-available/yuelei-test.conf"
            available.parent.mkdir(parents=True)
            available.write_text("server {}", encoding="utf-8")
            enabled = root / "etc/nginx/sites-enabled/yuelei-test.conf"
            enabled.parent.mkdir(parents=True)
            os.symlink("../sites-available/yuelei-test.conf", enabled)
            mapped = self.executor._mapped_symlink_node(
                root, "/etc/nginx/sites-enabled/yuelei-test.conf",
            )
            self.assertEqual(enabled, mapped)
            self.assertTrue(mapped.is_symlink())
            self.assertEqual("../sites-available/yuelei-test.conf", os.readlink(mapped))

    def test_replace_symlink_is_atomic_at_the_observed_replace_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "yuelei-test.conf"
            os.symlink("old.conf", path)
            real_replace = os.replace

            def inspect_replace(source, destination):
                self.assertTrue(path.is_symlink())
                self.assertEqual("old.conf", os.readlink(path))
                self.assertTrue(pathlib.Path(source).is_symlink())
                self.assertEqual("new.conf", os.readlink(source))
                return real_replace(source, destination)

            with mock.patch.object(
                    self.executor.os, "replace", side_effect=inspect_replace):
                self.executor.SystemHooks().replace_symlink(path, "new.conf")
            self.assertEqual("new.conf", os.readlink(path))
            self.assertEqual([], list(path.parent.glob(".*.tmp")))

    def test_real_symlink_success_and_preimage_mismatch(self):
        with tempfile.TemporaryDirectory() as target_dir, \
                tempfile.TemporaryDirectory() as backup_dir:
            target, enabled = self._target_tree(target_dir)
            result = self.executor.execute_locked_release(
                MANIFEST, ROOT, target, pathlib.Path(backup_dir),
                hooks=FilesystemHooks(self.executor), verify_repository=False,
                reviewed_head="r" * 40, merged_main="m" * 40,
            )
            self.assertEqual("deployed", result["status"])
            self.assertTrue(enabled.is_symlink())
            self.assertEqual(
                self.manifest["nginx_contract"]["enabled_postimage_target"],
                os.readlink(enabled),
            )
        with tempfile.TemporaryDirectory() as target_dir, \
                tempfile.TemporaryDirectory() as backup_dir:
            target, _ = self._target_tree(target_dir, link_target="wrong.conf")
            with self.assertRaisesRegex(
                    self.executor.ReleaseError, "symlink preimage mismatch"):
                self.executor.execute_locked_release(
                    MANIFEST, ROOT, target, pathlib.Path(backup_dir),
                    hooks=FilesystemHooks(self.executor), verify_repository=False,
                    reviewed_head="r" * 40, merged_main="m" * 40,
                )
            self.assertEqual([], list(pathlib.Path(backup_dir).iterdir()))

    def test_real_symlink_is_restored_after_forward_failure(self):
        with tempfile.TemporaryDirectory() as target_dir, \
                tempfile.TemporaryDirectory() as backup_dir:
            target, enabled = self._target_tree(target_dir)
            with self.assertRaisesRegex(RuntimeError, "forward health"):
                self.executor.execute_locked_release(
                    MANIFEST, ROOT, target, pathlib.Path(backup_dir),
                    hooks=FilesystemHooks(self.executor, fail_probe=True),
                    verify_repository=False, reviewed_head="r" * 40,
                    merged_main="m" * 40,
                )
            nginx = self.manifest["nginx_contract"]
            self.assertTrue(enabled.is_symlink())
            self.assertEqual(nginx["enabled_preimage_target"], os.readlink(enabled))
            audit_path = next(pathlib.Path(backup_dir).glob("*/audit.json"))
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
            self.assertEqual("rolled_back", audit["status"])
            self.assertEqual([], audit["rollback_errors"])


if __name__ == "__main__":
    unittest.main()
