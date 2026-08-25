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
import json
import http.client
import os
import re
import socket
import ssl
import shutil
import stat
import subprocess
import sys
import time
import types
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
TRANSACTION_LAUNCHER = "/usr/local/sbin/huangque-release-test-transaction"
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
TRANSACTION_ID_RE = re.compile(r"^release-[0-9a-f]{12}-[0-9a-f]{12}$")
BACKUP_NAME_RE = re.compile(r"^[0-9]{4}\.bin$")
TYPE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,127}$")
OWNER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
SITE_LOOPBACK_ADDRESS = ("127.0.0.1", 443)
SITE_LOOPBACK_HOST = "yuelei.huangquechuanmei.com"
SITE_LOOPBACK_PATH = "/workbench/private-domain-video.html"
SITE_LOOPBACK_REPOSITORY_PATH = "site/workbench/private-domain-video.html"
SITE_LOOPBACK_RUNTIME_PATH = "/var/www/huangquechuanmei/workbench/private-domain-video.html"
SITE_LOOPBACK_MAX_BYTES = 2 * 1024 * 1024


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

    @staticmethod
    def owner_group(info):
        if os.name != "posix":
            raise TransactionError("runtime ownership is supported only on Linux")
        return pwd.getpwuid(info.st_uid).pw_name, grp.getgrgid(info.st_gid).gr_name

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

    def health(self, probe_id, phase, expected_response=None):
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
        if probe_id == "site-loopback":
            if (not isinstance(expected_response, dict)
                    or set(expected_response) != {"sha256", "length"}
                    or not re.fullmatch(r"[0-9a-f]{64}", str(expected_response["sha256"]))
                    or type(expected_response["length"]) is not int
                    or not 0 <= expected_response["length"] <= SITE_LOOPBACK_MAX_BYTES):
                raise TransactionError("site loopback response lock is invalid")
            context = ssl.create_default_context(cafile="/etc/ssl/certs/ca-certificates.crt")
            raw_socket = socket.create_connection(SITE_LOOPBACK_ADDRESS, timeout=10)
            connection = None
            try:
                connection = context.wrap_socket(raw_socket, server_hostname=SITE_LOOPBACK_HOST)
                request = (
                    "GET %s HTTP/1.1\r\nHost: %s\r\nAccept: text/html\r\n"
                    "Connection: close\r\nUser-Agent: huangque-release-v1\r\n\r\n"
                    % (SITE_LOOPBACK_PATH, SITE_LOOPBACK_HOST)
                ).encode("ascii")
                connection.sendall(request)
                response = http.client.HTTPResponse(connection)
                response.begin()
                content_type = response.getheader("Content-Type", "").lower()
                nosniff = response.getheader("X-Content-Type-Options", "").lower()
                body = response.read(SITE_LOOPBACK_MAX_BYTES + 1)
            except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
                raise TransactionError("site loopback health request failed") from exc
            finally:
                if connection is not None:
                    with contextlib.suppress(Exception):
                        connection.close()
                with contextlib.suppress(Exception):
                    raw_socket.close()
            if (response.status != 200 or not content_type.startswith("text/html")
                    or nosniff != "nosniff" or len(body) > SITE_LOOPBACK_MAX_BYTES
                    or len(body) != expected_response["length"]
                    or _sha256(body) != expected_response["sha256"]):
                raise TransactionError("site loopback response contract is invalid")
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
                 hooks=None, crash_hook=None, clock=time.time, enforce_root_paths=True):
        self.planner = planner
        self.runtime_root = Path(runtime_root)
        self.state_root = Path(state_root)
        self.hooks = hooks or HostHooks(runtime_root)
        self.crash_hook = crash_hook or (lambda _point: None)
        self.clock = clock
        self.enforce_root_paths = bool(enforce_root_paths)
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
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.parent / (".%s-%s" % (path.name, uuid.uuid4().hex))
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
        try:
            raw = _json_bytes(data)
            offset = 0
            while offset < len(raw):
                offset += os.write(descriptor, raw[offset:])
            os.fsync(descriptor)
        except BaseException:
            os.close(descriptor)
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)
            raise
        else:
            os.close(descriptor)
        os.replace(temporary, path)
        _fsync_directory(path.parent)

    def _load_journal(self):
        try:
            info = os.lstat(self.journal_path)
        except FileNotFoundError:
            return None
        if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
                or self.enforce_root_paths and os.name == "posix"
                and (info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600)):
            raise TransactionError("transaction journal is unsafe")
        try:
            data = json.loads(self._read_backup(self.journal_path).decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise TransactionError("transaction journal is invalid") from exc
        required = {"schema_version", "transaction_id", "status", "from_commit", "target_commit",
                    "identity", "reviewed_evidence", "files", "restart_services",
                    "pre_health_probes", "post_health_probes", "written", "restarted",
                    "original_error"}
        if not isinstance(data, dict) or set(data) != required or data.get("schema_version") != JOURNAL_SCHEMA:
            raise TransactionError("transaction journal fields are invalid")
        if not COMMIT_RE.fullmatch(str(data.get("from_commit"))) or not COMMIT_RE.fullmatch(str(data.get("target_commit"))):
            raise TransactionError("transaction journal commits are invalid")
        if (not TRANSACTION_ID_RE.fullmatch(str(data.get("transaction_id") or ""))
                or data.get("status") not in {
                    "backing_up", "applying", "committed", "rollback_failed", "cleaning",
                }):
            raise TransactionError("transaction journal state is invalid")
        identity = data.get("identity")
        if (not isinstance(identity, dict)
                or set(identity) != {
                    "schema_version", "environment", "host_id", "hostname", "machine_id_sha256",
                }
                or identity.get("schema_version") != 1
                or identity.get("environment") != "test" or not identity.get("host_id")):
            raise TransactionError("transaction journal identity is invalid")
        evidence = data.get("reviewed_evidence")
        if (not isinstance(evidence, dict) or not evidence
                or any(not isinstance(value, dict) or set(value) != {"base", "head"}
                       or not COMMIT_RE.fullmatch(str(value.get("base") or ""))
                       or not COMMIT_RE.fullmatch(str(value.get("head") or ""))
                       for value in evidence.values())):
            raise TransactionError("transaction review evidence is invalid")
        if (not isinstance(data.get("files"), list) or not data["files"]
                or not isinstance(data.get("restart_services"), list)
                or not isinstance(data.get("pre_health_probes"), list)
                or not isinstance(data.get("post_health_probes"), list)
                or not isinstance(data.get("written"), list)
                or not isinstance(data.get("restarted"), list)
                or data.get("original_error") is not None
                   and not TYPE_NAME_RE.fullmatch(str(data.get("original_error")))):
            raise TransactionError("transaction journal collections are invalid")
        return data

    def _bind_journal(self, journal):
        contract = self.planner.trusted_transaction_contract(
            journal["from_commit"], journal["target_commit"], journal["reviewed_evidence"],
        )
        if (journal["restart_services"] != contract["restart_services"]
                or journal["pre_health_probes"] != contract["pre_health_probes"]
                or journal["post_health_probes"] != contract["post_health_probes"]
                or len(journal["files"]) != len(contract["files"])):
            raise TransactionError("transaction journal differs from the trusted release contract")
        runtime_paths = []
        expected_backups = set()
        for index, (record, expected) in enumerate(zip(journal["files"], contract["files"])):
            if not isinstance(record, dict) or set(record) != {"plan", "backup", "metadata"}:
                raise TransactionError("transaction file record is invalid")
            if record["plan"] != expected:
                raise TransactionError("transaction file plan differs from trusted Git/catalog data")
            runtime_path = _safe_runtime_path(expected["runtime_path"])
            runtime_paths.append(runtime_path)
            metadata = record["metadata"]
            if (not isinstance(metadata, dict) or set(metadata) != {"mode", "owner", "group"}
                    or type(metadata["mode"]) is not int or not 0 <= metadata["mode"] <= 0o7777
                    or not OWNER_RE.fullmatch(str(metadata["owner"]))
                    or not OWNER_RE.fullmatch(str(metadata["group"]))):
                raise TransactionError("transaction backup metadata is invalid")
            if metadata != expected["before_metadata"]:
                raise TransactionError("transaction backup metadata differs from trusted preimage")
            expected_name = "%04d.bin" % index if expected["before"]["state"] == "file" else None
            if record["backup"] != expected_name:
                raise TransactionError("transaction backup name is invalid")
            if expected_name is not None:
                expected_backups.add(expected_name)
        if len(set(runtime_paths)) != len(runtime_paths):
            raise TransactionError("transaction runtime paths are duplicated")
        if (len(set(journal["restart_services"])) != len(journal["restart_services"])
                or any(unit not in self.planner.catalog.allowed_units
                       or not UNIT_RE.fullmatch(unit) for unit in journal["restart_services"])
                or len(set(journal["pre_health_probes"])) != len(journal["pre_health_probes"])
                or len(set(journal["post_health_probes"])) != len(journal["post_health_probes"])
                or any(probe not in self.planner.catalog.health_probes for probe in
                       journal["pre_health_probes"] + journal["post_health_probes"])
                or len(set(journal["written"])) != len(journal["written"])
                or not set(journal["written"]).issubset(runtime_paths)
                or len(set(journal["restarted"])) != len(journal["restarted"])
                or not set(journal["restarted"]).issubset(journal["restart_services"])):
            raise TransactionError("transaction journal contains an invalid operation set")
        if (journal["status"] == "backing_up"
                and (journal["written"] or journal["restarted"] or journal["original_error"])
                or journal["status"] == "committed"
                and (journal["written"] != runtime_paths
                     or journal["restarted"] != journal["restart_services"]
                     or journal["original_error"] is not None)
                or journal["status"] == "rollback_failed"
                and journal["original_error"] is None):
            raise TransactionError("transaction journal status contradicts its operations")
        transaction_dir = self.transactions / journal["transaction_id"]
        if transaction_dir.parent != self.transactions:
            raise TransactionError("transaction directory escaped its root")
        backup_root = transaction_dir / "backups"
        if transaction_dir.exists():
            info = os.lstat(transaction_dir)
            if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
                    or self.enforce_root_paths and os.name == "posix"
                    and (info.st_uid != 0 or info.st_mode & 0o077)):
                raise TransactionError("transaction backup path is unsafe")
            if not backup_root.exists():
                if journal["status"] not in {"backing_up", "cleaning"}:
                    raise TransactionError("transaction backup directory is missing")
                return contract
            info = os.lstat(backup_root)
            if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
                    or self.enforce_root_paths and os.name == "posix"
                    and (info.st_uid != 0 or info.st_mode & 0o077)):
                raise TransactionError("transaction backup path is unsafe")
            actual = set()
            for entry in os.scandir(backup_root):
                if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                    raise TransactionError("transaction backup directory contains an unsafe entry")
                entry_info = entry.stat(follow_symlinks=False)
                if (self.enforce_root_paths and os.name == "posix"
                        and (entry_info.st_uid != 0 or stat.S_IMODE(entry_info.st_mode) != 0o600)):
                    raise TransactionError("transaction backup file is not root-private")
                if not BACKUP_NAME_RE.fullmatch(entry.name):
                    raise TransactionError("transaction backup basename is invalid")
                actual.add(entry.name)
            if not actual.issubset(expected_backups):
                raise TransactionError("transaction backup set is invalid")
            if journal["status"] not in {"backing_up", "cleaning"} and actual != expected_backups:
                raise TransactionError("transaction backup set is incomplete")
        elif journal["status"] not in {"backing_up", "cleaning"}:
            raise TransactionError("transaction backup directory is missing")
        return contract

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
        required = {
            "files", "restart_services", "pre_health_probes", "post_health_probes",
            "required_free_bytes",
        }
        if any(key not in plan for key in required):
            raise TransactionError("trusted release plan is incomplete")
        if type(plan["required_free_bytes"]) is not int or plan["required_free_bytes"] < 0:
            raise TransactionError("trusted release disk-space contract is invalid")
        seen = set()
        for item in plan["files"]:
            keys = {"repository_path", "runtime_path", "change", "before", "after", "services",
                    "before_metadata", "mode", "owner", "group", "daemon_reload"}
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
            before_metadata = item["before_metadata"]
            if (not isinstance(before_metadata, dict)
                    or set(before_metadata) != {"mode", "owner", "group"}
                    or type(before_metadata["mode"]) is not int
                    or not 0 <= before_metadata["mode"] <= 0o7777
                    or not OWNER_RE.fullmatch(str(before_metadata["owner"]))
                    or not OWNER_RE.fullmatch(str(before_metadata["group"]))):
                raise TransactionError("release preimage metadata lock is invalid")
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
            owner, group = self.hooks.owner_group(info)
            metadata = {"mode": stat.S_IMODE(info.st_mode), "owner": owner, "group": group}
        if actual != locked:
            raise TransactionError("runtime image differs from locked %s" % which)
        expected_metadata = item["before_metadata"] if which == "before" else {
            "mode": item["mode"], "owner": item["owner"], "group": item["group"],
        }
        if metadata is not None and metadata != expected_metadata:
            raise TransactionError("runtime metadata differs from locked %s" % which)
        return record, metadata

    def _actual_image(self, item):
        record = self.hooks.read(item["runtime_path"])
        if record is None:
            return {"state": "absent", "sha256": None}, None
        raw, info = record
        owner, group = self.hooks.owner_group(info)
        return {"state": "file", "sha256": _sha256(raw)}, {
            "mode": stat.S_IMODE(info.st_mode), "owner": owner, "group": group,
        }

    def _verify_all(self, records, which):
        for record in records:
            self._observe(record["plan"], which)

    def _site_loopback_response_lock(self, files, which):
        matches = [
            record["plan"] if "plan" in record else record
            for record in files
            if (record["plan"] if "plan" in record else record).get("repository_path")
            == SITE_LOOPBACK_REPOSITORY_PATH
            and (record["plan"] if "plan" in record else record).get("runtime_path")
            == SITE_LOOPBACK_RUNTIME_PATH
        ]
        if len(matches) != 1:
            raise TransactionError("site loopback requires one exact managed page")
        item = matches[0]
        locked = item[which]
        if locked["state"] != "file":
            raise TransactionError("site loopback managed page must be a locked file")
        record, _metadata = self._observe(item, which)
        if record is None:
            raise TransactionError("site loopback managed page is absent")
        raw, _info = record
        if len(raw) > SITE_LOOPBACK_MAX_BYTES:
            raise TransactionError("site loopback managed page exceeds response limit")
        return {"sha256": locked["sha256"], "length": len(raw)}

    def _run_health(self, probes, phase, files, which):
        site_lock = None
        if "site-loopback" in probes:
            site_lock = self._site_loopback_response_lock(files, which)
        for probe in probes:
            self.hooks.health(probe, phase, site_lock if probe == "site-loopback" else None)

    def _identity(self):
        verify = getattr(self.planner, "verify_identity", None)
        if verify is None:
            return {"environment": "test", "host_id": "test-01"}
        identity = verify()
        if (not isinstance(identity, dict)
                or set(identity) != {"schema_version", "environment", "host_id", "hostname", "machine_id_sha256"}):
            raise TransactionError("trusted planner returned an incomplete host identity")
        return identity

    def _prepare_backup_records(self, plan):
        records = []
        for index, item in enumerate(plan["files"]):
            _record, metadata = self._observe(item, "before")
            records.append({
                "plan": item,
                "backup": "%04d.bin" % index if item["before"]["state"] == "file" else None,
                "metadata": metadata or item["before_metadata"],
            })
        return records

    def _write_backups(self, journal):
        transaction_id = journal["transaction_id"]
        backup_root = self.transactions / transaction_id / "backups"
        backup_root.parent.mkdir(mode=0o700)
        backup_root.mkdir(mode=0o700)
        for item_record in journal["files"]:
            item = item_record["plan"]
            record, _metadata = self._observe(item, "before")
            if record is not None:
                raw, _info = record
                backup_name = item_record["backup"]
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
                backup_hash = _sha256(self._read_backup(backup_path))
                if backup_hash != item["before"]["sha256"]:
                    raise TransactionError("backup integrity verification failed")
            self.crash_hook("during-backup")
        _fsync_directory(backup_root)

    @staticmethod
    def _read_backup(path):
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise TransactionError("backup is not a regular file")
            chunks = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            os.close(descriptor)
        current = os.lstat(path)
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise TransactionError("backup changed while it was read")
        return b"".join(chunks)

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
            self.crash_hook("after-write-before-journal")
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
        classifications = []
        for record in journal["files"]:
            item = record["plan"]
            try:
                actual, metadata = self._actual_image(item)
                before_metadata = record["metadata"] if item["before"]["state"] == "file" else None
                after_metadata = {
                    "mode": item["mode"], "owner": item["owner"], "group": item["group"],
                } if item["after"]["state"] == "file" else None
                if actual == item["before"] and metadata == before_metadata:
                    classifications.append("before")
                elif actual == item["after"] and metadata == after_metadata:
                    classifications.append("after")
                else:
                    classifications.append("third_party")
            except Exception:
                classifications.append("third_party")
        if "third_party" in classifications:
            journal["status"] = "rollback_failed"
            journal["original_error"] = journal["original_error"] or "ThirdPartyState"
            self._atomic_state_file(self.journal_path, journal)
            raise TransactionError("rollback refused to overwrite a neither-before-nor-after state")
        for index in reversed(range(len(journal["files"]))):
            record = journal["files"][index]
            item = record["plan"]
            if classifications[index] == "before":
                continue
            try:
                if item["before"]["state"] == "absent":
                    current = self.hooks.read(item["runtime_path"])
                    if current is not None:
                        self.hooks.delete(item["runtime_path"])
                else:
                    backup = backup_root / str(record["backup"])
                    raw = self._read_backup(backup)
                    if _sha256(raw) != item["before"]["sha256"]:
                        raise TransactionError("backup post-read integrity failed")
                    metadata = record["metadata"]
                    self.hooks.atomic_write(
                        item["runtime_path"], raw, metadata["mode"], metadata["owner"], metadata["group"],
                    )
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
            self._run_health(journal["pre_health_probes"], "rollback", journal["files"], "before")
        except Exception as exc:
            errors.append("health:%s" % type(exc).__name__)
        if errors:
            journal["status"] = "rollback_failed"
            journal["original_error"] = journal["original_error"] or "RollbackError"
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
        journal["status"] = "cleaning"
        self._atomic_state_file(self.journal_path, journal)
        transaction_dir = self.transactions / journal["transaction_id"]
        if transaction_dir.exists():
            shutil.rmtree(transaction_dir)
        _fsync_directory(self.transactions)
        self.crash_hook("after-backup-cleanup")
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.journal_path)
        _fsync_directory(self.transactions)

    def _clean_orphans(self, active_transaction_id=None):
        self.transactions.mkdir(parents=True, exist_ok=True, mode=0o700)
        for entry in list(os.scandir(self.transactions)):
            if entry.name == "active.json" or entry.name == active_transaction_id:
                continue
            if entry.name.startswith(".active.json-") and entry.is_file(follow_symlinks=False):
                info = entry.stat(follow_symlinks=False)
                if (self.enforce_root_paths and os.name == "posix"
                        and (info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600)):
                    raise TransactionError("transaction temporary file is not root-private")
                os.unlink(entry.path)
                continue
            if (not TRANSACTION_ID_RE.fullmatch(entry.name) or entry.is_symlink()
                    or not entry.is_dir(follow_symlinks=False)):
                raise TransactionError("transaction root contains an untrusted orphan")
            info = entry.stat(follow_symlinks=False)
            if (self.enforce_root_paths and os.name == "posix"
                    and (info.st_uid != 0 or info.st_mode & 0o077)):
                raise TransactionError("transaction orphan is not root-private")
            shutil.rmtree(entry.path)
        _fsync_directory(self.transactions)

    def apply(self, target_commit, reviewed_evidence):
        with self._lock():
            existing = self._load_journal()
            if existing is not None:
                raise TransactionError("unfinished transaction requires recover")
            self._clean_orphans()
            plan = self.planner.build_plan(target_commit, reviewed_evidence)
            self._validate_plan(plan, target_commit, reviewed_evidence)
            if plan["status"] == "already_deployed":
                return {"ok": True, "status": "already_deployed", "target_commit": target_commit}
            if shutil.disk_usage(self.state_root).free < plan["required_free_bytes"]:
                raise TransactionError("transaction storage does not satisfy the complete plan reserve")
            identity = self._identity()
            if (identity["environment"] != plan["environment"]
                    or identity["host_id"] != plan["host_id"]):
                raise TransactionError("release plan identity differs from the host")
            self._run_health(plan["pre_health_probes"], "pre", plan["files"], "before")
            transaction_id = "release-%s-%s" % (target_commit[:12], uuid.uuid4().hex[:12])
            files = self._prepare_backup_records(plan)
            journal = {
                "schema_version": JOURNAL_SCHEMA,
                "transaction_id": transaction_id,
                "status": "backing_up",
                "from_commit": plan["from_commit"],
                "target_commit": target_commit,
                "identity": identity,
                "reviewed_evidence": reviewed_evidence,
                "files": files,
                "restart_services": list(plan["restart_services"]),
                "pre_health_probes": list(plan["pre_health_probes"]),
                "post_health_probes": list(plan["post_health_probes"]),
                "written": [], "restarted": [], "original_error": None,
            }
            self._atomic_state_file(self.journal_path, journal)
            self._write_backups(journal)
            self._bind_journal(journal)
            if self._identity() != identity:
                raise TransactionError("release identity changed before installation")
            self._verify_all(journal["files"], "before")
            journal["status"] = "applying"
            self._atomic_state_file(self.journal_path, journal)
            self.crash_hook("after-backup")
            try:
                self._install(journal)
                self._restart(journal)
                self._run_health(journal["post_health_probes"], "post", journal["files"], "after")
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
                self._clean_orphans()
                return {"ok": True, "status": "nothing_to_recover"}
            self._bind_journal(journal)
            self._clean_orphans(journal["transaction_id"])
            state = self.planner.load_state()
            if self._identity() != journal["identity"]:
                raise TransactionError("release identity differs from the active transaction")
            if state["deployed_main_commit"] == journal["target_commit"]:
                self._verify_all(journal["files"], "after")
                if not self.planner.verify_target_inventory(journal["target_commit"]):
                    raise TransactionError("committed recovery target inventory has drifted")
                self.planner.verify_services(journal["restart_services"])
                self._run_health(journal["post_health_probes"], "post", journal["files"], "after")
                self._finish(journal)
                return {"ok": True, "status": "committed_recovered",
                        "transaction_id": journal["transaction_id"]}
            if state["deployed_main_commit"] != journal["from_commit"]:
                raise TransactionError("ledger commit matches neither side of active transaction")
            if journal["status"] in {"backing_up", "cleaning"}:
                self._verify_all(journal["files"], "before")
                self._run_health(journal["pre_health_probes"], "rollback", journal["files"], "before")
                self._finish(journal)
                return {"ok": True, "status": "prewrite_recovered",
                        "transaction_id": journal["transaction_id"]}
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
        plan = self.engine.build_plan(target, evidence)
        if plan.get("status") == "planned_read_only":
            state = self.engine.load_state()
            for item in plan["files"]:
                metadata = state["runtime_metadata"].get(item["runtime_path"])
                expected = {"mode": item["mode"], "owner": item["owner"], "group": item["group"]}
                if item["before"]["state"] == "file" and metadata != expected:
                    raise TransactionError("preimage metadata differs from immutable catalog mapping")
                item["before_metadata"] = dict(expected if metadata is None else metadata)
        return plan

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

    def verify_services(self, services):
        self.engine._verify_service_preconditions(list(services))

    def trusted_transaction_contract(self, older, target, evidence):
        self.repo.require_commit(older)
        self.repo.require_commit(target)
        self.repo.require_ancestor(older, target)
        self.module.validate_catalog_coverage(self.repo, self.catalog, target)
        collected = self.module.collect_release_impact(self.repo, self.catalog, older, target)
        impacts = collected["impacts"]
        self.module.verify_review_evidence(
            self.repo, self.catalog, older, target, impacts, evidence,
        )
        services = sorted({unit for impact in impacts for unit in impact["restart_services"]})
        pre_health = sorted({probe for impact in impacts for probe in impact["pre_health_checks"]})
        post_health = sorted({probe for impact in impacts for probe in impact["health_checks"]})
        files = []
        seen = set()
        for status_value, repository_path in collected["changed_paths"]:
            before = self.repo.file_at(older, repository_path)
            after = self.repo.file_at(target, repository_path)
            for mapping in self.catalog.mappings(repository_path):
                runtime_path = mapping["runtime_path"]
                if runtime_path in seen:
                    raise TransactionError("trusted transaction has duplicate runtime paths")
                seen.add(runtime_path)
                if mapping["planning_blocker"]:
                    raise TransactionError("trusted transaction contains a planning blocker")
                if status_value == "D" and not mapping["delete_allowed"]:
                    raise TransactionError("trusted transaction contains a forbidden deletion")
                files.append({
                    "repository_path": repository_path,
                    "runtime_path": runtime_path,
                    "change": "delete" if after is None else "write",
                    "before": self.engine._state_hash(before),
                    "after": self.engine._state_hash(after),
                    "services": mapping.get("services", []),
                    "before_metadata": {
                        "mode": mapping["mode"], "owner": mapping["owner"], "group": mapping["group"],
                    },
                    "mode": mapping["mode"],
                    "owner": mapping["owner"],
                    "group": mapping["group"],
                    "daemon_reload": mapping["daemon_reload"],
                })
        return {
            "files": files,
            "restart_services": services,
            "pre_health_probes": pre_health,
            "post_health_probes": post_health,
        }


