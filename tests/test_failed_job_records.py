# -*- coding: utf-8 -*-
"""#201 失败生成记录查看与删除 回归测试。

覆盖三条链路：
1. expand_job_results(include_failed=True)：失败记录展开为 error 卡片，payload 容错，旧调用兼容；
2. delete_failed_job：本人 failed/error 可删、视频类连动清理 video_assets、
   非失败状态拒绝、跨用户拒绝、重复删除拒绝、非法 job_id 拒绝；
3. 路由与 SQL 门禁：/api/gen/job/delete 在 do_POST 且先鉴权，history 查询用 ?=1 门控 include_failed。
"""
import importlib, json, os, re, sys, time, unittest
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.content_domains.history import expand_job_results

CORE = (ROOT / "server" / "content_domains" / "core.py").read_text(encoding="utf-8")


class ExpandJobResultsFailedTests(unittest.TestCase):
    def test_failed_row_expands_to_error_card(self):
        rows = [{
            "id": 12, "status": "failed",
            "payload": json.dumps({"prompt": "一只猫"}),
            "result": None, "error": "余额不足", "created_at": 200,
        }]
        items = expand_job_results(rows, 9, include_failed=True)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["status"], "error")
        self.assertEqual(items[0]["error"], "余额不足")
        self.assertEqual(items[0]["prompt"], "一只猫")
        self.assertEqual(items[0]["job_id"], 12)

    def test_failed_row_with_broken_payload_json_tolerated(self):
        rows = [{
            "id": 5, "status": "error", "payload": "{bad",
            "result": None, "error": "上游超时", "created_at": 1,
        }]
        items = expand_job_results(rows, 9, include_failed=True)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["status"], "error")
        self.assertFalse(items[0]["prompt"])

    def test_failed_row_without_error_text_gets_default(self):
        rows = [{"id": 6, "status": "failed", "payload": None,
                 "result": None, "error": None, "created_at": 1}]
        items = expand_job_results(rows, 9, include_failed=True)
        self.assertEqual(items[0]["error"], "生成失败")

    def test_failed_rows_skipped_without_include_failed(self):
        rows = [{
            "id": 12, "status": "failed", "payload": "{}",
            "result": None, "error": "e", "created_at": 200,
        }, {
            "id": 11, "status": "done", "payload": None,
            "result": json.dumps({"url": "/a.png"}), "error": None, "created_at": 100,
        }]
        items = expand_job_results(rows, 9)
        self.assertEqual([it["url"] for it in items], ["/a.png"])
        self.assertEqual(items[0]["status"], "done")

    def test_legacy_rows_without_status_column_still_work(self):
        # 旧查询只选 id/result/created_at 三列，缺 status 列不得炸
        rows = [{"id": 8, "result": json.dumps({"url": "/c.png"}), "created_at": 1}]
        items = expand_job_results(rows, 9, include_failed=True)
        self.assertEqual(items[0]["url"], "/c.png")
        self.assertEqual(items[0]["status"], "done")


