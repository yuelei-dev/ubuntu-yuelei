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
import urllib.error
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SUCCESSOR_PATH = (
    ROOT / "deploy" / "test-runtime" /
    "digital-human-material-feishu-priority-20260823.json"
)
HISTORICAL_MANIFEST_PATH = (
    ROOT / "deploy" / "test-runtime" /
    "digital-human-material-seedream-v3-20260821.json"
)
REJECTED_HISTORICAL_MANIFEST_PATH = (
    ROOT / "deploy" / "test-runtime" / "digital-human-material-v2-20260818.json"
)
HISTORICAL_EXECUTOR_PATH = ROOT / "scripts" / "deploy_content_whisper_runtime.py"
VERSIONED_EXECUTOR_PATH = ROOT / "scripts" / "deploy_seedream_v3_locked_manifest.py"
HISTORICAL_MANIFEST_BLOB = "1a1bef4ec64323208fe0212961e72e38206a69bf"
HISTORICAL_MANIFEST_SHA256 = (
    "1cb48ad76a69d05ea39d96f3bc0607ee18d2566354d7317e5bdb0dd6308d8578"
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

    def _release(self, module, manifest=None, **kwargs):
        return module.ContentWhisperRelease(
            manifest or self.manifest,
            self.source_root,
            self.runtime_root,
            self.backup_root,
            git_runner=FakeGitRunner(),
            reviewed_source_commit=REVIEWED_SOURCE,
            reviewed_main_commit=REVIEWED_MAIN,
            **kwargs,
        )

    @staticmethod
    def _service_environment():
        return {
            "FEISHU_APP_ID": "test-app-id",
            "FEISHU_APP_SECRET": "test-app-secret",
        }

    def test_versioned_executor_accepts_locked_successor(self):
        release = self._release(self.versioned)
        release._verify_release_tools()
        release._verify_source_checkout(REVIEWED_SOURCE, REVIEWED_MAIN)

    def test_versioned_executor_rejects_historical_manifest(self):
        manifest = dict(self.manifest)
        manifest["_manifest_path"] = str(
            self.source_root / REJECTED_HISTORICAL_MANIFEST_PATH.relative_to(ROOT)
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

    def test_feishu_preflight_fails_closed_when_service_credentials_are_missing(self):
        release = self._release(
            self.versioned, service_environment_getter=lambda: {},
            feishu_json_getter=lambda _request, _environment: self.fail(
                "network must not run"
            ),
        )
        with self.assertRaisesRegex(
                self.versioned.ReleaseError, "credentials are missing"):
            release._verify_feishu_operational("pre-deployment")

    def test_default_http_status_accepts_only_locked_digital_human_history_url(self):
        locked_url = (
            "http://127.0.0.1:8096/api/gen/digital-human-v2/history"
        )
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.HTTPError(
            locked_url, 401, "Unauthorized", {}, None,
        )
        release = self._release(self.versioned)
        with mock.patch.object(
                self.versioned.urllib.request, "build_opener",
                return_value=opener):
            self.assertEqual(401, release._http_status(locked_url))
        request = opener.open.call_args.args[0]
        self.assertEqual(locked_url, request.full_url)

    def test_default_http_status_rejects_unapproved_path_even_if_manifest_locked(self):
        unapproved_url = "http://127.0.0.1:8096/api/gen/arbitrary"
        self.manifest["health_checks"].append({
            "url": unapproved_url,
            "expected_status": 401,
        })
        release = self._release(self.versioned)
        with mock.patch.object(
                self.versioned.urllib.request, "build_opener") as build_opener:
            with self.assertRaisesRegex(
                    self.versioned.ReleaseError,
                    "health probe URL is not an approved local endpoint"):
                release._http_status(unapproved_url)
        build_opener.assert_not_called()

    def test_feishu_preflight_rejects_table_or_view_permission_failure(self):
        responses = iter([
            {"code": 0, "tenant_access_token": "tenant-token"},
            {"code": 1254302, "msg": "permission denied"},
        ])
        release = self._release(
            self.versioned,
            service_environment_getter=self._service_environment,
            feishu_json_getter=lambda _request, _environment: next(responses),
        )
        with self.assertRaisesRegex(
                self.versioned.ReleaseError, "permission verification failed"):
            release._verify_feishu_operational("pre-deployment")

    def test_feishu_preflight_rejects_invalid_pagination_before_attachment(self):
        responses = iter([
            {"code": 0, "tenant_access_token": "tenant-token"},
            {"code": 0, "data": {
                "items": [], "has_more": True, "page_token": "",
            }},
        ])
        release = self._release(
            self.versioned,
            service_environment_getter=self._service_environment,
            feishu_json_getter=lambda _request, _environment: next(responses),
            feishu_media_getter=lambda _request, _environment: self.fail(
                "media must not run"
            ),
        )
        with self.assertRaisesRegex(
                self.versioned.ReleaseError, "pagination cursor is invalid"):
            release._verify_feishu_operational("pre-deployment")

    def test_feishu_preflight_reads_all_pages_and_downloads_attachment(self):
        calls = []

        def json_getter(request, environment):
            self.assertEqual(environment["FEISHU_APP_ID"], "test-app-id")
            calls.append(request.full_url)
            if "/auth/" in request.full_url:
                return {"code": 0, "tenant_access_token": "tenant-token"}
            if "page_token=next-page" in request.full_url:
                return {"code": 0, "data": {
                    "items": [{"fields": {"素材": [{
                        "file_token": "locked-attachment", "name": "material.png",
                    }]}}], "has_more": False,
                }}
            return {"code": 0, "data": {
                "items": [], "has_more": True, "page_token": "next-page",
            }}

        media_calls = []
        release = self._release(
            self.versioned,
            service_environment_getter=self._service_environment,
            feishu_json_getter=json_getter,
            feishu_media_getter=lambda request, _environment: media_calls.append(
                request.full_url
            ),
        )
        release._verify_feishu_operational("post-restart")
        self.assertEqual(len(calls), 3)
        self.assertIn("view_id=vewa9ZW0Og", calls[1])
        self.assertIn("page_token=next-page", calls[2])
        self.assertEqual(len(media_calls), 1)
        self.assertTrue(media_calls[0].endswith("/locked-attachment/download"))

    def test_post_restart_feishu_failure_triggers_full_restore(self):
        release = self._release(self.versioned)
        release._validate_target = mock.Mock()
        release._verify_source_checkout = mock.Mock()
        release._verify_release_tools = mock.Mock()
        release._preflight_release_commands = mock.Mock()
        release._source_payloads = mock.Mock(return_value={})
        release._health_probe_policy = mock.Mock(return_value=(60, 1))
        release._run_stage = mock.Mock()
        release._verify_health = mock.Mock()
        release._verify_feishu_operational = mock.Mock(side_effect=[
            None, self.versioned.ReleaseError("post-restart Feishu failed"),
        ])
        release._backup_all = mock.Mock()
        release._install_all = mock.Mock(return_value=1)
        release._restore_all = mock.Mock()
        with mock.patch.object(
                self.versioned.manifest_verify, "classify_start_states",
                return_value=[]):
            with self.assertRaisesRegex(
                    self.versioned.ReleaseError,
                    "all manifest targets were restored"):
                release.execute("test@8.148.158.106")
        release._restore_all.assert_called_once_with()
        self.assertEqual(
            release._verify_feishu_operational.call_args_list,
            [mock.call("pre-deployment"), mock.call("post-restart")],
        )


if __name__ == "__main__":
    unittest.main()