def _locked_regular(path, expected_sha256, label, *, final_mode=None):
    path = Path(path)
    _validate_root_chain(path, final_kind="file", private=False, final_mode=final_mode)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        raise TransactionError("%s is missing" % label) from exc
    try:
        info = os.fstat(descriptor)
        chunks = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        raw = b"".join(chunks)
    finally:
        os.close(descriptor)
    current = os.lstat(path)
    if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
        raise TransactionError("%s changed while it was read" % label)
    if _sha256(raw) != expected_sha256:
        raise TransactionError("%s SHA-256 differs from bootstrap" % label)
    return raw


def _validate_root_chain(path, *, final_kind, private, final_mode=None):
    path = Path(os.path.abspath(path))
    current = Path(path.anchor)
    root_info = os.lstat(current)
    if (stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode)
            or os.name == "posix" and (root_info.st_uid != 0 or root_info.st_mode & 0o022)):
        raise TransactionError("trusted root path is not immutable")
    parts = path.parts[1:]
    for index, part in enumerate(parts):
        current /= part
        try:
            info = os.lstat(current)
        except FileNotFoundError as exc:
            raise TransactionError("trusted path chain is incomplete: %s" % current) from exc
        final = index == len(parts) - 1
        expected_regular = final and final_kind == "file"
        if (stat.S_ISLNK(info.st_mode)
                or expected_regular and not stat.S_ISREG(info.st_mode)
                or not expected_regular and not stat.S_ISDIR(info.st_mode)
                or os.name == "posix" and info.st_uid != 0
                or os.name == "posix" and info.st_mode & 0o022
                or final and private and info.st_mode & 0o077
                or final and final_mode is not None
                and stat.S_IMODE(info.st_mode) != final_mode):
            raise TransactionError("trusted path chain is not root-owned and immutable: %s" % current)


