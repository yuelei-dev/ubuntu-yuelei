# -*- coding: utf-8 -*-
"""ship --pr 透传：部署登记闭环最后一环。

合同（部署登记 §三）：
  ./ship [--exact-files] [--pr <号>] "说明" 文件...
  --pr（或 env HQ_SHIP_PR）必须透传到远端 drift_sentinel --bless-deploy，
  把 PR 号落账 deploy_bless.jsonl；不传时 bless 命令与旧版完全一致。

证明方式（参照 test_ship_version_contract 的静态检查 + 假环境动态执行风格）：
  1. 静态：ship 本文含 --pr 解析、HQ_SHIP_PR 兜底、bless 透传。
  2. 动态：假 ssh 环境跑 ship，按 ssh 调用日志断言 bless 命令带/不带 --pr、
     数字校验在连接服务器之前拦截、显式 --pr 覆盖 env。
"""
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHIP = ROOT / "ship"
SHIP_TEXT = SHIP.read_text(encoding="utf-8")


class ShipPrPassthroughStaticTests(unittest.TestCase):
    """静态合同：--pr 解析、env 兜底、bless 透传都在 ship 本文里。"""

    def test_pr_arg_parsed_and_validated(self):
        self.assertIn('if [ "${1:-}" = "--pr" ]', SHIP_TEXT)
        self.assertIn('HQ_SHIP_PR', SHIP_TEXT)
        self.assertIn("必须是纯数字", SHIP_TEXT)

    def test_bless_deploy_carries_pr(self):
        self.assertIn('--bless-deploy$BLESS_PR', SHIP_TEXT)
        self.assertIn('[ -n "$PR_NO" ] && BLESS_PR=" --pr $PR_NO"', SHIP_TEXT)

    def test_usage_mentions_pr(self):
        self.assertIn('[--pr <号>]', SHIP_TEXT)


class ShipPrPassthroughDynamicTests(unittest.TestCase):
    """动态合同：假环境跑 ship，按 ssh 调用日志验证 bless 命令形态。"""

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

    def test_pr_passed_to_remote_bless(self):
        result = self._run_ship("site/workbench/collect.html", pr="174")
        self.assertEqual(0, result.returncode, result.stdout)
        bless = self._bless_lines()
        self.assertEqual(1, len(bless))
        self.assertIn("--bless-deploy --pr 174", bless[0])
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
        self.assertIn("--bless-deploy --pr 277", bless[0])

    def test_env_hq_ship_pr_non_numeric_rejected(self):
        result = self._run_ship("site/workbench/collect.html", HQ_SHIP_PR="pr-174")
        self.assertEqual(1, result.returncode, result.stdout)
        self.assertIn("必须是纯数字", result.stdout)

    def test_explicit_pr_overrides_env(self):
        result = self._run_ship("site/workbench/collect.html", pr="222", HQ_SHIP_PR="111")
        self.assertEqual(0, result.returncode, result.stdout)
        bless = self._bless_lines()
        self.assertEqual(1, len(bless))
        self.assertIn("--bless-deploy --pr 222", bless[0])
        self.assertNotIn("111", bless[0])


if __name__ == "__main__":
    unittest.main()
