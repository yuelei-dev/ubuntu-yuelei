# -*- coding: utf-8 -*-
"""漂移哨兵「已登记例外」（指纹版）与 bless --pr / verify-deploy 严格化的测试。

用临时 git 仓库 + 临时运行目录构造 changed/missing/added 三种漂移，验证：
- verify-deploy 完全绕开例外（上一轮门禁抓到的假阴性回归）
- sha256 / ref / absent 三种指纹的匹配与不匹配
- drift_kind 不符 → 登记例外状态已变，按真漂移处理
- 清单 schema 各类非法 → 告警 + 按零例外巡检，绝不崩溃
- bless --pr 落账
"""
import hashlib
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SENTINEL_PATH = ROOT / "scripts" / "drift_sentinel.py"

RUNTIME_CHANGED = b"runtime version\n"


def load_sentinel():
    spec = importlib.util.spec_from_file_location("drift_sentinel", SENTINEL_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def entry(path, kind, expected):
    return {"path": path, "drift_kind": kind, "expected": expected,
            "reason": "t", "registered": "2026-08-03"}


def sha256(data):
    return hashlib.sha256(data).hexdigest()


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
            return subprocess.run(["git", "-C", str(self.repo)] + list(args),
                                  check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        self._git = git
        git("init", "-q", "-b", "main")
        git("config", "user.email", "test@example.com")
        git("config", "user.name", "test")
        # changed: git 与线上内容不同；missing: git 有线上无
        (self.repo / "site").mkdir()
        (self.repo / "site" / "changed.html").write_bytes(b"git version\n")
        (self.repo / "site" / "missing.html").write_bytes(b"only in git\n")
        # ref 型例外用：refile 先提交 v1，运行文件 = v1，随后 git 前进到 v2
        (self.repo / "site" / "refile.html").write_bytes(b"v1\n")
        (self.webroot / "changed.html").write_bytes(RUNTIME_CHANGED)
        (self.webroot / "refile.html").write_bytes(b"v1\n")
        git("add", ".")
        git("commit", "-q", "-m", "init")
        self.v1_sha = git("rev-parse", "HEAD").stdout.decode().strip()
        (self.repo / "site" / "refile.html").write_bytes(b"v2\n")
        git("add", ".")
        git("commit", "-q", "-m", "bump refile to v2")
        self.v2_sha = git("rev-parse", "HEAD").stdout.decode().strip()

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

        self._old_env = {k: os.environ.get(k) for k in ("HQ_DRIFT_EXCEPTIONS", "HQ_DRIFT_EXCEPTIONS_REF")}
        os.environ.pop("HQ_DRIFT_EXCEPTIONS", None)
        os.environ.pop("HQ_DRIFT_EXCEPTIONS_REF", None)

    def tearDown(self):
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def write_exceptions(self, obj):
        f = Path(self.tmp.name) / "exceptions.json"
        f.write_text(obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False),
                     encoding="utf-8")
        os.environ["HQ_DRIFT_EXCEPTIONS"] = str(f)
        return f

    def good_doc(self, entries):
        return {"schema_version": 1, "exceptions": entries}

    def sha_entry(self, path="site/changed.html"):
        return entry(path, "changed", {"type": "sha256", "value": sha256(RUNTIME_CHANGED)})

    def read_log(self):
        p = Path(self.ds.LOG)
        return p.read_text(encoding="utf-8") if p.exists() else ""

    # ---------- 指纹匹配 ----------

    def test_sha256_match_registers(self):
        self.write_exceptions(self.good_doc([self.sha_entry()]))
        d = self.ds.diff_paths()
        self.assertEqual(d["changed"], ["site/refile.html"])
        self.assertEqual(d["registered"], ["site/changed.html"])
        self.assertEqual(d["exceptions_stale"], [])

    def test_sha256_mismatch_is_drift_and_stale(self):
        e = entry("site/changed.html", "changed", {"type": "sha256", "value": "0" * 64})
        self.write_exceptions(self.good_doc([e]))
        d = self.ds.diff_paths()
        self.assertIn("site/changed.html", d["changed"])
        self.assertEqual(d["registered"], [])
        self.assertEqual(d["exceptions_stale"], ["site/changed.html"])
        self.assertIn("登记例外状态已变", self.ds.format_diff(d))

    def test_drift_kind_mismatch_is_stale(self):
        # 路径对、指纹对，但 drift_kind 登记错（实际是 changed 不是 missing）
        e = entry("site/changed.html", "missing", {"type": "absent"})
        self.write_exceptions(self.good_doc([e]))
        d = self.ds.diff_paths()
        self.assertIn("site/changed.html", d["changed"])
        self.assertEqual(d["exceptions_stale"], ["site/changed.html"])

    def test_ref_match_registers(self):
        e = entry("site/refile.html", "changed", {"type": "ref", "value": self.v1_sha})
        self.write_exceptions(self.good_doc([e]))
        d = self.ds.diff_paths()
        self.assertEqual(d["registered"], ["site/refile.html"])
        self.assertNotIn("site/refile.html", d["changed"])

    def test_ref_mismatch_is_stale(self):
        # 运行文件是 v1，例外要求等于 v2 提交 → 不匹配
        e = entry("site/refile.html", "changed", {"type": "ref", "value": self.v2_sha})
        self.write_exceptions(self.good_doc([e]))
        d = self.ds.diff_paths()
        self.assertIn("site/refile.html", d["changed"])
        self.assertEqual(d["exceptions_stale"], ["site/refile.html"])

    def test_moving_ref_probe_regression(self):
        # 门禁探针回归：ref 锁定不可变提交后，分支前进 + 运行文件同步改成新内容，
        # 不得再进 registered（旧版用分支名 origin/main 时会自动跟随 = 假阴性）
        (self.webroot / "refile.html").write_bytes(b"v2\n")
        e = entry("site/refile.html", "changed", {"type": "ref", "value": self.v1_sha})
        self.write_exceptions(self.good_doc([e]))
        d = self.ds.diff_paths()
        # 运行文件 == git HEAD(v2)，本身无漂移；但对 v1 的例外登记而言状态已变？——
        # 无漂移即无例外判定，refile 不得出现在 registered
        self.assertEqual(d["registered"], [])
        self.assertNotIn("site/refile.html", d["changed"])
        # 反之：运行文件被改成 v1（真漂移），例外指纹虽匹配 v1……
        (self.webroot / "refile.html").write_bytes(b"v1\n")
        d = self.ds.diff_paths()
        self.assertEqual(d["registered"], ["site/refile.html"])  # kind+指纹完全匹配才豁免
        # 再改成第三版内容：漂移且与登记指纹不符 → stale，绝不静默豁免
        (self.webroot / "refile.html").write_bytes(b"v3\n")
        d = self.ds.diff_paths()
        self.assertIn("site/refile.html", d["changed"])
        self.assertEqual(d["exceptions_stale"], ["site/refile.html"])
        self.assertEqual(d["registered"], [])

    def test_absent_match_registers(self):
        self.write_exceptions(self.good_doc([entry("site/missing.html", "missing", {"type": "absent"})]))
        d = self.ds.diff_paths()
        self.assertEqual(d["missing"], [])
        self.assertEqual(d["registered"], ["site/missing.html"])

    def test_registered_summary_in_format_diff(self):
        self.write_exceptions(self.good_doc([
            self.sha_entry(),
            entry("site/missing.html", "missing", {"type": "absent"}),
            entry("site/refile.html", "changed", {"type": "ref", "value": self.v1_sha}),
        ]))
        d = self.ds.diff_paths()
        total = len(d["changed"]) + len(d["missing"]) + len(d["added"])
        self.assertEqual(total, 0)
        self.assertEqual(len(d["registered"]), 3)
        msg = self.ds.format_diff(d)
        self.assertIn("已登记例外 3 处", msg)
        self.assertIn("检测到 0 处文件漂移", msg)

    # ---------- verify-deploy 严格化（假阴性回归） ----------

    def test_verify_deploy_ignores_exceptions(self):
        # 运行文件与 git 不一致，且例外指纹完全匹配登记状态——巡检豁免，
        # 但 verify-deploy 必须失败返回非零（上一轮探针抓到的假阴性）
        self.write_exceptions(self.good_doc([self.sha_entry()]))
        rc = self.ds.handle_verify(["site/changed.html"])
        self.assertEqual(rc, 2)

    def test_verify_deploy_passes_when_matching(self):
        (self.webroot / "changed.html").write_bytes(b"git version\n")
        self.write_exceptions(self.good_doc([self.sha_entry()]))
        rc = self.ds.handle_verify(["site/changed.html"])
        self.assertEqual(rc, 0)

    # ---------- 读取顺序与兜底 ----------

    def test_no_exceptions_file_behaves_as_before(self):
        d = self.ds.diff_paths()
        self.assertEqual(d["changed"], ["site/changed.html", "site/refile.html"])
        self.assertEqual(d["missing"], ["site/missing.html"])
        self.assertEqual(d["registered"], [])

    def test_default_loads_from_git_ref(self):
        (self.repo / "deploy").mkdir()
        (self.repo / "deploy" / "test-server-exceptions.json").write_text(
            json.dumps(self.good_doc([self.sha_entry()]), ensure_ascii=False), encoding="utf-8")
        self._git("add", ".")
        self._git("commit", "-q", "-m", "exceptions")
        d = self.ds.diff_paths()
        self.assertEqual(d["registered"], ["site/changed.html"])

    def test_exceptions_ref_env_pins_commit(self):
        # 清单提交进 git 后工作区删除，靠 HQ_DRIFT_EXCEPTIONS_REF 锁定精确提交读取
        (self.repo / "deploy").mkdir()
        (self.repo / "deploy" / "test-server-exceptions.json").write_text(
            json.dumps(self.good_doc([self.sha_entry()]), ensure_ascii=False), encoding="utf-8")
        self._git("add", ".")
        self._git("commit", "-q", "-m", "exceptions")
        pin = self._git("rev-parse", "HEAD").stdout.decode().strip()
        (self.repo / "deploy" / "test-server-exceptions.json").unlink()
        self._git("add", ".")
        self._git("commit", "-q", "-m", "drop exceptions")
        os.environ["HQ_DRIFT_EXCEPTIONS_REF"] = pin
        d = self.ds.diff_paths()
        self.assertEqual(d["registered"], ["site/changed.html"])

    # ---------- schema 严格校验：非法 → 告警 + 零例外 ----------

    def assert_bad_schema(self, obj):
        self.write_exceptions(obj)
        d = self.ds.diff_paths()
        self.assertEqual(d["registered"], [])
        self.assertIn("site/changed.html", d["changed"])
        self.assertIn("例外清单配置错误，按零例外巡检", self.read_log())
        os.environ.pop("HQ_DRIFT_EXCEPTIONS", None)
        if Path(self.ds.LOG).exists():
            Path(self.ds.LOG).unlink()

    def test_schema_non_object_entry_regression(self):
        # 门禁发现：合法 JSON 里混非对象条目会 .get() 崩溃 —— 回归
        self.assert_bad_schema(self.good_doc(["not-a-dict", self.sha_entry()]))

    def test_schema_top_level_not_object(self):
        self.assert_bad_schema('[{"path": "x"}]')

    def test_schema_exceptions_not_list(self):
        self.assert_bad_schema({"schema_version": 1, "exceptions": {"a": 1}})

    def test_schema_version_required(self):
        self.assert_bad_schema({"exceptions": [self.sha_entry()]})

    def test_schema_duplicate_path(self):
        self.assert_bad_schema(self.good_doc([self.sha_entry(), self.sha_entry()]))

    def test_schema_unknown_field(self):
        e = dict(self.sha_entry(), extra="x")
        self.assert_bad_schema(self.good_doc([e]))

    def test_schema_missing_field(self):
        e = self.sha_entry()
        del e["reason"]
        self.assert_bad_schema(self.good_doc([e]))

    def test_schema_bad_drift_kind(self):
        self.assert_bad_schema(self.good_doc([
            entry("site/changed.html", "renamed", {"type": "absent"})]))

    def test_schema_bad_expected_type(self):
        self.assert_bad_schema(self.good_doc([
            entry("site/changed.html", "changed", {"type": "md5", "value": "x"})]))

    def test_schema_absent_with_value(self):
        self.assert_bad_schema(self.good_doc([
            entry("site/missing.html", "missing", {"type": "absent", "value": "x"})]))

    def test_schema_missing_kind_must_be_absent(self):
        self.assert_bad_schema(self.good_doc([
            entry("site/missing.html", "missing", {"type": "sha256", "value": "0" * 64})]))

    def test_schema_bad_sha256_format(self):
        self.assert_bad_schema(self.good_doc([
            entry("site/changed.html", "changed", {"type": "sha256", "value": "xyz"})]))

    def test_schema_ref_rejects_branch_name(self):
        self.assert_bad_schema(self.good_doc([
            entry("site/refile.html", "changed", {"type": "ref", "value": "origin/main"})]))

    def test_schema_ref_rejects_tag(self):
        self.assert_bad_schema(self.good_doc([
            entry("site/refile.html", "changed", {"type": "ref", "value": "v1.0"})]))

    def test_schema_ref_rejects_short_sha(self):
        self.assert_bad_schema(self.good_doc([
            entry("site/refile.html", "changed", {"type": "ref", "value": "f03cab3"})]))

    def test_schema_malformed_json(self):
        self.assert_bad_schema("{not json")

    def test_unreadable_env_file_alerts_and_continues(self):
        os.environ["HQ_DRIFT_EXCEPTIONS"] = str(Path(self.tmp.name) / "no-such.json")
        d = self.ds.diff_paths()
        self.assertEqual(d["registered"], [])
        self.assertIn("site/changed.html", d["changed"])
        self.assertIn("例外清单配置错误，按零例外巡检", self.read_log())

    # ---------- bless --pr ----------

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
