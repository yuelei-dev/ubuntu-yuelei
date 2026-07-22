# OpenRouter Grok Fallback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep xAI as the primary Grok video provider and safely fall back to OpenRouter when xAI definitively rejects creation for authentication or billing reasons.

**Architecture:** Add a self-contained OpenRouter asynchronous video adapter and connect it only to the existing Grok generate path. xAI exposes a dedicated pre-creation-unavailable exception; `video.py` catches only that exception, while ambiguous create outcomes, edit requests, polling, and downloads remain single-provider operations.

**Tech Stack:** Python 3.10 standard library (`urllib`, `json`), `unittest`, existing `content_domains.egress`, systemd, GitHub Actions.

## Global Constraints

- Keep `GROK_VIDEO_PROVIDER=xai`; OpenRouter is a fallback, not the default.
- Fall back only after definite xAI pre-creation 401, 402, 403, or missing-key failures.
- Never fall back after ambiguous xAI network/timeout outcomes or after a provider task ID exists.
- Grok video edit remains xAI-only.
- Do not modify startup recovery, `core.py`, frontend, pricing, points settlement, or database schema.
- Never commit or log API keys; copy `OPENROUTER_API_KEY` directly between in-scope server environment files.
- Modify and deploy only the clone repository/server; production remains read-only.
- Use main-site PR #721 commit `34216287` and #722 commit `4dcf79a` as behavioral references, adapting only for clone-repository differences.

---

### Task 1: Add failing OpenRouter adapter and xAI classification tests

**Files:**
- Create: `tests/test_video_openrouter.py`
- Modify: `tests/test_video_xai.py`

**Interfaces:**
- Consumes: existing `video_xai.generate(...)` and `_request_json(...)` behavior.
- Produces: required `video_openrouter.generate`, `resume`, `available`, `download_headers`, and `video_xai.XaiCreateUnavailableError` behavior.

- [ ] **Step 1: Add OpenRouter request-shape tests**

Create `tests/test_video_openrouter.py` with fake opener/response helpers. Cover:

```python
with patch.object(video_openrouter, "OPENROUTER_API_KEY", "or-test"), \
     patch.object(video_openrouter, "_opener", return_value=opener), \
     patch.object(video_openrouter, "OPENROUTER_VIDEO_POLL_INTERVAL", 0):
    result = video_openrouter.generate(
        "grok-imagine-video", "demo", 10, "16:9", "720p",
        image_urls=["https://img.example/a.jpg"],
    )

self.assertEqual(create_request.full_url, "https://openrouter.ai/api/v1/videos")
self.assertEqual(create_request.get_header("Authorization"), "Bearer or-test")
self.assertEqual(create_body["model"], "x-ai/grok-imagine-video")
self.assertEqual(create_body["aspect_ratio"], "16:9")
self.assertEqual(create_body["input_references"][0]["image_url"]["url"], "https://img.example/a.jpg")
self.assertEqual(result["provider"], "openrouter")
```

Also assert `grok-imagine-video-1.5` uses one `frame_images` first-frame entry and does not send `aspect_ratio`.

- [ ] **Step 2: Add OpenRouter safety tests**

Test that `_safe_url` rejects a different host or non-HTTPS URL; create network failure performs one POST; polling 429/5xx/network failures retry GET only; completed responses require `unsigned_urls`; and `download_headers()` returns only `{"Authorization": "Bearer ..."}`.

- [ ] **Step 3: Add xAI pre-creation classification tests**

Extend `tests/test_video_xai.py` so generate wraps missing key and HTTP 401/402/403 in `XaiCreateUnavailableError`, while a POST `URLError` remains a non-fallback network error. Assert `edit(...)` does not wrap credential errors as fallback-eligible.

- [ ] **Step 4: Run RED tests**

Run:

```powershell
& 'C:\Users\23329\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest tests.test_video_openrouter tests.test_video_xai -v
```

Expected: import failure for `content_domains.video_openrouter` and missing `XaiCreateUnavailableError` behavior.

