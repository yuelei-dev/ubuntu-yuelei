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
- `tools/test_release_external_boundaries_v1.py` as
  `/usr/local/libexec/huangque-release/test_release_external_boundaries_v1.py`,
  root:root mode `0755`, and its JSON contract as
  `/usr/local/share/huangque-release/test_release_external_boundaries_v1.json`,
  root:root mode `0644`;
- `/etc/huangque/release-ancestor-initializer-v1.json`, root:root mode `0600`,
  with exactly the reviewed values below.

```json
{
  "schema_version": 1,
  "source_root": "/opt/huangque-test-release",
  "state_root": "/var/lib/huangque-release",
  "initializer_entrypoint": "/usr/local/libexec/huangque-release/test_release_ancestor_initializer_v1.py",
  "initializer_sha256": "4427899f2586dbf8c7195c72bc641bdc0afe3256fcc7a6811137166697e92014",
  "phase_one_entrypoint": "/usr/local/libexec/huangque-release/release_test.py",
  "phase_one_sha256": "d740f5e1656caebf67a33ee52aa6c731433886290a7bf8badd8b307126492abb",
  "boundary_entrypoint": "/usr/local/libexec/huangque-release/test_release_external_boundaries_v1.py",
  "boundary_sha256": "5cc6fba20a5f575d955ebb861e8b25fd82a0b0639504c5219a6b836b5925b2e7",
  "boundary_contract": "/usr/local/share/huangque-release/test_release_external_boundaries_v1.json",
  "boundary_contract_sha256": "98889991d7b2db6f151ff1671b17670e3e62a1b1bc5deee25d921f58aba79e19",
  "launcher": "/usr/local/sbin/huangque-release-test-initialize-ancestor-v1",
  "launcher_sha256": "a194d305a95548766c2310e74a42e49ff023bc1081ba3450806dbf3acf3459f4"
}
```

The launcher requires root, exact installed paths and root-owned non-writable
path chains, verifies the initializer SHA, and starts exact `/usr/bin/python3`
through `env -i` with `-I -E -s -B`. The initializer then verifies its own,
the launcher, and the already-installed phase-one bytes against the private
bootstrap before compiling the verified phase-one bytes in memory. Python is
approved by comparing the resolved target of `sys.executable` with the resolved
target of `/usr/bin/python3`; the normal distro symlink to its versioned binary
is accepted, while any different resolved interpreter remains rejected.

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
ancestry, catalog, impacts, inventory and every declared external-boundary
link/target/parent lstat identity are repeated before the lock, inside
the lock and immediately before state write. Existing state, mixed inventory,
catalog evolution or any TOCTOU drift fails closed.

After success, the unchanged phase-one `status`/`plan` and the v1 transaction
executor consume the ledger directly. There is no migration, alternate path,
live-origin bypass, plan, apply, rollback or recover command in this tool.

Hermes' `/home/ubuntu/hermes-web` code root remains owned by its independent
atomic release manager. Leadgen A/B shared JSON, database and files links remain
shared runtime data. Both classes are explicit catalog extensions with
`never_follow_never_write`; the root transaction inventory validates link and
target metadata but never follows or hashes their mutable target contents.
The reviewed contract binds the observed `/home/ubuntu` parent to exact mode
`0751`; broader `0755`, `0775`, or `0777` modes are not accepted.
The Hermes releases parent `/home/ubuntu/hermes-ip12-releases` is bound to
root:root `0755`, while the selected release directory remains ubuntu:ubuntu
`0775` and the `/home/ubuntu/hermes-web` link remains root:root `0777`.

Mutable regular runtime data, including SQLite databases, is validated without
reading its content: the boundary-aware engine locks the real parent chain and
uses `lstat`, `open(O_NOFOLLOW)`, `fstat`, and a second path/parent inspection
to require one stable regular-file identity, mode, owner, and group. Logical
file size therefore does not determine initializer memory use. Managed code
continues through the immutable phase-one full-content SHA-256 path.

Upgrade order is boundary module, boundary JSON, initializer, launcher, then the
private bootstrap in one root-controlled maintenance operation. Verify every
installed SHA before invoking initialize. Rollback restores the previously
reviewed four installed files and private bootstrap together; never roll back
only one hash-bound component, and never change the runtime symlinks as part of
this tooling upgrade.
