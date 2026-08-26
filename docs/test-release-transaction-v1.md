# Unified test release transaction executor v1

`tools/test_release_transaction.py` is the write-capable second phase of the
test-release platform. It does not change the immutable phase-one verifier,
runtime catalog, or GitHub workflows. It consumes the exact read-only plan
returned by the installed `scripts/release_test.py` and adds only transactional
`apply` and `recover` behavior.

This PR installs or deploys nothing. An administrator must separately review
and install the executor as
`/usr/local/libexec/huangque-release/test_release_transaction.py`, owned by
root and mode `0755`, and install `tools/test_release_transaction_launcher.sh`
as `/usr/local/sbin/huangque-release-test-transaction`, root-owned mode `0755`.
The same operation installs the reviewed external-boundary Python module at
`/usr/local/libexec/huangque-release/test_release_external_boundaries_v1.py`
(root:root `0755`) and JSON contract at
`/usr/local/share/huangque-release/test_release_external_boundaries_v1.json`
(root:root `0644`).
`/etc/huangque/release-transaction-v1.json`
must be root-owned, mode `0600`, and contain exactly:

```json
{
  "schema_version": 1,
  "source_root": "/opt/huangque-test-release",
  "transaction_entrypoint": "/usr/local/libexec/huangque-release/test_release_transaction.py",
  "transaction_sha256": "b4a6a9b4bd1d368dbd33d30588a4cee151c9446f6fef172c4fca2881e82fba61",
  "phase_one_entrypoint": "/usr/local/libexec/huangque-release/release_test.py",
  "phase_one_sha256": "d740f5e1656caebf67a33ee52aa6c731433886290a7bf8badd8b307126492abb",
  "boundary_entrypoint": "/usr/local/libexec/huangque-release/test_release_external_boundaries_v1.py",
  "boundary_sha256": "5cc6fba20a5f575d955ebb861e8b25fd82a0b0639504c5219a6b836b5925b2e7",
  "boundary_contract": "/usr/local/share/huangque-release/test_release_external_boundaries_v1.json",
  "boundary_contract_sha256": "98889991d7b2db6f151ff1671b17670e3e62a1b1bc5deee25d921f58aba79e19",
  "launcher": "/usr/local/sbin/huangque-release-test-transaction",
  "launcher_sha256": "8163285d21e7de7e003e0b361f2d10ebe1500457473b8c03c01dcb27690d8a9d",
  "state_root": "/var/lib/huangque-release"
}
```

Whenever the reviewed phase-one entrypoint changes, the administrator must
replace both the installed phase-one file and this transaction bootstrap value
with that same reviewed SHA-256 before running the transaction launcher. A
mixed old/new pair remains fail-closed.

The launcher verifies the transaction entrypoint against its reviewed
SHA-256, then starts exact `/usr/bin/python3` through `env -i` with
`-I -E -s -B` and one exact minimal environment. The command checks its exact
installed `__file__`, interpreter and isolation flags, validates complete
root-owned/non-writable path chains, and validates both installed entrypoint
hashes before compiling the phase-one planner from those same verified bytes.
The interpreter check compares the resolved target of `sys.executable` with
the resolved target of `/usr/bin/python3`, accepting the approved distro
symlink to its versioned binary but rejecting every different resolved target.
It accepts
no alternate source, catalog, identity, state, service, health dispatcher, or
runtime root from the CLI.

## Apply contract

```text
/usr/local/sbin/huangque-release-test-transaction apply \
  --target-commit MERGED_MAIN_SHA \
  --reviewed-head RELEASE_ID=EXACT_MAIN_PARENT..REVIEWED_HEAD
```

Before a backup or runtime write, the immutable planner proves all of these:

- the checkout, local `origin/main`, and live approved GitHub `main` all equal
  the requested 40-character merge commit;
- each reviewed Head is the unique second parent of its ordinary main merge,
  and the supplied merge base is that merge's exact first parent;
- identity includes environment, host ID, hostname, and machine-id SHA-256;
- the deployment ledger, catalog blob/SHA-256, accepted impacts, complete
  inventory, every managed runtime preimage, mode, owner, group, service state,
  required environment, free space, and named pre-health contracts match;
