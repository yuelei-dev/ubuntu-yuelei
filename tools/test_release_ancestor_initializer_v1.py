#!/usr/bin/env python3
"""One-time, versioned initializer for an older deployed main ancestor."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import types
from pathlib import Path


SCHEMA_VERSION = 1
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
SOURCE_ROOT = "/opt/huangque-test-release"
STATE_ROOT = "/var/lib/huangque-release"
INITIALIZER_ENTRYPOINT = "/usr/local/libexec/huangque-release/test_release_ancestor_initializer_v1.py"
PHASE_ONE_ENTRYPOINT = "/usr/local/libexec/huangque-release/release_test.py"
BOUNDARY_ENTRYPOINT = "/usr/local/libexec/huangque-release/test_release_external_boundaries_v1.py"
BOUNDARY_CONTRACT = "/usr/local/share/huangque-release/test_release_external_boundaries_v1.json"
LAUNCHER = "/usr/local/sbin/huangque-release-test-initialize-ancestor-v1"
BOOTSTRAP = "/etc/huangque/release-ancestor-initializer-v1.json"
EXPECTED_ENVIRONMENT = {
    "HOME": "/root",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PATH": "/usr/bin:/bin",
}


class InitializerError(RuntimeError):
    """Fail-closed initializer error."""


def _sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def _validate_root_chain(path, *, final_kind, private=False, final_mode=None):
    path = Path(os.path.abspath(path))
    current = Path(path.anchor)
    root_info = os.lstat(current)
    if (stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode)
            or os.name == "posix" and (root_info.st_uid != 0 or root_info.st_mode & 0o022)):
        raise InitializerError("trusted root path is not immutable")
    for index, part in enumerate(path.parts[1:]):
        current /= part
        try:
            info = os.lstat(current)
        except FileNotFoundError as exc:
            raise InitializerError("trusted path chain is incomplete") from exc
        final = index == len(path.parts[1:]) - 1
        expected_regular = final and final_kind == "file"
        if (stat.S_ISLNK(info.st_mode)
                or expected_regular and not stat.S_ISREG(info.st_mode)
                or not expected_regular and not stat.S_ISDIR(info.st_mode)
                or os.name == "posix" and (info.st_uid != 0 or info.st_mode & 0o022)
                or final and private and info.st_mode & 0o077
                or final and final_mode is not None
                and stat.S_IMODE(info.st_mode) != final_mode):
            raise InitializerError("trusted path chain is not root-owned and immutable")


def _locked_regular(path, expected_sha256, label, *, final_mode=None):
    _validate_root_chain(path, final_kind="file", final_mode=final_mode)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        chunks = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(descriptor)
    current = os.lstat(path)
    if ((current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
            or not stat.S_ISREG(opened.st_mode)):
        raise InitializerError("%s changed while it was read" % label)
    raw = b"".join(chunks)
    if _sha256(raw) != expected_sha256:
        raise InitializerError("%s SHA-256 differs from bootstrap" % label)
    return raw


def _load_bootstrap():
    _validate_root_chain(BOOTSTRAP, final_kind="file", private=True, final_mode=0o600)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(BOOTSTRAP, flags)
    try:
        info = os.fstat(descriptor)
        raw = b""
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            raw += chunk
            if len(raw) > 65536:
                raise InitializerError("initializer bootstrap is too large")
    finally:
        os.close(descriptor)
    current = os.lstat(BOOTSTRAP)
    if (not stat.S_ISREG(info.st_mode)
            or (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino)):
        raise InitializerError("initializer bootstrap changed while it was read")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise InitializerError("initializer bootstrap JSON is invalid") from exc
    required = {
        "schema_version", "source_root", "state_root", "initializer_entrypoint",
        "initializer_sha256", "phase_one_entrypoint", "phase_one_sha256",
        "boundary_entrypoint", "boundary_sha256", "boundary_contract",
        "boundary_contract_sha256", "launcher", "launcher_sha256",
    }
    if (not isinstance(value, dict) or set(value) != required
            or value.get("schema_version") != SCHEMA_VERSION
            or value.get("source_root") != SOURCE_ROOT
            or value.get("state_root") != STATE_ROOT
            or value.get("initializer_entrypoint") != INITIALIZER_ENTRYPOINT
            or value.get("phase_one_entrypoint") != PHASE_ONE_ENTRYPOINT
            or value.get("boundary_entrypoint") != BOUNDARY_ENTRYPOINT
            or value.get("boundary_contract") != BOUNDARY_CONTRACT
            or value.get("launcher") != LAUNCHER
            or any(not re.fullmatch(r"[0-9a-f]{64}", str(value.get(key) or ""))
                   for key in ("initializer_sha256", "phase_one_sha256", "boundary_sha256",
                               "boundary_contract_sha256", "launcher_sha256"))):
        raise InitializerError("initializer bootstrap fields are invalid")
    return value


def _runtime_is_isolated():
    flags = sys.flags
    return (
        os.name == "posix"
        and os.geteuid() == 0
        and os.path.realpath(sys.executable) == os.path.realpath("/usr/bin/python3")
        and os.path.realpath(__file__) == INITIALIZER_ENTRYPOINT
        and flags.isolated == 1
        and flags.ignore_environment == 1
        and flags.no_user_site == 1
        and flags.dont_write_bytecode == 1
        and dict(os.environ) == EXPECTED_ENVIRONMENT
    )


def _load_verified_phase_one():
    if not _runtime_is_isolated():
        raise InitializerError("initializer runtime is not isolated and root-locked")
    bootstrap = _load_bootstrap()
    own_raw = _locked_regular(
        INITIALIZER_ENTRYPOINT, bootstrap["initializer_sha256"], "initializer entrypoint",
        final_mode=0o755,
    )
    phase_raw = _locked_regular(
        PHASE_ONE_ENTRYPOINT, bootstrap["phase_one_sha256"], "phase-one entrypoint",
        final_mode=0o755,
    )
    boundary_raw = _locked_regular(
        BOUNDARY_ENTRYPOINT, bootstrap["boundary_sha256"], "external boundary entrypoint",
        final_mode=0o755,
    )
    boundary_contract_raw = _locked_regular(
        BOUNDARY_CONTRACT, bootstrap["boundary_contract_sha256"],
        "external boundary contract", final_mode=0o644,
    )
    _locked_regular(
        LAUNCHER, bootstrap["launcher_sha256"], "initializer launcher", final_mode=0o755,
    )
    if _sha256(own_raw) != bootstrap["initializer_sha256"]:
        raise InitializerError("initializer bytes changed after verification")
    module = types.ModuleType("huangque_locked_phase_one")
    module.__file__ = PHASE_ONE_ENTRYPOINT
    try:
        exec(compile(phase_raw, PHASE_ONE_ENTRYPOINT, "exec"), module.__dict__)
    except Exception as exc:
        raise InitializerError("verified phase-one entrypoint could not be loaded") from exc
    boundary = types.ModuleType("huangque_locked_external_boundaries")
    boundary.__file__ = BOUNDARY_ENTRYPOINT
    try:
        exec(compile(boundary_raw, BOUNDARY_ENTRYPOINT, "exec"), boundary.__dict__)
        module._external_boundary_module = boundary
        module._external_boundary_contract = boundary.load_contract(boundary_contract_raw)
    except Exception as exc:
        raise InitializerError("verified external boundary contract could not be loaded") from exc
    return module


class AncestorInitializer:
    def __init__(self, phase_one, engine):
        self.phase_one = phase_one
        self.engine = engine
        self.repo = engine.repo
        self.catalog = engine.catalog

    def _latest_main(self, expected=None):
        origin_url = self.catalog.target["origin_url"]
        if self.repo.output(["status", "--porcelain", "--untracked-files=normal"]):
            raise InitializerError("release source checkout must be clean")
        if self.repo.output(["symbolic-ref", "--short", "HEAD"]) != "main":
            raise InitializerError("release source checkout must be on main")
        if self.repo.output(["remote", "get-url", "origin"]) != origin_url:
            raise InitializerError("origin repository identity is not approved")
        head = self.repo.output(["rev-parse", "HEAD"])
        local_main = self.repo.output(["rev-parse", "refs/remotes/origin/main"])
        if not COMMIT_RE.fullmatch(head) or local_main != head:
            raise InitializerError("HEAD and local origin/main must equal current main")
        self.repo.require_commit(head)
        if expected is not None and head != expected:
            raise InitializerError("local source changed from captured latest main")
        live_main = self.repo.remote_main_resolver(origin_url)
        if live_main != head:
            raise InitializerError("live approved origin/main must equal current main")
        return head

    def _snapshot(self, deployed_commit, expected_latest=None):
        identity = self.engine.verify_identity()
        latest_main = self._latest_main(expected_latest)
        self.repo.require_ancestor(deployed_commit, latest_main)
        catalog_record = self.engine._catalog_record(deployed_commit)
        self.phase_one.validate_catalog_coverage(self.repo, self.catalog, deployed_commit)
        runtime_hashes, repository_paths, runtime_metadata = (
            self.engine._expected_runtime(deployed_commit)
        )
        mismatches = [
            path for path, expected in runtime_hashes.items()
            if not self.engine._runtime_matches(path, expected, runtime_metadata[path])
        ]
        unexpected = self.engine._inventory_drift(set(runtime_hashes))
        if mismatches or unexpected:
            raise InitializerError("runtime inventory does not exactly match deployed commit")
        accepted_impacts = self.phase_one.collect_impact_index(
            self.repo, self.catalog, deployed_commit,
        )
        external_boundaries = self.engine.external_boundary_snapshot()
        return {
            "identity": identity,
            "latest_main": latest_main,
            "catalog_record": catalog_record,
            "accepted_impacts": accepted_impacts,
            "runtime_hashes": runtime_hashes,
            "repository_paths": repository_paths,
            "runtime_metadata": runtime_metadata,
            "external_boundaries": external_boundaries,
        }

    def initialize(self, deployed_commit, confirmation):
        if confirmation != "test":
            raise InitializerError("initialize requires exact test environment confirmation")
        if not COMMIT_RE.fullmatch(str(deployed_commit or "")):
            raise InitializerError("deployed commit must be an exact 40-character SHA")
        before = self._snapshot(deployed_commit)
        with self.engine._release_lock():
            state_path = self.engine._state_runtime_path("state.json")
            if self.phase_one._read_regular(self.engine.runtime_root, state_path) is not None:
                raise InitializerError("deployment ledger is already initialized")
            locked = self._snapshot(deployed_commit, before["latest_main"])
            if locked != before:
                raise InitializerError("initializer trust snapshot changed inside lock")
            final = self._snapshot(deployed_commit, before["latest_main"])
            if final != before:
                raise InitializerError("initializer trust snapshot changed before state write")
            if self.phase_one._read_regular(self.engine.runtime_root, state_path) is not None:
                raise InitializerError("deployment ledger was initialized concurrently")
            identity = final["identity"]
            state = {
                "schema_version": self.phase_one.SCHEMA_VERSION,
                "environment": identity["environment"],
                "host_id": identity["host_id"],
                "deployed_main_commit": deployed_commit,
                "runtime_catalog": final["catalog_record"],
                "accepted_impacts": final["accepted_impacts"],
                "runtime_hashes": final["runtime_hashes"],
                "repository_paths": final["repository_paths"],
                "runtime_metadata": final["runtime_metadata"],
                "managed_runtime_paths": sorted(final["runtime_hashes"]),
                "last_release_id": None,
                "last_successful_release": None,
            }
            self.engine._write_state(state)
        return {
            "ok": True,
            "status": "initialized",
            "deployed_main_commit": deployed_commit,
            "latest_main_commit": before["latest_main"],
            "tracked_runtime_paths": len(before["runtime_hashes"]),
        }


def main(argv=None):
    parser = argparse.ArgumentParser(description="One-time Huangque ancestor initializer v1")
    subparsers = parser.add_subparsers(dest="command", required=True)
    initialize = subparsers.add_parser("initialize")
    initialize.add_argument("--deployed-commit", required=True)
    initialize.add_argument("--confirm-environment", required=True)
    args = parser.parse_args(argv)
    try:
        phase_one = _load_verified_phase_one()
        phase_one._verify_runtime_entrypoint(SOURCE_ROOT)
        catalog = phase_one.RuntimeCatalog.load(SOURCE_ROOT, phase_one.DEFAULT_CATALOG)
        catalog, engine_class, _policy = phase_one._external_boundary_module.install(
            phase_one, catalog, phase_one._external_boundary_contract,
        )
        engine = engine_class(SOURCE_ROOT, "/", catalog)
        result = AncestorInitializer(phase_one, engine).initialize(
            args.deployed_commit, args.confirm_environment,
        )
    except (InitializerError, RuntimeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
