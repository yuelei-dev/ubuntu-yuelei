# Runtime baseline validation evidence

This document records the pre-PR local and read-only test-server evidence for
the captured test runtime. It is intentionally explicit about failing
`main`-branch contracts; those failures must not be hidden by deleting tests or
weakening assertions.

## Snapshot

- Included runtime files: 305
- Included bytes: 23,733,604
- Source classifications:
  - production behavior snapshot: 296
  - PR #141 overlays: 3
  - PR #142 overlay: 1
  - explicitly retained test-runtime-only files: 5
- Excluded path groups: 35
- Secret detector findings: 0
- Git path collisions: 0
- End-of-capture remote hash verification: 305/305 exact
- Formal test services changed during capture: no

## Passing gates

- Python compilation: pass
- JavaScript syntax: 19/19
- Existing static/security validator: pass
- Workbench cache stamps: pass
- Node sidebar regressions: 10/10
- Runtime manifest/build tests: 4/4
- Deterministic release build: 305/305 SHA-256 exact
- Six service import contracts: 6/6
- Six service local isolated startup:
  - auth/content/dl/imggen/leadgen health: HTTP 200
  - admin unauthenticated health: HTTP 401, proving the service is listening
    and preserving its current authentication contract
  - all six processes stopped after the smoke test
- Paid model calls: 0

## Known failing gates

Targeted current-`main` contracts:

- tests run: 320
- failures: 3
- errors: 147
- passing: 170

Full Python suite:

- tests run: 1,331
- failures: 110
- errors: 247
- passing: 974

The dominant failures prove that current `main` tests describe behavior newer
than the formal test runtime:

- the formal runtime still has the reviewed PR #141 `breakdown.py`, while
  `main` and its tests include merged but undeployed PR #143;
- newer breakdown contracts expect `_reverse_frame_pair_ssim` and the
  generation-readiness pipeline that the captured runtime does not expose;
- current image reverse tests expect cleanup and thinking-budget behavior that
  the running `imggen_api.py` does not implement;
- current copy tests expect multimodal helpers absent from the running
  `text.py`;
- jobs/refund and several frontend/API contracts differ between `main` and the
  production-behavior runtime snapshot.

These are release blockers for a direct `main`-to-runtime switch. They are not
silenced or reclassified as success. The Draft PR exists to give the runtime a
reviewable, immutable source branch before behavior reconciliation proceeds in
smaller follow-up PRs.

## Release status

- Formal test deployment: not performed
- Formal test service restart: not performed
- Production connection: not performed
- Merge authorization: none
- Recommended PR state: Draft / blocked pending independent gate review
