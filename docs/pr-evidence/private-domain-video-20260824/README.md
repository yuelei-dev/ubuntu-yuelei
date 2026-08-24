# PR #295 private-domain batch-video evidence

This directory records the current-code browser acceptance for the private-domain
batch-video workflow.  The screenshots use an isolated local HTTP fixture with
16 synthetic asset records and the repository BGM manifest.  They do not use a
production identity, submit a Director Agent request, create a paid render, or
write server state.

## Scope and why the Agent files are included

The release is one coherent workbench unit:

- `private-domain-video.html` provides the new page and its safe planning flow.
- `script.html` and `digital-human-oneclick.html` provide the adjacent navigation.
- `script-agent.js` and `director_agent.py` provide the strict
  `private_domain_video` Agent context and allowlist required by the page.
- `site/assets/bgm/private-domain-v1/manifest.json` replaces the existing BGM
  catalog with stable UTF-8 Chinese titles.

The six runtime files are backed up, installed, checked and rolled back as one
transaction.  The existing six MP3 files remain immutable external assets and
are hash-checked before the first backup.

## Browser evidence

- `desktop-1280-agent-open.png`: 1280x900, two plans created, the private-domain
  Agent panel open, no horizontal overflow.
- `mobile-390.png`: 390x844, no horizontal overflow, Agent launcher present and
  not intersecting the Generate button.
- The 16-item fixture rendered only the first 12 candidates.  Every randomly
  selected asset ID was inside those 12 visible candidates.
- Before the material grid entered the viewport, 0 of its 12 video elements had
  a `src`; all 12 held only lazy `data-src` values.  Media uses direct same-origin
  Range-capable URLs or server-issued HTTPS signed preview URLs and never calls
  `response.blob()` or creates Object URLs.
- The BGM selector showed all six stable Chinese titles.

## Release identity and local acceptance

The executor requires `--confirm-target test@8.148.158.106` and validates the
manifest's exact role, public host, and logical host ID.  Before any network
preflight, backup or runtime write it reads the root-managed
`/etc/huangque/release-identity.json` (`root:root`, mode `0600`) and compares its
environment, host ID, public host, hostname, and machine-id SHA-256 with the
current machine's `/etc/hostname` and `/etc/machine-id`.

Health and authenticated Agent acceptance are locked to
`http://127.0.0.1:8096`; static acceptance reads the installed runtime bytes and
matches every postimage hash.  Public DNS cannot make the wrong machine pass.

The real identity file is deliberately not committed.  Release remains
fail-closed until a test-server administrator provisions that root-owned file.
This PR does not authorize that provisioning, deployment, restart, merge, or
server write.

## Rollback

Any failure after backup disables the Agent, restores all six files (including
removal of the newly introduced page when its locked preimage is absent),
restores the exact feature row, restarts the service, verifies loopback health,
and writes a durable rollback audit.  Injected failures cover every replacement
stage, compile/import, restart, activation, local installed-file acceptance,
authenticated Agent acceptance and final audit.
