#!/usr/bin/env python3
"""Canonical, commit-driven release engine for the Huangque test server.

This program never connects to a remote host.  It is intended to run from an
exact merged ``main`` checkout on the test server.  Runtime preimages are
derived from the ledger's deployed commit, then compared with the live files
before a backup or write.  A mismatch is drift and always fails closed.

Legacy locked-manifest executors remain immutable compatibility paths while
their manifests are migrated.  New release contracts must use this engine.
"""

from __future__ import annotations

import argparse
import copy
import contextlib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path, PurePosixPath


SCHEMA_VERSION = 1
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
RELEASE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,79}$")
ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,79}$")
ENV_ASSIGNMENT_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,79}=.*$")
UNIT_RE = re.compile(r"^[A-Za-z0-9@_.:-]+\.(?:service|timer)$")
DEFAULT_CATALOG = "deploy/test-release/runtime-catalog.json"
DEFAULT_IMPACT_PREFIX = "deploy/test-release/impacts/"
DEFAULT_IDENTITY_FILE = "/etc/huangque/release-identity.json"
DEFAULT_STATE_ROOT = "/var/lib/huangque-release"


class ReleaseError(RuntimeError):
    """A fail-closed release validation or execution error."""


class RollbackError(ReleaseError):
    """The forward release failed and exact restoration was incomplete."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value) -> bytes:
    return (json.dumps(
        value, ensure_ascii=False, sort_keys=True, indent=2,
    ) + "\n").encode("utf-8")


def _load_json(path: Path, label: str):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseError("%s is unavailable or invalid" % label) from exc
    if not isinstance(value, dict):
        raise ReleaseError("%s must be a JSON object" % label)
    return value


def _relative_repository_path(value: str) -> str:
    path = PurePosixPath(str(value or ""))
    if (not str(path) or path.is_absolute() or ".." in path.parts
            or "\\" in str(value)):
        raise ReleaseError("repository path must be a safe relative POSIX path")
    return path.as_posix()


def _absolute_runtime_path(value: str) -> str:
    path = PurePosixPath(str(value or ""))
    if not path.is_absolute() or ".." in path.parts or str(path) == "/":
        raise ReleaseError("runtime path must be a safe absolute POSIX path")
    return path.as_posix()


def _mapped_path(root: Path, runtime_path: str) -> Path:
    absolute = PurePosixPath(_absolute_runtime_path(runtime_path))
    target = root.joinpath(*absolute.parts[1:])
    resolved_root = Path(os.path.abspath(root))
    resolved_target = Path(os.path.abspath(target))
    if os.path.commonpath((str(resolved_root), str(resolved_target))) != str(
            resolved_root):
        raise ReleaseError("runtime path escaped the configured root")
    return resolved_target


def _assert_real_parents(root: Path, target: Path, *, create=False):
    root = Path(os.path.abspath(root))
    root.mkdir(parents=True, exist_ok=True)
    root_info = os.lstat(root)
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise ReleaseError("runtime root must be a real directory")
    current = root
    for part in target.parent.relative_to(root).parts:
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            if not create:
                return
            os.mkdir(current, 0o755)
            info = os.lstat(current)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ReleaseError("runtime parent contains a symbolic link or non-directory")


def _read_regular(root: Path, runtime_path: str):
    target = _mapped_path(root, runtime_path)
    _assert_real_parents(root, target)
    try:
        info = os.lstat(target)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ReleaseError("runtime target is not a regular file: %s" % runtime_path)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(target, flags)
    try:
        chunks = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(descriptor)


def _atomic_write(root: Path, runtime_path: str, data: bytes, mode: int,
                  uid=None, gid=None):
    target = _mapped_path(root, runtime_path)
    _assert_real_parents(root, target, create=True)
    try:
        info = os.lstat(target)
    except FileNotFoundError:
        info = None
    if info is not None and (
            stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)):
        raise ReleaseError("refusing to replace non-regular runtime target")
    temporary = target.parent / (".hq-release-%s" % uuid.uuid4().hex)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(descriptor, view):]
        os.fsync(descriptor)
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, int(mode))
        if (hasattr(os, "fchown") and uid is not None and gid is not None
                and os.name == "posix"):
            os.fchown(descriptor, int(uid), int(gid))
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    if _read_regular(root, runtime_path) != data:
        raise ReleaseError("atomic runtime write verification failed")


def _remove_regular(root: Path, runtime_path: str):
    target = _mapped_path(root, runtime_path)
    _assert_real_parents(root, target)
    try:
        info = os.lstat(target)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ReleaseError("refusing to remove non-regular runtime target")
    target.unlink()


class GitRepository:
    def __init__(self, source_root, *, timeout=90):
        self.root = Path(os.path.abspath(source_root))
        self.timeout = int(timeout)

    def run(self, arguments, *, allow_failure=False, binary=False):
        result = subprocess.run(
            ["git", "-C", str(self.root)] + list(arguments),
            check=False, capture_output=True, timeout=self.timeout,
            **({} if binary else {"text": True}),
        )
        if result.returncode and not allow_failure:
            raise ReleaseError("Git verification failed: %s" % " ".join(arguments))
        return result

    def output(self, arguments):
        return self.run(arguments).stdout.strip()

    def require_commit(self, commit):
        if not COMMIT_RE.fullmatch(str(commit or "")):
            raise ReleaseError("an exact 40-character commit is required")
        result = self.run(
            ["cat-file", "-e", "%s^{commit}" % commit], allow_failure=True,
        )
        if result.returncode:
            raise ReleaseError("release commit is missing from the source repository")

    def require_ancestor(self, older, newer):
        self.require_commit(older)
        self.require_commit(newer)
        result = self.run(
            ["merge-base", "--is-ancestor", older, newer], allow_failure=True,
        )
        if result.returncode:
            raise ReleaseError("deployed commit is not an ancestor of target main")

    def changed_paths(self, older, newer):
        self.require_ancestor(older, newer)
        raw = self.run([
            "diff", "--name-status", "--no-renames", "-z", older, newer,
        ], binary=True).stdout
        parts = raw.split(b"\0")
        result = []
        index = 0
        while index < len(parts) and parts[index]:
            status_value = parts[index].decode("ascii", "strict")
            index += 1
            if index >= len(parts) or not parts[index]:
                raise ReleaseError("Git diff returned an incomplete path record")
            path = parts[index].decode("utf-8", "strict")
            index += 1
            status_code = status_value[:1]
            if status_code not in {"A", "M", "D", "T"}:
                raise ReleaseError("unsupported Git change type: %s" % status_value)
            result.append((status_code, _relative_repository_path(path)))
        return result

    def file_at(self, commit, repository_path):
        repository_path = _relative_repository_path(repository_path)
        result = self.run(
            ["show", "%s:%s" % (commit, repository_path)],
            allow_failure=True, binary=True,
        )
        return None if result.returncode else result.stdout

    def file_mode_at(self, commit, repository_path):
        repository_path = _relative_repository_path(repository_path)
        raw = self.run(
            ["ls-tree", "-z", commit, "--", repository_path], binary=True,
        ).stdout
        if not raw:
            return None
        header = raw.split(b"\t", 1)[0].split()
        if len(header) != 3:
            raise ReleaseError("Git tree returned an invalid file record")
        return header[0].decode("ascii", "strict")

    def files_at(self, commit):
        self.require_commit(commit)
        raw = self.run(
            ["ls-tree", "-r", "--name-only", "-z", commit], binary=True,
        ).stdout
        return [
            _relative_repository_path(item.decode("utf-8", "strict"))
            for item in raw.split(b"\0") if item
        ]

    def verify_apply_checkout(self, target_commit, reviewed_heads,
                              *, verify_live_origin=True):
        if reviewed_heads is None:
            reviewed_heads = []
        elif isinstance(reviewed_heads, str):
            reviewed_heads = [reviewed_heads]
        elif isinstance(reviewed_heads, dict):
            reviewed_heads = list(reviewed_heads.values())
        for reviewed_head in reviewed_heads:
            self.require_commit(reviewed_head)
            self.require_ancestor(reviewed_head, target_commit)
        if self.output(["status", "--porcelain", "--untracked-files=normal"]):
            raise ReleaseError("release source checkout must be clean")
        if self.output(["symbolic-ref", "--short", "HEAD"]) != "main":
            raise ReleaseError("release source checkout must be on main")
        head = self.output(["rev-parse", "HEAD"])
        origin_main = self.output(["rev-parse", "refs/remotes/origin/main"])
        if head != target_commit or origin_main != target_commit:
            raise ReleaseError("HEAD and local origin/main must equal target commit")
        if verify_live_origin:
            line = self.output([
                "ls-remote", "--exit-code", "origin", "refs/heads/main",
            ])
            remote_main = line.split()[0] if line else ""
            if remote_main != target_commit:
                raise ReleaseError("live origin/main must equal target commit")


class RuntimeCatalog:
    def __init__(self, data):
        if data.get("schema_version") != SCHEMA_VERSION:
            raise ReleaseError("runtime catalog schema version is unsupported")
        target = data.get("target") or {}
        if target.get("environment") != "test" or not target.get("host_id"):
            raise ReleaseError("runtime catalog must target one explicit test host")
        self.target = target
        self.impact_prefix = _relative_repository_path(
            data.get("impact_prefix") or DEFAULT_IMPACT_PREFIX,
        ).rstrip("/") + "/"
        self.candidate_prefixes = tuple(
            _relative_repository_path(item).rstrip("/") + "/"
            for item in data.get("runtime_candidate_prefixes") or []
        )
        self.ignored_paths = frozenset(
            _relative_repository_path(item)
            for item in data.get("ignored_repository_paths") or []
        )
        self.ignored_prefixes = tuple(
            _relative_repository_path(item).rstrip("/") + "/"
            for item in data.get("ignored_repository_prefixes") or []
        )
        self.allowed_tools = frozenset(
            _absolute_runtime_path(item) for item in data.get("allowed_tools") or []
        )
        self.allowed_units = frozenset(str(item) for item in data.get("allowed_units") or [])
        if any(not UNIT_RE.fullmatch(item) for item in self.allowed_units):
            raise ReleaseError("runtime catalog contains an invalid systemd unit")
        self.min_free_bytes = int(data.get("min_free_bytes") or 0)
        if self.min_free_bytes < 0:
            raise ReleaseError("runtime catalog minimum free bytes is invalid")
        self.rules = []
        for raw in data.get("rules") or []:
            kind = raw.get("kind")
            repository = _relative_repository_path(raw.get("repository") or "")
            runtime = _absolute_runtime_path(raw.get("runtime") or "")
            if kind not in {"exact", "prefix"}:
                raise ReleaseError("runtime catalog rule kind is invalid")
            if kind == "prefix":
                repository = repository.rstrip("/") + "/"
                runtime = runtime.rstrip("/") + "/"
            service = raw.get("service")
            service_from_repository = raw.get("service_from_repository") is True
            if service is not None and not UNIT_RE.fullmatch(str(service)):
                raise ReleaseError("runtime catalog service name is invalid")
            if service is not None and service_from_repository:
                raise ReleaseError("runtime catalog service strategy is ambiguous")
            if service is not None and service not in self.allowed_units:
                raise ReleaseError("runtime catalog service is not allowlisted")
            if service_from_repository and kind != "prefix":
                raise ReleaseError("derived systemd units require a prefix mapping")
            if raw.get("daemon_reload") is True and not runtime.startswith(
                    "/etc/systemd/system/"):
                raise ReleaseError("daemon-reload is limited to systemd runtime paths")
            mode_text = str(raw.get("mode") or "0644")
            if not re.fullmatch(r"0[0-7]{3}", mode_text):
                raise ReleaseError("runtime catalog file mode is invalid")
            self.rules.append({
                "kind": kind, "repository": repository, "runtime": runtime,
                "service": service, "mode": int(mode_text, 8),
                "delete_allowed": raw.get("delete_allowed") is True,
                "service_from_repository": service_from_repository,
                "daemon_reload": raw.get("daemon_reload") is True,
            })
        if (not self.rules or not self.candidate_prefixes or not self.allowed_tools
                or not self.allowed_units):
            raise ReleaseError("runtime catalog is incomplete")

    @classmethod
    def load(cls, source_root, path):
        relative = _relative_repository_path(path)
        return cls(_load_json(Path(source_root) / relative, "runtime catalog"))

    def is_candidate(self, repository_path):
        path = _relative_repository_path(repository_path)
        return any(path.startswith(prefix) for prefix in self.candidate_prefixes)

    def is_ignored(self, repository_path):
        path = _relative_repository_path(repository_path)
        return path in self.ignored_paths or any(
            path.startswith(prefix) for prefix in self.ignored_prefixes
        )

    def map(self, repository_path, *, strict_candidate=True):
        path = _relative_repository_path(repository_path)
        if self.is_ignored(path):
            return None
        matches = []
        for rule in self.rules:
            if rule["kind"] == "exact" and path == rule["repository"]:
                matches.append((len(rule["repository"]), rule, ""))
            elif rule["kind"] == "prefix" and path.startswith(rule["repository"]):
                matches.append((
                    len(rule["repository"]), rule,
                    path[len(rule["repository"]):],
                ))
        if not matches:
            if strict_candidate and self.is_candidate(path):
                raise ReleaseError("runtime candidate has no catalog mapping: %s" % path)
            return None
        longest = max(item[0] for item in matches)
        strongest = [item for item in matches if item[0] == longest]
        if len(strongest) != 1:
            raise ReleaseError("runtime catalog has ambiguous mappings for: %s" % path)
        _, rule, suffix = strongest[0]
        mapped = dict(rule)
        if rule["service_from_repository"]:
            first = suffix.split("/", 1)[0]
            if first.endswith((".service.d", ".timer.d")):
                first = first[:-2]
            if not UNIT_RE.fullmatch(first):
                raise ReleaseError(
                    "systemd runtime path does not identify a service or timer: %s" % path
                )
            mapped["service"] = first
        mapped["repository_path"] = path
        mapped["runtime_path"] = (
            rule["runtime"] + suffix if rule["kind"] == "prefix"
            else rule["runtime"]
        )
        return mapped


def _validate_health_check(item):
    if not isinstance(item, dict):
        raise ReleaseError("health check must be an object")
    url = str(item.get("url") or "")
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
            or parsed.port is None or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or not parsed.path.startswith("/")):
        raise ReleaseError("health checks must use an exact loopback HTTP URL")
    statuses = item.get("expected_statuses")
    if (not isinstance(statuses, list) or not statuses
            or any(not isinstance(value, int) or not 100 <= value <= 599
                   for value in statuses)):
        raise ReleaseError("health check expected statuses are invalid")
    return {
        "url": url, "expected_statuses": sorted(set(statuses)),
        "timeout_seconds": min(max(int(item.get("timeout_seconds") or 60), 1), 120),
        "interval_seconds": min(max(float(item.get("interval_seconds") or 1), .1), 5),
    }


def _validate_command(item, catalog, label):
    if not isinstance(item, dict):
        raise ReleaseError("%s command must be an object" % label)
    argv = item.get("argv")
    if (not isinstance(argv, list) or not argv
            or any(not isinstance(value, str) or not value for value in argv)):
        raise ReleaseError("%s argv must be a non-empty string list" % label)
    tool = _absolute_runtime_path(argv[0])
    if tool not in catalog.allowed_tools:
        raise ReleaseError("undeclared deployment tool: %s" % tool)
    if tool == "/usr/bin/systemctl":
        raise ReleaseError("release impacts cannot invoke systemctl directly")
    if tool == "/usr/bin/env":
        index = 1
        while index < len(argv) and ENV_ASSIGNMENT_RE.fullmatch(argv[index]):
            index += 1
        if index >= len(argv):
            raise ReleaseError("env command must declare an executable")
        nested = _absolute_runtime_path(argv[index])
        if nested not in catalog.allowed_tools or nested == "/usr/bin/systemctl":
            raise ReleaseError("undeclared deployment tool behind env: %s" % nested)
    cwd = str(item.get("cwd") or "{source}")
    if cwd not in {"{source}", "{runtime}"}:
        raise ReleaseError("%s cwd is not approved" % label)
    return {
        "argv": list(argv), "cwd": cwd,
        "timeout_seconds": min(max(int(item.get("timeout_seconds") or 300), 1), 1800),
    }


def validate_impact(data, catalog):
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        raise ReleaseError("release impact schema version is unsupported")
    release_id = str(data.get("release_id") or "")
    if not RELEASE_ID_RE.fullmatch(release_id):
        raise ReleaseError("release impact id is invalid")
    runtime_changes = data.get("runtime_changes")
    if (not isinstance(runtime_changes, list) or not runtime_changes
            or len(set(runtime_changes)) != len(runtime_changes)):
        raise ReleaseError("release impact must declare unique runtime changes")
    runtime_changes = [_relative_repository_path(item) for item in runtime_changes]
    mappings = [catalog.map(item) for item in runtime_changes]
    if any(item is None for item in mappings):
        raise ReleaseError("release impact declared a non-runtime path")
    services = data.get("restart_services") or []
    if (not isinstance(services, list) or len(set(services)) != len(services)
            or any(not UNIT_RE.fullmatch(str(item)) for item in services)
            or not set(services).issubset(catalog.allowed_units)):
        raise ReleaseError("release impact services are invalid")
    required_services = {
        item["service"] for item in mappings if item.get("service")
    }
    if not required_services.issubset(set(services)):
        raise ReleaseError("release impact omits a required service restart")
    required_env = data.get("required_env") or []
    if (not isinstance(required_env, list) or len(set(required_env)) != len(required_env)
            or any(not ENV_NAME_RE.fullmatch(str(item)) for item in required_env)):
        raise ReleaseError("release impact environment declarations are invalid")
    health_checks = [
        _validate_health_check(item) for item in data.get("health_checks") or []
    ]
    if runtime_changes and not health_checks:
        raise ReleaseError("runtime changes require a loopback health check")
    pre_health_checks = [
        _validate_health_check(item) for item in data.get("pre_health_checks") or []
    ]
    if services and not pre_health_checks:
        raise ReleaseError("a service restart requires a pre-release loopback health check")
    external_checks = []
    for item in data.get("external_checks") or []:
        if not isinstance(item, dict) or item.get("no_charge") is not True:
            raise ReleaseError("external preflight checks must be explicitly no-charge")
        command = _validate_command(item, catalog, "external preflight")
        command["name"] = str(item.get("name") or "external-check")[:80]
        command["no_charge"] = True
        external_checks.append(command)
    migrations = []
    migration_ids = set()
    for item in data.get("migrations") or []:
        if not isinstance(item, dict):
            raise ReleaseError("migration declaration must be an object")
        migration_id = str(item.get("id") or "")
        if (not RELEASE_ID_RE.fullmatch(migration_id)
                or migration_id in migration_ids):
            raise ReleaseError("migration id is invalid or duplicated")
        migration_ids.add(migration_id)
        if item.get("rollback") != "restore_sqlite_snapshot":
            raise ReleaseError("migration must use an exact SQLite snapshot rollback")
        database_path = _absolute_runtime_path(item.get("database_path") or "")
        migrations.append({
            "id": migration_id,
            "database_path": database_path,
            "up": _validate_command(item.get("up"), catalog, "migration up"),
            "verify": _validate_command(item.get("verify"), catalog, "migration verify"),
            "rollback": "restore_sqlite_snapshot",
        })
    if migrations and not services:
        raise ReleaseError("a database migration requires an explicit service stop/start")
    return {
        "schema_version": SCHEMA_VERSION,
        "release_id": release_id,
        "runtime_changes": runtime_changes,
        "restart_services": sorted(services),
        "required_env": sorted(required_env),
        "health_checks": health_checks,
        "pre_health_checks": pre_health_checks,
        "external_checks": external_checks,
        "migrations": migrations,
    }


def collect_release_impact(repo, catalog, older, newer):
    changes = repo.changed_paths(older, newer)
    runtime_changes = set()
    for _status, path in changes:
        if catalog.map(path) is None:
            continue
        runtime_changes.add(path)
        for commit in (older, newer):
            data = repo.file_at(commit, path)
            if (data is not None and repo.file_mode_at(commit, path)
                    not in {"100644", "100755"}):
                raise ReleaseError(
                    "runtime source must be a regular Git blob: %s" % path
                )
    impact_changes = [
        (status_value, path) for status_value, path in changes
        if path.startswith(catalog.impact_prefix) and path.endswith(".json")
    ]
    immutable_changes = sorted(
        path for status_value, path in impact_changes if status_value != "A"
    )
    if immutable_changes:
        raise ReleaseError(
            "release impact files are immutable after creation: %s"
            % ", ".join(immutable_changes)
        )
    impact_paths = sorted({
        path for status_value, path in changes
        if status_value != "D" and path.startswith(catalog.impact_prefix)
        and path.endswith(".json")
    })
    impacts = []
    for path in impact_paths:
        raw = repo.file_at(newer, path)
        if raw is None:
            raise ReleaseError("release impact disappeared from target commit")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ReleaseError("release impact JSON is invalid: %s" % path) from exc
        impact = validate_impact(data, catalog)
        impact["repository_path"] = path
        impacts.append(impact)
    declared = {
        path for impact in impacts for path in impact["runtime_changes"]
    }
    missing = sorted(runtime_changes - declared)
    extra = sorted(declared - runtime_changes)
    if missing:
        raise ReleaseError("runtime changes lack release impact: %s" % ", ".join(missing))
    if extra:
        raise ReleaseError("release impact declares unchanged runtime paths: %s" % ", ".join(extra))
    if runtime_changes and not impacts:
        raise ReleaseError("runtime changes require a release impact declaration")
    release_ids = [item["release_id"] for item in impacts]
    if len(set(release_ids)) != len(release_ids):
        raise ReleaseError("release impact ids must be unique in a commit range")
    return {
        "changed_paths": changes,
        "runtime_changes": sorted(runtime_changes),
        "impacts": impacts,
    }


class CommandRunner:
    def __init__(self, *, tool_root="/"):
        self.tool_root = Path(os.path.abspath(tool_root))

    def validate(self, command, catalog):
        tools = [command["argv"][0]]
        if tools[0] == "/usr/bin/env":
            index = 1
            while (index < len(command["argv"])
                   and ENV_ASSIGNMENT_RE.fullmatch(command["argv"][index])):
                index += 1
            if index >= len(command["argv"]):
                raise ReleaseError("env command must declare an executable")
            tools.append(command["argv"][index])
        for tool in tools:
            if tool not in catalog.allowed_tools:
                raise ReleaseError("undeclared deployment tool: %s" % tool)
            target = _mapped_path(self.tool_root, tool)
            _assert_real_parents(self.tool_root, target)
            try:
                info = os.lstat(target)
            except FileNotFoundError as exc:
                raise ReleaseError("deployment tool is unavailable: %s" % tool) from exc
            inspected = target
            if stat.S_ISLNK(info.st_mode):
                try:
                    inspected = target.resolve(strict=True)
                    approved_parent = target.parent.resolve(strict=True)
                except OSError as exc:
                    raise ReleaseError(
                        "deployment tool is unsafe or not executable: %s" % tool
                    ) from exc
                if os.path.commonpath((str(approved_parent), str(inspected))) != str(
                        approved_parent):
                    raise ReleaseError(
                        "deployment tool symbolic link escaped its approved directory: %s"
                        % tool
                    )
                info = os.lstat(inspected)
            if (not stat.S_ISREG(info.st_mode)
                    or os.name == "posix" and not info.st_mode & 0o111):
                raise ReleaseError("deployment tool is unsafe or not executable: %s" % tool)

    def run(self, command, *, source_root, runtime_root, environment):
        cwd = source_root if command["cwd"] == "{source}" else runtime_root
        arguments = []
        for value in command["argv"]:
            if value.startswith("{source:") and value.endswith("}"):
                relative = _relative_repository_path(value[8:-1])
                value = str(Path(source_root) / PurePosixPath(relative))
            elif value.startswith("{runtime:") and value.endswith("}"):
                value = str(_mapped_path(Path(runtime_root), value[9:-1]))
            elif "{" in value or "}" in value:
                raise ReleaseError("deployment command contains an unsupported placeholder")
            arguments.append(value)
        subprocess.run(
            arguments, cwd=cwd, env=environment, check=True,
            timeout=command["timeout_seconds"],
        )

    def service(self, action, service, *, timeout=180):
        if action not in {"start", "stop", "restart"} or not UNIT_RE.fullmatch(service):
            raise ReleaseError("systemd service action is invalid")
        subprocess.run(
            ["/usr/bin/systemctl", action, service], check=True, timeout=timeout,
        )

    def daemon_reload(self, *, timeout=180):
        subprocess.run(
            ["/usr/bin/systemctl", "daemon-reload"], check=True, timeout=timeout,
        )

    def is_active(self, service, *, timeout=30):
        if not UNIT_RE.fullmatch(service):
            raise ReleaseError("systemd unit name is invalid")
        result = subprocess.run(
            ["/usr/bin/systemctl", "is-active", "--quiet", service],
            check=False, timeout=timeout,
        )
        return result.returncode == 0


class ReleaseEngine:
    def __init__(
            self, source_root, runtime_root, catalog,
            *, identity_path=DEFAULT_IDENTITY_FILE,
            state_root=DEFAULT_STATE_ROOT, repo=None, runner=None,
            health_getter=None, environment=None, clock=None, sleeper=None,
            tool_root="/"):
        self.source_root = Path(os.path.abspath(source_root))
        self.runtime_root = Path(os.path.abspath(runtime_root))
        self.catalog = catalog
        self.identity_path = _absolute_runtime_path(identity_path)
        self.state_root_path = _absolute_runtime_path(state_root)
        self.state_root = _mapped_path(self.runtime_root, self.state_root_path)
        self.repo = repo or GitRepository(self.source_root)
        self.runner = runner or CommandRunner(tool_root=tool_root)
        self.health_getter = health_getter or self._http_status
        self.environment = dict(os.environ if environment is None else environment)
        self.clock = clock or time.monotonic
        self.sleeper = sleeper or time.sleep

    @property
    def state_path(self):
        return self.state_root / "state.json"

    @property
    def releases_root(self):
        return self.state_root / "releases"

    @property
    def backups_root(self):
        return self.state_root / "backups"

    @property
    def lock_path(self):
        return self.state_root / "release.lock"

    def _state_runtime_path(self, leaf):
        leaf = _relative_repository_path(leaf)
        return self.state_root_path.rstrip("/") + "/" + leaf

    def _ensure_state_directory(self, directory):
        directory = Path(os.path.abspath(directory))
        state_root = Path(os.path.abspath(self.state_root))
        if os.path.commonpath((str(state_root), str(directory))) != str(state_root):
            raise ReleaseError("release state path escaped the state root")
        try:
            os.lstat(directory)
            existed = True
        except FileNotFoundError:
            existed = False
        _assert_real_parents(self.runtime_root, directory / ".directory-check", create=True)
        if not existed:
            os.chmod(directory, 0o700)
        info = os.lstat(directory)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ReleaseError("release state directory is unsafe")
        if (os.name == "posix"
                and (stat.S_IMODE(info.st_mode) & 0o077
                     or info.st_uid != os.geteuid())):
            raise ReleaseError("release state directory must be private and owner-controlled")

    @contextlib.contextmanager
    def _release_lock(self):
        self._ensure_state_directory(self.state_root)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.lock_path, flags, 0o600)
        except FileExistsError as exc:
            raise ReleaseError("another release or rollback is already running") from exc
        locked = os.fstat(descriptor)
        try:
            payload = _json_bytes({"pid": os.getpid(), "created_unix": int(time.time())})
            os.write(descriptor, payload)
            os.fsync(descriptor)
            yield
        finally:
            os.close(descriptor)
            try:
                current = os.lstat(self.lock_path)
            except FileNotFoundError:
                current = None
            if (current is None or current.st_dev != locked.st_dev
                    or current.st_ino != locked.st_ino):
                raise ReleaseError("release lock was unexpectedly replaced or removed")
            self.lock_path.unlink()

    def _runtime_json(self, runtime_path, label):
        data = _read_regular(self.runtime_root, runtime_path)
        if data is None:
            raise ReleaseError("%s is not initialized" % label)
        try:
            value = json.loads(data.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ReleaseError("%s is invalid" % label) from exc
        if not isinstance(value, dict):
            raise ReleaseError("%s must be a JSON object" % label)
        return value

    def _verify_private_runtime_file(self, runtime_path, label):
        target = _mapped_path(self.runtime_root, runtime_path)
        _assert_real_parents(self.runtime_root, target)
        try:
            info = os.lstat(target)
        except FileNotFoundError as exc:
            raise ReleaseError("%s is not initialized" % label) from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ReleaseError("%s is not a regular file" % label)
        if (os.name == "posix"
                and (stat.S_IMODE(info.st_mode) & 0o077
                     or info.st_uid != os.geteuid())):
            raise ReleaseError("%s must be private and owner-controlled" % label)

    def verify_identity(self):
        self._verify_private_runtime_file(self.identity_path, "release identity")
        identity = self._runtime_json(self.identity_path, "release identity")
        target = self.catalog.target
        required = {
            "schema_version": SCHEMA_VERSION,
            "environment": target["environment"],
            "host_id": target["host_id"],
        }
        if any(identity.get(key) != value for key, value in required.items()):
            raise ReleaseError("release target environment or host identity is wrong")
        hostname_bytes = _read_regular(self.runtime_root, "/etc/hostname")
        machine_bytes = _read_regular(self.runtime_root, "/etc/machine-id")
        if hostname_bytes is None or machine_bytes is None:
            raise ReleaseError("host identity inputs are unavailable")
        hostname = hostname_bytes.decode("utf-8", "strict").strip()
        machine_digest = _sha256(machine_bytes.strip())
        if (identity.get("hostname") != hostname
                or identity.get("machine_id_sha256") != machine_digest):
            raise ReleaseError("release host fingerprint does not match this machine")
        return identity

    def load_state(self):
        self._verify_private_runtime_file(
            self._state_runtime_path("state.json"), "deployment ledger",
        )
        state = self._runtime_json(
            self._state_runtime_path("state.json"), "deployment ledger",
        )
        if (state.get("schema_version") != SCHEMA_VERSION
                or state.get("environment") != self.catalog.target["environment"]
                or state.get("host_id") != self.catalog.target["host_id"]
                or not COMMIT_RE.fullmatch(str(state.get("deployed_main_commit") or ""))
                or not isinstance(state.get("runtime_hashes"), dict)):
            raise ReleaseError("deployment ledger is invalid")
        if ("repository_paths" in state
                and not isinstance(state.get("repository_paths"), dict)):
            raise ReleaseError("deployment ledger repository path map is invalid")
        applied_migrations = state.get("applied_migrations") or []
        if (not isinstance(applied_migrations, list)
                or len(set(applied_migrations)) != len(applied_migrations)
                or any(not RELEASE_ID_RE.fullmatch(str(item)) for item in applied_migrations)):
            raise ReleaseError("deployment ledger migration list is invalid")
        for runtime_path, expected in state["runtime_hashes"].items():
            _absolute_runtime_path(runtime_path)
            if (not isinstance(expected, dict)
                    or expected.get("state") not in {"file", "absent"}
                    or (expected["state"] == "file"
                        and not re.fullmatch(r"[0-9a-f]{64}", str(expected.get("sha256") or "")))
                    or (expected["state"] == "absent" and expected.get("sha256") is not None)):
                raise ReleaseError("deployment ledger contains an invalid runtime hash")
        return state

    def _write_state_file(self, path, data):
        relative = "/" + path.relative_to(self.runtime_root).as_posix()
        _atomic_write(self.runtime_root, relative, _json_bytes(data), 0o600)

    def _read_state_binary(self, path, label):
        path = Path(os.path.abspath(path))
        state_root = Path(os.path.abspath(self.state_root))
        if os.path.commonpath((str(state_root), str(path))) != str(state_root):
            raise ReleaseError("%s escaped the release state root" % label)
        relative = "/" + path.relative_to(self.runtime_root).as_posix()
        data = _read_regular(self.runtime_root, relative)
        if data is None:
            raise ReleaseError("%s is missing" % label)
        return data

    def _write_state(self, state):
        self._write_state_file(self.state_path, state)

    def _write_release(self, release_id, record):
        if not RELEASE_ID_RE.fullmatch(release_id):
            raise ReleaseError("release record id is invalid")
        self._ensure_state_directory(self.releases_root)
        self._write_state_file(self.releases_root / (release_id + ".json"), record)

    def _read_release(self, release_id):
        if not RELEASE_ID_RE.fullmatch(str(release_id or "")):
            raise ReleaseError("release record id is invalid")
        runtime_path = self._state_runtime_path("releases/%s.json" % release_id)
        self._verify_private_runtime_file(runtime_path, "release record")
        return self._runtime_json(
            runtime_path,
            "release record",
        )

    @staticmethod
    def _state_hash(data):
        return {"state": "absent", "sha256": None} if data is None else {
            "state": "file", "sha256": _sha256(data),
        }

    def status(self):
        self.verify_identity()
        state = self.load_state()
        drift = []
        for runtime_path, expected in sorted(state["runtime_hashes"].items()):
            actual = self._state_hash(_read_regular(self.runtime_root, runtime_path))
            if actual != expected:
                drift.append(runtime_path)
        return {
            "ok": not drift,
            "status": "deployed" if not drift else "drifted",
            "environment": state["environment"],
            "host_id": state["host_id"],
            "deployed_main_commit": state["deployed_main_commit"],
            "last_release_id": state.get("last_release_id"),
            "drifted_paths": drift,
        }

    def initialize(self, deployed_commit, confirmation, *, verify_live_origin=True):
        if confirmation != "test":
            raise ReleaseError("initialize requires exact test environment confirmation")
        identity = self.verify_identity()
        if _read_regular(self.runtime_root, self._state_runtime_path("state.json")) is not None:
            raise ReleaseError("deployment ledger is already initialized")
        self.repo.verify_apply_checkout(
            deployed_commit, deployed_commit,
            verify_live_origin=verify_live_origin,
        )
        self._build_initial_state(identity, deployed_commit)
        with self._release_lock():
            if _read_regular(self.runtime_root, self._state_runtime_path("state.json")) is not None:
                raise ReleaseError("deployment ledger is already initialized")
            self.repo.verify_apply_checkout(
                deployed_commit, deployed_commit,
                verify_live_origin=verify_live_origin,
            )
            state = self._build_initial_state(identity, deployed_commit)
            self._write_state(state)
        return {
            "ok": True,
            "status": "initialized",
            "environment": identity["environment"],
            "host_id": identity["host_id"],
            "deployed_main_commit": deployed_commit,
            "tracked_runtime_paths": len(state["runtime_hashes"]),
        }

    def _build_initial_state(self, identity, deployed_commit):
        runtime_hashes = {}
        repository_paths = {}
        for repository_path in self.repo.files_at(deployed_commit):
            mapping = self.catalog.map(repository_path, strict_candidate=False)
            if mapping is None:
                continue
            runtime_path = mapping["runtime_path"]
            if runtime_path in repository_paths:
                raise ReleaseError(
                    "multiple repository files map to one runtime path: %s" % runtime_path
                )
            expected = self.repo.file_at(deployed_commit, repository_path)
            if (expected is not None
                    and self.repo.file_mode_at(deployed_commit, repository_path)
                    not in {"100644", "100755"}):
                raise ReleaseError(
                    "runtime source must be a regular Git blob: %s" % repository_path
                )
            actual = _read_regular(self.runtime_root, runtime_path)
            if self._state_hash(actual) != self._state_hash(expected):
                raise ReleaseError(
                    "runtime does not match the initialization commit: %s" % runtime_path
                )
            repository_paths[runtime_path] = repository_path
            runtime_hashes[runtime_path] = self._state_hash(actual)
        if not runtime_hashes:
            raise ReleaseError("runtime catalog produced no initialization targets")
        return {
            "schema_version": SCHEMA_VERSION,
            "environment": identity["environment"],
            "host_id": identity["host_id"],
            "deployed_main_commit": deployed_commit,
            "runtime_hashes": runtime_hashes,
            "repository_paths": repository_paths,
            "last_release_id": None,
            "last_successful_release": None,
            "applied_migrations": [],
        }

    def _aggregate_impact(self, older, newer):
        collected = collect_release_impact(self.repo, self.catalog, older, newer)
        impacts = collected["impacts"]
        migration_ids = [
            migration["id"] for item in impacts for migration in item["migrations"]
        ]
        if len(set(migration_ids)) != len(migration_ids):
            raise ReleaseError("migration ids must be unique in a release range")
        return collected, {
            "release_ids": [item["release_id"] for item in impacts],
            "restart_services": sorted({
                service for item in impacts for service in item["restart_services"]
            }),
            "required_env": sorted({
                name for item in impacts for name in item["required_env"]
            }),
            "health_checks": [
                check for item in impacts for check in item["health_checks"]
            ],
            "pre_health_checks": [
                check for item in impacts for check in item["pre_health_checks"]
            ],
            "external_checks": [
                check for item in impacts for check in item["external_checks"]
            ],
            "migrations": [
                migration for item in impacts for migration in item["migrations"]
            ],
        }

    def _verify_commands_and_environment(self, impact):
        missing = [name for name in impact["required_env"] if not self.environment.get(name)]
        if missing:
            raise ReleaseError("required environment variables are missing: %s" % ", ".join(missing))
        commands = list(impact["external_checks"])
        for migration in impact["migrations"]:
            commands.extend((migration["up"], migration["verify"]))
        for command in commands:
            self.runner.validate(command, self.catalog)
        if impact["restart_services"]:
            self.runner.validate({
                "argv": ["/usr/bin/systemctl"], "cwd": "{source}",
                "timeout_seconds": 30,
            }, self.catalog)
            inactive = [
                service for service in impact["restart_services"]
                if not self.runner.is_active(service)
            ]
            if inactive:
                raise ReleaseError(
                    "required systemd units are not active: %s" % ", ".join(inactive)
                )

    def build_plan(self, target_commit, *, run_external_checks=True):
        identity = self.verify_identity()
        state = self.load_state()
        current = self.status()
        if not current["ok"]:
            raise ReleaseError("deployment ledger runtime has drifted")
        target_commit = str(target_commit or "")
        self.repo.require_commit(target_commit)
        older = state["deployed_main_commit"]
        if older == target_commit:
            return {
                "ok": True, "status": "already_deployed",
                "environment": identity["environment"],
                "host_id": identity["host_id"],
                "from_commit": older, "target_commit": target_commit,
                "files": [], "restart_services": [], "migrations": [],
                "required_env": [], "health_checks": [], "release_ids": [],
                "pre_health_checks": [], "daemon_reload": False,
            }
        self.repo.require_ancestor(older, target_commit)
        collected, impact = self._aggregate_impact(older, target_commit)
        self._verify_commands_and_environment(impact)
        self._verify_health(impact["pre_health_checks"])
        files = []
        runtime_paths = set()
        total_bytes = 0
        for status_value, repository_path in collected["changed_paths"]:
            mapping = self.catalog.map(repository_path)
            if mapping is None:
                continue
            before = self.repo.file_at(older, repository_path)
            after = self.repo.file_at(target_commit, repository_path)
            for commit, data in ((older, before), (target_commit, after)):
                if (data is not None and self.repo.file_mode_at(commit, repository_path)
                        not in {"100644", "100755"}):
                    raise ReleaseError(
                        "runtime source must be a regular Git blob: %s" % repository_path
                    )
            if status_value == "D" and not mapping["delete_allowed"]:
                raise ReleaseError("runtime deletion is not allowed by catalog: %s" % repository_path)
            actual = _read_regular(self.runtime_root, mapping["runtime_path"])
            if self._state_hash(actual) != self._state_hash(before):
                raise ReleaseError("runtime drift detected before backup: %s" % mapping["runtime_path"])
            if mapping["runtime_path"] in runtime_paths:
                raise ReleaseError(
                    "multiple repository files map to one runtime path: %s"
                    % mapping["runtime_path"]
                )
            runtime_paths.add(mapping["runtime_path"])
            files.append({
                **mapping,
                "change": "delete" if after is None else "write",
                "before": self._state_hash(before),
                "after": self._state_hash(after),
                "before_size": 0 if before is None else len(before),
                "after_size": 0 if after is None else len(after),
            })
            total_bytes += (0 if before is None else len(before)) + (
                0 if after is None else len(after)
            )
        for migration in impact["migrations"]:
            database = _read_regular(self.runtime_root, migration["database_path"])
            if database is None:
                raise ReleaseError("migration database is missing")
            total_bytes += len(database)
        daemon_reload = any(item.get("daemon_reload") for item in files)
        if daemon_reload:
            self.runner.validate({
                "argv": ["/usr/bin/systemctl"], "cwd": "{source}",
                "timeout_seconds": 30,
            }, self.catalog)
        free = shutil.disk_usage(self.state_root.parent).free
        required = max(self.catalog.min_free_bytes, total_bytes * 2)
        if free < required:
            raise ReleaseError("insufficient disk space for release backup and install")
        if run_external_checks:
            for check in impact["external_checks"]:
                self.runner.run(
                    check, source_root=self.source_root,
                    runtime_root=self.runtime_root, environment=self.environment,
                )
        release_id = "main-%s-%s" % (target_commit[:12], uuid.uuid4().hex[:12])
        return {
            "ok": True, "status": "planned",
            "release_id": release_id,
            "release_ids": impact["release_ids"],
            "environment": identity["environment"], "host_id": identity["host_id"],
            "from_commit": older, "target_commit": target_commit,
            "files": files,
            "restart_services": impact["restart_services"],
            "required_env": impact["required_env"],
            "health_checks": impact["health_checks"],
            "pre_health_checks": impact["pre_health_checks"],
            "external_checks": impact["external_checks"],
            "migrations": impact["migrations"],
            "daemon_reload": daemon_reload,
            "required_free_bytes": required,
        }

    def _http_status(self, url):
        request = urllib.request.Request(url, headers={"User-Agent": "hq-release-test"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=8) as response:
                return int(response.status)
        except urllib.error.HTTPError as exc:
            return int(exc.code)

    def _verify_health(self, checks):
        for check in checks:
            deadline = self.clock() + check["timeout_seconds"]
            last = None
            while True:
                try:
                    last = self.health_getter(check["url"])
                except (OSError, TimeoutError, ConnectionError, urllib.error.URLError):
                    last = None
                if last in check["expected_statuses"]:
                    break
                remaining = deadline - self.clock()
                if remaining <= 0:
                    raise ReleaseError("loopback health check did not become ready")
                self.sleeper(min(check["interval_seconds"], remaining))

    def _backup_plan(self, plan, state):
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        self._ensure_state_directory(self.backups_root)
        backup = self.backups_root / (
            "%s-%s-%s" % (plan["release_id"], plan["target_commit"][:12], stamp)
        )
        try:
            backup.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise ReleaseError("release backup directory already exists") from exc
        self._ensure_state_directory(backup)
        entries = []
        for index, item in enumerate(plan["files"]):
            data = _read_regular(self.runtime_root, item["runtime_path"])
            metadata = None
            backup_file = None
            if data is not None:
                target = _mapped_path(self.runtime_root, item["runtime_path"])
                info = os.lstat(target)
                metadata = {
                    "mode": stat.S_IMODE(info.st_mode),
                    "uid": getattr(info, "st_uid", None),
                    "gid": getattr(info, "st_gid", None),
                }
                backup_file = "file-%04d.bin" % index
                with (backup / backup_file).open("xb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
            entries.append({
                "kind": "runtime", "runtime_path": item["runtime_path"],
                "state": self._state_hash(data), "metadata": metadata,
                "backup_file": backup_file,
            })
        for index, migration in enumerate(plan["migrations"]):
            source = _mapped_path(self.runtime_root, migration["database_path"])
            _assert_real_parents(self.runtime_root, source)
            if source.is_symlink() or not source.is_file():
                raise ReleaseError("migration database is not a regular file")
            backup_file = "database-%04d.sqlite" % index
            destination = backup / backup_file
            source_connection = sqlite3.connect(str(source))
            destination_connection = sqlite3.connect(str(destination))
            try:
                source_connection.execute("PRAGMA query_only=ON")
                source_connection.backup(destination_connection)
            finally:
                destination_connection.close()
                source_connection.close()
            snapshot = destination.read_bytes()
            entries.append({
                "kind": "database", "runtime_path": migration["database_path"],
                "state": self._state_hash(snapshot),
                "live_preimage": self._state_hash(source.read_bytes()),
                "metadata": {
                    "mode": stat.S_IMODE(os.lstat(source).st_mode),
                    "uid": getattr(os.lstat(source), "st_uid", None),
                    "gid": getattr(os.lstat(source), "st_gid", None),
                },
                "backup_file": backup_file,
            })
        with (backup / "state-before.json").open("xb") as handle:
            handle.write(_json_bytes(state))
            handle.flush()
            os.fsync(handle.fileno())
        manifest = {"entries": entries, "state_before": state}
        with (backup / "backup.json").open("xb") as handle:
            handle.write(_json_bytes(manifest))
            handle.flush()
            os.fsync(handle.fileno())
        return backup, entries

    def _restore_entries(self, backup, entries, services, checks, daemon_reload=False):
        failures = []
        if (any(not UNIT_RE.fullmatch(str(service)) for service in services)
                or not set(services).issubset(self.catalog.allowed_units)):
            raise RollbackError("release record contains an undeclared systemd unit")
        if services:
            for service in services:
                try:
                    self.runner.service("stop", service)
                except Exception as exc:
                    failures.append("stop %s: %s" % (service, type(exc).__name__))
        for entry in entries:
            try:
                if entry["state"]["state"] == "absent":
                    _remove_regular(self.runtime_root, entry["runtime_path"])
                    continue
                data = self._read_state_binary(
                    backup / str(entry.get("backup_file") or ""), "release backup file",
                )
                metadata = entry["metadata"] or {"mode": 0o640, "uid": None, "gid": None}
                _atomic_write(
                    self.runtime_root, entry["runtime_path"], data,
                    metadata["mode"], metadata.get("uid"), metadata.get("gid"),
                )
            except Exception as exc:
                failures.append("restore %s: %s" % (
                    entry["runtime_path"], type(exc).__name__,
                ))
        if daemon_reload:
            try:
                self.runner.daemon_reload()
            except Exception as exc:
                failures.append("daemon-reload: %s" % type(exc).__name__)
        for service in services:
            try:
                self.runner.service("restart", service)
            except Exception as exc:
                failures.append("restart %s: %s" % (service, type(exc).__name__))
        if not failures:
            try:
                self._verify_health(checks)
            except Exception as exc:
                failures.append("rollback health: %s" % type(exc).__name__)
        if failures:
            raise RollbackError("; ".join(failures))

    def apply(self, target_commit, reviewed_head, confirmation,
              *, verify_live_origin=True):
        if confirmation != "test":
            raise ReleaseError("apply requires exact test environment confirmation")
        self.verify_identity()
        self.load_state()
        self.repo.verify_apply_checkout(
            target_commit, reviewed_head, verify_live_origin=verify_live_origin,
        )
        with self._release_lock():
            return self._apply_locked(
                target_commit, reviewed_head, verify_live_origin=verify_live_origin,
            )

    def _apply_locked(self, target_commit, reviewed_head, *, verify_live_origin):
        self.repo.verify_apply_checkout(
            target_commit, reviewed_head, verify_live_origin=verify_live_origin,
        )
        plan = self.build_plan(target_commit, run_external_checks=False)
        if plan["status"] == "already_deployed":
            return {**plan, "restart_count": 0, "backup": None}
        reviewed_heads = self._normalize_reviewed_heads(
            reviewed_head, plan["release_ids"], target_commit,
        )
        for check in plan["external_checks"]:
            self.runner.run(
                check, source_root=self.source_root,
                runtime_root=self.runtime_root, environment=self.environment,
            )
        state = self.load_state()
        services_stopped = False
        stopped_services = []
        try:
            if plan["migrations"]:
                for service in plan["restart_services"]:
                    self.runner.service("stop", service)
                    stopped_services.append(service)
                services_stopped = bool(stopped_services)
            backup, entries = self._backup_plan(plan, state)
        except Exception as backup_error:
            recovery_failures = []
            if stopped_services:
                for service in reversed(stopped_services):
                    try:
                        self.runner.service("start", service)
                    except Exception as exc:
                        recovery_failures.append(
                            "start %s: %s" % (service, type(exc).__name__)
                        )
                if not recovery_failures:
                    try:
                        self._verify_health(plan["pre_health_checks"])
                    except Exception as exc:
                        recovery_failures.append(
                            "pre-release health: %s" % type(exc).__name__
                        )
            if recovery_failures:
                raise RollbackError(
                    "backup failed and stopped services were not fully restored: %s"
                    % "; ".join(recovery_failures)
                ) from backup_error
            raise
        record = {
            "schema_version": SCHEMA_VERSION,
            "release_id": plan["release_id"], "status": "deploying",
            "environment": plan["environment"], "host_id": plan["host_id"],
            "from_commit": plan["from_commit"], "target_commit": plan["target_commit"],
            "reviewed_heads": reviewed_heads, "backup": str(backup),
            "files": copy.deepcopy(plan["files"]), "backup_entries": entries,
            "restart_services": plan["restart_services"],
            "health_checks": plan["health_checks"],
            "pre_health_checks": plan["pre_health_checks"],
            "state_before": state,
            "impact_ids": plan["release_ids"],
            "daemon_reload": plan["daemon_reload"],
        }
        forward_started = services_stopped
        try:
            self._write_release(plan["release_id"], record)
            for item in plan["files"]:
                after = self.repo.file_at(target_commit, item["repository_path"])
                current = _read_regular(self.runtime_root, item["runtime_path"])
                if self._state_hash(current) != item["before"]:
                    raise ReleaseError("runtime changed after backup")
                if after is None:
                    forward_started = True
                    _remove_regular(self.runtime_root, item["runtime_path"])
                else:
                    target = _mapped_path(self.runtime_root, item["runtime_path"])
                    try:
                        info = os.lstat(target)
                        uid, gid = getattr(info, "st_uid", None), getattr(info, "st_gid", None)
                    except FileNotFoundError:
                        parent = target.parent
                        info = os.lstat(parent)
                        uid, gid = getattr(info, "st_uid", None), getattr(info, "st_gid", None)
                    forward_started = True
                    _atomic_write(
                        self.runtime_root, item["runtime_path"], after,
                        item["mode"], uid, gid,
                    )
            for migration in plan["migrations"]:
                self.runner.run(
                    migration["up"], source_root=self.source_root,
                    runtime_root=self.runtime_root, environment=self.environment,
                )
                self.runner.run(
                    migration["verify"], source_root=self.source_root,
                    runtime_root=self.runtime_root, environment=self.environment,
                )
            if plan["daemon_reload"]:
                self.runner.daemon_reload()
            for service in plan["restart_services"]:
                self.runner.service("start" if services_stopped else "restart", service)
            self._verify_health(plan["health_checks"])
            runtime_hashes = dict(state["runtime_hashes"])
            repository_paths = dict(state.get("repository_paths") or {})
            for item in plan["files"]:
                runtime_hashes[item["runtime_path"]] = item["after"]
                repository_paths[item["runtime_path"]] = item["repository_path"]
            next_state = {
                **state,
                "deployed_main_commit": target_commit,
                "runtime_hashes": runtime_hashes,
                "repository_paths": repository_paths,
                "applied_migrations": list(dict.fromkeys(
                    list(state.get("applied_migrations") or [])
                    + [item["id"] for item in plan["migrations"]]
                )),
                "last_release_id": plan["release_id"],
                "last_successful_release": {
                    "release_id": plan["release_id"],
                    "from_commit": plan["from_commit"],
                    "target_commit": target_commit,
                    "backup": str(backup),
                },
            }
            self._write_state(next_state)
            record["status"] = "deployed"
            record["state_after"] = next_state
            self._write_release(plan["release_id"], record)
        except Exception as release_error:
            if not forward_started:
                record["status"] = "blocked_before_write"
                record["failure_type"] = type(release_error).__name__
                try:
                    self._write_release(plan["release_id"], record)
                except Exception:
                    pass
                raise ReleaseError("release was blocked before runtime writes") from release_error
            try:
                self._restore_entries(
                    backup, entries, plan["restart_services"],
                    plan["pre_health_checks"],
                    plan["daemon_reload"],
                )
                self._write_state(state)
                record["status"] = "rolled_back"
                record["failure_type"] = type(release_error).__name__
                self._write_release(plan["release_id"], record)
            except Exception as rollback_error:
                record["status"] = "rollback_failed"
                record["failure_type"] = type(release_error).__name__
                record["rollback_failure_type"] = type(rollback_error).__name__
                try:
                    self._write_release(plan["release_id"], record)
                except Exception:
                    pass
                raise RollbackError(
                    "release failed and rollback was incomplete",
                ) from rollback_error
            raise ReleaseError("release failed and was rolled back") from release_error
        return {
            "ok": True, "status": "deployed", "release_id": plan["release_id"],
            "from_commit": plan["from_commit"], "target_commit": target_commit,
            "backup": str(backup), "files": len(plan["files"]),
            "restart_count": len(plan["restart_services"]),
        }

    def _normalize_reviewed_heads(self, reviewed, release_ids, target_commit):
        if isinstance(reviewed, str):
            if len(release_ids) != 1:
                raise ReleaseError(
                    "one reviewed Head must be mapped to every release impact"
                )
            reviewed = {release_ids[0]: reviewed}
        elif reviewed is None:
            reviewed = {}
        elif not isinstance(reviewed, dict):
            raise ReleaseError("reviewed Head evidence is invalid")
        if set(reviewed) != set(release_ids):
            raise ReleaseError(
                "reviewed Head evidence must exactly cover release impact ids"
            )
        if len(set(reviewed.values())) != len(reviewed):
            raise ReleaseError("each release impact requires a distinct reviewed Head")
        normalized = {}
        for release_id, commit in sorted(reviewed.items()):
            if not RELEASE_ID_RE.fullmatch(str(release_id)):
                raise ReleaseError("reviewed Head release id is invalid")
            self.repo.require_commit(commit)
            self.repo.require_ancestor(commit, target_commit)
            normalized[release_id] = commit
        return normalized

    def rollback(self, release_id, confirmation):
        if confirmation != "test":
            raise ReleaseError("rollback requires exact test environment confirmation")
        self.verify_identity()
        self.load_state()
        with self._release_lock():
            return self._rollback_locked(release_id)

    def _rollback_locked(self, release_id):
        self.verify_identity()
        state = self.load_state()
        record = self._read_release(release_id)
        if record.get("status") == "rolled_back":
            return {"ok": True, "status": "already_rolled_back", "release_id": release_id}
        if (record.get("status") != "deployed"
                or state.get("last_release_id") != release_id
                or state.get("deployed_main_commit") != record.get("target_commit")):
            raise ReleaseError("only the current successful release can be rolled back")
        backup = Path(record.get("backup") or "")
        expected_parent = Path(os.path.abspath(self.backups_root))
        resolved_backup = Path(os.path.abspath(backup))
        if (os.path.commonpath((str(expected_parent), str(resolved_backup)))
                != str(expected_parent)):
            raise ReleaseError("release backup path is unsafe or missing")
        _assert_real_parents(
            self.runtime_root, resolved_backup / ".backup-directory-check",
        )
        try:
            backup_info = os.lstat(resolved_backup)
        except FileNotFoundError as exc:
            raise ReleaseError("release backup path is unsafe or missing") from exc
        if stat.S_ISLNK(backup_info.st_mode) or not stat.S_ISDIR(backup_info.st_mode):
            raise ReleaseError("release backup path is unsafe or missing")
        try:
            self._restore_entries(
                resolved_backup, record["backup_entries"],
                record["restart_services"], record["pre_health_checks"],
                record.get("daemon_reload") is True,
            )
            self._write_state(record["state_before"])
            record["status"] = "rolled_back"
            self._write_release(release_id, record)
        except Exception as exc:
            record["status"] = "rollback_failed"
            record["rollback_failure_type"] = type(exc).__name__
            try:
                self._write_release(release_id, record)
            except Exception:
                pass
            raise RollbackError("explicit rollback was incomplete") from exc
        return {
            "ok": True, "status": "rolled_back", "release_id": release_id,
            "deployed_main_commit": record["from_commit"],
        }


def _engine_from_args(args):
    source_root = Path(os.path.abspath(args.source_root))
    catalog = RuntimeCatalog.load(source_root, args.catalog)
    return ReleaseEngine(
        source_root, args.runtime_root, catalog,
        identity_path=args.identity_file, state_root=args.state_root,
    )


def _parse_reviewed_head_arguments(values):
    result = {}
    for value in values or []:
        release_id, separator, commit = str(value).partition("=")
        if (separator != "=" or not RELEASE_ID_RE.fullmatch(release_id)
                or not COMMIT_RE.fullmatch(commit) or release_id in result):
            raise ReleaseError(
                "reviewed Head must use unique RELEASE_ID=40_CHARACTER_SHA entries"
            )
        result[release_id] = commit
    return result


def _add_runtime_arguments(parser):
    parser.add_argument("--source-root", default=".")
    parser.add_argument("--runtime-root", default="/")
    parser.add_argument("--catalog", default=DEFAULT_CATALOG)
    parser.add_argument("--identity-file", default=DEFAULT_IDENTITY_FILE)
    parser.add_argument("--state-root", default=DEFAULT_STATE_ROOT)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Canonical commit-driven Huangque test release engine",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    impact_parser = subparsers.add_parser("check-impact")
    impact_parser.add_argument("--source-root", default=".")
    impact_parser.add_argument("--catalog", default=DEFAULT_CATALOG)
    impact_parser.add_argument("--base", required=True)
    impact_parser.add_argument("--target", required=True)
    plan_parser = subparsers.add_parser("plan")
    _add_runtime_arguments(plan_parser)
    plan_parser.add_argument("--target-commit", required=True)
    initialize_parser = subparsers.add_parser("initialize")
    _add_runtime_arguments(initialize_parser)
    initialize_parser.add_argument("--deployed-commit", required=True)
    initialize_parser.add_argument("--confirm-environment", required=True)
    apply_parser = subparsers.add_parser("apply")
    _add_runtime_arguments(apply_parser)
    apply_parser.add_argument("--target-commit", required=True)
    apply_parser.add_argument(
        "--reviewed-head", action="append", default=[],
        metavar="RELEASE_ID=SHA",
    )
    apply_parser.add_argument("--confirm-environment", required=True)
    status_parser = subparsers.add_parser("status")
    _add_runtime_arguments(status_parser)
    rollback_parser = subparsers.add_parser("rollback")
    _add_runtime_arguments(rollback_parser)
    rollback_parser.add_argument("--release-id", required=True)
    rollback_parser.add_argument("--confirm-environment", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "check-impact":
            source_root = Path(os.path.abspath(args.source_root))
            catalog = RuntimeCatalog.load(source_root, args.catalog)
            repo = GitRepository(source_root)
            result = collect_release_impact(repo, catalog, args.base, args.target)
            if len(result["impacts"]) > 1:
                raise ReleaseError("one pull request must add exactly one release impact file")
            output = {
                "ok": True, "status": "impact_covered",
                "runtime_changes": result["runtime_changes"],
                "release_ids": [item["release_id"] for item in result["impacts"]],
            }
        else:
            engine = _engine_from_args(args)
            if args.command == "plan":
                output = engine.build_plan(args.target_commit)
            elif args.command == "initialize":
                output = engine.initialize(
                    args.deployed_commit, args.confirm_environment,
                )
            elif args.command == "apply":
                output = engine.apply(
                    args.target_commit,
                    _parse_reviewed_head_arguments(args.reviewed_head),
                    args.confirm_environment,
                )
            elif args.command == "status":
                output = engine.status()
            else:
                output = engine.rollback(args.release_id, args.confirm_environment)
    except ReleaseError as exc:
        print(json.dumps({
            "ok": False, "status": "release_blocked",
            "error": str(exc), "error_type": type(exc).__name__,
        }, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
