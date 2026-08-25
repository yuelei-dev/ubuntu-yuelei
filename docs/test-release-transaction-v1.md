# Unified test release transaction executor v1

`tools/test_release_transaction.py` is the write-capable second phase of the
test-release platform. It does not change the runtime catalog or GitHub
workflows. It consumes the exact read-only plan
returned by the installed `scripts/release_test.py` and adds only transactional
`apply` and `recover` behavior.

This PR installs or deploys nothing. An administrator must separately review
and install the executor as
`/usr/local/libexec/huangque-release/test_release_transaction.py`, owned by
root and mode `0755`, and install `tools/test_release_transaction_launcher.sh`
as `/usr/local/sbin/huangque-release-test-transaction`, root-owned mode `0755`.
`/etc/huangque/release-transaction-v1.json`
must be root-owned, mode `0600`, and contain exactly:

```json
{
  "schema_version": 1,
  "source_root": "/opt/huangque-test-release",
  "transaction_entrypoint": "/usr/local/libexec/huangque-release/test_release_transaction.py",
  "transaction_sha256": "REPLACE_WITH_REVIEWED_EXECUTOR_SHA256",
  "phase_one_entrypoint": "/usr/local/libexec/huangque-release/release_test.py",
  "phase_one_sha256": "056318930098881f45c5dd8cab7dbe1ee33d3f2b21b32e4fb0b1e72eb312143b",
  "launcher": "/usr/local/sbin/huangque-release-test-transaction",
  "launcher_sha256": "REPLACE_WITH_REVIEWED_LAUNCHER_SHA256",
  "state_root": "/var/lib/huangque-release"
}
```

Whenever the reviewed phase-one entrypoint changes, the administrator must
replace both the installed phase-one file and this transaction bootstrap value
with that same reviewed SHA-256 before running the transaction launcher. A
mixed old/new pair remains fail-closed.

The launcher starts exact `/usr/bin/python3` through `env -i` with
`-I -E -s -B` and one exact minimal environment. The command checks its exact
installed `__file__`, interpreter and isolation flags, validates complete
root-owned/non-writable path chains, and validates both installed entrypoint
hashes before compiling the phase-one planner from those same verified bytes.
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
