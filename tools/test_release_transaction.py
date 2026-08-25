#!/usr/bin/env python3
"""Versioned transactional apply/recover engine for the Huangque test host.

This module deliberately consumes the immutable phase-one planner rather than
changing its trust roots.  The production installer must lock this file's Git
blob and SHA-256 before exposing it through a root-owned launcher.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path, PurePosixPath

if os.name == "posix":
    import fcntl
    import grp
    import pwd
else:  # pragma: no cover - exercised by Windows developer workstations
    import msvcrt


COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
PROBE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,79}$")
UNIT_RE = re.compile(r"^[A-Za-z0-9@_.:-]+\.(?:service|timer)$")
DEFAULT_STATE_ROOT = "/var/lib/huangque-release"
DEFAULT_SOURCE_ROOT = "/opt/huangque-test-release"
PHASE_ONE_ENTRYPOINT = "/usr/local/libexec/huangque-release/release_test.py"
TRANSACTION_ENTRYPOINT = "/usr/local/libexec/huangque-release/test_release_transaction.py"
TRANSACTION_BOOTSTRAP = "/etc/huangque/release-transaction-v1.json"
SYSTEMCTL = "/usr/bin/systemctl"
JOURNAL_SCHEMA = 1
HTTP_HEALTH_PROBES = {
    "admin-health": "http://127.0.0.1:8098/api/admin/health",
    "auth-health": "http://127.0.0.1:8095/api/auth/health",
    "content-health": "http://127.0.0.1:8096/api/gen/health",
    "dl-health": "http://127.0.0.1:8097/api/gen/dl/health",
    "hermes-health": "http://127.0.0.1:3102/healthz",
    "imggen-health": "http://127.0.0.1:8101/api/gen/banana/health",
    "leadgen-api-health": "http://127.0.0.1:8100/api/gen/leadgen/health",
}
SERVICE_HEALTH_PROBES = {
    "egress-active": "huangque-egress-tunnel.service",
    "invite-claims-active": "huangque-invite-reward-claims.service",
    "invite-claims-timer-active": "huangque-invite-reward-claims.timer",
    "leadgen-a-active": "leadgen-A.service",
    "leadgen-b-active": "leadgen-B.service",
    "xiaotan-active": "xiaotan.service",
}


class TransactionError(RuntimeError):
    """A fail-closed transaction error."""


class CrashInjection(BaseException):
    """Test-only process-crash analogue; normal rollback must not catch it."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def _safe_runtime_path(value: str) -> str:
    path = PurePosixPath(str(value or ""))
    if not path.is_absolute() or path == PurePosixPath("/") or ".." in path.parts:
        raise TransactionError("runtime path is unsafe")
    return path.as_posix()


def _mapped(root: Path, runtime_path: str) -> Path:
    path = PurePosixPath(_safe_runtime_path(runtime_path))
    target = Path(root).joinpath(*path.parts[1:])
    root_abs = Path(os.path.abspath(root))
    target_abs = Path(os.path.abspath(target))
    if os.path.commonpath((str(root_abs), str(target_abs))) != str(root_abs):
        raise TransactionError("runtime path escaped the runtime root")
    return target_abs


def _real_parents(root: Path, target: Path, *, create=False):
    root = Path(os.path.abspath(root))
    root.mkdir(parents=True, exist_ok=True)
    root_info = os.lstat(root)
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise TransactionError("runtime root is not a real directory")
    current = root
    for part in target.parent.relative_to(root).parts:
        current /= part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            if not create:
                return
            os.mkdir(current, 0o755)
            info = os.lstat(current)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise TransactionError("runtime parent is a link or non-directory")


