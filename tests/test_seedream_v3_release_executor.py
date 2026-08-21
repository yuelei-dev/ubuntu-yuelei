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


ROOT = pathlib.Path(__file__).resolve().parents[1]
SUCCESSOR_PATH = (
    ROOT / "deploy" / "test-runtime" /
    "digital-human-material-seedream-v3-20260821.json"
)
HISTORICAL_MANIFEST_PATH = (
    ROOT / "deploy" / "test-runtime" / "digital-human-material-v2-20260818.json"
)
HISTORICAL_EXECUTOR_PATH = ROOT / "scripts" / "deploy_content_whisper_runtime.py"
VERSIONED_EXECUTOR_PATH = ROOT / "scripts" / "deploy_seedream_v3_locked_manifest.py"
HISTORICAL_MANIFEST_BLOB = "e7d139001c065f6a3e8ec7137057abdc86bf5d06"
HISTORICAL_MANIFEST_SHA256 = (
    "44006751e15f56c946d8dfadeb4ad74f497dab44ceda66512bdb9603b919e453"
)
HISTORICAL_EXECUTOR_BLOB = "c9dd02ba92e03a476751ae15e03f4e1c5f68886a"
HISTORICAL_EXECUTOR_SHA256 = (
    "fbc4a9200d4b769aee37cb8de18ea614521e92baa19744d5e2a954021085c36f"
)
REVIEWED_SOURCE = "1" * 40
REVIEWED_MAIN = "2" * 40


def _blob(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _load_executor(path, name, scripts_path=None):
    if scripts_path is not None:
        scripts = str(scripts_path)
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        sys.modules.pop("verify_content_whisper_deployment", None)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeGitRunner:
    def run(self, arguments, *, source_root, allow_failure=False):
        if arguments[:2] == ["status", "--porcelain"]:
            output = ""
        elif arguments[:3] == ["symbolic-ref", "--short", "HEAD"]:
            output = "main\n"
        elif arguments == ["rev-parse", "HEAD"]:
            output = REVIEWED_MAIN + "\n"
        elif arguments == ["rev-parse", "refs/remotes/origin/main"]:
            output = REVIEWED_MAIN + "\n"
        elif arguments[:2] == ["ls-remote", "--exit-code"]:
            output = REVIEWED_MAIN + "\trefs/heads/main\n"
        elif arguments[:2] == ["merge-base", "--is-ancestor"]:
            return types.SimpleNamespace(stdout="", returncode=0)
        else:
            raise AssertionError("unexpected git call: %r" % (arguments,))
        return types.SimpleNamespace(stdout=output, returncode=0)


class SeedreamV3ReleaseExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.source_root = self.root / "source"
        self.runtime_root = self.root / "runtime"
        self.backup_root = self.root / "backups"
        self.source_root.mkdir()
        self.runtime_root.mkdir()
        self.manifest = json.loads(SUCCESSOR_PATH.read_text(encoding="utf-8"))

        manifest_path = self.source_root / SUCCESSOR_PATH.relative_to(ROOT)
        manifest_path.parent.mkdir(parents=True)
        manifest_path.write_bytes(SUCCESSOR_PATH.read_bytes())
        self.manifest["_manifest_path"] = str(manifest_path)

        locked_tools = (
            self.manifest["executor"],
            self.manifest["executor"]["verifier"],
            self.manifest["executor"]["requirements_verifier"],
        )
        for lock in locked_tools:
            source = ROOT / lock["repository_path"]
            target = self.source_root / lock["repository_path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)

        executor_path = self.source_root / self.manifest["executor"]["repository_path"]
        self.versioned = _load_executor(
            executor_path, "seedream_v3_release_test", self.source_root / "scripts",
        )

    def tearDown(self):
        self.temporary.cleanup()

    def _release(self, module, manifest=None):
        return module.ContentWhisperRelease(
            manifest or self.manifest,
            self.source_root,
            self.runtime_root,
            self.backup_root,
            git_runner=FakeGitRunner(),
            reviewed_source_commit=REVIEWED_SOURCE,
            reviewed_main_commit=REVIEWED_MAIN,
        )

    def test_versioned_executor_accepts_locked_successor(self):
        release = self._release(self.versioned)
        release._verify_release_tools()
        release._verify_source_checkout(REVIEWED_SOURCE, REVIEWED_MAIN)

    def test_versioned_executor_rejects_historical_manifest(self):
        manifest = dict(self.manifest)
        manifest["_manifest_path"] = str(
            self.source_root / HISTORICAL_MANIFEST_PATH.relative_to(ROOT)
        )
        with self.assertRaisesRegex(
                self.versioned.ReleaseError,
                "manifest must come from the locked source checkout"):
            self._release(self.versioned, manifest)._verify_source_checkout(
                REVIEWED_SOURCE, REVIEWED_MAIN,
            )

    def test_historical_executor_rejects_successor_and_bytes_remain_exact(self):
        historical = _load_executor(
            HISTORICAL_EXECUTOR_PATH, "historical_seedream_release_test", ROOT / "scripts",
        )
        with self.assertRaisesRegex(
                historical.ReleaseError,
                "manifest must come from the locked source checkout"):
            self._release(historical)._verify_source_checkout(
                REVIEWED_SOURCE, REVIEWED_MAIN,
            )

        for path, blob_id, sha256 in (
            (
                HISTORICAL_EXECUTOR_PATH,
                HISTORICAL_EXECUTOR_BLOB,
                HISTORICAL_EXECUTOR_SHA256,
            ),
            (
                HISTORICAL_MANIFEST_PATH,
                HISTORICAL_MANIFEST_BLOB,
                HISTORICAL_MANIFEST_SHA256,
            ),
        ):
            data = path.read_bytes()
            self.assertEqual(_blob(data), blob_id)
            self.assertEqual(hashlib.sha256(data).hexdigest(), sha256)
            locked = subprocess.run(
                [
                    "git", "-c", "safe.directory=" + ROOT.as_posix(),
                    "cat-file", "blob", blob_id,
                ],
                cwd=ROOT,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ).stdout
            self.assertEqual(locked, data)


if __name__ == "__main__":
    unittest.main()
