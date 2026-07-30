# Gemini 3.1 Pro reverse-prompt validation

## Scope and immutable base

- Base commit: `0c1e4d18b0e5556034eeec9afe29ccc0e0b7d0e2` (runtime capture `4d7afb8b95a58a5a2cb178e5e74c35592e169cda` plus cache-stamp-only alignment).
- Runtime identity: the checked-in test-only snapshot with PR #141 breakdown/egress/tikhub and PR #142 workbench behavior.
- Production connected: false.
- Paid model calls: zero. All provider behavior is mocked.
- Runtime behavior changed only in `server/content_domains/breakdown.py` for `reverse_prompt`.
- Ordinary breakdown continues to use its existing provider routing.

## Provider contract

- Reverse model: `gemini-3.1-pro-preview` through the official Gemini Developer API.
- Credential source: environment variable `GEMINI_API_KEY` only.
- Small input: inline video/image, at most 14 MiB and, for video, at most 15 seconds.
- Larger input: resumable Files API upload, poll to `ACTIVE`, then delete in `finally`.
- Once upload returns a file handle, every success/failure path performs exactly one best-effort DELETE under an independent cleanup deadline; sanitized cleanup failures never mask the primary error.
- Structured output: JSON MIME plus a strict JSON Schema; incomplete or extra-root JSON is rejected.
- Retry: one same-provider physical retry for network/429/5xx; HTTP 4xx is not retried.
- Validation retry: at most one new analysis of the original media and validation error; rejected draft text is not sent back.
- Cross-provider fallback: disabled; reverse_prompt does not call GLM or OpenAI.

Official references checked during implementation:

- https://ai.google.dev/gemini-api/docs/models/gemini-3.1-pro-preview
- https://ai.google.dev/api/files
- https://ai.google.dev/api/generate-content
- https://ai.google.dev/gemini-api/docs/generate-content/structured-output

## Output and validation contract

- Detect hard cuts before describing shots; return 1-4 gap-free 0.1-second windows.
- Keep observable facts separate from generation advice.
- Require evidence timestamps for every applicable fact.
- Require action start evidence in the first half and action end evidence in the second half.
- Require at least 90 percent of applicable generation slots to be evidence-ready.
- Preserve the existing eight-frame audit bundle, explicit reference thumbnail indexes, ASR binding, duplicate/subjective inference checks, and SSIM-only static acceptance.
- Persist model id, provider, attempts, evidence timestamps, per-shot readiness, and separate quality dimensions.
- `end_to_end_similarity_claimed` remains false: source evidence readiness is not a generated-video similarity measurement.

## Local evidence

- `tests.test_breakdown_gemini31` plus `tests.test_breakdown_content_compat`: 26 passed.
- Runtime manifest verification/reproduction: 4 passed.
- Workbench display/copy/downstream mapping tests: 69 passed.
- Python syntax and `git diff --check`: passed.

The repository's inherited `tests/test_breakdown.py` targets undeployed PR #143 private helpers while this runtime file is intentionally PR #141/#142. It currently fails in class setup before test bodies. Existing refund and unrelated script-submission failures also reproduce on this immutable runtime snapshot. These baseline mismatches are not converted to skips or non-blocking checks by this change.