def _read_file(root: Path, runtime_path: str):
    target = _mapped(root, runtime_path)
    _real_parents(root, target)
    try:
        info = os.lstat(target)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise TransactionError("runtime target is not a regular file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(target, flags)
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise TransactionError("runtime target changed while opening")
        chunks = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                return b"".join(chunks), opened
            chunks.append(chunk)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path):
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class HostHooks:
    """Narrow, deterministic host mutation surface."""

    def __init__(self, runtime_root="/"):
        self.runtime_root = Path(runtime_root)

    def read(self, runtime_path):
        return _read_file(self.runtime_root, runtime_path)

    def atomic_write(self, runtime_path, data, mode, owner, group):
        target = _mapped(self.runtime_root, runtime_path)
        _real_parents(self.runtime_root, target, create=True)
        try:
            existing = os.lstat(target)
        except FileNotFoundError:
            existing = None
        if existing is not None and (stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode)):
            raise TransactionError("runtime write target is unsafe")
        temporary = target.parent / (".%s.release-%s" % (target.name, uuid.uuid4().hex))
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, 0o600)
        try:
            offset = 0
            while offset < len(data):
                offset += os.write(descriptor, data[offset:])
            os.fchmod(descriptor, mode)
            if os.name == "posix":
                os.fchown(descriptor, pwd.getpwnam(owner).pw_uid, grp.getgrnam(group).gr_gid)
            os.fsync(descriptor)
        except BaseException:
            os.close(descriptor)
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)
            raise
        else:
            os.close(descriptor)
        os.replace(temporary, target)
        _fsync_directory(target.parent)

    def delete(self, runtime_path):
        target = _mapped(self.runtime_root, runtime_path)
        _real_parents(self.runtime_root, target)
        info = os.lstat(target)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise TransactionError("runtime delete target is unsafe")
        os.unlink(target)
        _fsync_directory(target.parent)

    def restart(self, unit):
        if not UNIT_RE.fullmatch(str(unit)):
            raise TransactionError("service unit is invalid")
        subprocess.run([SYSTEMCTL, "restart", unit], check=True, timeout=60,
                       env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"})

    def daemon_reload(self):
        subprocess.run([SYSTEMCTL, "daemon-reload"], check=True, timeout=60,
                       env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"})

    def health(self, probe_id, phase):
        if not PROBE_RE.fullmatch(str(probe_id)) or phase not in {"pre", "post", "rollback"}:
            raise TransactionError("health invocation is invalid")
        if probe_id in SERVICE_HEALTH_PROBES:
            result = subprocess.run(
                [SYSTEMCTL, "is-active", "--quiet", SERVICE_HEALTH_PROBES[probe_id]],
                check=False, timeout=15, env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
            )
            if result.returncode != 0:
                raise TransactionError("named service health is not active")
            return
        url = HTTP_HEALTH_PROBES.get(probe_id)
        if url is None:
            raise TransactionError("named health probe has no v1 write-safe implementation")

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        request = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
        try:
            with opener.open(request, timeout=10) as response:
                status_code = int(response.status)
                response.read(64 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            status_code = int(exc.code)
        except (OSError, ValueError) as exc:
            raise TransactionError("named HTTP health request failed") from exc
        if status_code != 200:
            raise TransactionError("named HTTP health returned unexpected status")


class TransactionExecutor:
    def __init__(self, planner, *, runtime_root="/", state_root=DEFAULT_STATE_ROOT,
                 hooks=None, crash_hook=None, clock=time.time):
        self.planner = planner
        self.runtime_root = Path(runtime_root)
        self.state_root = Path(state_root)
        self.hooks = hooks or HostHooks(runtime_root)
        self.crash_hook = crash_hook or (lambda _point: None)
        self.clock = clock
        self.transactions = self.state_root / "transactions-v1"
        self.journal_path = self.transactions / "active.json"
        self.lock_path = self.state_root / "transaction-v1.lock"

    @contextlib.contextmanager
    def _lock(self):
        self.state_root.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            if os.name == "posix":
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"0")
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            os.close(descriptor)
            raise TransactionError("another release transaction is active") from exc
        try:
            yield
        finally:
            if os.name == "posix":
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            else:
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            os.close(descriptor)

    def _atomic_state_file(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.parent / (".%s-%s" % (path.name, uuid.uuid4().hex))
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
        try:
            raw = _json_bytes(data)
            os.write(descriptor, raw)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        _fsync_directory(path.parent)

    def _load_journal(self):
        try:
            info = os.lstat(self.journal_path)
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise TransactionError("transaction journal is unsafe")
        try:
            data = json.loads(self.journal_path.read_text("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise TransactionError("transaction journal is invalid") from exc
        required = {"schema_version", "transaction_id", "status", "from_commit", "target_commit",
                    "identity", "files", "restart_services", "pre_health_probes",
                    "post_health_probes", "written", "restarted", "original_error"}
        if not isinstance(data, dict) or set(data) != required or data.get("schema_version") != JOURNAL_SCHEMA:
            raise TransactionError("transaction journal fields are invalid")
        if not COMMIT_RE.fullmatch(str(data.get("from_commit"))) or not COMMIT_RE.fullmatch(str(data.get("target_commit"))):
            raise TransactionError("transaction journal commits are invalid")
        identity = data.get("identity")
        if (not isinstance(identity, dict)
                or set(identity) not in (
                    {"environment", "host_id"},
                    {"schema_version", "environment", "host_id", "hostname", "machine_id_sha256"},
                )
                or identity.get("environment") != "test" or not identity.get("host_id")):
            raise TransactionError("transaction journal identity is invalid")
        return data

    def _validate_plan(self, plan, target_commit, reviewed_evidence):
        if not isinstance(plan, dict) or plan.get("status") not in {"planned_read_only", "already_deployed"}:
            raise TransactionError("trusted planner did not return a releasable plan")
        if plan.get("target_commit") != target_commit or not COMMIT_RE.fullmatch(target_commit):
            raise TransactionError("target must be the exact merged main commit")
        if plan["status"] == "already_deployed":
            return
        planned_evidence = plan.get("review_evidence")
        if (not reviewed_evidence or not isinstance(planned_evidence, dict)
                or set(planned_evidence) != set(reviewed_evidence)
                or any(planned_evidence[key].get("merge_base") != value.get("base")
                       or planned_evidence[key].get("head") != value.get("head")
                       or not COMMIT_RE.fullmatch(str(planned_evidence[key].get("merge_commit") or ""))
                       for key, value in reviewed_evidence.items())):
            raise TransactionError("reviewed-head topology evidence is not exact")
        required = {"files", "restart_services", "pre_health_probes", "post_health_probes"}
        if any(key not in plan for key in required):
            raise TransactionError("trusted release plan is incomplete")
        seen = set()
        for item in plan["files"]:
            keys = {"repository_path", "runtime_path", "change", "before", "after", "services",
                    "mode", "owner", "group", "daemon_reload"}
            if not isinstance(item, dict) or set(item) != keys:
                raise TransactionError("release inventory entry is invalid")
            path = _safe_runtime_path(item["runtime_path"])
            if path in seen or item["change"] not in {"write", "delete"}:
                raise TransactionError("release inventory is ambiguous")
            seen.add(path)
            for image in (item["before"], item["after"]):
                if (not isinstance(image, dict) or set(image) != {"state", "sha256"}
                        or image["state"] not in {"file", "absent"}
                        or (image["state"] == "file") != bool(re.fullmatch(r"[0-9a-f]{64}", str(image["sha256"] or "")))):
                    raise TransactionError("release image lock is invalid")
            if type(item["mode"]) is not int or not 0 <= item["mode"] <= 0o7777:
                raise TransactionError("release mode lock is invalid")
            if not item["owner"] or not item["group"]:
                raise TransactionError("release ownership lock is invalid")
        allowed = set(self.planner.catalog.allowed_units)
        if any(unit not in allowed for unit in plan["restart_services"]):
            raise TransactionError("release requests a non-allowlisted service")
        allowed_probes = set(self.planner.catalog.health_probes)
        for key in ("pre_health_probes", "post_health_probes"):
            if any(probe not in allowed_probes for probe in plan[key]):
                raise TransactionError("release requests an unknown health probe")

    def _observe(self, item, which):
        record = self.hooks.read(item["runtime_path"])
        locked = item[which]
        if record is None:
            actual = {"state": "absent", "sha256": None}
            metadata = None
        else:
            raw, info = record
            actual = {"state": "file", "sha256": _sha256(raw)}
            metadata = {"mode": stat.S_IMODE(info.st_mode), "uid": info.st_uid, "gid": info.st_gid}
        if actual != locked:
            raise TransactionError("runtime image differs from locked %s" % which)
        return record, metadata

    def _run_health(self, probes, phase):
        for probe in probes:
            self.hooks.health(probe, phase)

    def _identity(self):
        verify = getattr(self.planner, "verify_identity", None)
        if verify is None:
            return {"environment": "test", "host_id": "test-01"}
        identity = verify()
        if (not isinstance(identity, dict)
                or set(identity) != {"schema_version", "environment", "host_id", "hostname", "machine_id_sha256"}):
            raise TransactionError("trusted planner returned an incomplete host identity")
        return identity

    def _backup(self, plan, transaction_id):
        backup_root = self.transactions / transaction_id / "backups"
        backup_root.mkdir(parents=True, exist_ok=False)
        files = []
        for index, item in enumerate(plan["files"]):
            record, metadata = self._observe(item, "before")
            backup_name = None
            if record is not None:
                raw, _info = record
                backup_name = "%04d.bin" % index
                backup_path = backup_root / backup_name
                descriptor = os.open(
                    backup_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                    0o600,
                )
                try:
                    offset = 0
                    while offset < len(raw):
                        offset += os.write(descriptor, raw[offset:])
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                backup_hash = _sha256(backup_path.read_bytes())
                if backup_hash != item["before"]["sha256"]:
                    raise TransactionError("backup integrity verification failed")
            files.append({"plan": item, "backup": backup_name, "metadata": metadata})
        _fsync_directory(backup_root)
        return files

    def _install(self, journal):
        target = journal["target_commit"]
        for item_record in journal["files"]:
            item = item_record["plan"]
            if item["change"] == "delete":
                self.hooks.delete(item["runtime_path"])
            else:
                raw = self.planner.repo.file_at(target, item["repository_path"])
                if raw is None or _sha256(raw) != item["after"]["sha256"]:
                    raise TransactionError("target Git blob differs from locked postimage")
                self.hooks.atomic_write(item["runtime_path"], raw, item["mode"], item["owner"], item["group"])
            self._observe(item, "after")
            journal["written"].append(item["runtime_path"])
            self._atomic_state_file(self.journal_path, journal)
            self.crash_hook("after-write")

    def _restart(self, journal):
        if any(record["plan"]["daemon_reload"] for record in journal["files"]):
            self.hooks.daemon_reload()
        for unit in journal["restart_services"]:
            self.hooks.restart(unit)
            journal["restarted"].append(unit)
            self._atomic_state_file(self.journal_path, journal)
            self.crash_hook("after-restart")

    def _restore(self, journal):
        errors = []
        backup_root = self.transactions / journal["transaction_id"] / "backups"
        for record in reversed(journal["files"]):
            item = record["plan"]
            try:
                if item["before"]["state"] == "absent":
                    current = self.hooks.read(item["runtime_path"])
                    if current is not None:
                        self.hooks.delete(item["runtime_path"])
                else:
                    backup = backup_root / str(record["backup"])
                    raw = backup.read_bytes()
                    if _sha256(raw) != item["before"]["sha256"]:
                        raise TransactionError("backup post-read integrity failed")
                    metadata = record["metadata"]
                    owner = pwd.getpwuid(metadata["uid"]).pw_name if os.name == "posix" else item["owner"]
                    group = grp.getgrgid(metadata["gid"]).gr_name if os.name == "posix" else item["group"]
                    self.hooks.atomic_write(item["runtime_path"], raw, metadata["mode"], owner, group)
                self._observe(item, "before")
            except Exception as exc:
                errors.append("file:%s:%s" % (item["runtime_path"], type(exc).__name__))
        try:
            if any(record["plan"]["daemon_reload"] for record in journal["files"]):
                self.hooks.daemon_reload()
        except Exception as exc:
            errors.append("daemon-reload:%s" % type(exc).__name__)
        for unit in journal["restart_services"]:
            try:
                self.hooks.restart(unit)
            except Exception as exc:
                errors.append("service:%s:%s" % (unit, type(exc).__name__))
        try:
            self._run_health(journal["pre_health_probes"], "rollback")
        except Exception as exc:
            errors.append("health:%s" % type(exc).__name__)
        if errors:
            journal["status"] = "rollback_failed"
            self._atomic_state_file(self.journal_path, journal)
            raise TransactionError("rollback failed: " + ", ".join(errors))

    def _new_ledger(self, journal):
        state = self.planner.load_state()
        if state["deployed_main_commit"] != journal["from_commit"]:
            raise TransactionError("deployment ledger changed during apply")
        target = journal["target_commit"]
        hashes, paths, metadata = self.planner._expected_runtime(target)
        accepted = self.planner.collect_impact_index(target) if hasattr(self.planner, "collect_impact_index") else None
        if accepted is None:
            accepted = state["accepted_impacts"]
        state.update({
            "deployed_main_commit": target,
            "runtime_hashes": hashes,
            "repository_paths": paths,
            "runtime_metadata": metadata,
            "managed_runtime_paths": sorted(hashes),
            "accepted_impacts": accepted,
            "last_release_id": journal["transaction_id"],
            "last_successful_release": {
                "transaction_id": journal["transaction_id"],
                "from_commit": journal["from_commit"],
                "target_commit": target,
                "completed_at": int(self.clock()),
            },
        })
        return state

    def _finish(self, journal):
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.journal_path)
        transaction_dir = self.transactions / journal["transaction_id"]
        shutil.rmtree(transaction_dir)
        _fsync_directory(self.transactions)

    def apply(self, target_commit, reviewed_evidence):
        with self._lock():
            if self._load_journal() is not None:
                raise TransactionError("unfinished transaction requires recover")
            plan = self.planner.build_plan(target_commit, reviewed_evidence)
            self._validate_plan(plan, target_commit, reviewed_evidence)
            if plan["status"] == "already_deployed":
                return {"ok": True, "status": "already_deployed", "target_commit": target_commit}
            identity = self._identity()
            if (identity["environment"] != plan["environment"]
                    or identity["host_id"] != plan["host_id"]):
                raise TransactionError("release plan identity differs from the host")
            self._run_health(plan["pre_health_probes"], "pre")
            transaction_id = "release-%s-%s" % (target_commit[:12], uuid.uuid4().hex[:12])
            files = self._backup(plan, transaction_id)
            journal = {
                "schema_version": JOURNAL_SCHEMA,
                "transaction_id": transaction_id,
                "status": "applying",
                "from_commit": plan["from_commit"],
                "target_commit": target_commit,
                "identity": identity,
                "files": files,
                "restart_services": list(plan["restart_services"]),
                "pre_health_probes": list(plan["pre_health_probes"]),
                "post_health_probes": list(plan["post_health_probes"]),
                "written": [], "restarted": [], "original_error": None,
            }
            self._atomic_state_file(self.journal_path, journal)
            if self._identity() != identity:
                raise TransactionError("release identity changed before installation")
            self.crash_hook("after-backup")
            try:
                self._install(journal)
                self._restart(journal)
                self._run_health(journal["post_health_probes"], "post")
                if self._identity() != identity:
                    raise TransactionError("release identity changed during apply")
                for record in journal["files"]:
                    self._observe(record["plan"], "after")
                if not self.planner.verify_target_inventory(target_commit):
                    raise TransactionError("complete runtime inventory differs from target commit")
                state = self._new_ledger(journal)
                self.planner._write_state(state)
                journal["status"] = "committed"
                self._atomic_state_file(self.journal_path, journal)
                self.crash_hook("after-ledger")
            except Exception as exc:
                journal["original_error"] = type(exc).__name__
                self._atomic_state_file(self.journal_path, journal)
                try:
                    self._restore(journal)
                except Exception as rollback_exc:
                    raise TransactionError("release failed (%s); %s" % (type(exc).__name__, rollback_exc)) from exc
                self._finish(journal)
                raise TransactionError("release failed and rolled back: %s" % type(exc).__name__) from exc
            self._finish(journal)
            return {"ok": True, "status": "deployed", "transaction_id": transaction_id,
                    "target_commit": target_commit, "files": len(files)}

    def recover(self):
        with self._lock():
            journal = self._load_journal()
            if journal is None:
                return {"ok": True, "status": "nothing_to_recover"}
            state = self.planner.load_state()
            if self._identity() != journal["identity"]:
                raise TransactionError("release identity differs from the active transaction")
            if state["deployed_main_commit"] == journal["target_commit"]:
                self._run_health(journal["post_health_probes"], "post")
                self._finish(journal)
                return {"ok": True, "status": "committed_recovered",
                        "transaction_id": journal["transaction_id"]}
            if state["deployed_main_commit"] != journal["from_commit"]:
                raise TransactionError("ledger commit matches neither side of active transaction")
            self._restore(journal)
            self._finish(journal)
            return {"ok": True, "status": "rolled_back_recovered",
                    "transaction_id": journal["transaction_id"]}


class TrustedPlannerAdapter:
    """Expose only the immutable planner operations required by apply/recover."""

    def __init__(self, module, engine):
        self.module = module
        self.engine = engine
        self.repo = engine.repo
        self.catalog = engine.catalog

    def build_plan(self, target, evidence):
        return self.engine.build_plan(target, evidence)

    def verify_identity(self):
        return self.engine.verify_identity()

    def load_state(self):
        return self.engine.load_state()

    def _expected_runtime(self, target):
        return self.engine._expected_runtime(target)

    def _write_state(self, state):
        return self.engine._write_state(state)

    def collect_impact_index(self, target):
        return self.module.collect_impact_index(self.repo, self.catalog, target)

    def verify_target_inventory(self, target):
        hashes, _paths, metadata = self.engine._expected_runtime(target)
        mismatches = [
            path for path, expected in hashes.items()
            if not self.engine._runtime_matches(path, expected, metadata[path])
        ]
        unexpected = self.engine._inventory_drift(set(hashes))
        return not mismatches and not unexpected


def _locked_regular(path, expected_sha256, label):
    path = Path(path)
    try:
        info = os.lstat(path)
    except FileNotFoundError as exc:
        raise TransactionError("%s is missing" % label) from exc
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
            or os.name == "posix" and (info.st_uid != 0 or info.st_mode & 0o022)):
        raise TransactionError("%s is not a root-owned immutable regular file" % label)
    raw = path.read_bytes()
    if _sha256(raw) != expected_sha256:
        raise TransactionError("%s SHA-256 differs from bootstrap" % label)
    return raw


def _load_production_planner():
    try:
        bootstrap = json.loads(Path(TRANSACTION_BOOTSTRAP).read_text("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TransactionError("transaction bootstrap is unavailable or invalid") from exc
    required = {"schema_version", "source_root", "transaction_entrypoint", "transaction_sha256",
                "phase_one_entrypoint", "phase_one_sha256", "state_root"}
    if (not isinstance(bootstrap, dict) or set(bootstrap) != required
            or bootstrap.get("schema_version") != 1
            or bootstrap.get("source_root") != DEFAULT_SOURCE_ROOT
            or bootstrap.get("state_root") != DEFAULT_STATE_ROOT
            or bootstrap.get("transaction_entrypoint") != TRANSACTION_ENTRYPOINT
            or bootstrap.get("phase_one_entrypoint") != PHASE_ONE_ENTRYPOINT
            or any(not re.fullmatch(r"[0-9a-f]{64}", str(bootstrap.get(key) or ""))
                   for key in ("transaction_sha256", "phase_one_sha256"))):
        raise TransactionError("transaction bootstrap fields are invalid")
    _locked_regular(TRANSACTION_ENTRYPOINT, bootstrap["transaction_sha256"], "transaction entrypoint")
    _locked_regular(PHASE_ONE_ENTRYPOINT, bootstrap["phase_one_sha256"], "phase-one entrypoint")
    spec = importlib.util.spec_from_file_location("huangque_release_phase_one", PHASE_ONE_ENTRYPOINT)
    if spec is None or spec.loader is None:
        raise TransactionError("phase-one planner cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    catalog = module.RuntimeCatalog.load(Path(DEFAULT_SOURCE_ROOT), module.DEFAULT_CATALOG)
    engine = module.ReleaseEngine(
        DEFAULT_SOURCE_ROOT, "/", catalog,
        identity_path=module.DEFAULT_IDENTITY_FILE,
        state_root=DEFAULT_STATE_ROOT,
    )
    return module, TrustedPlannerAdapter(module, engine)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Huangque transactional test release v1")
    sub = parser.add_subparsers(dest="command", required=True)
    apply_parser = sub.add_parser("apply")
    apply_parser.add_argument("--target-commit", required=True)
    apply_parser.add_argument("--reviewed-head", action="append", required=True)
    sub.add_parser("recover")
    args = parser.parse_args(argv)
    if os.name != "posix" or os.geteuid() != 0 or os.path.realpath(sys.executable) != "/usr/bin/python3":
        raise TransactionError("transaction executor requires the fixed root Python runtime")
    module, planner = _load_production_planner()
    executor = TransactionExecutor(planner)
    if args.command == "apply":
        evidence = module._parse_reviewed_head_arguments(args.reviewed_head)
        output = executor.apply(args.target_commit, evidence)
    else:
        output = executor.recover()
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        main()
    except TransactionError as exc:
        print("release error: %s" % exc, file=sys.stderr)
        raise SystemExit(2)
