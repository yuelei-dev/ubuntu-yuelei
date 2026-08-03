# -*- coding: utf-8 -*-
"""漂移哨兵「已登记例外」与 bless --pr 的测试。

用临时 git 仓库 + 临时运行目录构造 changed/missing/added 三种漂移，
验证：命中例外的路径进 registered 桶不计漂移、无清单时行为同旧版、
bless --pr 落账到 deploy_bless.jsonl。
"""
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SENTINEL_PATH = ROOT / "scripts" / "drift_sentinel.py"


def load_sentinel():
    spec = importlib.util.spec_from_file_location("drift_sentinel", SENTINEL_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class DriftExceptionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.repo = base / "repo"
        self.webroot = base / "webroot"
        self.drift_dir = base / "hq-drift"
        self.repo.mkdir()
        self.webroot.mkdir()
        self.drift_dir.mkdir()

        def git(*args):
            subprocess.run(["git", "-C", str(self.repo)] + list(args),
                           check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        git("init", "-q", "-b", "main")
        git("config", "user.email", "test@example.com")
        git("config", "user.name", "test")
        # changed: git 与线上内容不同；missing: git 有线上无
        (self.repo / "site").mkdir()
        (self.repo / "site" / "changed.html").write_text("git version\n", encoding="utf-8")
        (self.repo / "site" / "missing.html").write_text("only in git\n", encoding="utf-8")
        (self.webroot / "changed.html").write_text("runtime version\n", encoding="utf-8")
        git("add", ".")
        git("commit", "-q", "-m", "init")

        self.ds = load_sentinel()
        self.ds.REPO = str(self.repo)
        self.ds.GIT_REF = "HEAD"
        self.ds.WEBROOT = str(self.webroot)
        self.ds.DRIFT_DIR = str(self.drift_dir)
        self.ds.LOG = str(self.drift_dir / "sentinel.log")
        self.ds.DEPLOY_LOG = str(self.drift_dir / "deploy_bless.jsonl")
        self.ds.BASELINE = str(self.drift_dir / "baseline.json")
        self.ds.STATE = str(self.drift_dir / ".state.json")
        # 后端/域名/systemd 映射全部指到空目录，只留 site 这条链路
        self.ds.BACKEND_RUNTIME = {}
        self.ds.CONTENT_DOMAINS_RUNTIME = str(base / "domains")
        self.ds.SYSTEMD_DIR = str(base / "systemd")

        self._old_exc = os.environ.get("HQ_DRIFT_EXCEPTIONS")
        os.environ.pop("HQ_DRIFT_EXCEPTIONS", None)

    def tearDown(self):
        if self._old_exc is None:
            os.environ.pop("HQ_DRIFT_EXCEPTIONS", None)
        else:
            os.environ["HQ_DRIFT_EXCEPTIONS"] = self._old_exc
        self.tmp.cleanup()

    def write_exceptions(self, paths):
        f = Path(self.tmp.name) / "exceptions.json"
        f.write_text(json.dumps({
            "exceptions": [{"path": p, "reason": "t", "registered": "2026-08-03"} for p in paths]
        }, ensure_ascii=False), encoding="utf-8")
        os.environ["HQ_DRIFT_EXCEPTIONS"] = str(f)
        return f

    def test_exception_paths_move_to_registered_bucket(self):
        self.write_exceptions(["site/changed.html"])
        d = self.ds.diff_paths()
        self.assertEqual(d["changed"], [])
        self.assertEqual(d["missing"], ["site/missing.html"])
        self.assertEqual(d["added"], [])
        self.assertEqual(d["registered"], ["site/changed.html"])

    def test_registered_summary_in_format_diff(self):
        self.write_exceptions(["site/changed.html", "site/missing.html"])
        d = self.ds.diff_paths()
        msg = self.ds.format_diff(d)
        self.assertIn("已登记例外 2 处", msg)
        self.assertIn("检测到 0 处文件漂移", msg)

    def test_all_exceptions_means_no_drift(self):
        self.write_exceptions(["site/changed.html", "site/missing.html"])
        d = self.ds.diff_paths()
        total = len(d["changed"]) + len(d["missing"]) + len(d["added"])
        self.assertEqual(total, 0)
        self.assertEqual(sorted(d["registered"]), ["site/changed.html", "site/missing.html"])

    def test_no_exceptions_file_behaves_as_before(self):
        # 既不设 env，git ref 里也没有 deploy/test-server-exceptions.json
        d = self.ds.diff_paths()
        self.assertEqual(d["changed"], ["site/changed.html"])
        self.assertEqual(d["missing"], ["site/missing.html"])
        self.assertEqual(d["registered"], [])

    def test_default_loads_exceptions_from_git_ref(self):
        # 不设 env：从 GIT_REF 的 deploy/test-server-exceptions.json 读取
        (self.repo / "deploy").mkdir()
        (self.repo / "deploy" / "test-server-exceptions.json").write_text(json.dumps({
            "exceptions": [{"path": "site/changed.html", "reason": "t", "registered": "2026-08-03"}]
        }, ensure_ascii=False), encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-q", "-m", "exceptions"],
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        d = self.ds.diff_paths()
        self.assertEqual(d["changed"], [])
        self.assertEqual(d["registered"], ["site/changed.html"])

    def test_bless_pr_recorded(self):
        self.ds.bless(["site/changed.html"], pr="175")
        rec = json.loads(Path(self.ds.DEPLOY_LOG).read_text(encoding="utf-8").strip().splitlines()[-1])
        self.assertEqual(rec["pr"], "175")
        self.assertEqual(rec["files"], ["site/changed.html"])

    def test_bless_without_pr_unchanged(self):
        self.ds.bless(["site/changed.html"])
        rec = json.loads(Path(self.ds.DEPLOY_LOG).read_text(encoding="utf-8").strip().splitlines()[-1])
        self.assertNotIn("pr", rec)


if __name__ == "__main__":
    unittest.main()
