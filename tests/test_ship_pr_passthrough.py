# -*- coding: utf-8 -*-
"""ship --pr 透传：部署登记闭环最后一环。

合同（部署登记 §三）：
  ./ship [--exact-files] [--pr <号>] "说明" 文件...
  --pr（或 env HQ_SHIP_PR）必须透传到远端 drift_sentinel，落账 deploy_bless.jsonl。

关键约束（门禁复现）：哨兵 --bless-deploy 是 nargs='*'，参数顺序必须
  --pr N --bless-deploy <文件...>
「--bless-deploy --pr N 文件」会把文件判为未识别参数（退出码 2）。
显式传 --pr = 要求登记，远端报错必须让 ship 非零退出；不传 --pr 保持旧容错。

证明方式（参照 test_ship_version_contract 的静态检查 + 假环境动态执行风格）：
  1. 静态：ship 本文里 bless 命令行 --pr 在前、显式 --pr 失败分支非零。
  2. 动态：假 ssh 环境跑 ship，按 ssh 调用日志断言 bless 命令形态与失败语义。
  3. 集成：用仓库真实 scripts/drift_sentinel.py 的 argparse 跑一遍，
     新顺序解析成功且文件列表完整、旧顺序确实失败（防回归）。
"""
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHIP = ROOT / "ship"
SHIP_TEXT = SHIP.read_text(encoding="utf-8")
SENTINEL_PATH = ROOT / "scripts" / "drift_sentinel.py"


class ShipPrPassthroughStaticTests(unittest.TestCase):
    """静态合同：--pr 解析、env 兜底、bless 命令形态与失败语义都在 ship 本文里。"""

    def test_pr_arg_parsed_and_validated(self):
        self.assertIn('if [ "${1:-}" = "--pr" ]', SHIP_TEXT)
        self.assertIn('HQ_SHIP_PR', SHIP_TEXT)
        self.assertIn("必须是纯数字", SHIP_TEXT)

    def test_bless_command_pr_comes_first(self):
        # nargs='*' 顺序合同：--pr 必须在 --bless-deploy 之前
        self.assertIn('--pr $PR_NO --bless-deploy $DEPLOYED', SHIP_TEXT)
        self.assertNotIn('--bless-deploy$BLESS_PR', SHIP_TEXT)

    def test_explicit_pr_bless_failure_aborts(self):
        self.assertIn("部署登记失败", SHIP_TEXT)
        # 显式 --pr 分支的失败必须是 exit 1，不是 || echo 吞掉
        self.assertIn('部署登记失败（--pr $PR_NO）——远端哨兵报错"; exit 1;', SHIP_TEXT)

    def test_usage_mentions_pr(self):
        self.assertIn('[--pr <号>]', SHIP_TEXT)


class ShipPrPassthroughDynamicTests(unittest.TestCase):
    """动态合同：假环境跑 ship，按 ssh 调用日志验证 bless 命令形态与失败语义。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.bin = Path(self.tmp.name) / "bin"
        self.home.mkdir()
        self.bin.mkdir()
        self.ssh_log = Path(self.tmp.name) / "ssh.log"
        self._write_executable(
            "git",
            """#!/bin/sh
if [ "$1" = "diff" ]; then exit 0; fi
if [ "$1" = "rev-parse" ]; then echo f47ac10b58cc4372a5670e02b2c3d479; exit 0; fi
exit 0
""",
        )
        self._write_executable(
            "ssh",
            """#!/bin/sh
printf '%s\\n' "$*" >> "$SSH_LOG"
case "$*" in
  *"--bless-deploy"*) [ "$FAKE_BLESS_FAIL" = "1" ] && exit 1 ;;
esac
exit 0
""",
        )
        self._write_executable(
            "curl",
            """#!/bin/sh
printf %s "200"
""",
        )
        self._write_executable(
            "rsync",
            """#!/bin/sh
