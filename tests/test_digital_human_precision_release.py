import hashlib
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "deploy/test-runtime/digital-human-precision-director-v3-20260822.json"
EXECUTOR = ROOT / "scripts/deploy_precision_director_locked_manifest.py"
HISTORICAL_LOCKS = {
    "deploy/test-runtime/director-agent-v1-20260820.json": (
        "0dc5c7eb3aa7dc69d68e6362baafa52b2780a2b7",
        "608a2b1e2b20ddecb1a1e48f44844de70b9d3dbe7b22aa07d922cc2250a6f450",
    ),
    "deploy/test-runtime/director-agent-v2-20260821.json": (
        "f0138db2dd5bcc670aa0ceffd8a6073c5d8cc68e",
        "871f4268fe2e314e826da5a1ad12e10cd3c12d6cfc7f1291975c15606c5089b9",
    ),
}
HISTORICAL_EXECUTOR = (
    ROOT / "scripts/deploy_director_locked_manifest.py",
    "4805b8f79e0f650be3185b505253231520e74e63",
    "a319b3edf1ac66d44e1c3e1b10defc753eeb7a7b8e172f1f12cd89ebf17e0aa4",
)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def git_blob(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


class Hooks:
    def __init__(self, fail_probe=False):
        self.calls = []
        self.fail_probe = fail_probe

    def validate_import(self, python_root, modules):
        self.calls.append(("imports", tuple(modules)))

    def validate_node(self, path):
        self.calls.append(("node", pathlib.Path(path).name))

    def validate_nginx(self):
        self.calls.append(("nginx-test",))

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


class PrecisionDirectorReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        specification = importlib.util.spec_from_file_location("precision_release", EXECUTOR)
        cls.executor = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(cls.executor)
        cls.manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    def _target_tree(self, root):
        for item in self.manifest["files"]:
            target = pathlib.Path(root).joinpath(*pathlib.PurePosixPath(item["runtime_path"]).parts[1:])
            target.parent.mkdir(parents=True, exist_ok=True)
            if item["target_preimage_state"] == "file":
                data = subprocess.run(
                    ["git", "show", "origin/main:" + item["repository_path"]],
                    cwd=ROOT, check=True, stdout=subprocess.PIPE,
                ).stdout
                self.assertEqual(item["preimage_blob"], git_blob(data))
                self.assertEqual(item["preimage_sha256"], sha256(data))
                target.write_bytes(data)
        return pathlib.Path(root)

    def test_historical_director_manifests_remain_exact_locked_blobs(self):
        for path, (blob_id, digest) in HISTORICAL_LOCKS.items():
            data = (ROOT / path).read_bytes()
            self.assertEqual(blob_id, git_blob(data), path)
            self.assertEqual(digest, sha256(data), path)
            locked = subprocess.run(
                ["git", "cat-file", "blob", blob_id], cwd=ROOT,
                check=True, stdout=subprocess.PIPE,
            ).stdout
            self.assertEqual(data, locked, path)
        path, blob_id, digest = HISTORICAL_EXECUTOR
        data = path.read_bytes()
        self.assertEqual(blob_id, git_blob(data))
        self.assertEqual(digest, sha256(data))
        self.assertEqual(
            data,
            subprocess.run(
                ["git", "cat-file", "blob", blob_id], cwd=ROOT,
                check=True, stdout=subprocess.PIPE,
            ).stdout,
        )

    def test_successor_manifest_locks_complete_runtime_and_executor(self):
        loaded = self.executor._load_manifest(MANIFEST)
        release = loaded["release_executor"]
        self.assertEqual("digital_human_precision_director_v3", release["contract"])
        self.assertEqual(
            set(release["required_repository_paths"]),
            {item["repository_path"] for item in loaded["files"]},
        )
        executor_data = EXECUTOR.read_bytes()
        self.assertEqual(release["git_blob"], git_blob(executor_data))
        self.assertEqual(release["sha256"], sha256(executor_data))
        for item in loaded["files"]:
            data = (ROOT / item["repository_path"]).read_bytes()
            self.assertEqual(item["postimage_blob"], git_blob(data), item["repository_path"])
            self.assertEqual(item["postimage_sha256"], sha256(data), item["repository_path"])

    def test_successor_real_entry_accepts_only_locked_manifest_path(self):
        self.assertEqual(self.manifest, self.executor._load_manifest(MANIFEST))
        for old_name in HISTORICAL_LOCKS:
            with self.assertRaisesRegex(
                self.executor.ReleaseError, "rejects every other",
            ):
                self.executor._load_manifest(ROOT / old_name)
        with tempfile.TemporaryDirectory() as directory:
            wrong = pathlib.Path(directory) / MANIFEST.name
            shutil.copy2(MANIFEST, wrong)
            with self.assertRaisesRegex(
                self.executor.ReleaseError, "locked source path",
            ):
                self.executor._load_manifest(wrong)

    def test_successor_executes_full_overlay_with_nginx_gate(self):
        with tempfile.TemporaryDirectory() as target_dir, tempfile.TemporaryDirectory() as backup_dir:
            target = self._target_tree(target_dir)
            hooks = Hooks()
            result = self.executor.execute_locked_release(
                MANIFEST, ROOT, target, pathlib.Path(backup_dir), hooks=hooks,
                verify_repository=False,
                reviewed_head="r" * 40, merged_main="m" * 40,
            )
            self.assertEqual("deployed", result["status"])
            self.assertIn(("nginx-test",), hooks.calls)
            self.assertIn(("nginx-reload",), hooks.calls)
            self.assertEqual(1, sum(call[0] == "restart" for call in hooks.calls))
            for item in self.manifest["files"]:
                deployed = target.joinpath(*pathlib.PurePosixPath(item["runtime_path"]).parts[1:])
                self.assertEqual(item["postimage_sha256"], sha256(deployed.read_bytes()))

    def test_forward_failure_restores_every_preimage_and_reloads_nginx(self):
        with tempfile.TemporaryDirectory() as target_dir, tempfile.TemporaryDirectory() as backup_dir:
            target = self._target_tree(target_dir)
            hooks = Hooks(fail_probe=True)
            with self.assertRaisesRegex(RuntimeError, "forward health"):
                self.executor.execute_locked_release(
                    MANIFEST, ROOT, target, pathlib.Path(backup_dir), hooks=hooks,
                    verify_repository=False,
                    reviewed_head="r" * 40, merged_main="m" * 40,
                )
            self.assertEqual(2, sum(call[0] == "restart" for call in hooks.calls))
            self.assertEqual(2, sum(call[0] == "nginx-reload" for call in hooks.calls))
            for item in self.manifest["files"]:
                restored = target.joinpath(*pathlib.PurePosixPath(item["runtime_path"]).parts[1:])
                if item["target_preimage_state"] == "absent":
                    self.assertFalse(restored.exists(), item["runtime_path"])
                else:
                    self.assertEqual(item["preimage_sha256"], sha256(restored.read_bytes()))


if __name__ == "__main__":
    unittest.main()
