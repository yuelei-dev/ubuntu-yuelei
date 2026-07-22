# Breakdown Zhipu Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route breakdown multimodal analysis to Zhipu `glm-4v-plus`, with OpenAI `gpt-4o` fallback only for provable pre-delivery failures.

**Architecture:** `breakdown.py` builds provider-neutral multimodal messages, posts first to Zhipu's OpenAI-compatible endpoint, and reuses the existing egress safety classifier before considering OpenAI fallback. Existing job settlement remains the single authority for refunding failed links.

**Tech Stack:** Python 3.10 standard library (`urllib`), `unittest`, existing `content_domains.egress` helpers, systemd, GitHub PR workflow.

## Global Constraints

- Keep `glm-4v-plus` as the primary model and `gpt-4o` as the fallback model.
- Fall back only for DNS, connection refused, host/network unreachable, or TLS handshake failures classified by `egress._pre_delivery_failure`.
- Never fall back after HTTP errors, timeouts, connection resets, or invalid provider responses.
- Read credentials only from environment variables; never log or commit secrets.
- Preserve 20 points per link and all existing refund behavior.
- Modify backend code and related tests only; do not touch the frontend or production server.

---

### Task 1: Add failing provider-routing tests

**Files:**
- Modify: `tests/test_breakdown.py`
- Test: `tests/test_breakdown.py`

**Interfaces:**
- Consumes: existing `content_domains.breakdown._chat_multimodal(sysmsg, usermsg, image_paths, temp=0.7)`.
- Produces: executable requirements for Zhipu primary routing and safe OpenAI fallback.

- [ ] **Step 1: Extend test setup to restore environment and network seams**

Add imports and snapshot the relevant environment keys in `setUp`; restore them in `tearDown`:

```python
import json
import os
import socket
import urllib.error

_ROUTING_ENV = (
    "BREAKDOWN_MODEL", "BREAKDOWN_FALLBACK_MODEL",
    "REVERSE_ZHIPU_BASE", "REVERSE_ZHIPU_KEY",
)

self.orig_routing_env = {key: os.environ.get(key) for key in _ROUTING_ENV}
self.orig_zhipu_post = getattr(self.breakdown, "_post_zhipu", None)
self.orig_openai_post = getattr(self.breakdown, "_post_openai_fallback", None)
```

Restore each key by setting its old value or deleting it, and restore both helper attributes when present.

- [ ] **Step 2: Write the failing Zhipu-success test**

Create a temporary JPEG and replace `_post_zhipu` / `_post_openai_fallback` with capture functions. Assert that `_chat_multimodal`:

```python
os.environ["BREAKDOWN_MODEL"] = "glm-4v-plus"
os.environ["REVERSE_ZHIPU_KEY"] = "zhipu-test-key"
result = self.breakdown._chat_multimodal("system", "user", [frame_path])
self.assertEqual(result, "智谱结果")
self.assertEqual(captured["zhipu"]["model"], "glm-4v-plus")
self.assertEqual(captured["zhipu_key"], "zhipu-test-key")
self.assertFalse(captured.get("openai_called", False))
```

The fake Zhipu helper returns `{"choices": [{"message": {"content": "智谱结果"}}]}`.

- [ ] **Step 3: Write the failing safe-fallback test**

Make `_post_zhipu` raise `urllib.error.URLError(socket.gaierror(-2, "name resolution failed"))`; make `_post_openai_fallback` capture the body and return an OpenAI-compatible response. Assert the result is the GPT text and the fallback body uses `BREAKDOWN_FALLBACK_MODEL=gpt-4o` rather than `glm-4v-plus`.

- [ ] **Step 4: Write failing no-fallback tests for ambiguous/delivered failures**

Use `subTest` for these Zhipu exceptions:

```python
urllib.error.HTTPError("https://open.bigmodel.cn", 404, "Not Found", {}, None)
urllib.error.HTTPError("https://open.bigmodel.cn", 500, "Server Error", {}, None)
TimeoutError("timed out")
urllib.error.URLError(ConnectionResetError("reset"))
```

For every exception, assert `_chat_multimodal` raises the same exception and `_post_openai_fallback` is never called.

- [ ] **Step 5: Write the failing double-failure propagation test**

Make Zhipu raise a DNS `URLError`, make the fallback helper raise `RuntimeError("openai failed")`, and assert that exact fallback error propagates to the caller.

- [ ] **Step 6: Run tests and verify RED**

Run:

```powershell
& 'C:\Users\23329\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest tests.test_breakdown -v
```

Expected: new routing tests fail because `_post_zhipu` and `_post_openai_fallback` do not exist and `_chat_multimodal` still always calls OpenAI.

- [ ] **Step 7: Commit the failing tests**

```bash
git add tests/test_breakdown.py
git commit -m "test: cover zhipu breakdown routing"
```

### Task 2: Implement Zhipu primary and safe GPT fallback

**Files:**
- Modify: `server/content_domains/breakdown.py`
- Test: `tests/test_breakdown.py`

**Interfaces:**
- Consumes: `egress._DIRECT`, `egress._pre_delivery_failure`, `egress.post_json`, `OPENAI_BASE`, and `OPENAI_KEY`.
- Produces: `_post_zhipu(body: dict, api_key: str) -> dict`, `_post_openai_fallback(body: dict) -> dict`, and unchanged `_chat_multimodal(...) -> str`.

