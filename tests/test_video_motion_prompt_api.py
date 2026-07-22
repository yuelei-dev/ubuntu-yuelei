import json
import pathlib
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import closing
from http.server import ThreadingHTTPServer


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from content_domains import core, text


class FakePoints:
    def __init__(self):
        self.deductions = []

    def deduct_points(self, *args):
        self.deductions.append(args)
        raise AssertionError("motion prompt optimization must not deduct points")


class MotionPromptOptimizeApiTests(unittest.TestCase):
    endpoint = "/api/gen/video/motion-prompt-optimize"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.points = FakePoints()
        self.originals = {
            "JOB_DB": core.JOB_DB,
            "verify": core.verify,
            "domains": core._domains,
            "chat": text._chat,
        }
        core.JOB_DB = str(pathlib.Path(self.tmp.name) / "jobs.db")
        with closing(sqlite3.connect(core.JOB_DB)) as db:
            db.execute("CREATE TABLE jobs(id INTEGER PRIMARY KEY, kind TEXT)")
            db.commit()
        core.verify = lambda token: {"username": "alice", "must_change": False} if token == "test" else None
        core._domains = lambda: (None, self.points, None)
        core._motion_prompt_inflight.clear()
        text._chat = lambda system, prompt, temperature: "A woman smiles, nods gently, and gestures with one hand."
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), core.H)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        core.JOB_DB = self.originals["JOB_DB"]
        core.verify = self.originals["verify"]
        core._domains = self.originals["domains"]
        text._chat = self.originals["chat"]
        core._motion_prompt_inflight.clear()
        self.tmp.cleanup()

    def post(self, body, token="test"):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        request = urllib.request.Request(
            self.base + self.endpoint,
            data=json.dumps(body).encode("utf-8"), method="POST", headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def test_requires_login(self):
        status, _ = self.post({"prompt": "Smile"}, token=None)
        self.assertEqual(401, status)

    def test_rejects_forced_password_change(self):
        core.verify = lambda token: {"username": "alice", "must_change": True}
        status, _ = self.post({"prompt": "Smile"})
        self.assertEqual(403, status)

    def test_rejects_empty_whitespace_non_string_and_overlong_prompts(self):
        for prompt in ("", " \t\n ", ["Smile"], {"prompt": "Smile"}, 42, 0.3, "x" * 501):
            with self.subTest(prompt=repr(prompt)[:20]):
                status, _ = self.post({"prompt": prompt})
                self.assertEqual(400, status)

    def test_accepts_a_prompt_of_exactly_500_characters(self):
        status, payload = self.post({"prompt": "x" * 500})
        self.assertEqual(200, status)
        self.assertEqual("A woman smiles, nods gently, and gestures with one hand.", payload["motion_prompt"])

    def test_trims_and_delegates_to_zhipu_chat_at_point_three(self):
        captured = {}

        def fake_chat(system, prompt, temperature):
            captured.update(system=system, prompt=prompt, temperature=temperature)
            return "The avatar gives a warm smile, nods once, and makes a small welcoming hand gesture."

        text._chat = fake_chat
        status, payload = self.post({"prompt": "  warmly greet viewers with a small wave  "})

        self.assertEqual(200, status)
        self.assertEqual(
            "The avatar gives a warm smile, nods once, and makes a small welcoming hand gesture.",
            payload["motion_prompt"],
        )
        self.assertEqual("warmly greet viewers with a small wave", captured["prompt"])
        self.assertEqual(0.3, captured["temperature"])
        requirements = (
            "concise English", "Photo Avatar IV", "preserve", "facial", "head", "upper-body",
            "hand", "pacing", "camera", "scene", "multiple-person", "unsafe",
            "no explanation, title", "Markdown",
        )
        for requirement in requirements:
            self.assertIn(requirement, captured["system"])

    def test_returns_controlled_400_for_empty_model_output(self):
        text._chat = lambda *args: "  \n"
        status, _ = self.post({"prompt": "Smile"})
        self.assertEqual(400, status)

    def test_sanitizes_provider_errors_as_502(self):
        secret_prompt = "private input that must never be echoed"
        raw_provider_response = "raw upstream response: zhipu-key=super-secret"

        def failing_chat(*args):
            raise TimeoutError(raw_provider_response)

        text._chat = failing_chat
        status, payload = self.post({"prompt": secret_prompt})
        rendered = json.dumps(payload)
        self.assertEqual(502, status)
        self.assertNotIn(secret_prompt, rendered)
        self.assertNotIn("super-secret", rendered)
        self.assertNotIn(raw_provider_response, rendered)

    def test_success_creates_no_job_and_deducts_no_points(self):
        status, _ = self.post({"prompt": "Smile and nod"})
        self.assertEqual(200, status)
        with closing(sqlite3.connect(core.JOB_DB)) as db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
        self.assertEqual([], self.points.deductions)

    def test_same_user_second_request_is_rejected_without_second_chat_call(self):
        entered, release, calls, first = threading.Event(), threading.Event(), [], {}

        def blocking_chat(system, prompt, temperature):
            calls.append(prompt)
            entered.set()
            release.wait(5)
            return "The avatar smiles."

        text._chat = blocking_chat
        worker = threading.Thread(target=lambda: first.setdefault("response", self.post({"prompt": "first"})))
        worker.start()
        self.assertTrue(entered.wait(2))
        try:
            status, payload = self.post({"prompt": "second"})
            self.assertEqual(429, status)
            self.assertEqual("motion_prompt_optimize_busy", payload["code"])
            self.assertEqual(1000, payload["retry_after_ms"])
            self.assertIn("动作提示", payload["detail"])
            self.assertEqual(["first"], calls)
        finally:
            release.set()
            worker.join(5)
        self.assertEqual(200, first["response"][0])

    def test_provider_error_releases_user_slot(self):
        text._chat = lambda *args: (_ for _ in ()).throw(TimeoutError("upstream timeout"))
        self.assertEqual(502, self.post({"prompt": "first"})[0])
        text._chat = lambda *args: "The avatar nods."
        self.assertEqual(200, self.post({"prompt": "retry"})[0])

    def test_different_users_are_not_blocked_by_each_other(self):
        entered, release = threading.Event(), threading.Event()
        core.verify = lambda token: ({"username": token, "must_change": False} if token in {"alice", "bob"} else None)

        def selectively_blocking_chat(system, prompt, temperature):
            if prompt == "hold":
                entered.set()
                release.wait(5)
            return "The avatar smiles."

        text._chat = selectively_blocking_chat
        worker = threading.Thread(target=lambda: self.post({"prompt": "hold"}, token="alice"))
        worker.start()
        self.assertTrue(entered.wait(2))
        try:
            self.assertEqual(200, self.post({"prompt": "continue"}, token="bob")[0])
        finally:
            release.set()
            worker.join(5)


if __name__ == "__main__":
    unittest.main()
