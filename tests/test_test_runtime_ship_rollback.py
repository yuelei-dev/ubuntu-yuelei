# -*- coding: utf-8 -*-
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads(
    (ROOT / "deploy/test-runtime/pr180/manifest.json").read_text(encoding="utf-8")
)
SOURCES = [entry["source"] for entry in MANIFEST["deploy_files"]]
HEAD_SHA = "1" * 40


@unittest.skipUnless(os.name == "posix" and shutil.which("bash"), "requires POSIX bash")
class TestRuntimeShipRollbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bin_dir = Path(self.tmp.name) / "bin"
        self.bin_dir.mkdir()
        self.log = Path(self.tmp.name) / "remote.log"
        self.push_count = Path(self.tmp.name) / "push-count"
        self.real_git = shutil.which("git")
        self.assertIsNotNone(self.real_git)
        self._write_fake("git", f"""
            #!{sys.executable}
            import os, subprocess, sys
            args = sys.argv[1:]
            if args and args[0] == "hash-object":
                raise SystemExit(subprocess.call([{self.real_git!r}, *args]))
            if args[:2] == ["rev-parse", "HEAD"] or args[:2] == ["rev-parse", "origin/main"]:
                print({HEAD_SHA!r}); raise SystemExit(0)
            if args[:3] == ["rev-parse", "--short=8", "HEAD"]:
                print({HEAD_SHA[:8]!r}); raise SystemExit(0)
            if args[:3] == ["rev-parse", "--short", "HEAD"]:
                print({HEAD_SHA[:7]!r}); raise SystemExit(0)
            raise SystemExit(0)
        """)
        self._write_fake("rsync", f"""
            #!{sys.executable}
            import os, pathlib, sys
            state = pathlib.Path(os.environ["FAKE_PUSH_COUNT"])
            count = int(state.read_text() or "0") + 1 if state.exists() else 1
            state.write_text(str(count))
            with open(os.environ["FAKE_REMOTE_LOG"], "a", encoding="utf-8") as f:
                f.write("PUSH %d %s\\n" % (count, " ".join(sys.argv[1:])))
            fail = int(os.environ.get("FAIL_PUSH_N", "0"))
            raise SystemExit(1 if fail == count else 0)
        """)
        preimages = {
            entry["target"]: entry.get("expected_target_sha256")
            for entry in MANIFEST["deploy_files"]
            if entry["expected_target_state"] == "sha256"
        }
        postimages = {entry["target"]: entry["sha256"] for entry in MANIFEST["deploy_files"]}
        self._write_fake("ssh", f"""
            #!{sys.executable}
            import json, os, pathlib, sys
            command = " ".join(sys.argv[1:])
            log = pathlib.Path(os.environ["FAKE_REMOTE_LOG"])
            with log.open("a", encoding="utf-8") as f:
                f.write("SSH " + command + "\\n")
            if " bash -s" in command or command.rstrip().endswith("python3 -"):
                sys.stdin.read()
            if "sudo test -f '/home/ubuntu/hq-drift/active_overlays.json'" in command:
                raise SystemExit(1)
            if "sudo sha256sum '" in command and "cut -d" in command:
                count_path = pathlib.Path(os.environ["FAKE_PUSH_COUNT"])
                pushed = int(count_path.read_text()) if count_path.exists() else 0
                mapping = {postimages!r} if pushed else {preimages!r}
                for target, digest in mapping.items():
                    if target in command:
                        print(digest)
                        raise SystemExit(0)
                raise SystemExit(1)
            if "sudo cat '/home/ubuntu/content-api/.deploy-version.tmp." in command:
                print({HEAD_SHA!r}); raise SystemExit(0)
            if "date +%s" in command:
                print("1770000000"); raise SystemExit(0)
            if " bash -s" in command and os.environ.get("FAIL_STAGE") == "smoke":
                raise SystemExit(1)
            if "sudo systemctl restart " in command and os.environ.get("FAIL_STAGE") == "restart":
                raise SystemExit(1)
            if "--verify-deploy" in command and os.environ.get("FAIL_STAGE") == "verify":
                raise SystemExit(1)
            if "curl -sS -o /dev/null -w" in command:
                print("503" if os.environ.get("FAIL_STAGE") == "health" else "200", end="")
                raise SystemExit(0)
            if command.rstrip().endswith("python3 -") and os.environ.get("FAIL_STAGE") == "pricing":
                raise SystemExit(1)
            if "--activate-overlay" in command and os.environ.get("FAIL_STAGE") == "activate":
                raise SystemExit(1)
            if "HQ_DRIFT_REF=origin/baseline/test-server" in command and os.environ.get("FAIL_STAGE") == "patrol":
                raise SystemExit(1)
            if "sudo '/opt/huangque-deploy-backups/" in command and command.rstrip().endswith("/restore.sh'"):
                with log.open("a", encoding="utf-8") as f:
                    f.write("RESTORE_EXECUTED preimage absent services-active\\n")
                raise SystemExit(0)
            raise SystemExit(0)
        """)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_fake(self, name, body):
        path = self.bin_dir / name
        path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def run_ship(self, **extra_env):
        env = os.environ.copy()
        env.update({
            "PATH": str(self.bin_dir) + os.pathsep + env["PATH"],
            "HQ_REMOTE": "root@8.148.158.106",
            "HQ_SHIP_TARGET": "test",
            "HQ_SERVICE_WAIT_SECONDS": "1",
            "FAKE_REMOTE_LOG": str(self.log),
            "FAKE_PUSH_COUNT": str(self.push_count),
        })
        env.update(extra_env)
        return subprocess.run(
            [shutil.which("bash"), "ship", "--exact-files", "--pr", "184",
             "test overlay", *SOURCES],
            cwd=ROOT, env=env, text=True, capture_output=True, timeout=30,
        )

    def read_log(self):
        return self.log.read_text(encoding="utf-8") if self.log.exists() else ""

    def assert_restore_ran(self, result):
        self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("RESTORE_EXECUTED preimage absent services-active", self.read_log())
        self.assertIn("自动恢复通过", result.stdout)

    def test_nth_push_failure_automatically_restores(self):
        result = self.run_ship(FAIL_PUSH_N="5")
        self.assert_restore_ran(result)
        self.assertEqual("5", self.push_count.read_text())

    def test_each_late_postcondition_failure_automatically_restores(self):
        for stage in ("smoke", "restart", "verify", "health", "pricing", "activate", "patrol"):
            with self.subTest(stage=stage):
                self.log.unlink(missing_ok=True)
                self.push_count.unlink(missing_ok=True)
                result = self.run_ship(FAIL_STAGE=stage)
                self.assert_restore_ran(result)
                self.assertEqual(str(len(SOURCES)), self.push_count.read_text())

    def test_success_disarms_restore_only_after_patrol(self):
        result = self.run_ship()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(str(len(SOURCES)), self.push_count.read_text())
        self.assertNotIn("RESTORE_EXECUTED", self.read_log())
        self.assertIn("常规巡检无 stale", result.stdout)
        self.assertIn("上线完成", result.stdout)


if __name__ == "__main__":
    unittest.main()