- [ ] **Step 1: Add the standard-library HTTP import**

Import `urllib.request`. Provider settings are read inside the request helper so tests and service restarts always observe the current environment.

- [ ] **Step 2: Implement the minimal Zhipu request helper**

```python
def _post_zhipu(body, api_key):
    base = os.environ.get(
        "REVERSE_ZHIPU_BASE", "https://open.bigmodel.cn/api/paas/v4"
    ).rstrip("/")
    timeout = int(os.environ.get("BREAKDOWN_ZHIPU_TIMEOUT", "210") or 210)
    req = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode(),
        headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
        method="POST",
    )
    with egress._DIRECT.open(req, timeout=timeout) as response:
        return json.loads(response.read())
```

- [ ] **Step 3: Implement the existing OpenAI path as a focused helper**

```python
def _post_openai_fallback(body):
    from .image import OPENAI_OFFICIAL_BASE

    return egress.post_json(
        OPENAI_OFFICIAL_BASE, OPENAI_BASE,
        "/v1/chat/completions", json.dumps(body, ensure_ascii=False).encode(),
        {"Authorization": "Bearer " + OPENAI_KEY, "Content-Type": "application/json"},
        log=lambda message: print("[breakdown] %s" % message, flush=True),
    )
```

Remove the old `OPENAI_OFFICIAL_BASE` import from `_chat_multimodal`; the helper-local import above avoids a module import cycle.

- [ ] **Step 4: Route `_chat_multimodal` through Zhipu first**

Build the shared `messages` once. For the primary request, use `BREAKDOWN_MODEL` defaulting to `glm-4v-plus` and require a non-empty `REVERSE_ZHIPU_KEY`. On success, return the content.

Catch only the Zhipu request exception. If `egress._pre_delivery_failure(exc)` is false, log the error category and re-raise. If true, log the safe fallback, clone the body, replace its model with `BREAKDOWN_FALLBACK_MODEL` defaulting to `gpt-4o`, and call `_post_openai_fallback`.

Treat a missing Zhipu key as a configuration error, not as a network failure, so it fails and refunds rather than silently spending on GPT.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run:

```powershell
& 'C:\Users\23329\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest tests.test_breakdown tests.test_egress -v
```

Expected: all routing and existing breakdown/egress tests pass.

- [ ] **Step 6: Run the backend regression set**

Run:

```powershell
& 'C:\Users\23329\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest tests.test_breakdown tests.test_egress tests.test_cost_of tests.test_content_domains -v
```

Expected: all tests pass with zero failures and zero errors.

- [ ] **Step 7: Commit the implementation**

```bash
git add server/content_domains/breakdown.py
git commit -m "fix: route breakdown through zhipu"
```

### Task 3: Verify, publish, review, merge, and deploy to clone server

**Files:**
- Verify: `server/content_domains/breakdown.py`
- Verify: `tests/test_breakdown.py`
- Deploy: `/home/ubuntu/content-api/content_domains/breakdown.py` on `8.148.158.106`

**Interfaces:**
- Consumes: the two reviewed commits from Tasks 1 and 2.
- Produces: merged PR and verified clone-server deployment.

- [ ] **Step 1: Perform pre-PR verification**

Run `git diff origin/main...HEAD --check`, inspect the complete diff, confirm only the design document, `breakdown.py`, and `test_breakdown.py` changed, then rerun the regression command from Task 2.

- [ ] **Step 2: Push and open a PR**

```bash
git push -u origin fix/breakdown-zhipu-route
gh pr create --repo yuelei-dev/ubuntu-yuelei --base main --head fix/breakdown-zhipu-route --title "fix: route breakdown through zhipu" --body "根因：glm-4v-plus 被误发到 OpenAI，导致 HTTP 404。修复：智谱作为主通道，仅在可证明未送达时安全回退 gpt-4o。验证：拆解、egress、计费和模块门禁测试全部通过。成本：正常请求使用智谱，GPT 只作安全回退。部署范围：合并后仅部署克隆服务器，不触碰生产。"
```

The PR body must include root cause, safe fallback rules, tests run, cost implications, and the restriction that production was not touched.

- [ ] **Step 3: Review the PR and CI before merge**

Inspect the PR diff independently, check all GitHub Actions results, and address any actionable feedback. Merge only when required checks pass and review finds no blocking issue.

- [ ] **Step 4: Merge and verify GitHub state**

Merge through the GitHub PR workflow, then query the PR again and require `state=MERGED` with a non-empty merge commit SHA.

- [ ] **Step 5: Deploy only the reviewed backend file to the clone server**

Back up the current clone-server file with a timestamp, upload the merged `server/content_domains/breakdown.py` to `/home/ubuntu/content-api/content_domains/breakdown.py`, run `python3 -m py_compile`, then restart `huangque-content.service`. Do not connect to or mutate the production server.

- [ ] **Step 6: Verify service and functional behavior**

Require `systemctl is-active huangque-content.service` to return `active`. Run one reverse-prompt breakdown and one scene breakdown against the clone server, confirm both finish successfully, confirm logs identify the Zhipu primary path, and confirm no GPT fallback log appears.

- [ ] **Step 7: Record deployment evidence**

Report the PR number and merge SHA, deployed file SHA-256, service status, test counts, both functional test durations, success/failure, actual provider route, and confirmation that the production server was untouched.