- [ ] **Step 5: Commit failing tests**

```bash
git add tests/test_video_openrouter.py tests/test_video_xai.py
git commit -m "test: cover OpenRouter Grok fallback"
```

### Task 2: Implement the standalone OpenRouter adapter

**Files:**
- Create: `server/content_domains/video_openrouter.py`
- Test: `tests/test_video_openrouter.py`

**Interfaces:**
- Produces: `available() -> bool`, `download_headers() -> dict`, `generate(model, prompt, duration, aspect_ratio, resolution, image_urls=None, job_id=None, heartbeat=None, now=None, sleep=None) -> dict`, and `resume(...) -> dict` for internal consistency even though startup recovery is out of scope.

- [ ] **Step 1: Add configuration, model mapping, and trusted URL validation**

Implement environment-backed constants and exact model mapping:

```python
OPENROUTER_API_BASE = os.environ.get("OPENROUTER_API_BASE", "https://openrouter.ai/api/v1").rstrip("/")
OPENROUTER_MODEL_MAP = {
    "grok-imagine-video": "x-ai/grok-imagine-video",
    "grok-imagine-video-1.5": "x-ai/grok-imagine-video-1.5",
}
```

`_safe_url(path)` must use `urllib.parse.urljoin`, require HTTPS, and require the same host as `OPENROUTER_API_BASE`.

- [ ] **Step 2: Implement authenticated JSON requests**

Send Bearer auth, `HTTP-Referer: https://huangquechuanmei.com`, `X-Title: Huangque Content`, and JSON content type. Classify 408/429/500/502/503/504 and network errors as `TransientOpenRouterError`; classify 401/403 and 402 with actionable errors. Do not retry create POST inside `_request_json`.

- [ ] **Step 3: Implement create-once and GET-only polling**

`generate` sends one `POST /videos`, requires response `id`, records `openrouter_pending`, then polls `GET /videos/{id}`. Poll transient failures with delays `(5, 10, 20, 30)` until timeout. On `completed`, require the first `unsigned_urls` entry and return provider `openrouter`; on failed/cancelled/expired, raise without another POST.

- [ ] **Step 4: Run adapter tests and verify GREEN**

Run the Task 1 command. Expected: OpenRouter tests pass; xAI classification tests still fail until Task 3.

- [ ] **Step 5: Commit the adapter**

```bash
git add server/content_domains/video_openrouter.py
git commit -m "feat(video): add OpenRouter Grok adapter"
```

### Task 3: Wire safe fallback and authenticated result downloads

**Files:**
- Modify: `server/content_domains/video_xai.py`
- Modify: `server/content_domains/video.py`
- Modify: `tests/test_xiaole_video.py`
- Test: `tests/test_video_xai.py`
- Test: `tests/test_xiaole_video.py`

**Interfaces:**
- Consumes: `video_openrouter.available`, `generate`, and `download_headers`; `video_xai.XaiCreateUnavailableError`.
- Produces: xAI-first Grok generate behavior with narrowly scoped OpenRouter fallback.

- [ ] **Step 1: Complete xAI classification**

Add `XaiCredentialError` and `XaiCreateUnavailableError`. `_request_json` raises `XaiCredentialError` for 401/402/403. In `generate`, wrap only `XaiCredentialError` and missing-key `ValueError` from create as `XaiCreateUnavailableError`. Leave edit, network errors, task-ID validation, polling, and downloads outside that wrapper.

- [ ] **Step 2: Add failing integration tests in `test_xiaole_video.py`**

Assert:

```python
with patch("content_domains.video_xai.generate", side_effect=video_xai.XaiCreateUnavailableError("credits")), \
     patch("content_domains.video_openrouter.available", return_value=True), \
     patch("content_domains.video_openrouter.generate", return_value=openrouter_result) as fallback:
    result = self.video.gen_xiaole_video(payload)
self.assertEqual(result["provider_video_id"], "or-1")
fallback.assert_called_once()
```