class DeleteFailedJobTests(unittest.TestCase):
    def setUp(self):
        server_dir = str(ROOT / "server")
        if server_dir not in sys.path:
            sys.path.insert(0, server_dir)
        self.core = importlib.import_module("content_domains.core")
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self._orig_jobdb = self.core.JOB_DB
        self._orig_audiodb = self.core.AUDIO_DB
        self.core.JOB_DB = os.path.join(self.tmp.name, "jobs.db")
        self.core.AUDIO_DB = os.path.join(self.tmp.name, "assets.db")
        with closing(self.core.jdb()) as c:
            c.execute("""CREATE TABLE jobs(
                id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, username TEXT, cost INTEGER,
                status TEXT DEFAULT 'pending', payload TEXT, result TEXT, error TEXT,
                created_at INTEGER, updated_at INTEGER, deleted INTEGER DEFAULT 0,
                refunded INTEGER DEFAULT 0, owner TEXT)""")  # schema 跟着 init_db 走
            c.commit()
        with closing(self.core.adb()) as c:
            c.execute("""CREATE TABLE video_assets(
                id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER UNIQUE,
                username TEXT NOT NULL, status TEXT DEFAULT 'ready',
                updated_at INTEGER)""")
            c.commit()

    def tearDown(self):
        self.core.JOB_DB = self._orig_jobdb
        self.core.AUDIO_DB = self._orig_audiodb
        self.tmp.cleanup()

    def _insert_job(self, username="u1", kind="image", status="failed"):
        now = int(time.time())
        with closing(self.core.jdb()) as c:
            cur = c.execute(
                "INSERT INTO jobs(kind,username,status,payload,error,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (kind, username, status, "{}", "boom", now, now),
            )
            c.commit()
            return cur.lastrowid

    def _job_deleted(self, jid):
        with closing(self.core.jdb()) as c:
            return c.execute("SELECT COALESCE(deleted,0) FROM jobs WHERE id=?", (jid,)).fetchone()[0]

    def test_delete_own_failed_image_job(self):
        jid = self._insert_job()
        out = self.core.delete_failed_job("u1", jid)
        self.assertTrue(out["deleted"])
        self.assertEqual(out["job_id"], jid)
        self.assertEqual(self._job_deleted(jid), 1)

    def test_delete_failed_video_job_cascades_video_assets(self):
        jid = self._insert_job(kind="video", status="error")
        with closing(self.core.adb()) as c:
            c.execute("INSERT INTO video_assets(job_id,username,status) VALUES(?,?,?)",
                      (jid, "u1", "ready"))
            c.commit()
        self.core.delete_failed_job("u1", jid)
        with closing(self.core.adb()) as c:
            st = c.execute("SELECT status FROM video_assets WHERE job_id=?", (jid,)).fetchone()[0]
        self.assertEqual(st, "deleted")
        self.assertEqual(self._job_deleted(jid), 1)

    def test_done_job_rejected(self):
        jid = self._insert_job(status="done")
        with self.assertRaises(ValueError):
            self.core.delete_failed_job("u1", jid)
        self.assertEqual(self._job_deleted(jid), 0)

    def test_running_job_rejected(self):
        jid = self._insert_job(status="running")
        with self.assertRaises(ValueError):
            self.core.delete_failed_job("u1", jid)
        self.assertEqual(self._job_deleted(jid), 0)

    def test_cross_user_delete_rejected(self):
        jid = self._insert_job(username="u2")
        with self.assertRaises(LookupError):
            self.core.delete_failed_job("u1", jid)
        self.assertEqual(self._job_deleted(jid), 0)

    def test_repeat_delete_rejected(self):
        jid = self._insert_job()
        self.core.delete_failed_job("u1", jid)
        with self.assertRaises(LookupError):
            self.core.delete_failed_job("u1", jid)

    def test_invalid_job_id_rejected(self):
        with self.assertRaises(ValueError):
            self.core.delete_failed_job("u1", "abc")
        with self.assertRaises(ValueError):
            self.core.delete_failed_job("u1", None)


class FailedJobRouteGateTests(unittest.TestCase):
    def test_delete_route_requires_auth_and_scoped_call(self):
        idx = CORE.find('"/api/gen/job/delete"')
        self.assertGreater(idx, 0, "缺少 /api/gen/job/delete 路由")
        # 路由必须在 do_POST 内
        self.assertGreater(idx, CORE.find("def do_POST(self"))
        post_end = CORE.find("def do_GET(self")
        self.assertTrue(post_end < 0 or idx < post_end, "路由不在 do_POST 内")
        block = CORE[idx:idx + 700]
        self.assertIn("verify(self._token())", block, "删除路由未鉴权")
        self.assertIn('return self._send(401', block, "删除路由缺 401")
        self.assertIn('delete_failed_job(user["username"]', block, "删除未按登录用户名限定")

    def test_history_query_gates_include_failed(self):
        self.assertIn("(?=1 AND status IN ('error','failed'))", CORE,
                      "history 查询缺 include_failed 门控")
        self.assertIn('include_failed = kind == "image"', CORE,
                      "include_failed 未限定 image 类")
        self.assertIn("items = history.expand_job_results(rows, lim, offset, include_failed=True)", CORE)


if __name__ == "__main__":
    unittest.main()