- every target has one exact repository path, runtime path, before/after
  SHA-256, mode, owner, group, and allowlisted service mapping.

The executor then runs named pre-health checks and atomically persists a
`backing_up` journal before creating the private backup directory. It copies
and fsyncs every locked preimage, binds the complete journal back to Git,
catalog, reviewed topology and ledger metadata, and rechecks every preimage
immediately before the first write. It writes each Git postimage through a
same-directory temporary regular file, fsyncs it, applies locked metadata, and
atomically replaces the target without following symlinks. It performs an
allowlisted `daemon-reload` when the plan requires one, restarts only services
from the immutable catalog, and runs the named post-health contracts.

Only after all postimages and post-health checks succeed is the deployment
ledger atomically advanced to the target commit and complete target inventory.
An apply failure classifies every target before restoring: locked preimages are
left untouched, locked postimages are restored, and a neither-before-nor-after
state stops without overwriting third-party bytes. It then repeats required
daemon reload/service restart and runs named pre-health contracts as rollback
health. A rollback error is retained in the active journal and never reported
as success.

## Crash recovery

```text
/usr/local/sbin/huangque-release-test-transaction recover
```

`recover` takes the same kernel transaction lock, cleans only schema-valid
orphan transaction directories when no active writer can exist, and revalidates
the full host identity and journal contract. If the ledger is still on the
preimage commit, it safely restores locked postimages and services before
removing the journal. If the ledger has already been atomically committed, it
rechecks every postimage and metadata lock, complete target inventory, service
state and post-health before removing the backup. `backing_up` and `cleaning`
states are recoverable, so a crash cannot create invisible persistent backups.
Any third ledger commit, corrupt journal, missing/tampered backup, identity
change, unsafe path, health failure, or rollback failure stops fail-closed and
preserves evidence for operator review.

The journal also binds the complete external-boundary snapshot. Apply verifies
it after planning, after backup, before every managed write, after post-health
and postimage checks, on both sides of final inventory acceptance, and directly
before ledger write. Rollback and every crash-recovery completion branch verify
the exact journal snapshot again before restoring or deleting evidence. Hermes'
externally managed current-release link and Leadgen's shared
runtime-data links are never transaction targets and are never followed; an
undeclared link or any declared link, target, owner, mode or parent-chain drift
fails closed with the affected runtime path.
The observed `/home/ubuntu` parent is locked to exact mode `0751`; `0755`,
`0775`, and `0777` are deliberately rejected rather than treated as aliases.
The Hermes releases parent is separately locked to root:root `0755`; its
selected release directory remains ubuntu:ubuntu `0775`, so parent ownership
cannot be accidentally inferred from the child release ownership.

The same boundary adapter validates mutable regular runtime data with metadata
only. It snapshots every real parent, opens the final path with `O_NOFOLLOW`,
compares `lstat`/`fstat` identity and permission metadata before and after the
open, and never reads database contents. Managed transaction targets retain
their existing complete byte hash and preimage/postimage checks.

Upgrade the boundary module and JSON first, then transaction entrypoint and
launcher, and finally atomically replace the private bootstrap. Do not run apply
or recover while the set is mixed. Rollback restores the previous reviewed
module, JSON, transaction, launcher and bootstrap as one unit. It does not alter
Hermes or Leadgen links and does not roll back business files by itself.

V1 implements fixed loopback HTTP checks for the catalog's admin, auth,
content, download, Hermes, image-generation, and lead-generation health IDs,
plus fixed `systemctl is-active` checks for its service-only health IDs. It
also implements `site-loopback` by connecting only to `127.0.0.1:443`, using
fixed TLS SNI and HTTP Host `yuelei.huangquechuanmei.com`, requesting only
`/workbench/private-domain-video.html`, and requiring a verified certificate,
HTTP 200, HTML/nosniff headers, and a complete response of at most 2 MiB whose
length and SHA-256 exactly match the transaction's locked managed page for the
current pre, post, or rollback phase. The TLS connection is always closed. It
rejects redirects, proxies, arbitrary URLs, arbitrary units, and the catalog's
specialized Bitable and drift-sentinel contracts until separately
reviewed write-safe implementations exist. Probe IDs must already exist in the
immutable catalog; the executor never accepts a URL or shell command from an
impact file.
