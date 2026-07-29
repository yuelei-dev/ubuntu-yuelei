# Test runtime baseline

This directory documents the immutable code baseline for the Huangque **test
server only**. It is not a production deployment definition.

## Identity

- Git base used to open this baseline PR:
  `main@5bc6dfacfb09ef6b885b48cefa35b164a5df522f`
- Runtime snapshot time: recorded in `deploy/runtime-manifest.json`
- Runtime composition:
  - sanitized production behavior snapshot
    `/opt/huangque-production-baseline-20260729-031728`
  - reviewed PR #141 overlays for `breakdown.py`, `egress.py`, and `tikhub.py`
  - reviewed PR #142 overlay for `site/workbench/script.html`
  - five explicitly inventoried test-runtime-only files
- PR #143 is merged in `main` but was not deployed to the captured runtime.
  The manifest therefore pins the running PR #141 `breakdown.py` bytes.
- Draft PR #144 failed real-model isolation and is not part of this baseline.

## External state

The release deliberately excludes all environment values, keys, tokens,
passwords, certificates, databases, logs, caches, user/transaction data,
uploads, generated output, and `content_out`. The runtime supplies those
through existing test-only external paths. The checked-in systemd and Nginx
files are sanitized reference contracts; they are not copied into a release by
the build script.

## Deterministic build

Run:

```bash
python scripts/build_test_runtime_release.py --verify-only
python scripts/build_test_runtime_release.py --output /tmp/huangque-release
```

The builder refuses missing files, duplicate or unsafe paths, unresolved
secret-file findings, and any SHA-256 mismatch. The output contains only the
305 code/template/static files listed in `deploy/runtime-manifest.json`, plus a
release manifest.

## Activation gate

This PR must not activate or restart formal test services. After independent
review, a separate release operation must:

1. build from the reviewed exact commit into a new immutable release directory;
2. bind only the existing external test env/data paths;
3. start all six services on loopback-only preview ports;
4. pass health, unauthenticated 401, ABI/import, frontend, and domain contracts;
5. record service and database baselines without copying database contents;
6. switch only after approval, with a previous-release pointer for rollback;
7. verify rollback before declaring the migration complete.

Production must never consume this test-runtime branch.
