# -*- coding: utf-8 -*-
"""任务级扣点/退点的审计流水（points_audit）。

接入前 points_audit 只记录管理员加减点和充值审批，任务扣退点完全隐形 ——
线上那 21 条「既退了点又出了结果」的僵尸记录之所以核不了账，就是因为没有流水。

核心不变量：
1. 扣点/退点与审计行同生共死（同一个事务；余额不足回滚时不能留下孤儿审计行）
2. amount=0 不写审计行（避免噪声）
3. who_admin='system' 与人工操作可区分，后台能按 actor 过滤
4. reason 原样落库并截断，不影响点数正确性
"""
import os
import sqlite3
import tempfile
import threading
import unittest


class PointsAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["HQ_TEST_AUTH_DB"] = os.path.join(self.tmp.name, "users.db")

        import importlib
        import server.auth_server as auth_server

        self.auth = importlib.reload(auth_server)
        self.auth.DB = os.environ["HQ_TEST_AUTH_DB"]
        self.auth.init_db()
        c = sqlite3.connect(self.auth.DB)
        try:
            c.execute(
                "INSERT INTO users(username,pw_hash,pw_salt,display_name,points,role,must_change) "
                "VALUES('fang','h','s','fang',10,'member',0)"
            )
            c.commit()
        finally:
            c.close()
        self.addCleanup(self.tmp.cleanup)

    def _audit(self):
        c = sqlite3.connect(self.auth.DB)
        c.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in c.execute("SELECT * FROM points_audit ORDER BY id")]
        finally:
            c.close()

    def _points(self):
        c = sqlite3.connect(self.auth.DB)
        try:
            return c.execute("SELECT points FROM users WHERE username='fang'").fetchone()[0]
        finally:
            c.close()

    def test_deduct_writes_audit_row_with_before_and_after(self):
        pts, err = self.auth.deduct_points("fang", 3, "job:collect")
        self.assertIsNone(err)
        rows = self._audit()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["who_admin"], self.auth.SYSTEM_ACTOR)
        self.assertEqual(r["delta"], -3)
        self.assertEqual(r["before_points"], 10)
        self.assertEqual(r["after_points"], 7)
        self.assertEqual(r["reason"], "job:collect")
        self.assertEqual(self._points(), 7)

    def test_refund_writes_positive_delta(self):
        self.auth.deduct_points("fang", 3, "job:collect")
        pts, err = self.auth.refund_points("fang", 3, "job#1354")
        self.assertIsNone(err)
        r = self._audit()[-1]
        self.assertEqual(r["delta"], 3)
        self.assertEqual(r["before_points"], 7)
        self.assertEqual(r["after_points"], 10)
        self.assertEqual(r["reason"], "job#1354")
        self.assertEqual(self._points(), 10)

    def test_insufficient_deduct_leaves_no_orphan_audit_row(self):
        """余额不足要整体回滚：既不能扣点，也不能留下一条「扣了 99 点」的假流水。"""
        pts, err = self.auth.deduct_points("fang", 99, "job:image")
        self.assertEqual(err, "insufficient")
        self.assertIsNone(pts)
        self.assertEqual(self._audit(), [])
        self.assertEqual(self._points(), 10)

    def test_unknown_user_writes_nothing(self):
        pts, err = self.auth.deduct_points("nobody", 1, "job:image")
        self.assertEqual(err, "not_found")
        self.assertEqual(self._audit(), [])

    def test_zero_amount_writes_no_audit_row(self):
        """amount=0 是合法的空操作（points.py 会直接短路），不该污染流水。"""
        pts, err = self.auth.deduct_points("fang", 0, "job:copy")
        self.assertIsNone(err)
        self.assertEqual(self._audit(), [])
        self.assertEqual(self._points(), 10)

    def test_long_reason_is_truncated_but_points_still_correct(self):
        self.auth.deduct_points("fang", 1, "x" * 500)
        r = self._audit()[-1]
        self.assertEqual(len(r["reason"]), 120)
        self.assertEqual(self._points(), 9)

    def test_missing_reason_defaults_to_empty_not_crash(self):
        """老调用方（未升级的服务）不传 reason，必须照常扣点。"""
        pts, err = self.auth.deduct_points("fang", 2)
        self.assertIsNone(err)
        self.assertEqual(self._audit()[-1]["reason"], "")
        self.assertEqual(self._points(), 8)

    def test_list_filters_system_vs_admin(self):
        self.auth.deduct_points("fang", 1, "job:collect")             # system
        self.auth.adjust_points_admin("boss", "fang", 5, "人工补点")   # admin
        all_rows = self.auth.list_points_audit()["items"]
        self.assertEqual(len(all_rows), 2)
        sys_rows = self.auth.list_points_audit(actor="system")["items"]
        self.assertEqual([r["reason"] for r in sys_rows], ["job:collect"])
        adm_rows = self.auth.list_points_audit(actor="admin")["items"]
        self.assertEqual([r["who_admin"] for r in adm_rows], ["boss"])

    def test_list_by_username_still_works_with_actor_filter(self):
        self.auth.deduct_points("fang", 1, "job:collect")
        rows = self.auth.list_points_audit(username="fang", actor="system")["items"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.auth.list_points_audit(username="ghost")["items"], [])

    def test_deduct_then_refund_nets_to_zero_and_pairs_by_job(self):
        """对账场景：一次任务的扣与退能配对，净额为 0。"""
        self.auth.deduct_points("fang", 4, "job:collect")
        self.auth.refund_points("fang", 4, "job#1354")
        rows = self.auth.list_points_audit(actor="system")["items"]
        self.assertEqual(sum(r["delta"] for r in rows), 0)
        self.assertEqual(self._points(), 10)

    def test_deduct_transaction_key_replays_without_second_charge(self):
        key = "breakdown-upload-charge:test0001"
        first = self.auth.deduct_points(
            "fang", 3, "job:breakdown", transaction_key=key)
        replay = self.auth.deduct_points(
            "fang", 3, "job:breakdown", transaction_key=key)
        self.assertEqual(first[1], None)
        self.assertEqual(replay[1], None)
        self.assertEqual(self._points(), 7)
        rows = self._audit()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["transaction_key"], key)

    def test_refund_transaction_key_replays_without_second_refund(self):
        self.auth.deduct_points("fang", 3, "job:breakdown")
        key = "breakdown-upload-refund:test0001"
        self.auth.refund_points(
            "fang", 3, "job#1", transaction_key=key)
        self.auth.refund_points(
            "fang", 3, "job#1", transaction_key=key)
        self.assertEqual(self._points(), 10)
        self.assertEqual(
            [row["transaction_key"] for row in self._audit()].count(key), 1)

    def test_transaction_key_rejects_different_amount_or_direction(self):
        key = "breakdown-upload-charge:test0002"
        self.auth.deduct_points(
            "fang", 2, "job:breakdown", transaction_key=key)
        with self.assertRaises(self.auth.PointsTransactionConflict):
            self.auth.deduct_points(
                "fang", 3, "job:breakdown", transaction_key=key)
        with self.assertRaises(self.auth.PointsTransactionConflict):
            self.auth.refund_points(
                "fang", 2, "job#1", transaction_key=key)
        self.assertEqual(self._points(), 8)

    def test_concurrent_same_transaction_key_charges_once(self):
        key = "breakdown-upload-charge:concurrent0001"
        barrier = threading.Barrier(6)
        results = []
        errors = []

        def worker():
            try:
                barrier.wait(timeout=5)
                results.append(self.auth.deduct_points(
                    "fang", 3, "job:breakdown", transaction_key=key))
            except Exception as error:
                errors.append(error)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 6)
        self.assertEqual(self._points(), 7)
        self.assertEqual(
            [row["transaction_key"] for row in self._audit()].count(key), 1)

    def test_init_db_migrates_legacy_audit_schema_idempotently(self):
        legacy_db = os.path.join(self.tmp.name, "legacy-users.db")
        c = sqlite3.connect(legacy_db)
        try:
            c.execute(
                "CREATE TABLE users("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, "
                "pw_hash TEXT NOT NULL, pw_salt TEXT NOT NULL, display_name TEXT, "
                "points INTEGER DEFAULT 0, role TEXT DEFAULT 'member', "
                "must_change INTEGER DEFAULT 1, created_at TEXT DEFAULT (datetime('now')))"
            )
            c.execute(
                "CREATE TABLE points_audit("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, who_admin TEXT NOT NULL, "
                "username TEXT NOT NULL, delta INTEGER NOT NULL, "
                "before_points INTEGER NOT NULL, after_points INTEGER NOT NULL, "
                "reason TEXT, created_at INTEGER NOT NULL)"
            )
            c.execute(
                "INSERT INTO points_audit("
                "who_admin,username,delta,before_points,after_points,reason,created_at) "
                "VALUES('system','fang',-1,10,9,'legacy',1)"
            )
            c.commit()
        finally:
            c.close()

        original_db = self.auth.DB
        self.auth.DB = legacy_db
        try:
            self.auth.init_db()
            self.auth.init_db()
            c = sqlite3.connect(legacy_db)
            try:
                columns = {
                    row[1] for row in c.execute("PRAGMA table_info(points_audit)")
                }
                indexes = {
                    row[1] for row in c.execute("PRAGMA index_list(points_audit)")
                }
                legacy = c.execute(
                    "SELECT delta,reason,transaction_key FROM points_audit WHERE id=1"
                ).fetchone()
            finally:
                c.close()
        finally:
            self.auth.DB = original_db

        self.assertIn("transaction_key", columns)
        self.assertIn("idx_points_audit_transaction_key", indexes)
        self.assertEqual(legacy, (-1, "legacy", None))


if __name__ == "__main__":
    unittest.main()
