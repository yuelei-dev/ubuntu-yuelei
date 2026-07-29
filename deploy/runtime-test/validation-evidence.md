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

## Runtime/test gate follow-up

PR #145 now has a dedicated gate that is selected only when the pull request
base is `runtime/test`. The original `main` workflow keeps the same steps and
commands for pushes and for pull requests whose base is not `runtime/test`.

Blocking runtime suite:

- previously established runtime contracts: 91/91
- added gap contracts: 9/9
- total: 100/100
- added coverage:
  - manifest source classifications and external-state exclusions
  - key-only environment contract and sanitized service/Nginx references
  - PR #141 link/upload ownership, eight-frame, GLM-only, SSIM/reference and
    egress/TikHub compatibility through the existing compatibility contracts
  - PR #142 live/history prompt rendering through the existing render contracts
  - refund state `0 -> 2 -> 1` and concurrent idempotent financial effect
  - runtime-only workflow routing and unchanged main workflow commands

The six-service disposable-state smoke remains green: auth/content/dl/imggen/
leadgen returned HTTP 200, admin preserved its unauthenticated HTTP 401
contract, all child processes stopped, and paid model calls were zero.

Resource-stamp refresh:

- only `site/workbench/script.html` changed under `site/`
- old SHA-256:
  `e0e49d646299edfcb831eefad0b259664fc8df47c89b9cc39f9be2450aa85f23`
- new SHA-256:
  `b66cc86fef6f4bb5bf1d046da4331fe0f26cdbb0cbda262e836d6c96f81cf58a`
- changed text: `cloud-shell.js?v=302ad2ce` to
  `cloud-shell.js?v=642453c4`
- normalized semantic SHA-256 before/after:
  `27b6e0920a64215587f37d9a227b7004322ab8daad475b047441898e03717c73`
- semantic diff after removing the cache stamp: zero

Two independent release builds each contained 305 files. Their complete
relative-path and SHA-256 maps were identical with zero extra, missing or
mismatched files.

## Structured diagnostics

The diagnostic runner stores one record per test with test id, module, class,
method, duration, status and complete traceback. It does not skip, delete or
weaken a test.

- preserved original 1,331-test log: 974 pass, 110 failure, 247 error
- reconstructed original 320-test diagnostic: 170 pass, 3 failure, 147 error
- current complete 1,331-test rerun is retained separately because environment
  and order-sensitive legacy tests can produce a different distribution

These diagnostics are non-blocking only for pull requests whose base is
`runtime/test`. They remain fully executed and uploaded as artifacts. They do
not change the behavior or assertions of the original main workflow.