exit 0
""",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _write_executable(self, name, content):
        path = self.bin / name
        path.write_text(content, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def _run_ship(self, target, pr=None, **overrides):
        env = os.environ.copy()
        env.update({
            "PATH": str(self.bin) + os.pathsep + env.get("PATH", ""),
            "HOME": str(self.home),
            "HQ_REMOTE": "fake-server",
            "HQ_SERVICE_WAIT_SECONDS": "1",
            "SSH_LOG": str(self.ssh_log),
        })
        env.pop("HQ_SHIP_PR", None)
        env.pop("FAKE_BLESS_FAIL", None)
        env.update(overrides)
        argv = ["bash", str(SHIP)]
        if pr is not None:
            argv += ["--pr", pr]
        argv += ["test deployment", target]
        return subprocess.run(
            argv, cwd=ROOT, env=env, text=True, encoding="utf-8", errors="replace",
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=15,
        )

    def _bless_lines(self):
        if not self.ssh_log.exists():
            return []
        return [l for l in self.ssh_log.read_text(encoding="utf-8").splitlines()
                if "--bless-deploy" in l]

    def test_pr_passed_to_remote_bless_pr_first(self):
        result = self._run_ship("site/workbench/collect.html", pr="174")
        self.assertEqual(0, result.returncode, result.stdout)
        bless = self._bless_lines()
        self.assertEqual(1, len(bless))
        # nargs='*' 合同：--pr 必须在 --bless-deploy 之前
        self.assertIn("--pr 174 --bless-deploy", bless[0])
        self.assertIn("site/workbench/collect.html", bless[0])
        self.assertIn("PR #174", result.stdout)

    def test_no_pr_keeps_bless_command_unchanged(self):
        result = self._run_ship("site/workbench/collect.html")
        self.assertEqual(0, result.returncode, result.stdout)
        bless = self._bless_lines()
        self.assertEqual(1, len(bless))
        self.assertIn("--bless-deploy", bless[0])
        self.assertNotIn("--pr", bless[0])          # 不传时命令与旧版一致
        self.assertIn("本次部署文件已记录", result.stdout)
        self.assertNotIn("（PR #", result.stdout)

    def test_bless_failure_with_explicit_pr_fails_ship(self):
        # 显式 --pr = 要求登记：远端报错必须非零退出，不能被 || echo 吞掉
        result = self._run_ship("site/workbench/collect.html", pr="174", FAKE_BLESS_FAIL="1")
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertIn("部署登记失败", result.stdout)
        self.assertNotIn("上线完成", result.stdout)

    def test_bless_failure_without_pr_tolerated(self):
        # 不传 --pr 保持旧容错：远端旧哨兵/未安装时跳过登记，不阻断发布
        result = self._run_ship("site/workbench/collect.html", FAKE_BLESS_FAIL="1")
        self.assertEqual(0, result.returncode, result.stdout)
        self.assertIn("跳过部署记录", result.stdout)
        self.assertIn("上线完成", result.stdout)

    def test_non_numeric_pr_rejected_before_connect(self):
        result = self._run_ship("site/workbench/collect.html", pr="abc")
        self.assertEqual(1, result.returncode, result.stdout)
        self.assertIn("必须是纯数字", result.stdout)
        self.assertFalse(self.ssh_log.exists())      # 在连接服务器之前拦截

    def test_env_hq_ship_pr_used(self):
        result = self._run_ship("site/workbench/collect.html", HQ_SHIP_PR="277")
        self.assertEqual(0, result.returncode, result.stdout)
        bless = self._bless_lines()
        self.assertEqual(1, len(bless))
        self.assertIn("--pr 277 --bless-deploy", bless[0])

    def test_env_hq_ship_pr_non_numeric_rejected(self):
        result = self._run_ship("site/workbench/collect.html", HQ_SHIP_PR="pr-174")
        self.assertEqual(1, result.returncode, result.stdout)
        self.assertIn("必须是纯数字", result.stdout)

    def test_explicit_pr_overrides_env(self):
        result = self._run_ship("site/workbench/collect.html", pr="222", HQ_SHIP_PR="111")
        self.assertEqual(0, result.returncode, result.stdout)
        bless = self._bless_lines()
        self.assertEqual(1, len(bless))
        self.assertIn("--pr 222 --bless-deploy", bless[0])
        self.assertNotIn("111", bless[0])


class RealSentinelArgparseTests(unittest.TestCase):
    """集成回归：用仓库真实 drift_sentinel.py 的 argparse 验证 ship 生成的命令行。

    防的就是上一轮门禁复现的那类错：nargs='*' 下 --pr 放错位置，
    文件被判为未识别参数、退出码 2，再被 || echo 吞掉。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.repo = base / "repo"
        self.drift_dir = base / "hq-drift"
        self.repo.mkdir()
        self.drift_dir.mkdir()
        (base / "webroot").mkdir()

        def git(*args):
            subprocess.run(["git", "-C", str(self.repo)] + list(args),
                           check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        git("init", "-q", "-b", "main")
        git("config", "user.email", "test@example.com")
        git("config", "user.name", "test")
        (self.repo / "a.py").write_text("a\n", encoding="utf-8")
        git("add", ".")
        git("commit", "-q", "-m", "init")

        spec = importlib.util.spec_from_file_location("drift_sentinel_real", SENTINEL_PATH)
        self.ds = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.ds)
        self.ds.REPO = str(self.repo)
        self.ds.GIT_REF = "HEAD"
        self.ds.WEBROOT = str(base / "webroot")
        self.ds.DRIFT_DIR = str(self.drift_dir)
        self.ds.LOG = str(self.drift_dir / "sentinel.log")
        self.ds.DEPLOY_LOG = str(self.drift_dir / "deploy_bless.jsonl")
        self.ds.BASELINE = str(self.drift_dir / "baseline.json")
        self.ds.STATE = str(self.drift_dir / ".state.json")
        self.ds.BACKEND_RUNTIME = {}
        self.ds.CONTENT_DOMAINS_RUNTIME = str(base / "domains")
        self.ds.SYSTEMD_DIR = str(base / "systemd")
        os.environ.pop("HQ_DRIFT_EXCEPTIONS", None)
        os.environ.pop("HQ_DRIFT_EXCEPTIONS_REF", None)
        os.environ.pop("HQ_DEPLOY_PR", None)

    def tearDown(self):
        self.tmp.cleanup()

    def _run_main(self, argv):
        old = sys.argv
        sys.argv = ["drift_sentinel.py"] + argv
        try:
            return self.ds.main()
        except SystemExit as e:
            return e.code if isinstance(e.code, int) else 1
        finally:
            sys.argv = old

    def test_ship_command_shape_parses_and_records(self):
        # ship 生成的形态：--pr 174 --bless-deploy a.py b.py —— 解析成功、文件列表完整、pr 落账
        rc = self._run_main(["--pr", "174", "--bless-deploy", "a.py", "b.py"])
        self.assertEqual(rc, 0)
        rec = json.loads(Path(self.ds.DEPLOY_LOG).read_text(encoding="utf-8").strip().splitlines()[-1])
        self.assertEqual(rec["pr"], "174")
        self.assertEqual(rec["files"], ["a.py", "b.py"])

    def test_old_order_really_fails(self):
        # 旧 ship 生成的形态 --bless-deploy --pr 174 a.py b.py：
        # nargs='*' 先吃到空列表，--pr 消费 174 后，文件变未识别参数，退出码 2
        rc = self._run_main(["--bless-deploy", "--pr", "174", "a.py", "b.py"])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
