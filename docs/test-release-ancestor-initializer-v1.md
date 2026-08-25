# Test release ancestor initializer v1

This additive, one-time initializer exists for the case where the test host's
complete managed runtime still matches an older merged `main` commit while the
root-owned source mirror has already advanced to the latest `main`. It does not
replace or modify the phase-one trust root and exposes only `initialize`.

Nothing in this PR installs or runs the initializer. A separately authorized
administrator must install:

- `tools/test_release_ancestor_initializer_v1.py` as
  `/usr/local/libexec/huangque-release/test_release_ancestor_initializer_v1.py`,
  root:root mode `0755`;
- `tools/test_release_ancestor_initializer_v1_launcher.sh` as
  `/usr/local/sbin/huangque-release-test-initialize-ancestor-v1`, root:root mode
  `0755`;
- `/etc/huangque/release-ancestor-initializer-v1.json`, root:root mode `0600`,
  with exactly the reviewed values below.

```json
{
  "schema_version": 1,
  "source_root": "/opt/huangque-test-release",
  "state_root": "/var/lib/huangque-release",
  "initializer_entrypoint": "/usr/local/libexec/huangque-release/test_release_ancestor_initializer_v1.py",
  "initializer_sha256": "5ab5a71b60e070906f3987f88f8fad95b045a99bb86abcdca1ae755ba638f445",
  "phase_one_entrypoint": "/usr/local/libexec/huangque-release/release_test.py",
  "phase_one_sha256": "d740f5e1656caebf67a33ee52aa6c731433886290a7bf8badd8b307126492abb",
  "launcher": "/usr/local/sbin/huangque-release-test-initialize-ancestor-v1",
  "launcher_sha256": "b52fc98f061c57ca40b6a0d1312110d6a2845f50c99f5e94122f8f62b0878aa4"
}
```

The launcher requires root, exact installed paths and root-owned non-writable
path chains, verifies the initializer SHA, and starts exact `/usr/bin/python3`
through `env -i` with `-I -E -s -B`. The initializer then verifies its own,
the launcher, and the already-installed phase-one bytes against the private
bootstrap before compiling the verified phase-one bytes in memory.

```text
sudo /usr/local/sbin/huangque-release-test-initialize-ancestor-v1 initialize \
  --deployed-commit EXACT_OLDER_MERGED_MAIN_SHA \
  --confirm-environment test
```

The source `HEAD`, local `origin/main`, and live approved `origin/main` must
all equal the same latest main SHA. The deployed commit must exist and be its
Git ancestor (or equal it). The initializer uses the deployed commit's exact
catalog blob, accepted impacts, complete expected runtime inventory and
metadata to write the existing phase-one ledger schema. The worktree catalog
must still match that older blob byte-for-byte. Identity, source/live main,
ancestry, catalog, impacts and inventory are repeated before the lock, inside
the lock and immediately before state write. Existing state, mixed inventory,
catalog evolution or any TOCTOU drift fails closed.

After success, the unchanged phase-one `status`/`plan` and the v1 transaction
executor consume the ledger directly. There is no migration, alternate path,
live-origin bypass, plan, apply, rollback or recover command in this tool.
