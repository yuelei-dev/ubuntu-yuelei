# PR #182 test-runtime deployment overlay

This directory is the exact test-server overlay for the functionality merged by
PR #182 at `bbfeac84d4e60811ba9988bcb468438a94921427`. It contains no credentials
and performs no connection or deployment by itself.

## Dependency and scope

PR #182 was developed on top of PR #180 dynamic pricing. Therefore PR #184's
`deploy/test-runtime/pr180/manifest.json` overlay must be deployed first. This
manifest pins the overlapping `core.py`, `points.py`, and drift sentinel
preimages to PR #180's exact postimages and explicitly declares that relationship
through `overrides`. All other preimages remain pinned to their read-only test
runtime snapshots. The prior drift sentinel is included as a fifth immutable
audit preimage, so validation does not depend on historical Git objects being
available in a shallow CI checkout.

The overlay deploys nine exact dependencies:

- the seven PR #182 content/UI runtime files: `breakdown.py`, `core.py`,
  `jobs_store.py`, `payment_recovery.py`, `points.py`,
  `submission_idempotency.py`, and `script.html`;
- `pricing_config.py`, carried forward byte-for-byte because `core.py` and
  `points.py` import it;
- `drift_sentinel.py`, which allows an overlap only when the newer immutable
  manifest explicitly names the older manifest in `overrides`.

Only `huangque-content` is restarted. Auth, database, environment, secrets,
logs, user data, certificates, and unrelated services are not targets.

## Verification and deployment handoff

After independent review, merge, and exact-SHA approval, the test-server
operator uses the repository's unified overlay path from the exact merged
`origin/main` checkout:

```bash
python3 deploy/test-runtime/pr182/build_overlay.py --check
python3 scripts/test_runtime_overlay.py validate \
  deploy/test-runtime/pr182/manifest.json

HQ_SHIP_TARGET=test HQ_REMOTE=<test-user>@8.148.158.106 \
./ship --exact-files --pr <DEPLOYMENT_PR_NUMBER> \
  "deploy PR182 test runtime overlay" \
  deploy/test-runtime/pr182/runtime/server/content_domains/breakdown.py \
  deploy/test-runtime/pr182/runtime/server/content_domains/core.py \
  deploy/test-runtime/pr182/runtime/server/content_domains/jobs_store.py \
  deploy/test-runtime/pr182/runtime/server/content_domains/payment_recovery.py \
  deploy/test-runtime/pr182/runtime/server/content_domains/points.py \
  deploy/test-runtime/pr182/runtime/server/content_domains/submission_idempotency.py \
  deploy/test-runtime/pr182/runtime/site/workbench/script.html \
  server/pricing_config.py \
  deploy/test-runtime/pr182/runtime/scripts/drift_sentinel.py
```

The unified `ship` flow requires `HEAD == origin/main`, verifies every preimage
before the first write, snapshots every target and the active-overlay state,
pushes only the declared set, performs import/restart/health/drift gates, and
records success only after all gates pass. Any failure after the first write
runs the generated exact restore script automatically. The command prints the
backup directory and manual rollback command on success.

The preimage check intentionally fails if PR #184 has not been deployed or if
the test runtime has drifted. Operators must not bypass the mismatch or copy
individual files manually.