Add tests that a generic xAI `RuntimeError` does not call OpenRouter, missing OpenRouter configuration re-raises the xAI error, and edit never calls OpenRouter.

- [ ] **Step 3: Wire the generate-only fallback**

In the existing xAI generate branch, import both adapters and catch only `video_xai.XaiCreateUnavailableError`. Require `video_openrouter.available()`, then call OpenRouter with the same model, prompt, reference image URLs, duration, ratio, resolution, job ID, and heartbeat.

- [ ] **Step 4: Add provider-aware download authentication**

Set `provider = xres.get("provider") or "xai"`. For OpenRouter only, pass `video_openrouter.download_headers()` into `_download_xiaole_video`; modify its candidate builder so authorization is attached only to the original URL request and never copied to relay/fallback hosts.

- [ ] **Step 5: Run focused and regression tests**

Run:

```powershell
& 'C:\Users\23329\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest tests.test_video_openrouter tests.test_video_xai tests.test_xiaole_video tests.test_grok_official_points tests.test_cost_of tests.test_content_domains -v
```

Expected: all tests pass with zero failures and zero errors.

- [ ] **Step 6: Commit integration**

```bash
git add server/content_domains/video_xai.py server/content_domains/video.py tests/test_xiaole_video.py tests/test_video_xai.py tests/test_video_openrouter.py
git commit -m "feat(video): add safe OpenRouter fallback"
```

### Task 4: Review, PR, merge, secret sync, and clone deployment

**Files:**
- Review/deploy: `server/content_domains/video_openrouter.py`
- Review/deploy: `server/content_domains/video_xai.py`
- Review/deploy: `server/content_domains/video.py`
- Configure: `/home/ubuntu/content-api/content.env` on clone server only

**Interfaces:**
- Consumes: merged implementation from Tasks 1–3 and production `OPENROUTER_API_KEY` as a secret value.
- Produces: reviewed clone deployment and real xAI-403-to-OpenRouter evidence.

- [ ] **Step 1: Verify scope and behavior**

Run the focused regression command, `git diff origin/main...HEAD --check`, and inspect all changed files. Require no `core.py`, frontend, pricing, settlement, production configuration, or real secret changes.

- [ ] **Step 2: Independent code review**

Review complete diff against the design, emphasizing non-idempotent create safety, host-bound authorization, edit isolation, and secret handling. Resolve all Critical and Important findings before publishing.

- [ ] **Step 3: Push, create PR, and wait for CI**

Push `fix/openrouter-grok-fallback`, create a ready-for-review PR targeting `yuelei-dev/ubuntu-yuelei:main`, include main-site #721/#722 provenance and test evidence, and merge only after CI passes and GitHub reports the PR mergeable.

- [ ] **Step 4: Copy the secret without displaying it**

Over authenticated SSH, read the `OPENROUTER_API_KEY` value from the production service environment or its environment file directly into a local process variable, immediately write it into the clone server's `/home/ubuntu/content-api/content.env`, and clear the local variable. Commands and logs must print only `SET`/`MISSING`, never the value.

- [ ] **Step 5: Deploy merged files to clone server**

Back up the three target files plus `content.env`, upload files from the verified merged commit, run `python3 -m py_compile`, restart `huangque-content.service`, require `active`, verify deployed hashes, and confirm `OPENROUTER_API_KEY=SET` in the running process.

- [ ] **Step 6: Run real fallback smoke test**

Submit one minimal-duration Grok generation on the clone server while the current xAI account still returns the known pre-creation credit/spending-limit error. Require exactly one OpenRouter create ID, successful polling/download, terminal job success, correct point settlement, and logs showing xAI pre-creation failure followed by OpenRouter—not repeated xAI/OpenRouter creates.

- [ ] **Step 7: Report evidence and preserve rollback**

Report PR/merge SHA, test count, deployed hashes, service status, provider route, request ID redacted to a short prefix, duration, point result, and confirmation production was read-only. Keep timestamped backups on the clone server for rollback.
