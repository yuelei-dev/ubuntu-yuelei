# -*- coding: utf-8 -*-
"""部署顺序合同：.deploy-version 必须原子写入、且先于服务重启。

合同要证明的不变量（PR #174 评审 P1）：
    重启后进程的 jobs.service_sha == health.deploy_sha == 本次部署精确 SHA

证明方式（参照 test_ship_health_gate 的静态检查 + 假环境动态执行风格）：
  1. 静态：ship 中「写入部署版本标记」阶段出现在「重启服务」阶段之前；
     写入是原子模式（临时文件 + 校验 + mv），不存在只警告不阻断的旧路径。
  2. 动态：假 ssh 环境下跑 ship，按 ssh 调用日志断言版本标记写在 systemctl
     restart 之前、内容为精确 SHA；写入/校验/替换任一步失败都在 restart 之前
     中止发布。
  3. jobs_store 导入时只读一次缓存的单元测试在 test_deploy_version_binding.py
     （ServiceShaImportCacheTests）—— 它是「必须先写再重启」的根本原因。
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

FAKE_SHA = "f47ac10b58cc4372a5670e02b2c3d479"


class ShipVersionOrderStaticTests(unittest.TestCase):
    """静态合同：阶段顺序 + 原子写模式，直接断言 ship 脚本本文。"""

    def test_version_write_stage_precedes_restart_stage(self):
        write_at = SHIP_TEXT.index("写入部署版本标记")
        restart_at = SHIP_TEXT.index("==> 4/5 重启服务")
        self.assertLess(write_at, restart_at,
                        "版本标记必须在重启前写入（SERVICE_SHA 只在进程启动时读一次）")

    def test_version_write_stage_after_import_smoke(self):
        smoke_at = SHIP_TEXT.index("import 冒烟")
        write_at = SHIP_TEXT.index("写入部署版本标记")
        self.assertLess(smoke_at, write_at,
                        "代码部署 + import 合同检查通过之后才写版本标记")

    def test_atomic_write_pattern_tmp_verify_mv(self):
        self.assertIn('VERSION_FILE="/home/ubuntu/content-api/.deploy-version"', SHIP_TEXT)
        self.assertIn('.tmp.$$', SHIP_TEXT)                                   # 临时文件
        self.assertIn("sudo mv '$VERSION_TMP' '$VERSION_FILE'", SHIP_TEXT)    # 原子替换
        self.assertIn("WRITTEN_SHA", SHIP_TEXT)                               # 回读校验
        # 不允许回到「直接 tee 正式文件、失败只警告」的旧路径
        self.assertNotIn("不阻断发布", SHIP_TEXT)
        self.assertNotIn("sudo tee /home/ubuntu/content-api/.deploy-version", SHIP_TEXT)

    def test_exact_files_guard_present(self):
        self.assertIn('--exact-files', SHIP_TEXT)
        self.assertIn("必须显式用 --exact-files", SHIP_TEXT)


class ShipVersionOrderDynamicTests(unittest.TestCase):
    """动态合同：假环境跑 ship，按 ssh 调用日志验证顺序/原子/中止。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.bin = Path(self.tmp.name) / "bin"
        self.home.mkdir()
        self.bin.mkdir()
        self.ssh_log = Path(self.tmp.name) / "ssh.log"
        self.rsync_log = Path(self.tmp.name) / "rsync.log"
        self._write_executable(
            "git",
            """#!/bin/sh
if [ "$1" = "diff" ]; then exit 0; fi
if [ "$1" = "rev-parse" ]; then echo "${FAKE_SHA:-f47ac10b58cc4372a5670e02b2c3d479}"; exit 0; fi
exit 0
""",
        )
        self._write_executable(
            "ssh",
            """#!/bin/sh
printf '%s\\n' "$*" >> "$SSH_LOG"
case "$*" in
  *"bash -s"*)
    cat >/dev/null 2>&1   # 吞掉 stdin 里的远端脚本（smoke_import / check_restart_effective）
    echo "    ✓ ok"
    exit 0
    ;;
  *"date +%s"*) echo 1700000000; exit 0 ;;
  *"systemctl is-active"*) exit 0 ;;
  *"sudo tee"*"deploy-version.tmp"*)
    if [ "$FAKE_VERSION_WRITE_FAIL" = "1" ]; then exit 1; fi
    exit 0
    ;;
  *"sudo cat"*"deploy-version.tmp"*)
    if [ "$FAKE_VERSION_MISMATCH" = "1" ]; then echo deadbeefdeadbeef; else echo "${FAKE_SHA:-f47ac10b58cc4372a5670e02b2c3d479}"; fi
    exit 0
    ;;
  *"sudo mv"*"deploy-version"*)
    if [ "$FAKE_MV_FAIL" = "1" ]; then exit 1; fi
    exit 0
    ;;
esac
exit 0
""",
        )
        self._write_executable(
            "curl",
            """#!/bin/sh
printf %s "${FAKE_CURL_CODE:-200}"
""",
        )
        self._write_executable(
            "rsync",
            """#!/bin/sh
if [ -n "$RSYNC_LOG" ]; then printf '%s\n' "$*" >> "$RSYNC_LOG"; fi
exit 0
""",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _write_executable(self, name, content):
        path = self.bin / name
        path.write_text(content, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def _run_ship(self, target, exact=False, **overrides):
        env = os.environ.copy()
        env.update({
            "PATH": str(self.bin) + os.pathsep + env.get("PATH", ""),
            "HOME": str(self.home),
            "HQ_REMOTE": "fake-server",
            "HQ_SERVICE_WAIT_SECONDS": "1",
            "SSH_LOG": str(self.ssh_log),
            "RSYNC_LOG": str(self.rsync_log),
            "FAKE_SHA": FAKE_SHA,
        })
        env.update(overrides)
        argv = ["bash", str(SHIP)]
        if exact:
            argv.append("--exact-files")
        argv += ["test deployment", target]
        return subprocess.run(
            argv, cwd=ROOT, env=env, text=True, encoding="utf-8", errors="replace",
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=15,
        )

    def _ssh_log_lines(self):
        return self.ssh_log.read_text(encoding="utf-8").splitlines()

    def test_version_written_atomically_before_restart_with_exact_sha(self):
        result = self._run_ship("server/content_domains/jobs_store.py", exact=True)
        self.assertEqual(0, result.returncode, result.stdout)
        self.assertIn("版本标记已原子写入", result.stdout)
        lines = self._ssh_log_lines()
        write_idx = next(i for i, l in enumerate(lines)
                         if "deploy-version.tmp" in l and "tee" in l)
        restart_idx = next(i for i, l in enumerate(lines) if "systemctl restart" in l)
        self.assertLess(write_idx, restart_idx,
                        "版本标记写入必须先于 systemctl restart：\n" + "\n".join(lines))
        # 写入内容 = 本次部署精确 SHA（git rev-parse HEAD）
        self.assertIn(FAKE_SHA, lines[write_idx])
        # 原子替换落在 restart 之前
        mv_idx = next(i for i, l in enumerate(lines)
                      if "sudo mv" in l and "deploy-version" in l)
        self.assertLess(mv_idx, restart_idx)

    def test_exact_files_mode_skips_directory_sync(self):
        result = self._run_ship("server/content_domains/jobs_store.py", exact=True)
        self.assertEqual(0, result.returncode, result.stdout)
        self.assertIn("不触发 content_domains 整目录同步", result.stdout)
        self.assertNotIn("整目录同步", result.stdout.replace("不触发 content_domains 整目录同步", ""))
        if self.rsync_log.exists():
            self.assertNotIn("--delete", self.rsync_log.read_text(encoding="utf-8"))

    def test_version_write_failure_aborts_before_restart(self):
        result = self._run_ship("server/content_domains/jobs_store.py", exact=True,
                                FAKE_VERSION_WRITE_FAIL="1")
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertIn("版本标记临时文件写入失败", result.stdout)
        self.assertIn("旧标记未被触碰", result.stdout)
        logged = "\n".join(self._ssh_log_lines())
        self.assertNotIn("systemctl restart", logged)   # 中止于重启之前
        self.assertNotIn("sudo mv", logged)             # 旧标记未被替换
        self.assertNotIn("上线完成", result.stdout)

    def test_version_mismatch_aborts_before_restart(self):
        result = self._run_ship("server/content_domains/jobs_store.py", exact=True,
                                FAKE_VERSION_MISMATCH="1")
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertIn("版本标记校验失败", result.stdout)
        logged = "\n".join(self._ssh_log_lines())
        self.assertNotIn("systemctl restart", logged)
        self.assertNotIn("sudo mv", logged)

    def test_pure_frontend_deploy_skips_version_write(self):
        """无重启 = 进程未变：不写版本标记，SERVICE_SHA 与 health.deploy_sha 仍一致。"""
        result = self._run_ship("site/workbench/collect.html", exact=True)
        self.assertEqual(0, result.returncode, result.stdout)
        logged = "\n".join(self._ssh_log_lines())
        self.assertNotIn("deploy-version", logged)
        self.assertNotIn("systemctl restart", logged)


if __name__ == "__main__":
    unittest.main()
