#!/usr/bin/env python3
"""Fail-closed planning foundation for Huangque test releases.

Phase one deliberately performs no runtime installation, service mutation,
database migration, rollback, or external network preflight.  It provides the
trusted CI impact gate plus host identity, exact-main, deployment-ledger,
inventory, review-topology and read-only plan checks needed before a later
transactional apply engine can be introduced safely.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path, PurePosixPath

if os.name == "posix":
    import fcntl
else:  # pragma: no cover - the release host is Linux
    import msvcrt


SCHEMA_VERSION = 1
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
RELEASE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,79}$")
ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,79}$")
UNIT_RE = re.compile(r"^[A-Za-z0-9@_.:-]+\.(?:service|timer)$")
DEFAULT_CATALOG = "deploy/test-release/runtime-catalog.json"
DEFAULT_IMPACT_PREFIX = "deploy/test-release/impacts/"
DEFAULT_IDENTITY_FILE = "/etc/huangque/release-identity.json"
DEFAULT_STATE_ROOT = "/var/lib/huangque-release"
GIT_BINARY = "/usr/bin/git"


class ReleaseError(RuntimeError):
    """A fail-closed release validation error."""


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
    target = Path(root).joinpath(*absolute.parts[1:])
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


def _fsync_directory(path: Path):
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(root: Path, runtime_path: str, data: bytes, mode: int):
    target = _mapped_path(root, runtime_path)
    _assert_real_parents(root, target, create=True)
    try:
        info = os.lstat(target)
    except FileNotFoundError:
        info = None
    if info is not None and (
            stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)):
        raise ReleaseError("refusing to replace non-regular state target")
    temporary = target.parent / (".hq-release-%s" % uuid.uuid4().hex)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(descriptor, view):]
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, int(mode))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    if _read_regular(root, runtime_path) != data:
        raise ReleaseError("atomic state write verification failed")


def _minimal_subprocess_environment():
    return {
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
    }


def _validate_executable(path: str, *, approved_parent=None):
    path = _absolute_runtime_path(path)
    target = Path(path)
    try:
        info = os.lstat(target)
    except FileNotFoundError as exc:
        raise ReleaseError("required release tool is unavailable: %s" % path) from exc
    inspected = target
    if stat.S_ISLNK(info.st_mode):
        try:
            inspected = target.resolve(strict=True)
        except OSError as exc:
            raise ReleaseError("release tool symbolic link is invalid: %s" % path) from exc
        parent = Path(approved_parent or target.parent).resolve(strict=True)
        if os.path.commonpath((str(parent), str(inspected))) != str(parent):
            raise ReleaseError("release tool symbolic link escaped its approved directory")
        info = os.lstat(inspected)
    if not stat.S_ISREG(info.st_mode) or os.name == "posix" and not info.st_mode & 0o111:
        raise ReleaseError("release tool is not a regular executable: %s" % path)
    return path


class GitRepository:
    def __init__(self, source_root, *, timeout=90, git_binary=GIT_BINARY,
                 validate_binary=True):
        self.root = Path(os.path.abspath(source_root))
        self.timeout = int(timeout)
        self.git_binary = _absolute_runtime_path(git_binary)
        if validate_binary:
            _validate_executable(self.git_binary, approved_parent="/usr/bin")
        self.environment = _minimal_subprocess_environment()

    def run(self, arguments, *, allow_failure=False, binary=False):
        result = subprocess.run(
            [self.git_binary, "-C", str(self.root)] + list(arguments),
            check=False, capture_output=True, timeout=self.timeout,
            env=self.environment, **({} if binary else {"text": True}),
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
            raise ReleaseError("required Git ancestry relation is absent")

    def merge_base(self, left, right):
        self.require_commit(left)
        self.require_commit(right)
        value = self.output(["merge-base", left, right])
        if not COMMIT_RE.fullmatch(value):
            raise ReleaseError("Git merge base is invalid")
        return value

    def parents(self, commit):
        self.require_commit(commit)
        parts = self.output(["rev-list", "--parents", "-n", "1", commit]).split()
        if not parts or parts[0] != commit:
            raise ReleaseError("Git commit parents are invalid")
        return parts[1:]

    def first_parent_commits(self, older, newer):
        self.require_ancestor(older, newer)
        values = self.output([
            "rev-list", "--first-parent", "--reverse", "%s..%s" % (older, newer),
        ]).splitlines()
        if any(not COMMIT_RE.fullmatch(value) for value in values):
            raise ReleaseError("Git first-parent history is invalid")
        return values

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
        self.require_commit(commit)
        repository_path = _relative_repository_path(repository_path)
        result = self.run(
            ["show", "%s:%s" % (commit, repository_path)],
            allow_failure=True, binary=True,
        )
        return None if result.returncode else result.stdout

    def file_mode_at(self, commit, repository_path):
        self.require_commit(commit)
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

    def verify_checkout(self, target_commit, expected_origin_url,
                        *, verify_live_origin=True):
        self.require_commit(target_commit)
        if self.output(["status", "--porcelain", "--untracked-files=normal"]):
            raise ReleaseError("release source checkout must be clean")
        if self.output(["symbolic-ref", "--short", "HEAD"]) != "main":
            raise ReleaseError("release source checkout must be on main")
        if self.output(["remote", "get-url", "origin"]) != expected_origin_url:
            raise ReleaseError("origin repository identity is not approved")
        head = self.output(["rev-parse", "HEAD"])
        origin_main = self.output(["rev-parse", "refs/remotes/origin/main"])
        if head != target_commit or origin_main != target_commit:
            raise ReleaseError("HEAD and local origin/main must equal target commit")
        if verify_live_origin:
            line = self.output([
                "ls-remote", "--exit-code", expected_origin_url, "refs/heads/main",
            ])
            remote_main = line.split()[0] if line else ""
            if remote_main != target_commit:
                raise ReleaseError("live approved origin/main must equal target commit")


class RuntimeCatalog:
    def __init__(self, data):
        if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
            raise ReleaseError("runtime catalog schema version is unsupported")
        target = data.get("target") or {}
        if (target.get("environment") != "test" or not target.get("host_id")
                or target.get("origin_url") !=
                "https://github.com/yuelei-dev/ubuntu-yuelei.git"):
            raise ReleaseError("runtime catalog must lock the approved test repository")
        self.target = dict(target)
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
        self.unmanaged_runtime_paths = frozenset(
            _absolute_runtime_path(item)
            for item in data.get("unmanaged_runtime_paths") or []
        )
        self.unmanaged_runtime_prefixes = tuple(
            _absolute_runtime_path(item).rstrip("/") + "/"
            for item in data.get("unmanaged_runtime_prefixes") or []
        )
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
            mode_text = str(raw.get("mode") or "0644")
            if not re.fullmatch(r"0[0-7]{3}", mode_text):
                raise ReleaseError("runtime catalog file mode is invalid")
            self.rules.append({
                "kind": kind,
                "repository": repository,
                "runtime": runtime,
                "service": service,
                "service_from_repository": service_from_repository,
                "daemon_reload": raw.get("daemon_reload") is True,
                "delete_allowed": raw.get("delete_allowed") is True,
                "allow_unmanaged_runtime": raw.get("allow_unmanaged_runtime") is True,
                "mode": int(mode_text, 8),
            })
        if (not self.rules or not self.candidate_prefixes
                or GIT_BINARY not in self.allowed_tools
                or "/usr/bin/systemctl" not in self.allowed_tools
                or not self.allowed_units):
            raise ReleaseError("runtime catalog is incomplete")

    @classmethod
    def load(cls, source_root, path):
        relative = _relative_repository_path(path)
        return cls(_load_json(Path(source_root) / relative, "runtime catalog"))

    @classmethod
    def load_from_git(cls, repo, commit, path, *, required=True):
        relative = _relative_repository_path(path)
        raw = repo.file_at(commit, relative)
        if raw is None:
            if required:
                raise ReleaseError("runtime catalog is missing from commit: %s" % commit)
            return None
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ReleaseError("runtime catalog in Git is invalid") from exc
        return cls(data)

    def is_candidate(self, repository_path):
        path = _relative_repository_path(repository_path)
        return any(path.startswith(prefix) for prefix in self.candidate_prefixes)

    def is_ignored(self, repository_path):
        path = _relative_repository_path(repository_path)
        return path in self.ignored_paths or any(
            path.startswith(prefix) for prefix in self.ignored_prefixes
        )

    def is_unmanaged_runtime(self, runtime_path):
        path = _absolute_runtime_path(runtime_path)
        return path in self.unmanaged_runtime_paths or any(
            path.startswith(prefix) for prefix in self.unmanaged_runtime_prefixes
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
            if first not in self.allowed_units:
                raise ReleaseError("derived systemd unit is not allowlisted")
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
        "url": url,
        "expected_statuses": sorted(set(statuses)),
        "timeout_seconds": min(max(int(item.get("timeout_seconds") or 60), 1), 120),
        "interval_seconds": min(max(float(item.get("interval_seconds") or 1), .1), 5),
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
    required_services = {item["service"] for item in mappings if item.get("service")}
    if not required_services.issubset(set(services)):
        raise ReleaseError("release impact omits a required service restart")
    required_env = data.get("required_env") or []
    if (not isinstance(required_env, list) or len(set(required_env)) != len(required_env)
            or any(not ENV_NAME_RE.fullmatch(str(item)) for item in required_env)):
        raise ReleaseError("release impact environment declarations are invalid")
    pre_health_checks = [
        _validate_health_check(item) for item in data.get("pre_health_checks") or []
    ]
    health_checks = [
        _validate_health_check(item) for item in data.get("health_checks") or []
    ]
    if runtime_changes and not health_checks:
        raise ReleaseError("runtime changes require a loopback health check")
    if services and not pre_health_checks:
        raise ReleaseError("a service restart requires a pre-release loopback health check")
    if data.get("external_checks"):
        raise ReleaseError(
            "phase-one release contracts forbid executable external checks"
        )
    if data.get("migrations"):
        raise ReleaseError(
            "phase-one release contracts forbid database migrations"
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "release_id": release_id,
        "runtime_changes": runtime_changes,
        "restart_services": sorted(services),
        "required_env": sorted(required_env),
        "pre_health_checks": pre_health_checks,
        "health_checks": health_checks,
        "external_checks": [],
        "migrations": [],
    }


def collect_release_impact(
        repo, catalog, older, newer, *, base_catalog=None,
        catalog_path=DEFAULT_CATALOG, enforce_catalog_isolation=False):
    changes = repo.changed_paths(older, newer)
    catalogs = [item for item in (base_catalog, catalog) if item is not None]
    catalog_path = _relative_repository_path(catalog_path)
    impact_prefixes = {item.impact_prefix for item in catalogs}
    catalog_changed = any(path == catalog_path for _status, path in changes)
    impact_changes = [
        (status_value, path) for status_value, path in changes
        if any(path.startswith(prefix) for prefix in impact_prefixes)
        and path.endswith(".json")
    ]
    candidate_changes = sorted({
        path for _status, path in changes
        if any(item.is_candidate(path) for item in catalogs)
    })
    if (enforce_catalog_isolation and catalog_changed
            and (candidate_changes or impact_changes)):
        raise ReleaseError(
            "runtime catalog changes must be isolated from runtime files and impacts"
        )
    if enforce_catalog_isolation and catalog_changed and base_catalog is not None:
        raise ReleaseError(
            "phase-one runtime catalog is immutable after its bootstrap commit"
        )
    runtime_changes = set()
    for _status, path in changes:
        mappings = []
        mapping_errors = []
        for item in catalogs:
            try:
                mapping = item.map(path)
            except ReleaseError as exc:
                mapping_errors.append(exc)
                continue
            if mapping is not None:
                mappings.append(mapping)
        if not mappings and mapping_errors:
            raise mapping_errors[0]
        if not mappings:
            continue
        runtime_changes.add(path)
        for commit in (older, newer):
            content = repo.file_at(commit, path)
            if (content is not None and repo.file_mode_at(commit, path)
                    not in {"100644", "100755"}):
                raise ReleaseError(
                    "runtime source must be a regular Git blob: %s" % path
                )
    immutable_changes = sorted(
        path for status_value, path in impact_changes if status_value != "A"
    )
    if immutable_changes:
        raise ReleaseError(
            "release impact files are immutable after creation: %s"
            % ", ".join(immutable_changes)
        )
    impacts = []
    for path in sorted({path for status_value, path in impact_changes if status_value != "D"}):
        raw = repo.file_at(newer, path)
        if raw is None:
            raise ReleaseError("release impact disappeared from target commit")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ReleaseError("release impact JSON is invalid: %s" % path) from exc
        impact = validate_impact(data, catalog)
        impact["repository_path"] = path
        impact["blob_sha256"] = _sha256(raw)
        impacts.append(impact)
    declared = {path for impact in impacts for path in impact["runtime_changes"]}
    missing = sorted(runtime_changes - declared)
    extra = sorted(declared - runtime_changes)
    if missing:
        raise ReleaseError("runtime changes lack release impact: %s" % ", ".join(missing))
    if extra:
        raise ReleaseError(
            "release impact declares unchanged runtime paths: %s" % ", ".join(extra)
        )
    release_ids = [item["release_id"] for item in impacts]
    if len(set(release_ids)) != len(release_ids):
        raise ReleaseError("release impact ids must be unique in a commit range")
    return {
        "changed_paths": changes,
        "runtime_changes": sorted(runtime_changes),
        "impacts": impacts,
    }


def _parse_reviewed_head_arguments(values):
    result = {}
    for value in values or []:
        release_id, separator, commit_range = str(value).partition("=")
        base, range_separator, head = commit_range.partition("..")
        if (separator != "=" or range_separator != ".."
                or not RELEASE_ID_RE.fullmatch(release_id)
                or not COMMIT_RE.fullmatch(base) or not COMMIT_RE.fullmatch(head)
                or release_id in result):
            raise ReleaseError(
                "review evidence must use unique RELEASE_ID=MERGE_BASE..HEAD entries"
            )
        result[release_id] = {"base": base, "head": head}
    return result


def verify_review_evidence(repo, catalog, older, target, impacts, evidence):
    expected_ids = {item["release_id"] for item in impacts}
    if set(evidence) != expected_ids:
        raise ReleaseError("review evidence must exactly cover release impact ids")
    merge_commits = repo.first_parent_commits(older, target)
    used_heads = set()
    results = {}
    for impact in impacts:
        release_id = impact["release_id"]
        base = evidence[release_id]["base"]
        head = evidence[release_id]["head"]
        if head in used_heads:
            raise ReleaseError("each release impact requires a distinct reviewed Head")
        used_heads.add(head)
        repo.require_ancestor(base, head)
        repo.require_ancestor(head, target)
        matches = []
        for merge_commit in merge_commits:
            parents = repo.parents(merge_commit)
            if len(parents) == 2 and parents[1] == head:
                matches.append((merge_commit, parents[0]))
        if len(matches) != 1:
            raise ReleaseError(
                "reviewed Head is not the unique second parent of a main merge commit"
            )
        merge_commit, first_parent = matches[0]
        if repo.merge_base(first_parent, head) != base:
            raise ReleaseError("review evidence merge base does not match Git topology")
        pr_contract = collect_release_impact(repo, catalog, base, head)
        if len(pr_contract["impacts"]) != 1:
            raise ReleaseError("reviewed PR range must contain exactly one release impact")
        reviewed_impact = pr_contract["impacts"][0]
        if (reviewed_impact["release_id"] != release_id
                or reviewed_impact["repository_path"] != impact["repository_path"]
                or reviewed_impact["blob_sha256"] != impact["blob_sha256"]
                or reviewed_impact["runtime_changes"] != impact["runtime_changes"]):
            raise ReleaseError("reviewed Head is not bound to the target impact blob")
        results[release_id] = {
            "merge_base": base,
            "head": head,
            "merge_commit": merge_commit,
            "impact_sha256": impact["blob_sha256"],
        }
    return results


class SystemInspector:
    def __init__(self, *, tool_root="/"):
        self.tool_root = Path(os.path.abspath(tool_root))

    def validate_tool(self, tool, catalog):
        tool = _absolute_runtime_path(tool)
        if tool not in catalog.allowed_tools:
            raise ReleaseError("undeclared deployment tool: %s" % tool)
        target = _mapped_path(self.tool_root, tool)
        try:
            info = os.lstat(target)
        except FileNotFoundError as exc:
            raise ReleaseError("deployment tool is unavailable: %s" % tool) from exc
        inspected = target
        if stat.S_ISLNK(info.st_mode):
            inspected = target.resolve(strict=True)
            parent = target.parent.resolve(strict=True)
            if os.path.commonpath((str(parent), str(inspected))) != str(parent):
                raise ReleaseError("deployment tool symbolic link escaped its approved directory")
            info = os.lstat(inspected)
        if not stat.S_ISREG(info.st_mode) or os.name == "posix" and not info.st_mode & 0o111:
            raise ReleaseError("deployment tool is unsafe or not executable: %s" % tool)

    def is_active(self, service, *, timeout=30):
        if not UNIT_RE.fullmatch(service):
            raise ReleaseError("systemd unit name is invalid")
        result = subprocess.run(
            ["/usr/bin/systemctl", "is-active", "--quiet", service],
            check=False, timeout=timeout, env=_minimal_subprocess_environment(),
        )
        return result.returncode == 0


class ReleaseEngine:
    def __init__(
            self, source_root, runtime_root, catalog, *,
            identity_path=DEFAULT_IDENTITY_FILE, state_root=DEFAULT_STATE_ROOT,
            repo=None, inspector=None, health_getter=None, environment=None,
            clock=None, sleeper=None):
        self.source_root = Path(os.path.abspath(source_root))
        self.runtime_root = Path(os.path.abspath(runtime_root))
        self.catalog = catalog
        self.identity_path = _absolute_runtime_path(identity_path)
        self.state_root_path = _absolute_runtime_path(state_root)
        self.state_root = _mapped_path(self.runtime_root, self.state_root_path)
        self.repo = repo or GitRepository(self.source_root)
        self.inspector = inspector or SystemInspector()
        self.health_getter = health_getter or self._http_status
        self.environment = dict(os.environ if environment is None else environment)
        self.clock = clock or time.monotonic
        self.sleeper = sleeper or time.sleep

    @property
    def state_path(self):
        return self.state_root / "state.json"

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
            _fsync_directory(directory.parent)
        info = os.lstat(directory)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ReleaseError("release state directory is unsafe")
        if (os.name == "posix" and (stat.S_IMODE(info.st_mode) & 0o077
                or info.st_uid != os.geteuid())):
            raise ReleaseError("release state directory must be private and owner-controlled")

    @contextlib.contextmanager
    def _release_lock(self):
        self._ensure_state_directory(self.state_root)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.lock_path, flags, 0o600)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise ReleaseError("release lock is not a regular file")
            try:
                if os.name == "posix":
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                else:  # pragma: no cover
                    if os.fstat(descriptor).st_size == 0:
                        os.write(descriptor, b"\0")
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            except (BlockingIOError, OSError) as exc:
                raise ReleaseError("another release planner is already running") from exc
            os.ftruncate(descriptor, 0)
            os.write(descriptor, _json_bytes({
                "pid": os.getpid(), "updated_unix": int(time.time()),
            }))
            os.fsync(descriptor)
            yield
        finally:
            try:
                if os.name == "posix":
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                else:  # pragma: no cover
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            finally:
                os.close(descriptor)

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
        if (os.name == "posix" and (stat.S_IMODE(info.st_mode) & 0o077
                or info.st_uid != os.geteuid())):
            raise ReleaseError("%s must be private and owner-controlled" % label)

    def verify_identity(self):
        self._verify_private_runtime_file(self.identity_path, "release identity")
        identity = self._runtime_json(self.identity_path, "release identity")
        required = {
            "schema_version": SCHEMA_VERSION,
            "environment": self.catalog.target["environment"],
            "host_id": self.catalog.target["host_id"],
        }
        if any(identity.get(key) != value for key, value in required.items()):
            raise ReleaseError("release identity is wrong for this target")
        hostname_bytes = _read_regular(self.runtime_root, "/etc/hostname")
        machine_bytes = _read_regular(self.runtime_root, "/etc/machine-id")
        if hostname_bytes is None or machine_bytes is None:
            raise ReleaseError("host identity inputs are unavailable")
        if (identity.get("hostname") != hostname_bytes.decode("utf-8", "strict").strip()
                or identity.get("machine_id_sha256") != _sha256(machine_bytes.strip())):
            raise ReleaseError("release host fingerprint does not match this machine")
        return identity

    @staticmethod
    def _state_hash(data):
        return {"state": "absent", "sha256": None} if data is None else {
            "state": "file", "sha256": _sha256(data),
        }

    def load_state(self):
        runtime_path = self._state_runtime_path("state.json")
        self._verify_private_runtime_file(runtime_path, "deployment ledger")
        state = self._runtime_json(runtime_path, "deployment ledger")
        required_keys = {
            "schema_version", "environment", "host_id", "deployed_main_commit",
            "runtime_hashes", "repository_paths", "managed_runtime_paths",
            "last_release_id", "last_successful_release",
        }
        if set(state) != required_keys:
            raise ReleaseError("deployment ledger fields are invalid")
        if (state["schema_version"] != SCHEMA_VERSION
                or state["environment"] != self.catalog.target["environment"]
                or state["host_id"] != self.catalog.target["host_id"]
                or not COMMIT_RE.fullmatch(str(state["deployed_main_commit"]))
                or not isinstance(state["runtime_hashes"], dict)
                or not isinstance(state["repository_paths"], dict)
                or not isinstance(state["managed_runtime_paths"], list)
                or sorted(state["runtime_hashes"]) != sorted(state["managed_runtime_paths"])
                or set(state["runtime_hashes"]) != set(state["repository_paths"])):
            raise ReleaseError("deployment ledger is invalid")
        for runtime_path, expected in state["runtime_hashes"].items():
            _absolute_runtime_path(runtime_path)
            _relative_repository_path(state["repository_paths"][runtime_path])
            if (not isinstance(expected, dict) or set(expected) != {"state", "sha256"}
                    or expected.get("state") != "file"
                    or not re.fullmatch(r"[0-9a-f]{64}", str(expected.get("sha256") or ""))):
                raise ReleaseError("deployment ledger contains an invalid runtime hash")
        return state

    def _write_state(self, state):
        _atomic_write(
            self.runtime_root, self._state_runtime_path("state.json"),
            _json_bytes(state), 0o600,
        )

    def _expected_runtime(self, commit):
        runtime_hashes = {}
        repository_paths = {}
        for repository_path in self.repo.files_at(commit):
            mapping = self.catalog.map(repository_path, strict_candidate=False)
            if mapping is None:
                continue
            if self.repo.file_mode_at(commit, repository_path) not in {"100644", "100755"}:
                raise ReleaseError("runtime source must be a regular Git blob")
            runtime_path = mapping["runtime_path"]
            if runtime_path in repository_paths:
                raise ReleaseError("multiple repository files map to one runtime path")
            content = self.repo.file_at(commit, repository_path)
            runtime_hashes[runtime_path] = self._state_hash(content)
            repository_paths[runtime_path] = repository_path
        if not runtime_hashes:
            raise ReleaseError("runtime catalog produced no managed targets")
        return runtime_hashes, repository_paths

    def _runtime_inventory(self):
        inventory = set()
        unsafe = set()
        for rule in self.catalog.rules:
            if rule["kind"] != "prefix" or rule["allow_unmanaged_runtime"]:
                continue
            runtime_prefix = rule["runtime"]
            root = _mapped_path(self.runtime_root, runtime_prefix)
            try:
                root_info = os.lstat(root)
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
                unsafe.add(runtime_prefix.rstrip("/"))
                continue
            for directory, names, filenames in os.walk(root, followlinks=False):
                directory_path = Path(directory)
                for name in list(names):
                    candidate = directory_path / name
                    info = os.lstat(candidate)
                    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                        runtime_path = "/" + candidate.relative_to(self.runtime_root).as_posix()
                        unsafe.add(runtime_path)
                        names.remove(name)
                for name in filenames:
                    candidate = directory_path / name
                    runtime_path = "/" + candidate.relative_to(self.runtime_root).as_posix()
                    info = os.lstat(candidate)
                    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                        unsafe.add(runtime_path)
                    elif not self.catalog.is_unmanaged_runtime(runtime_path):
                        inventory.add(runtime_path)
        return inventory, unsafe

    def _inventory_drift(self, expected_paths):
        inventory, unsafe = self._runtime_inventory()
        managed_prefixes = [
            rule["runtime"] for rule in self.catalog.rules
            if rule["kind"] == "prefix" and not rule["allow_unmanaged_runtime"]
        ]
        expected_inventory = {
            path for path in expected_paths
            if any(path.startswith(prefix) for prefix in managed_prefixes)
        }
        return sorted((inventory - expected_inventory) | unsafe)

    def status(self):
        self.verify_identity()
        state = self.load_state()
        drift = []
        for runtime_path, expected in sorted(state["runtime_hashes"].items()):
            if self._state_hash(_read_regular(self.runtime_root, runtime_path)) != expected:
                drift.append(runtime_path)
        unexpected = self._inventory_drift(set(state["managed_runtime_paths"]))
        return {
            "ok": not drift and not unexpected,
            "status": "deployed" if not drift and not unexpected else "drifted",
            "environment": state["environment"],
            "host_id": state["host_id"],
            "deployed_main_commit": state["deployed_main_commit"],
            "last_release_id": state["last_release_id"],
            "drifted_paths": drift,
            "unexpected_runtime_paths": unexpected,
        }

    def initialize(self, deployed_commit, confirmation, *, verify_live_origin=True):
        if confirmation != "test":
            raise ReleaseError("initialize requires exact test environment confirmation")
        identity = self.verify_identity()
        if _read_regular(self.runtime_root, self._state_runtime_path("state.json")) is not None:
            raise ReleaseError("deployment ledger is already initialized")
        self.repo.verify_checkout(
            deployed_commit, self.catalog.target["origin_url"],
            verify_live_origin=verify_live_origin,
        )
        runtime_hashes, repository_paths = self._expected_runtime(deployed_commit)
        mismatches = [
            path for path, expected in runtime_hashes.items()
            if self._state_hash(_read_regular(self.runtime_root, path)) != expected
        ]
        unexpected = self._inventory_drift(set(runtime_hashes))
        if mismatches or unexpected:
            raise ReleaseError("runtime inventory does not exactly match deployed commit")
        state = {
            "schema_version": SCHEMA_VERSION,
            "environment": identity["environment"],
            "host_id": identity["host_id"],
            "deployed_main_commit": deployed_commit,
            "runtime_hashes": runtime_hashes,
            "repository_paths": repository_paths,
            "managed_runtime_paths": sorted(runtime_hashes),
            "last_release_id": None,
            "last_successful_release": None,
        }
        with self._release_lock():
            if _read_regular(self.runtime_root, self._state_runtime_path("state.json")) is not None:
                raise ReleaseError("deployment ledger was initialized concurrently")
            self._write_state(state)
        return {
            "ok": True,
            "status": "initialized",
            "deployed_main_commit": deployed_commit,
            "tracked_runtime_paths": len(runtime_hashes),
        }

    def _http_status(self, url):
        request = urllib.request.Request(url, headers={"User-Agent": "hq-release-plan"})
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

    def build_plan(self, target_commit, reviewed_evidence=None):
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
                "ok": True,
                "status": "already_deployed",
                "environment": identity["environment"],
                "host_id": identity["host_id"],
                "from_commit": older,
                "target_commit": target_commit,
                "files": [],
                "release_ids": [],
                "review_evidence": {},
            }
        self.repo.require_ancestor(older, target_commit)
        self.repo.verify_checkout(
            target_commit, self.catalog.target["origin_url"], verify_live_origin=True,
        )
        collected = collect_release_impact(self.repo, self.catalog, older, target_commit)
        impacts = collected["impacts"]
        evidence = verify_review_evidence(
            self.repo, self.catalog, older, target_commit, impacts,
            reviewed_evidence or {},
        )
        required_env = sorted({
            name for impact in impacts for name in impact["required_env"]
        })
        missing = [name for name in required_env if not self.environment.get(name)]
        if missing:
            raise ReleaseError("required environment variables are missing: %s" % ", ".join(missing))
        services = sorted({
            service for impact in impacts for service in impact["restart_services"]
        })
        if services:
            self.inspector.validate_tool("/usr/bin/systemctl", self.catalog)
            inactive = [service for service in services if not self.inspector.is_active(service)]
            if inactive:
                raise ReleaseError("required systemd units are not active: %s" % ", ".join(inactive))
        pre_checks = [
            check for impact in impacts for check in impact["pre_health_checks"]
        ]
        self._verify_health(pre_checks)
        files = []
        seen_runtime = set()
        total_bytes = 0
        for status_value, repository_path in collected["changed_paths"]:
            mapping = self.catalog.map(repository_path)
            if mapping is None:
                continue
            before = self.repo.file_at(older, repository_path)
            after = self.repo.file_at(target_commit, repository_path)
            if status_value == "D" and not mapping["delete_allowed"]:
                raise ReleaseError("runtime deletion is not allowed by catalog")
            runtime_path = mapping["runtime_path"]
            if runtime_path in seen_runtime:
                raise ReleaseError("multiple repository files map to one runtime path")
            seen_runtime.add(runtime_path)
            if state["repository_paths"].get(runtime_path) not in {None, repository_path}:
                raise ReleaseError("catalog mapping changed relative to the deployment ledger")
            if self._state_hash(_read_regular(self.runtime_root, runtime_path)) != self._state_hash(before):
                raise ReleaseError("runtime drift detected before release planning")
            files.append({
                "repository_path": repository_path,
                "runtime_path": runtime_path,
                "change": "delete" if after is None else "write",
                "before": self._state_hash(before),
                "after": self._state_hash(after),
                "service": mapping.get("service"),
                "mode": mapping["mode"],
            })
            total_bytes += (0 if before is None else len(before)) + (
                0 if after is None else len(after)
            )
        free = shutil.disk_usage(self.state_root.parent).free
        required_free = max(self.catalog.min_free_bytes, total_bytes * 2)
        if free < required_free:
            raise ReleaseError("insufficient disk space for a future transactional release")
        return {
            "ok": True,
            "status": "planned_read_only",
            "phase": "planning_only_no_apply",
            "environment": identity["environment"],
            "host_id": identity["host_id"],
            "from_commit": older,
            "target_commit": target_commit,
            "files": files,
            "release_ids": [item["release_id"] for item in impacts],
            "review_evidence": evidence,
            "restart_services": services,
            "required_env": required_env,
            "required_free_bytes": required_free,
        }


def _engine_from_args(args):
    source_root = Path(os.path.abspath(args.source_root))
    catalog = RuntimeCatalog.load(source_root, args.catalog)
    return ReleaseEngine(
        source_root, args.runtime_root, catalog,
        identity_path=args.identity_file, state_root=args.state_root,
    )


def _add_runtime_arguments(parser):
    parser.add_argument("--source-root", default=".")
    parser.add_argument("--runtime-root", default="/")
    parser.add_argument("--catalog", default=DEFAULT_CATALOG)
    parser.add_argument("--identity-file", default=DEFAULT_IDENTITY_FILE)
    parser.add_argument("--state-root", default=DEFAULT_STATE_ROOT)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Huangque test release contract and read-only planning foundation",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    impact_parser = subparsers.add_parser("check-impact")
    impact_parser.add_argument("--source-root", default=".")
    impact_parser.add_argument("--catalog", default=DEFAULT_CATALOG)
    impact_parser.add_argument("--base", required=True)
    impact_parser.add_argument("--target", required=True)
    initialize_parser = subparsers.add_parser("initialize")
    _add_runtime_arguments(initialize_parser)
    initialize_parser.add_argument("--deployed-commit", required=True)
    initialize_parser.add_argument("--confirm-environment", required=True)
    plan_parser = subparsers.add_parser("plan")
    _add_runtime_arguments(plan_parser)
    plan_parser.add_argument("--target-commit", required=True)
    plan_parser.add_argument(
        "--reviewed-head", action="append", default=[],
        metavar="RELEASE_ID=MERGE_BASE..HEAD",
    )
    status_parser = subparsers.add_parser("status")
    _add_runtime_arguments(status_parser)
    args = parser.parse_args(argv)
    try:
        if args.command == "check-impact":
            source_root = Path(os.path.abspath(args.source_root))
            repo = GitRepository(source_root)
            catalog = RuntimeCatalog.load_from_git(repo, args.target, args.catalog)
            base_catalog = RuntimeCatalog.load_from_git(
                repo, args.base, args.catalog, required=False,
            )
            result = collect_release_impact(
                repo, catalog, args.base, args.target,
                base_catalog=base_catalog, catalog_path=args.catalog,
                enforce_catalog_isolation=True,
            )
            if len(result["impacts"]) > 1:
                raise ReleaseError("one pull request must add exactly one release impact file")
            output = {
                "ok": True,
                "status": "impact_covered",
                "runtime_changes": result["runtime_changes"],
                "release_ids": [item["release_id"] for item in result["impacts"]],
            }
        else:
            engine = _engine_from_args(args)
            if args.command == "initialize":
                output = engine.initialize(
                    args.deployed_commit, args.confirm_environment,
                )
            elif args.command == "plan":
                output = engine.build_plan(
                    args.target_commit,
                    _parse_reviewed_head_arguments(args.reviewed_head),
                )
            elif args.command == "status":
                output = engine.status()
            else:  # pragma: no cover
                raise ReleaseError("unsupported command")
    except ReleaseError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