def _runtime_environment_is_isolated():
    flags = sys.flags
    return (
        os.name == "posix"
        and os.geteuid() == 0
        and os.path.realpath(sys.executable) == os.path.realpath("/usr/bin/python3")
        and os.path.realpath(__file__) == TRANSACTION_ENTRYPOINT
        and flags.isolated == 1
        and flags.ignore_environment == 1
        and flags.no_user_site == 1
        and flags.dont_write_bytecode == 1
        and dict(os.environ) == {
            "HOME": "/root", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
        }
    )


def _load_private_json(path, label):
    _validate_root_chain(path, final_kind="file", private=True, final_mode=0o600)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        raw = b""
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            raw += chunk
            if len(raw) > 64 * 1024:
                raise TransactionError("%s is too large" % label)
    finally:
        os.close(descriptor)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise TransactionError("%s is unavailable or invalid" % label) from exc
    return value


def _load_production_planner():
    bootstrap = _load_private_json(TRANSACTION_BOOTSTRAP, "transaction bootstrap")
    required = {"schema_version", "source_root", "transaction_entrypoint", "transaction_sha256",
                "phase_one_entrypoint", "phase_one_sha256", "launcher", "launcher_sha256",
                "state_root"}
    if (not isinstance(bootstrap, dict) or set(bootstrap) != required
            or bootstrap.get("schema_version") != 1
            or bootstrap.get("source_root") != DEFAULT_SOURCE_ROOT
            or bootstrap.get("state_root") != DEFAULT_STATE_ROOT
            or bootstrap.get("transaction_entrypoint") != TRANSACTION_ENTRYPOINT
            or bootstrap.get("phase_one_entrypoint") != PHASE_ONE_ENTRYPOINT
            or bootstrap.get("launcher") != TRANSACTION_LAUNCHER
            or any(not re.fullmatch(r"[0-9a-f]{64}", str(bootstrap.get(key) or ""))
                   for key in ("transaction_sha256", "phase_one_sha256", "launcher_sha256"))):
        raise TransactionError("transaction bootstrap fields are invalid")
    _validate_root_chain(DEFAULT_SOURCE_ROOT, final_kind="directory", private=False)
    _validate_root_chain(DEFAULT_STATE_ROOT, final_kind="directory", private=True, final_mode=0o700)
    _validate_root_chain(
        os.path.realpath("/usr/bin/python3"), final_kind="file", private=False,
    )
    _locked_regular(
        TRANSACTION_ENTRYPOINT, bootstrap["transaction_sha256"], "transaction entrypoint",
        final_mode=0o755,
    )
    phase_one_raw = _locked_regular(
        PHASE_ONE_ENTRYPOINT, bootstrap["phase_one_sha256"], "phase-one entrypoint",
        final_mode=0o755,
    )
    _locked_regular(
        TRANSACTION_LAUNCHER, bootstrap["launcher_sha256"], "transaction launcher",
        final_mode=0o755,
    )
    module = types.ModuleType("huangque_release_phase_one")
    module.__file__ = PHASE_ONE_ENTRYPOINT
    sys.modules[module.__name__] = module
    exec(compile(phase_one_raw, PHASE_ONE_ENTRYPOINT, "exec"), module.__dict__)
    module._verify_runtime_entrypoint(DEFAULT_SOURCE_ROOT)
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
    if not _runtime_environment_is_isolated():
        raise TransactionError("transaction executor requires its fixed isolated root launcher")
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
