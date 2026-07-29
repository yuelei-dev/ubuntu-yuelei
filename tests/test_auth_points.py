import os
import json
import sqlite3
import sys
import tempfile
import threading
import urllib.error
import urllib.request
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock


class AuthPointsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = os.environ.get("HQ_TEST_AUTH_DB")
        os.environ["HQ_TEST_AUTH_DB"] = os.path.join(self.tmp.name, "users.db")

        import importlib
        import server.auth_server as auth_server

        self.auth = importlib.reload(auth_server)
        self.auth.DB = os.environ["HQ_TEST_AUTH_DB"]
        self.auth.INTERNAL_TOKEN = "test-internal-token"
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

    def tearDown(self):
        if self.old_db is None:
            os.environ.pop("HQ_TEST_AUTH_DB", None)
        else:
            os.environ["HQ_TEST_AUTH_DB"] = self.old_db
        self.tmp.cleanup()

    def test_deduct_is_atomic_and_rejects_insufficient_points(self):
        points, err = self.auth.deduct_points("fang", 7)
        self.assertIsNone(err)
        self.assertEqual(points["points"], 3)

        points, err = self.auth.deduct_points("fang", 4)
        self.assertIsNone(points)
        self.assertEqual(err, "insufficient")
        self.assertEqual(self.auth.get_points_row("fang")["points"], 3)

    def test_refund_adds_points(self):
        points, err = self.auth.refund_points("fang", 5)
        self.assertIsNone(err)
        self.assertEqual(points["points"], 15)

    def test_concurrent_deduct_never_overdraws(self):
        results = []
        lock = threading.Lock()

        def worker():
            points, err = self.auth.deduct_points("fang", 1)
            with lock:
                results.append((points, err))

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        ok = [r for r in results if r[1] is None]
        insufficient = [r for r in results if r[1] == "insufficient"]
        self.assertEqual(len(ok), 10)
        self.assertEqual(len(insufficient), 10)
        self.assertEqual(self.auth.get_points_row("fang")["points"], 0)

    def test_transaction_key_survives_reload_and_deducts_once(self):
        key = "breakdown-local-charge-101"
        points, err, replayed = self.auth.apply_points_transaction(
            key, "fang", "deduct", 4, "job:breakdown",
        )
        self.assertIsNone(err)
        self.assertFalse(replayed)
        self.assertEqual(points["points"], 6)

        import importlib
        auth = importlib.reload(self.auth)
        auth.DB = os.environ["HQ_TEST_AUTH_DB"]
        auth.init_db()
        points, err, replayed = auth.apply_points_transaction(
            key, "fang", "deduct", 4, "job:breakdown",
        )
        self.assertIsNone(err)
        self.assertTrue(replayed)
        self.assertEqual(points["points"], 6)
        self.assertEqual(auth.get_points_row("fang")["points"], 6)
        connection = auth.db()
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM points_audit"
                ).fetchone()[0],
                1,
            )
        finally:
            connection.close()

    def test_concurrent_same_transaction_key_only_applies_once(self):
        key = "breakdown-local-charge-102"
        results = []
        lock = threading.Lock()

        def worker():
            result = self.auth.apply_points_transaction(
                key, "fang", "deduct", 3, "job:breakdown",
            )
            with lock:
                results.append(result)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(self.auth.get_points_row("fang")["points"], 7)
        self.assertEqual(sum(1 for _, err, _ in results if err is None), 20)
        self.assertEqual(sum(1 for _, _, replayed in results if not replayed), 1)
        connection = self.auth.db()
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM points_transactions"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM points_audit"
                ).fetchone()[0],
                1,
            )
        finally:
            connection.close()

    def test_transaction_key_conflict_and_rejected_result_are_stable(self):
        key = "breakdown-local-charge-103"
        points, err, replayed = self.auth.apply_points_transaction(
            key, "fang", "deduct", 99, "job:breakdown",
        )
        self.assertIsNone(points)
        self.assertEqual(err, "insufficient")
        self.assertFalse(replayed)
        self.auth.refund_points("fang", 100, "top up")
        points, err, replayed = self.auth.apply_points_transaction(
            key, "fang", "deduct", 99, "job:breakdown",
        )
        self.assertIsNone(points)
        self.assertEqual(err, "insufficient")
        self.assertTrue(replayed)
        self.assertEqual(self.auth.get_points_row("fang")["points"], 110)
        _, err, replayed = self.auth.apply_points_transaction(
            key, "fang", "deduct", 1, "different amount",
        )
        self.assertEqual(err, "conflict")
        self.assertTrue(replayed)
        self.assertEqual(self.auth.get_points_row("fang")["points"], 110)

    def test_http_points_endpoints_require_internal_token(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), self.auth.H)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:%d" % server.server_address[1]
        try:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(base + "/api/auth/points?username=fang", timeout=3)
            self.assertEqual(ctx.exception.code, 403)

            req = urllib.request.Request(
                base + "/api/auth/points/deduct",
                data=json.dumps({"username": "fang", "amount": 4}).encode(),
                headers={
                    "Content-Type": "application/json",
                    "X-HQ-Internal-Token": "test-internal-token",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=3) as r:
                data = json.loads(r.read())
            self.assertEqual(data["points"], 6)

            transaction_key = "breakdown-local-refund-104"
            transaction_body = {
                "username": "fang", "amount": 4,
                "reason": "job#104", "transaction_key": transaction_key,
            }
            request = urllib.request.Request(
                base + "/api/auth/points/refund",
                data=json.dumps(transaction_body).encode(),
                headers={
                    "Content-Type": "application/json",
                    "X-HQ-Internal-Token": "test-internal-token",
                },
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=3) as response:
                first = json.loads(response.read())
            request = urllib.request.Request(
                base + "/api/auth/points/refund",
                data=json.dumps(transaction_body).encode(),
                headers={
                    "Content-Type": "application/json",
                    "X-HQ-Internal-Token": "test-internal-token",
                },
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=3) as response:
                replay = json.loads(response.read())
            self.assertFalse(first["replayed"])
            self.assertTrue(replay["replayed"])
            self.assertEqual(first["points"], 10)
            self.assertEqual(replay["points"], 10)

            conflict = urllib.request.Request(
                base + "/api/auth/points/refund",
                data=json.dumps({
                    **transaction_body, "amount": 3,
                }).encode(),
                headers={
                    "Content-Type": "application/json",
                    "X-HQ-Internal-Token": "test-internal-token",
                },
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(conflict, timeout=3)
            self.assertEqual(ctx.exception.code, 409)
            conflict_body = json.loads(ctx.exception.read())
            self.assertEqual(conflict_body["detail"], "transaction key conflict")
            self.assertNotIn("username", conflict_body)

            invalid = urllib.request.Request(
                base + "/api/auth/points/deduct",
                data=json.dumps({
                    "username": "fang", "amount": 1,
                    "transaction_key": "short",
                }).encode(),
                headers={
                    "Content-Type": "application/json",
                    "X-HQ-Internal-Token": "test-internal-token",
                },
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(invalid, timeout=3)
            self.assertEqual(ctx.exception.code, 400)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_points_client_only_sends_transaction_key_when_requested(self):
        server = str(Path(__file__).resolve().parents[1] / "server")
        if server not in sys.path:
            sys.path.insert(0, server)
        from content_domains import points as points_domain

        calls = []
        with mock.patch.object(
            points_domain, "_auth_points_request",
            side_effect=lambda path, payload=None, method="POST": (
                calls.append((path, payload, method)) or {"points": 8}
            ),
        ):
            self.assertEqual(
                points_domain.deduct_points("fang", 2, "legacy"),
                8,
            )
            self.assertEqual(
                points_domain.refund_points(
                    "fang", 2, "job#1",
                    transaction_key="breakdown-local-refund-1",
                ),
                8,
            )
        self.assertNotIn("transaction_key", calls[0][1])
        self.assertEqual(
            calls[1][1]["transaction_key"],
            "breakdown-local-refund-1",
        )

    def test_login_sets_http_only_cookie_without_plaintext_token_body(self):
        self.auth.create_user("cookie_user", "secret123", 5)
        server = ThreadingHTTPServer(("127.0.0.1", 0), self.auth.H)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:%d" % server.server_address[1]
        try:
            req = urllib.request.Request(
                base + "/api/auth/login",
                data=json.dumps({"username": "cookie_user", "password": "secret123"}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=3) as r:
                data = json.loads(r.read())
                cookie = r.headers.get("Set-Cookie") or ""

            self.assertNotIn("token", data)
            self.assertEqual(data["user"]["username"], "cookie_user")
            self.assertIn("HttpOnly", cookie)
            self.assertIn(self.auth.AUTH_COOKIE_NAME + "=", cookie)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_miniprogram_login_returns_token_usable_as_bearer(self):
        self.auth.create_user("mp_user", "secret123", 5)
        server = ThreadingHTTPServer(("127.0.0.1", 0), self.auth.H)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:%d" % server.server_address[1]
        try:
            req = urllib.request.Request(
                base + "/api/auth/miniprogram-login",
                data=json.dumps({"username": "mp_user", "password": "secret123"}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=3) as r:
                data = json.loads(r.read())

            self.assertIn("token", data)
            self.assertEqual(data["user"]["username"], "mp_user")

            req2 = urllib.request.Request(
                base + "/api/auth/me",
                headers={"Authorization": "Bearer " + data["token"]},
            )
            with urllib.request.urlopen(req2, timeout=3) as r:
                me_data = json.loads(r.read())
            self.assertEqual(me_data["user"]["username"], "mp_user")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_miniprogram_register_returns_token_and_creates_user(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), self.auth.H)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:%d" % server.server_address[1]
        try:
            req = urllib.request.Request(
                base + "/api/auth/miniprogram-register",
                data=json.dumps({"username": "mp_new", "password": "secret123"}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=3) as r:
                data = json.loads(r.read())

            self.assertIn("token", data)
            self.assertEqual(data["user"]["username"], "mp_new")
            self.assertEqual(data["user"]["points"], self.auth.NEW_USER_TRIAL_POINTS)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
