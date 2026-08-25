# Unified test release transaction executor v1

`tools/test_release_transaction.py` is the write-capable second phase of the
test-release platform. It does not change the immutable phase-one verifier,
runtime catalog, or GitHub workflows. It consumes the exact read-only plan
returned by the installed `scripts/release_test.py` and adds only transactional
`apply` and `recover` behavior.

This PR installs or deploys nothing. An administrator must separately review
and install the executor as
`/usr/local/libexec/huangque-release/test_release_transaction.py`, owned by
root and not group/other writable. `/etc/huangque/release-transaction-v1.json`
must be root-owned, mode `0600`, and contain exactly:

```json
{
  "schema_version": 1,
  "source_root": "/opt/huangque-test-release",
  "transaction_entrypoint": "/usr/local/libexec/huangque-release/test_release_transaction.py",
  "transaction_sha256": "REPLACE_WITH_REVIEWED_EXECUTOR_SHA256",
  "phase_one_entrypoint": "/usr/local/libexec/huangque-release/release_test.py",
  "phase_one_sha256": "REPLACE_WITH_ALREADY_INSTALLED_PHASE_ONE_SHA256",
  "state_root": "/var/lib/huangque-release"
}
```

The command runs only as root under exact `/usr/bin/python3`; it validates both
installed entrypoint hashes before importing the phase-one planner. It accepts
no alternate source, catalog, identity, state, service, health dispatcher, or
runtime root from the CLI.

## Apply contract

```text
/usr/bin/python3 /usr/local/libexec/huangque-release/test_release_transaction.py apply \
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

The executor then runs named pre-health checks, copies and fsyncs every locked
preimage into a private transaction directory, and atomically persists a
journal before the first write. It writes each Git postimage through a
same-directory temporary regular file, fsyncs it, applies locked metadata, and
atomically replaces the target without following symlinks. It performs an
allowlisted `daemon-reload` when the plan requires one, restarts only services
from the immutable catalog, and runs the named post-health contracts.

Only after all postimages and post-health checks succeed is the deployment
ledger atomically advanced to the target commit and complete target inventory.
An apply failure restores every target, repeats required daemon reload/service
restart, and runs the named pre-health contracts as rollback health. A rollback
error is retained in the active journal and never reported as success.

## Crash recovery

```text
/usr/bin/python3 /usr/local/libexec/huangque-release/test_release_transaction.py recover
```

`recover` takes the same kernel transaction lock and revalidates the full host
identity. If the ledger is still on the preimage commit, it restores every
backup and service before removing the journal. If the ledger has already been
atomically committed, it repeats post-health and only then removes the journal.
Any third ledger commit, corrupt journal, missing/tampered backup, identity
change, unsafe path, health failure, or rollback failure stops fail-closed and
preserves evidence for operator review.

V1 implements fixed loopback HTTP checks for the catalog's admin, auth,
content, download, Hermes, image-generation, and lead-generation health IDs,
plus fixed `systemctl is-active` checks for its service-only health IDs. It
rejects redirects, proxies, arbitrary URLs, arbitrary units, and the catalog's
specialized Bitable, drift-sentinel, and Nginx contracts until separately
reviewed write-safe implementations exist. Probe IDs must already exist in the
immutable catalog; the executor never accepts a URL or shell command from an
impact file.
