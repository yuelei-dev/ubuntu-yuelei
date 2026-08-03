import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SHIP = ROOT / "ship"


class ShipHealthGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.bin = Path(self.tmp.name) / "bin"
        self.home.mkdir()
        self.bin.mkdir()
        self._write_executable(
            "git",
            """#!/bin/sh
if [ "$1" = "diff" ]; then exit 0; fi
if [ "$1" = "rev-parse" ]; then echo abc1234; exit 0; fi
exit 0
""",
        )
        self._write_executable(
            "ssh",
            """#!/bin/sh
# smoke_import / check_restart_effective 把脚本从 stdin 喂给远端 bash -s，
# 参数里只有服务名等，所以按「第二个位置参数」区分二者：
#   smoke_import         → bash -s -- <svc> <python 路径>
#   check_restart_effective → bash -s -- <svc> <时间戳>
case "$*" in
  *"bash -s"*)
    cat >/dev/null 2>&1   # 吞掉 stdin 里的远端脚本
    case "$*" in
      *python3*)
        if [ "$FAKE_IMPORT_FAIL" = "1" ]; then echo "    ❌ import 失败 —— 中止，不重启"; exit 1; fi
        echo "    ✓ import 通过"
        exit 0
        ;;
      *)
        if [ "$FAKE_RESTART_INEFFECTIVE" = "1" ]; then echo "    ❌ restart 没有真的发生"; exit 1; fi
        if [ "$FAKE_ENV_NEWER" = "1" ]; then echo "    ❌ 新配置没被加载"; exit 1; fi
        echo "    ✓ 重启生效、配置已加载"
        exit 0
        ;;
    esac
    ;;
  *"date +%s"*)
    echo 1700000000
    exit 0
    ;;
  *"systemctl is-active"*)
    if [ "$FAKE_SERVICE_INACTIVE" = "1" ]; then exit 1; fi
    ;;
  # 3.6 版本标记原子写：tee 写临时文件 → cat 回读校验（回读=假 git 的 abc1234）→ mv 替换
  *"deploy-version.tmp"*)
    case "$*" in
      *"sudo cat"*) echo abc1234 ;;
    esac
    exit 0
    ;;
  *"sudo mv"*"deploy-version"*)
    exit 0
    ;;
esac
exit 0
""",
        )
        self._write_executable(
            "curl",
            """#!/bin/sh
if [ "$FAKE_CURL_FAIL" = "1" ]; then printf 000; exit 7; fi
printf %s "${FAKE_CURL_CODE:-200}"
""",
        )
        self._write_executable(
            "rsync",
            """#!/bin/sh
if [ -n "$FAKE_RSYNC_LOG" ]; then printf '%s\n' "$*" >> "$FAKE_RSYNC_LOG"; fi
exit 0
""",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _write_executable(self, name, content):
        path = self.bin / name
        path.write_text(content, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def _run_ship(self, target="unknown.file", exact=False, **overrides):
        env = os.environ.copy()
        env.update({
            "PATH": str(self.bin) + os.pathsep + env.get("PATH", ""),
            "HOME": str(self.home),
            "HQ_REMOTE": "fake-server",
            "HQ_SERVICE_WAIT_SECONDS": "1",
        })
        env.update(overrides)
        argv = ["bash", str(SHIP)]
        if exact:
            argv.append("--exact-files")
        argv += ["test deployment", target]
        return subprocess.run(
            argv,
            cwd=ROOT,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=10,
        )

    def test_http_200_allows_success(self):
        result = self._run_ship(FAKE_CURL_CODE="200")
        self.assertEqual(0, result.returncode, result.stdout)
        self.assertIn("健康检查: HTTP 200", result.stdout)
        self.assertIn("上线完成", result.stdout)

    def test_http_502_blocks_deployment_success(self):
        result = self._run_ship(FAKE_CURL_CODE="502")
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertIn("健康检查未通过: HTTP 502", result.stdout)
        self.assertNotIn("上线完成", result.stdout)

    def test_curl_failure_blocks_deployment_success(self):
        result = self._run_ship(FAKE_CURL_FAIL="1")
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertIn("健康检查请求失败", result.stdout)
        self.assertNotIn("上线完成", result.stdout)

    def test_service_must_be_active_after_restart(self):
        result = self._run_ship(
            target="server/content_api.py",
            FAKE_SERVICE_INACTIVE="1",
            FAKE_CURL_CODE="200",
        )
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertIn("未进入 active", result.stdout)
        self.assertNotIn("上线完成", result.stdout)

    def test_content_domain_change_requires_exact_files_mode(self):
        """默认模式部署 content_domains 文件被拒绝：整目录 --delete 同步会覆盖测试服登记例外。"""
        rsync_log = Path(self.tmp.name) / "rsync.log"
        result = self._run_ship(
            target="server/content_domains/core.py",
            FAKE_CURL_CODE="200",
            FAKE_RSYNC_LOG=str(rsync_log),
        )
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertIn("--exact-files", result.stdout)
        self.assertNotIn("上线完成", result.stdout)
        self.assertFalse(rsync_log.exists(), "拒绝发布前不应触发任何 rsync")

    def test_content_api_change_syncs_directory_and_imports_before_restart(self):
        rsync_log = Path(self.tmp.name) / "rsync.log"
        result = self._run_ship(
            target="server/content_api.py",
            FAKE_CURL_CODE="200",
            FAKE_RSYNC_LOG=str(rsync_log),
        )
        self.assertEqual(0, result.returncode, result.stdout)
        self.assertIn("整目录同步", result.stdout)
        self.assertIn("import 通过", result.stdout)
        self.assertIn("server/content_domains/", rsync_log.read_text(encoding="utf-8"))

    def test_content_import_failure_stops_before_restart(self):
        result = self._run_ship(
            target="server/content_api.py",
            FAKE_IMPORT_FAIL="1",
            FAKE_CURL_CODE="200",
        )
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertIn("import 失败", result.stdout)
        self.assertNotIn("上线完成", result.stdout)

    def test_silent_restart_blocks_deployment_success(self):
        """restart 没真的发生（启动时间早于 T0）→ 阻断，不打印上线完成。"""
        result = self._run_ship(
            target="server/content_api.py",
            FAKE_RESTART_INEFFECTIVE="1",
            FAKE_CURL_CODE="200",
        )
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertIn("restart 没有真的发生", result.stdout)
        self.assertNotIn("上线完成", result.stdout)

    def test_stale_env_not_loaded_blocks_deployment_success(self):
        """env 文件比进程启动还新（配置进了文件没进进程）→ 阻断。"""
        result = self._run_ship(
            target="server/content_api.py",
            FAKE_ENV_NEWER="1",
            FAKE_CURL_CODE="200",
        )
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertIn("新配置没被加载", result.stdout)
        self.assertNotIn("上线完成", result.stdout)


if __name__ == "__main__":
    unittest.main()
