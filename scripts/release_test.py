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
import ssl
import stat
import subprocess
import sys
import time
import uuid
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath

if os.name == "posix":
    import fcntl
else:  # pragma: no cover - the release host is Linux
    import msvcrt


SCHEMA_VERSION = 1
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
RELEASE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,79}$")
PROBE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,79}$")
ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,79}$")
UNIT_RE = re.compile(r"^[A-Za-z0-9@_.:-]+\.(?:service|timer)$")
DEFAULT_CATALOG = "deploy/test-release/runtime-catalog.json"
DEFAULT_IMPACT_PREFIX = "deploy/test-release/impacts/"
DEFAULT_IDENTITY_FILE = "/etc/huangque/release-identity.json"
DEFAULT_STATE_ROOT = "/var/lib/huangque-release"
DEFAULT_SOURCE_ROOT = "/opt/huangque-test-release"
RUNTIME_LAUNCHER = "/usr/local/sbin/huangque-release-test"
RUNTIME_ENTRYPOINT = "/usr/local/libexec/huangque-release/release_test.py"
RUNTIME_BOOTSTRAP_MANIFEST = "/etc/huangque/release-bootstrap.json"
SYSTEM_CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"
GIT_BINARY = "/usr/bin/git"
TRUST_ROOT_PATHS = frozenset({
    ".github/workflows/ci.yml",
    ".github/workflows/release-impact-gate.yml",
    DEFAULT_CATALOG,
    "deploy/test-release/bootstrap.example.json",
    "scripts/release_test.py",
    "scripts/release_test_launcher.sh",
    "deploy/hermes-ip12-release.sh",
    "ship",
})


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


def _read_regular_record(root: Path, runtime_path: str):
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
        descriptor_info = os.fstat(descriptor)
        if not stat.S_ISREG(descriptor_info.st_mode):
            raise ReleaseError("runtime target changed while it was opened: %s" % runtime_path)
        chunks = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                return b"".join(chunks), descriptor_info
            chunks.append(chunk)
    finally:
        os.close(descriptor)


def _read_regular(root: Path, runtime_path: str):
    record = _read_regular_record(root, runtime_path)
    return None if record is None else record[0]


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
        "GIT_ASKPASS": "/bin/false",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_PROTOCOL_FROM_USER": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
        "SSH_ASKPASS": "/bin/false",
    }


def _verify_root_owned_path_chain(path, *, final_kind, final_mode=None):
    target = Path(os.path.abspath(path))
    if not target.is_absolute():  # pragma: no cover - abspath always produces absolute
        raise ReleaseError("trusted runtime path must be absolute")
    current = Path(target.anchor)
    for index, part in enumerate(target.parts[1:], start=1):
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError as exc:
            raise ReleaseError("trusted runtime path is unavailable: %s" % target) from exc
        is_final = index == len(target.parts) - 1
        expected_kind = final_kind if is_final else "directory"
        if (stat.S_ISLNK(info.st_mode) or info.st_uid != 0
                or info.st_mode & 0o022
                or expected_kind == "directory" and not stat.S_ISDIR(info.st_mode)
                or expected_kind == "file" and not stat.S_ISREG(info.st_mode)):
            raise ReleaseError("trusted runtime path ownership or mode is invalid: %s" % current)
        if is_final and final_mode is not None \
                and stat.S_IMODE(info.st_mode) != final_mode:
            raise ReleaseError("trusted runtime path mode is invalid: %s" % current)


def _system_tls_context():
    _verify_root_owned_path_chain(SYSTEM_CA_BUNDLE, final_kind="file")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cafile=SYSTEM_CA_BUNDLE)
    context.keylog_filename = None
    return context


def _github_main_commit(origin_url, *, timeout=30):
    if origin_url != "https://github.com/yuelei-dev/ubuntu-yuelei.git":
        raise ReleaseError("live origin resolver only accepts the approved repository")
    url = "https://api.github.com/repos/yuelei-dev/ubuntu-yuelei/git/ref/heads/main"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "huangque-test-release/1",
        },
        method="GET",
    )
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=_system_tls_context()),
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            if response.geturl() != url:
                raise ReleaseError("live origin verification was redirected")
            raw = response.read(128 * 1024 + 1)
    except (OSError, urllib.error.URLError) as exc:
        raise ReleaseError("live approved origin is unavailable") from exc
    if len(raw) > 128 * 1024:
        raise ReleaseError("live origin response exceeded its size limit")
    try:
        payload = json.loads(raw.decode("utf-8"))
        commit = payload["object"]["sha"]
    except (KeyError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseError("live origin response is invalid") from exc
    if not COMMIT_RE.fullmatch(str(commit or "")):
        raise ReleaseError("live origin main commit is invalid")
    return commit


def _verify_runtime_entrypoint(source_root):
    if os.name != "posix" or os.geteuid() != 0:
        raise ReleaseError("runtime release commands require the root-owned installed entrypoint")
    if (not sys.flags.isolated or not sys.flags.ignore_environment
            or not sys.flags.no_user_site or not sys.flags.dont_write_bytecode):
        raise ReleaseError("runtime release Python must use isolated mode")
    approved_python = os.path.realpath("/usr/bin/python3")
    if os.path.realpath(sys.executable) != approved_python:
        raise ReleaseError("runtime release Python executable is not approved")
    _verify_root_owned_path_chain(approved_python, final_kind="file")
    expected_environment = {
        "HOME": "/root",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
    }
    if dict(os.environ) != expected_environment:
        raise ReleaseError("runtime release process environment is not isolated")
    _verify_root_owned_path_chain(
        RUNTIME_LAUNCHER, final_kind="file", final_mode=0o755,
    )
    _verify_root_owned_path_chain(
        RUNTIME_ENTRYPOINT, final_kind="file", final_mode=0o755,
    )
    _verify_root_owned_path_chain(
        RUNTIME_BOOTSTRAP_MANIFEST, final_kind="file", final_mode=0o600,
    )
    _verify_root_owned_path_chain(
        DEFAULT_SOURCE_ROOT, final_kind="directory",
    )
    manifest_record = _read_regular_record(Path("/"), RUNTIME_BOOTSTRAP_MANIFEST)
    if (manifest_record is None or manifest_record[1].st_uid != 0
            or stat.S_IMODE(manifest_record[1].st_mode) != 0o600):
        raise ReleaseError("runtime release bootstrap manifest is not trusted")
    manifest_raw = manifest_record[0]
    try:
        manifest = json.loads(manifest_raw.decode("utf-8"))
    except (AttributeError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseError("runtime release bootstrap manifest is unavailable") from exc
    if (not isinstance(manifest, dict)
            or set(manifest) != {
                "schema_version", "launcher", "launcher_sha256",
                "entrypoint", "entrypoint_sha256", "source_root",
            }
            or type(manifest.get("schema_version")) is not int
            or manifest["schema_version"] != SCHEMA_VERSION
            or manifest.get("launcher") != RUNTIME_LAUNCHER
            or not re.fullmatch(r"[0-9a-f]{64}", str(manifest.get("launcher_sha256") or ""))
            or manifest.get("entrypoint") != RUNTIME_ENTRYPOINT
            or not re.fullmatch(r"[0-9a-f]{64}", str(manifest.get("entrypoint_sha256") or ""))
            or manifest.get("source_root") != DEFAULT_SOURCE_ROOT):
        raise ReleaseError("runtime release bootstrap manifest is invalid")
    if os.path.abspath(__file__) != RUNTIME_ENTRYPOINT:
        raise ReleaseError("runtime release command was not launched from the installed entrypoint")
    launcher_record = _read_regular_record(Path("/"), RUNTIME_LAUNCHER)
    record = _read_regular_record(Path("/"), RUNTIME_ENTRYPOINT)
    if (launcher_record is None
            or _sha256(launcher_record[0]) != manifest["launcher_sha256"]
            or record is None or record[1].st_uid != 0
            or stat.S_IMODE(record[1].st_mode) != 0o755
            or _sha256(record[0]) != manifest["entrypoint_sha256"]):
        raise ReleaseError("installed runtime release entrypoint is not trusted")
    if os.path.abspath(source_root) != DEFAULT_SOURCE_ROOT:
        raise ReleaseError("runtime release source root is not the bootstrap-approved mirror")


def _default_owner_resolver(owner):
    if os.name != "posix":  # pragma: no cover - metadata enforcement is Linux-only
        return None
    import pwd
    try:
        return int(pwd.getpwnam(owner).pw_uid)
    except KeyError as exc:
        raise ReleaseError("runtime catalog owner is unavailable: %s" % owner) from exc


def _default_group_resolver(group):
    if os.name != "posix":  # pragma: no cover - metadata enforcement is Linux-only
        return None
    import grp
    try:
        return int(grp.getgrnam(group).gr_gid)
    except KeyError as exc:
        raise ReleaseError("runtime catalog group is unavailable: %s" % group) from exc


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
                 validate_binary=True, validate_repository=True,
                 remote_main_resolver=None):
        self.root = Path(os.path.abspath(source_root))
        self.timeout = int(timeout)
        self.git_binary = _absolute_runtime_path(git_binary)
        if validate_binary:
            _validate_executable(self.git_binary, approved_parent="/usr/bin")
        self.environment = _minimal_subprocess_environment()
        self.git_prefix = [
            self.git_binary,
            "--no-optional-locks",
            "-c", "core.fsmonitor=false",
            "-c", "core.hooksPath=/dev/null",
            "-c", "credential.helper=",
            "-c", "core.askPass=",
            "-c", "http.proxy=",
            "-c", "https.proxy=",
            "-c", "http.sslVerify=true",
            "-c", "protocol.file.allow=never",
            "-c", "protocol.ext.allow=never",
            "-c", "protocol.git.allow=never",
            "-c", "protocol.ssh.allow=never",
            "-c", "protocol.http.allow=never",
            "-c", "protocol.https.allow=always",
        ]
        self.remote_main_resolver = remote_main_resolver or _github_main_commit
        if validate_repository:
            self._verify_controlled_repository()

    def _verify_controlled_repository(self):
        if os.name != "posix":  # pragma: no cover - release host and CI are Linux
            return
        expected_uid = os.geteuid()
        for path, kind in ((self.root, "source root"), (self.root / ".git", "Git root")):
            try:
                info = os.lstat(path)
            except FileNotFoundError as exc:
                raise ReleaseError("release %s is missing" % kind) from exc
            if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
                    or info.st_uid != expected_uid or info.st_mode & 0o022):
                raise ReleaseError(
                    "release %s must be a real current-user-owned non-writable directory"
                    % kind
                )
        git_root = self.root / ".git"
        forbidden_metadata = (
            git_root / "objects" / "info" / "alternates",
            git_root / "objects" / "info" / "http-alternates",
            git_root / "info" / "grafts",
            git_root / "refs" / "replace",
            git_root / "shallow",
        )
        if any(path.exists() or path.is_symlink() for path in forbidden_metadata):
            raise ReleaseError("Git replacement, graft, alternate, or shallow metadata is not allowed")
        for directory, names, filenames in os.walk(self.root, followlinks=False):
            directory_path = Path(directory)
            for name in list(names) + list(filenames):
                candidate = directory_path / name
                info = os.lstat(candidate)
                if (stat.S_ISLNK(info.st_mode) or info.st_uid != expected_uid
                        or info.st_mode & 0o022):
                    raise ReleaseError(
                        "release repository files must be current-user-owned and non-writable"
                    )
        config_path = git_root / "config"
        try:
            config_text = config_path.read_text("utf-8").lower()
        except (OSError, UnicodeError) as exc:
            raise ReleaseError("Git local configuration is unavailable") from exc
        forbidden_sections = (
            '[alias', '[credential', '[diff', '[filter', '[http', '[include', '[url',
        )
        forbidden_keys = (
            "fsmonitor", "hookspath", "askpass", "sshcommand", "proxy",
            "sslverify", "helper", "external", "textconv", "process",
            "insteadof", "pushinsteadof", "worktree",
        )
        for raw_line in config_text.splitlines():
            line = raw_line.strip()
            if (line.startswith("[") and line.startswith(forbidden_sections)):
                raise ReleaseError("Git local configuration contains a forbidden section")
            key = line.split("=", 1)[0].strip().replace(" ", "")
            if key and any(item in key for item in forbidden_keys):
                raise ReleaseError("Git local configuration contains a forbidden key")
        packed_refs = git_root / "packed-refs"
        if packed_refs.exists():
            try:
                packed_text = packed_refs.read_text("utf-8")
            except (OSError, UnicodeError) as exc:
                raise ReleaseError("Git packed refs are unavailable") from exc
            if any(" refs/replace/" in line for line in packed_text.splitlines()):
                raise ReleaseError("Git packed replacement refs are not allowed")

    def run(self, arguments, *, allow_failure=False, binary=False):
        result = subprocess.run(
            self.git_prefix + ["-C", str(self.root)] + list(arguments),
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

    def file_oid_at(self, commit, repository_path):
        self.require_commit(commit)
        repository_path = _relative_repository_path(repository_path)
        raw = self.run(
            ["ls-tree", "-z", commit, "--", repository_path], binary=True,
        ).stdout
        if not raw:
            return None
        header = raw.split(b"\t", 1)[0].split()
        if len(header) != 3 or header[1] != b"blob":
            raise ReleaseError("Git tree returned an invalid blob record")
        value = header[2].decode("ascii", "strict")
        if not re.fullmatch(r"[0-9a-f]{40,64}", value):
            raise ReleaseError("Git blob object id is invalid")
        return value

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
            remote_main = self.remote_main_resolver(expected_origin_url)
            if remote_main != target_commit:
                raise ReleaseError("live approved origin/main must equal target commit")


class RuntimeCatalog:
    def __init__(self, data, *, source_bytes=None,
                 repository_path=DEFAULT_CATALOG):
        if (not isinstance(data, dict)
                or type(data.get("schema_version")) is not int
                or data.get("schema_version") != SCHEMA_VERSION):
            raise ReleaseError("runtime catalog schema version is unsupported")
        self.repository_path = _relative_repository_path(repository_path)
        self.source_bytes = bytes(source_bytes or _json_bytes(data))
        self.source_sha256 = _sha256(self.source_bytes)
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
        self.candidate_paths = frozenset(
            _relative_repository_path(item)
            for item in data.get("runtime_candidate_paths") or []
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
        raw_min_free_bytes = data.get("min_free_bytes")
        if type(raw_min_free_bytes) is not int or raw_min_free_bytes < 0:
            raise ReleaseError("runtime catalog minimum free bytes is invalid")
        self.min_free_bytes = raw_min_free_bytes
        if data.get("unmanaged_runtime_paths") not in (None, []) \
                or data.get("unmanaged_runtime_prefixes") not in (None, []):
            raise ReleaseError("legacy unmanaged runtime exemptions are forbidden")
        raw_runtime_data = data.get("runtime_data")
        if not isinstance(raw_runtime_data, list) or not raw_runtime_data:
            raise ReleaseError("runtime data contracts are incomplete")
        self.runtime_data_contracts = []
        self.runtime_data_exact = {}
        self.runtime_data_directories = []
        for raw in raw_runtime_data:
            if (not isinstance(raw, dict)
                    or set(raw) != {
                        "path", "kind", "owner", "group", "allowed_modes", "required",
                    }
                    or raw.get("kind") not in {
                        "secret_file", "sqlite_file", "mutable_file", "mutable_directory",
                    }
                    or type(raw.get("required")) is not bool
                    or not isinstance(raw.get("allowed_modes"), list)
                    or not raw["allowed_modes"]
                    or len(set(raw["allowed_modes"])) != len(raw["allowed_modes"])
                    or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", str(raw.get("owner")))
                    or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", str(raw.get("group")))):
                raise ReleaseError("runtime data contract is invalid")
            modes = []
            for mode_text in raw["allowed_modes"]:
                if not isinstance(mode_text, str) or not re.fullmatch(r"0[0-7]{3}", mode_text):
                    raise ReleaseError("runtime data mode is invalid")
                mode = int(mode_text, 8)
                if (raw["kind"] == "secret_file" and mode & 0o077
                        or raw["kind"] != "secret_file" and mode & 0o007
                        or raw["kind"] == "mutable_directory" and mode & 0o022):
                    raise ReleaseError("runtime data mode is too permissive")
                modes.append(mode)
            path = _absolute_runtime_path(raw["path"])
            contract = {
                "path": path.rstrip("/") + "/"
                if raw["kind"] == "mutable_directory" else path,
                "kind": raw["kind"],
                "owner": raw["owner"],
                "group": raw["group"],
                "allowed_modes": frozenset(modes),
                "required": raw["required"],
            }
            key = contract["path"]
            if (key in self.runtime_data_exact
                    or any(item["path"] == key for item in self.runtime_data_directories)):
                raise ReleaseError("runtime data paths are duplicated")
            self.runtime_data_contracts.append(contract)
            if contract["kind"] == "mutable_directory":
                self.runtime_data_directories.append(contract)
            else:
                self.runtime_data_exact[key] = contract
        directory_paths = [item["path"] for item in self.runtime_data_directories]
        if (any(path.startswith(prefix) for path in self.runtime_data_exact
                for prefix in directory_paths)
                or any(left != right and left.startswith(right)
                       for left in directory_paths for right in directory_paths)):
            raise ReleaseError("runtime data contracts overlap")
        ignored_directory_names = data.get("ignored_runtime_directory_names") or []
        if ignored_directory_names != []:
            raise ReleaseError("runtime catalog ignored directory names are invalid")
        self.ignored_runtime_directory_names = frozenset(ignored_directory_names)
        owner_rules = data.get("runtime_owner_rules")
        if not isinstance(owner_rules, list) or not owner_rules:
            raise ReleaseError("runtime catalog owner rules are incomplete")
        self.runtime_owner_rules = []
        for raw in owner_rules:
            if (not isinstance(raw, dict)
                    or set(raw) != {"runtime_prefix", "owner", "group"}):
                raise ReleaseError("runtime catalog owner rule is invalid")
            prefix = _absolute_runtime_path(raw["runtime_prefix"]).rstrip("/") + "/"
            owner = str(raw["owner"])
            group = str(raw["group"])
            if (not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", owner)
                    or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", group)):
                raise ReleaseError("runtime catalog owner or group is invalid")
            self.runtime_owner_rules.append((prefix, owner, group))
        raw_inventory_roots = data.get("inventory_roots")
        if not isinstance(raw_inventory_roots, list) or not raw_inventory_roots:
            raise ReleaseError("runtime catalog inventory roots are incomplete")
        self.inventory_roots = tuple(
            _absolute_runtime_path(item).rstrip("/") + "/"
            for item in raw_inventory_roots
        )
        if len(set(self.inventory_roots)) != len(self.inventory_roots):
            raise ReleaseError("runtime catalog inventory roots are duplicated")
        raw_exact_paths = data.get("inventory_exact_paths")
        if not isinstance(raw_exact_paths, list):
            raise ReleaseError("runtime catalog exact inventory paths are invalid")
        self.inventory_exact_paths = frozenset(
            _absolute_runtime_path(item) for item in raw_exact_paths
        )
        if len(self.inventory_exact_paths) != len(raw_exact_paths):
            raise ReleaseError("runtime catalog exact inventory paths are duplicated")
        raw_symlinks = data.get("required_runtime_symlinks")
        if not isinstance(raw_symlinks, dict):
            raise ReleaseError("runtime catalog symlink contracts are invalid")
        self.required_runtime_symlinks = {}
        for path, target_path in raw_symlinks.items():
            path = _absolute_runtime_path(path)
            target_path = _absolute_runtime_path(target_path)
            if path in self.required_runtime_symlinks:
                raise ReleaseError("runtime catalog symlink paths are duplicated")
            self.required_runtime_symlinks[path] = target_path
        raw_name_guards = data.get("inventory_name_guards")
        if not isinstance(raw_name_guards, list):
            raise ReleaseError("runtime catalog name guards are invalid")
        self.inventory_name_guards = []
        for raw in raw_name_guards:
            if (not isinstance(raw, dict)
                    or set(raw) != {"directory", "prefix", "allowed_names"}
                    or not isinstance(raw.get("prefix"), str)
                    or not raw["prefix"] or "/" in raw["prefix"]
                    or not isinstance(raw.get("allowed_names"), list)
                    or len(set(raw["allowed_names"])) != len(raw["allowed_names"])
                    or any(not isinstance(name, str) or "/" in name
                           or not name.startswith(raw["prefix"])
                           for name in raw["allowed_names"])):
                raise ReleaseError("runtime catalog name guard is invalid")
            self.inventory_name_guards.append({
                "directory": _absolute_runtime_path(raw["directory"]),
                "prefix": raw["prefix"],
                "allowed_names": frozenset(raw["allowed_names"]),
            })
        raw_preconditions = data.get("service_preconditions")
        if (not isinstance(raw_preconditions, dict)
                or set(raw_preconditions) != set(self.allowed_units)
                or any(value not in {"active", "loaded"}
                       for value in raw_preconditions.values())):
            raise ReleaseError("runtime catalog service preconditions are invalid")
        self.service_preconditions = dict(raw_preconditions)
        raw_service_environment = data.get("service_environment")
        if (not isinstance(raw_service_environment, dict)
                or set(raw_service_environment) != set(self.allowed_units)):
            raise ReleaseError("runtime catalog service environment is incomplete")
        self.service_environment = {}
        for unit, raw in raw_service_environment.items():
            if (not isinstance(raw, dict) or set(raw) != {"files", "inline"}
                    or not isinstance(raw["files"], list)
                    or not isinstance(raw["inline"], list)
                    or len(set(raw["files"])) != len(raw["files"])
                    or len(set(raw["inline"])) != len(raw["inline"])):
                raise ReleaseError("runtime catalog service environment is invalid")
            files = [_absolute_runtime_path(item) for item in raw["files"]]
            if (any(path not in self.runtime_data_exact
                    or self.runtime_data_exact[path]["kind"] != "secret_file"
                    for path in files)
                    or any(not ENV_NAME_RE.fullmatch(str(item)) for item in raw["inline"])):
                raise ReleaseError("runtime catalog service environment source is invalid")
            self.service_environment[unit] = {
                "files": files,
                "inline": frozenset(str(item) for item in raw["inline"]),
            }
        raw_probes = data.get("health_probes")
        if not isinstance(raw_probes, dict) or not raw_probes:
            raise ReleaseError("runtime catalog health probes are incomplete")
        self.health_probes = {}
        for probe_id, raw in raw_probes.items():
            if not PROBE_ID_RE.fullmatch(str(probe_id)) or not isinstance(raw, dict):
                raise ReleaseError("runtime catalog health probe is invalid")
            if set(raw) != {"description"} or not str(raw["description"]).strip():
                raise ReleaseError("runtime catalog health probe contract is invalid")
            self.health_probes[str(probe_id)] = {
                "id": str(probe_id),
                "description": str(raw["description"]).strip(),
            }
        raw_service_probes = data.get("service_health_probes")
        if not isinstance(raw_service_probes, dict):
            raise ReleaseError("runtime catalog service health probes are invalid")
        self.service_health_probes = {}
        for unit, probe_id in raw_service_probes.items():
            if (not UNIT_RE.fullmatch(str(unit))
                    or str(unit) not in self.allowed_units
                    or str(probe_id) not in self.health_probes):
                raise ReleaseError("runtime catalog service health probe is invalid")
            self.service_health_probes[str(unit)] = str(probe_id)
        self.rules = []
        for raw in data.get("rules") or []:
            for field in (
                    "service_from_repository", "daemon_reload", "delete_allowed",
                    "allow_unmanaged_runtime"):
                if field in raw and type(raw[field]) is not bool:
                    raise ReleaseError("runtime catalog rule boolean is invalid")
            if raw.get("allow_unmanaged_runtime") is True:
                raise ReleaseError("runtime mappings cannot bypass complete inventory")
            kind = raw.get("kind")
            repository = _relative_repository_path(raw.get("repository") or "")
            runtime = _absolute_runtime_path(raw.get("runtime") or "")
            if kind not in {"exact", "prefix"}:
                raise ReleaseError("runtime catalog rule kind is invalid")
            if kind == "prefix":
                repository = repository.rstrip("/") + "/"
                runtime = runtime.rstrip("/") + "/"
            service = raw.get("service")
            services = raw.get("services")
            service_from_repository = raw.get("service_from_repository") is True
            if services is not None and (
                    not isinstance(services, list) or not services
                    or len(set(services)) != len(services)
                    or any(not UNIT_RE.fullmatch(str(item)) for item in services)):
                raise ReleaseError("runtime catalog service list is invalid")
            if service is not None and not UNIT_RE.fullmatch(str(service)):
                raise ReleaseError("runtime catalog service name is invalid")
            if service is not None and services is not None:
                raise ReleaseError("runtime catalog service strategy is ambiguous")
            if (service is not None or services is not None) and service_from_repository:
                raise ReleaseError("runtime catalog service strategy is ambiguous")
            rule_services = ([str(service)] if service is not None
                             else [str(item) for item in services or []])
            if not set(rule_services).issubset(self.allowed_units):
                raise ReleaseError("runtime catalog service is not allowlisted")
            if service_from_repository and kind != "prefix":
                raise ReleaseError("derived systemd units require a prefix mapping")
            mode_text = str(raw.get("mode") or "0644")
            if not re.fullmatch(r"0[0-7]{3}", mode_text):
                raise ReleaseError("runtime catalog file mode is invalid")
            health_probe = raw.get("health_probe")
            if health_probe is not None and str(health_probe) not in self.health_probes:
                raise ReleaseError("runtime catalog rule health probe is invalid")
            planning_blocker = raw.get("planning_blocker")
            if planning_blocker is not None and (
                    not isinstance(planning_blocker, str)
                    or not planning_blocker.strip()):
                raise ReleaseError("runtime catalog planning blocker is invalid")
            self.rules.append({
                "kind": kind,
                "repository": repository,
                "runtime": runtime,
                "service": service,
                "services": rule_services,
                "service_from_repository": service_from_repository,
                "daemon_reload": raw.get("daemon_reload") is True,
                "delete_allowed": raw.get("delete_allowed") is True,
                "health_probe": None if health_probe is None else str(health_probe),
                "planning_blocker": (
                    None if planning_blocker is None else planning_blocker.strip()
                ),
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
        source = Path(source_root) / relative
        try:
            raw = source.read_bytes()
            data = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ReleaseError("runtime catalog is unavailable or invalid") from exc
        return cls(data, source_bytes=raw, repository_path=relative)

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
        return cls(data, source_bytes=raw, repository_path=relative)

    def is_candidate(self, repository_path):
        path = _relative_repository_path(repository_path)
        return path in self.candidate_paths or any(
            path.startswith(prefix) for prefix in self.candidate_prefixes
        )

    def is_ignored(self, repository_path):
        path = _relative_repository_path(repository_path)
        return path in self.ignored_paths or any(
            path.startswith(prefix) for prefix in self.ignored_prefixes
        )

    def is_unmanaged_runtime(self, runtime_path):
        path = _absolute_runtime_path(runtime_path)
        return path in self.runtime_data_exact or any(
            path.startswith(contract["path"])
            for contract in self.runtime_data_directories
        )

    def runtime_data_contract(self, runtime_path):
        path = _absolute_runtime_path(runtime_path)
        if path in self.runtime_data_exact:
            return self.runtime_data_exact[path]
        matches = [
            item for item in self.runtime_data_directories
            if path.startswith(item["path"])
        ]
        if not matches:
            return None
        return max(matches, key=lambda item: len(item["path"]))

    def is_inventory_scoped(self, runtime_path):
        path = _absolute_runtime_path(runtime_path)
        if path in self.inventory_exact_paths or any(
                path.startswith(prefix) for prefix in self.inventory_roots):
            return True
        systemd_prefix = "/etc/systemd/system/"
        if not path.startswith(systemd_prefix):
            return False
        relative = path[len(systemd_prefix):]
        first = relative.split("/", 1)[0]
        unit = first[:-2] if first.endswith((".service.d", ".timer.d")) else first
        return unit in self.allowed_units

    def owner_for(self, runtime_path):
        path = _absolute_runtime_path(runtime_path)
        matches = [item for item in self.runtime_owner_rules if path.startswith(item[0])]
        if not matches:
            raise ReleaseError("runtime path has no catalog owner: %s" % path)
        longest = max(len(item[0]) for item in matches)
        strongest = [item for item in matches if len(item[0]) == longest]
        if len(strongest) != 1:
            raise ReleaseError("runtime path has ambiguous catalog owners: %s" % path)
        return strongest[0][1], strongest[0][2]

    def mappings(self, repository_path, *, strict_candidate=True):
        path = _relative_repository_path(repository_path)
        if self.is_ignored(path):
            return []
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
            return []
        longest = max(item[0] for item in matches)
        strongest = [item for item in matches if item[0] == longest]
        results = []
        seen_runtime = set()
        for _length, rule, suffix in strongest:
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
                mapped["services"] = [first]
                if first not in self.allowed_units:
                    raise ReleaseError("derived systemd unit is not allowlisted")
            mapped["repository_path"] = path
            mapped["runtime_path"] = (
                rule["runtime"] + suffix if rule["kind"] == "prefix"
                else rule["runtime"]
            )
            mapped["owner"], mapped["group"] = self.owner_for(
                mapped["runtime_path"]
            )
            if mapped["runtime_path"] in seen_runtime:
                raise ReleaseError("runtime catalog repeats a runtime mapping for: %s" % path)
            seen_runtime.add(mapped["runtime_path"])
            results.append(mapped)
        return results

    def map(self, repository_path, *, strict_candidate=True):
        mappings = self.mappings(
            repository_path, strict_candidate=strict_candidate,
        )
        if len(mappings) > 1:
            raise ReleaseError("runtime path has multiple catalog targets: %s" % repository_path)
        return mappings[0] if mappings else None


def validate_impact(data, catalog):
    required_fields = {
        "schema_version", "release_id", "runtime_changes", "restart_services",
        "required_env", "pre_health_checks", "health_checks",
        "external_checks", "migrations",
    }
    if not isinstance(data, dict) or set(data) != required_fields:
        raise ReleaseError("release impact fields are invalid")
    if (type(data.get("schema_version")) is not int
            or data.get("schema_version") != SCHEMA_VERSION):
        raise ReleaseError("release impact schema version is unsupported")
    release_id = data.get("release_id")
    if not isinstance(release_id, str) or not RELEASE_ID_RE.fullmatch(release_id):
        raise ReleaseError("release impact id is invalid")
    runtime_changes = data.get("runtime_changes")
    if (not isinstance(runtime_changes, list) or not runtime_changes
            or len(set(runtime_changes)) != len(runtime_changes)):
        raise ReleaseError("release impact must declare unique runtime changes")
    runtime_changes = [_relative_repository_path(item) for item in runtime_changes]
    mapping_groups = [catalog.mappings(item) for item in runtime_changes]
    if any(not item for item in mapping_groups):
        raise ReleaseError("release impact declared a non-runtime path")
    mappings = [mapping for group in mapping_groups for mapping in group]
    services = data.get("restart_services")
    if (not isinstance(services, list) or len(set(services)) != len(services)
            or any(not UNIT_RE.fullmatch(str(item)) for item in services)
            or not set(services).issubset(catalog.allowed_units)):
        raise ReleaseError("release impact services are invalid")
    required_services = {
        service for item in mappings for service in item.get("services", [])
    }
    if required_services != set(services):
        raise ReleaseError("release impact service restarts are not exact")
    required_env = data.get("required_env")
    if (not isinstance(required_env, dict)
            or not set(required_env).issubset(required_services)
            or any(not isinstance(values, list) or not values
                   or len(set(values)) != len(values)
                   or any(not ENV_NAME_RE.fullmatch(str(item)) for item in values)
                   for values in required_env.values())):
        raise ReleaseError("release impact environment declarations are invalid")
    pre_health_checks = data.get("pre_health_checks")
    health_checks = data.get("health_checks")
    for label, values in (
            ("pre-release", pre_health_checks), ("post-release", health_checks)):
        if (not isinstance(values, list) or len(set(values)) != len(values)
                or any(not isinstance(item, str)
                       or item not in catalog.health_probes for item in values)):
            raise ReleaseError("%s health probe ids are invalid" % label)
    if not health_checks:
        raise ReleaseError("runtime changes require a named post-release health probe")
    required_probes = {
        catalog.service_health_probes[service]
        for service in services if service in catalog.service_health_probes
    } | {
        mapping["health_probe"] for mapping in mappings if mapping.get("health_probe")
    }
    missing_service_probes = sorted(
        service for service in services
        if service not in catalog.service_health_probes
    )
    if missing_service_probes:
        raise ReleaseError(
            "runtime catalog lacks health probes for services: %s"
            % ", ".join(missing_service_probes)
        )
    if (required_probes != set(pre_health_checks)
            or required_probes != set(health_checks)):
        raise ReleaseError("release impact named health probes are not exact")
    if not isinstance(data.get("external_checks"), list) or data["external_checks"]:
        raise ReleaseError(
            "phase-one release contracts forbid executable external checks"
        )
    if not isinstance(data.get("migrations"), list) or data["migrations"]:
        raise ReleaseError(
            "phase-one release contracts forbid database migrations"
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "release_id": release_id,
        "runtime_changes": runtime_changes,
        "restart_services": sorted(services),
        "required_env": {
            service: sorted(str(item) for item in required_env[service])
            for service in sorted(required_env)
        },
        "pre_health_checks": pre_health_checks,
        "health_checks": health_checks,
        "external_checks": [],
        "migrations": [],
    }


def collect_impact_index(repo, catalog, commit):
    records = {}
    for path in repo.files_at(commit):
        if not path.startswith(catalog.impact_prefix) or not path.endswith(".json"):
            continue
        raw = repo.file_at(commit, path)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (AttributeError, UnicodeError, json.JSONDecodeError) as exc:
            raise ReleaseError("release impact JSON is invalid: %s" % path) from exc
        if (not isinstance(data, dict)
                or not isinstance(data.get("release_id"), str)
                or not RELEASE_ID_RE.fullmatch(data["release_id"])):
            raise ReleaseError("release impact id is invalid: %s" % path)
        release_id = data["release_id"]
        if release_id in records:
            raise ReleaseError("release impact ids must be globally unique")
        records[release_id] = {"repository_path": path, "sha256": _sha256(raw)}
    return records


def validate_catalog_coverage(repo, catalog, commit):
    unmapped = []
    for path in repo.files_at(commit):
        if not catalog.is_candidate(path) or catalog.is_ignored(path):
            continue
        if not catalog.mappings(path, strict_candidate=False):
            unmapped.append(path)
    if unmapped:
        raise ReleaseError(
            "runtime catalog leaves candidate files unclassified: %s"
            % ", ".join(sorted(unmapped))
        )


def collect_release_impact(
        repo, catalog, older, newer, *, base_catalog=None,
        catalog_path=DEFAULT_CATALOG, enforce_catalog_isolation=False):
    changes = repo.changed_paths(older, newer)
    catalogs = [item for item in (base_catalog, catalog) if item is not None]
    catalog_path = _relative_repository_path(catalog_path)
    impact_prefixes = {item.impact_prefix for item in catalogs}
    catalog_changed = any(path == catalog_path for _status, path in changes)
    trust_root_changes = sorted(
        path for _status, path in changes
        if path in TRUST_ROOT_PATHS or path.startswith(".github/workflows/")
    )
    impact_changes = [
        (status_value, path) for status_value, path in changes
        if any(path.startswith(prefix) for prefix in impact_prefixes)
        and path.endswith(".json")
    ]
    candidate_changes = sorted({
        path for _status, path in changes
        if any(item.is_candidate(path) for item in catalogs)
        and path not in TRUST_ROOT_PATHS
        and not path.startswith(".github/workflows/")
        and not any(path.startswith(prefix) for prefix in impact_prefixes)
        and not all(item.is_ignored(path) for item in catalogs)
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
    if (enforce_catalog_isolation and base_catalog is not None
            and trust_root_changes):
        raise ReleaseError(
            "phase-one release trust roots are immutable after bootstrap: %s"
            % ", ".join(trust_root_changes)
        )
    runtime_changes = set()
    for _status, path in changes:
        mappings = []
        mapping_errors = []
        for item in catalogs:
            try:
                mapping = item.mappings(path)
            except ReleaseError as exc:
                mapping_errors.append(exc)
                continue
            mappings.extend(mapping)
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
    collect_impact_index(repo, catalog, newer)
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
        if base != first_parent or repo.merge_base(first_parent, head) != base:
            raise ReleaseError(
                "reviewed Head must include the exact main parent used by the merge"
            )
        pr_contract = collect_release_impact(repo, catalog, base, head)
        if len(pr_contract["impacts"]) != 1:
            raise ReleaseError("reviewed PR range must contain exactly one release impact")
        reviewed_impact = pr_contract["impacts"][0]
        if (reviewed_impact["release_id"] != release_id
                or reviewed_impact["repository_path"] != impact["repository_path"]
                or reviewed_impact["blob_sha256"] != impact["blob_sha256"]
                or reviewed_impact["runtime_changes"] != impact["runtime_changes"]):
            raise ReleaseError("reviewed Head is not bound to the target impact blob")
        for repository_path in impact["runtime_changes"]:
            if repo.file_at(merge_commit, repository_path) != repo.file_at(
                    head, repository_path):
                raise ReleaseError(
                    "merge commit runtime content differs from the reviewed Head"
                )
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

    def is_loaded(self, service, *, timeout=30):
        if not UNIT_RE.fullmatch(service):
            raise ReleaseError("systemd unit name is invalid")
        result = subprocess.run(
            [
                "/usr/bin/systemctl", "show", "--property=LoadState",
                "--value", service,
            ],
            check=False, capture_output=True, text=True, timeout=timeout,
            env=_minimal_subprocess_environment(),
        )
        return result.returncode == 0 and result.stdout.strip() == "loaded"


class ReleaseEngine:
    def __init__(
            self, source_root, runtime_root, catalog, *,
            identity_path=DEFAULT_IDENTITY_FILE, state_root=DEFAULT_STATE_ROOT,
            repo=None, inspector=None, environment=None, owner_resolver=None,
            group_resolver=None):
        self.source_root = Path(os.path.abspath(source_root))
        self.runtime_root = Path(os.path.abspath(runtime_root))
        self.catalog = catalog
        self.identity_path = _absolute_runtime_path(identity_path)
        self.state_root_path = _absolute_runtime_path(state_root)
        self.state_root = _mapped_path(self.runtime_root, self.state_root_path)
        self.repo = repo or GitRepository(self.source_root)
        self.inspector = inspector or SystemInspector()
        self.environment = dict(os.environ if environment is None else environment)
        self.owner_resolver = owner_resolver or _default_owner_resolver
        self.group_resolver = group_resolver or _default_group_resolver

    def _catalog_record(self, commit, *, expected=None):
        path = DEFAULT_CATALOG
        if self.catalog.repository_path != path:
            raise ReleaseError("release engine must use the canonical runtime catalog")
        raw = self.repo.file_at(commit, path)
        oid = self.repo.file_oid_at(commit, path)
        if raw is None or oid is None:
            raise ReleaseError("canonical runtime catalog is missing from Git")
        source = self.source_root / Path(*PurePosixPath(path).parts)
        resolved_root = self.source_root.resolve(strict=True)
        try:
            resolved_source = source.resolve(strict=True)
            info = os.lstat(source)
        except (FileNotFoundError, OSError) as exc:
            raise ReleaseError("canonical runtime catalog is unavailable") from exc
        if (os.path.commonpath((str(resolved_root), str(resolved_source)))
                != str(resolved_root) or resolved_source != source.absolute()
                or stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)):
            raise ReleaseError("canonical runtime catalog path is unsafe")
        worktree_raw = source.read_bytes()
        record = {
            "repository_path": path,
            "blob_oid": oid,
            "sha256": _sha256(raw),
            "schema_version": SCHEMA_VERSION,
        }
        if (worktree_raw != raw or self.catalog.source_bytes != raw
                or self.catalog.source_sha256 != record["sha256"]):
            raise ReleaseError("working tree runtime catalog differs from locked Git blob")
        if expected is not None and record != expected:
            raise ReleaseError("deployment ledger runtime catalog does not match Git")
        return record

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
        if (type(identity.get("schema_version")) is not int
                or any(identity.get(key) != value for key, value in required.items())):
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
            "last_release_id", "last_successful_release", "runtime_catalog",
            "accepted_impacts", "runtime_metadata",
        }
        if set(state) != required_keys:
            raise ReleaseError("deployment ledger fields are invalid")
        if (type(state["schema_version"]) is not int
                or state["schema_version"] != SCHEMA_VERSION
                or state["environment"] != self.catalog.target["environment"]
                or state["host_id"] != self.catalog.target["host_id"]
                or not COMMIT_RE.fullmatch(str(state["deployed_main_commit"]))
                or not isinstance(state["runtime_hashes"], dict)
                or not isinstance(state["repository_paths"], dict)
                or not isinstance(state["accepted_impacts"], dict)
                or not isinstance(state["runtime_metadata"], dict)
                or not isinstance(state["managed_runtime_paths"], list)
                or sorted(state["runtime_hashes"]) != sorted(state["managed_runtime_paths"])
                or set(state["runtime_hashes"]) != set(state["repository_paths"])
                or set(state["runtime_hashes"]) != set(state["runtime_metadata"])):
            raise ReleaseError("deployment ledger is invalid")
        catalog_record = state["runtime_catalog"]
        if (not isinstance(catalog_record, dict)
                or set(catalog_record) != {
                    "repository_path", "blob_oid", "sha256", "schema_version",
                }
                or catalog_record.get("repository_path") != DEFAULT_CATALOG
                or not re.fullmatch(r"[0-9a-f]{40,64}", str(
                    catalog_record.get("blob_oid") or ""))
                or not re.fullmatch(r"[0-9a-f]{64}", str(
                    catalog_record.get("sha256") or ""))
                or type(catalog_record.get("schema_version")) is not int
                or catalog_record.get("schema_version") != SCHEMA_VERSION):
            raise ReleaseError("deployment ledger runtime catalog record is invalid")
        for release_id, record in state["accepted_impacts"].items():
            if (not RELEASE_ID_RE.fullmatch(str(release_id))
                    or not isinstance(record, dict)
                    or set(record) != {"repository_path", "sha256"}
                    or not str(record.get("repository_path") or "").startswith(
                        self.catalog.impact_prefix)
                    or not re.fullmatch(r"[0-9a-f]{64}", str(
                        record.get("sha256") or ""))):
                raise ReleaseError("deployment ledger accepted impact record is invalid")
        for runtime_path, expected in state["runtime_hashes"].items():
            _absolute_runtime_path(runtime_path)
            _relative_repository_path(state["repository_paths"][runtime_path])
            if (not isinstance(expected, dict) or set(expected) != {"state", "sha256"}
                    or expected.get("state") != "file"
                    or not re.fullmatch(r"[0-9a-f]{64}", str(expected.get("sha256") or ""))):
                raise ReleaseError("deployment ledger contains an invalid runtime hash")
            metadata = state["runtime_metadata"][runtime_path]
            if (not isinstance(metadata, dict)
                    or set(metadata) != {"mode", "owner", "group"}
                    or type(metadata.get("mode")) is not int
                    or not 0 <= metadata["mode"] <= 0o7777
                    or not re.fullmatch(
                        r"[a-z_][a-z0-9_-]{0,31}", str(metadata.get("owner") or "")
                    )
                    or not re.fullmatch(
                        r"[a-z_][a-z0-9_-]{0,31}", str(metadata.get("group") or "")
                    )):
                raise ReleaseError("deployment ledger contains invalid runtime metadata")
        return state

    def _write_state(self, state):
        _atomic_write(
            self.runtime_root, self._state_runtime_path("state.json"),
            _json_bytes(state), 0o600,
        )

    def _expected_runtime(self, commit):
        runtime_hashes = {}
        repository_paths = {}
        runtime_metadata = {}
        for repository_path in self.repo.files_at(commit):
            mappings = self.catalog.mappings(repository_path)
            if not mappings:
                continue
            if self.repo.file_mode_at(commit, repository_path) not in {"100644", "100755"}:
                raise ReleaseError("runtime source must be a regular Git blob")
            content = self.repo.file_at(commit, repository_path)
            for mapping in mappings:
                runtime_path = mapping["runtime_path"]
                if runtime_path in repository_paths:
                    raise ReleaseError("multiple repository files map to one runtime path")
                runtime_hashes[runtime_path] = self._state_hash(content)
                repository_paths[runtime_path] = repository_path
                if not self.catalog.is_inventory_scoped(runtime_path):
                    raise ReleaseError(
                        "runtime mapping is outside complete inventory scope: %s"
                        % runtime_path
                    )
                runtime_metadata[runtime_path] = {
                    "mode": mapping["mode"], "owner": mapping["owner"],
                    "group": mapping["group"],
                }
        if not runtime_hashes:
            raise ReleaseError("runtime catalog produced no managed targets")
        return runtime_hashes, repository_paths, runtime_metadata

    def _runtime_matches(self, runtime_path, expected_hash, expected_metadata=None):
        record = _read_regular_record(self.runtime_root, runtime_path)
        if expected_hash["state"] == "absent":
            return record is None and expected_metadata is None
        if record is None or self._state_hash(record[0]) != expected_hash:
            return False
        if os.name != "posix":  # pragma: no cover - exercised by Linux CI
            return True
        if expected_metadata is None:
            return False
        info = record[1]
        return (stat.S_IMODE(info.st_mode) == expected_metadata["mode"]
                and info.st_uid == self.owner_resolver(expected_metadata["owner"])
                and info.st_gid == self.group_resolver(expected_metadata["group"]))

    def _runtime_data_drift(self):
        drift = set()
        for contract in self.catalog.runtime_data_contracts:
            runtime_path = contract["path"].rstrip("/")
            target = _mapped_path(self.runtime_root, runtime_path)
            try:
                info = os.lstat(target)
            except FileNotFoundError:
                if contract["required"]:
                    drift.add(runtime_path)
                continue
            expected_directory = contract["kind"] == "mutable_directory"
            if (stat.S_ISLNK(info.st_mode)
                    or expected_directory and not stat.S_ISDIR(info.st_mode)
                    or not expected_directory and not stat.S_ISREG(info.st_mode)):
                drift.add(runtime_path)
                continue
            if not expected_directory:
                record = _read_regular_record(self.runtime_root, runtime_path)
                if record is None:
                    drift.add(runtime_path)
                    continue
                info = record[1]
            if os.name == "posix" and (
                    stat.S_IMODE(info.st_mode) not in contract["allowed_modes"]
                    or info.st_uid != self.owner_resolver(contract["owner"])
                    or info.st_gid != self.group_resolver(contract["group"])):
                drift.add(runtime_path)
        return drift

    def _runtime_symlink_drift(self):
        drift = set()
        for runtime_path, expected_target in self.catalog.required_runtime_symlinks.items():
            target = _mapped_path(self.runtime_root, runtime_path)
            _assert_real_parents(self.runtime_root, target)
            try:
                info = os.lstat(target)
            except FileNotFoundError:
                drift.add(runtime_path)
                continue
            if not stat.S_ISLNK(info.st_mode):
                drift.add(runtime_path)
                continue
            raw_target = os.readlink(target)
            resolved = os.path.abspath(os.path.join(os.path.dirname(runtime_path), raw_target))
            if (resolved != expected_target
                    or os.name == "posix"
                    and info.st_uid != self.owner_resolver("root")):
                drift.add(runtime_path)
        return drift

    def _service_environment_names(self, service):
        contract = self.catalog.service_environment[service]
        names = set(contract["inline"])
        for runtime_path in contract["files"]:
            data_contract = self.catalog.runtime_data_exact[runtime_path]
            record = _read_regular_record(self.runtime_root, runtime_path)
            if record is None:
                continue
            info = record[1]
            if os.name == "posix" and (
                    stat.S_IMODE(info.st_mode) not in data_contract["allowed_modes"]
                    or info.st_uid != self.owner_resolver(data_contract["owner"])
                    or info.st_gid != self.group_resolver(data_contract["group"])):
                raise ReleaseError("service environment file metadata is invalid")
            try:
                text = record[0].decode("utf-8")
            except UnicodeError as exc:
                raise ReleaseError("service environment file is not UTF-8") from exc
            for raw_line in text.splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[7:].lstrip()
                if "=" not in line:
                    continue
                name, value = line.split("=", 1)
                name = name.strip()
                value = value.strip()
                if ENV_NAME_RE.fullmatch(name) and value not in {"", "''", '\"\"'}:
                    names.add(name)
        return names

    def _runtime_inventory(self):
        inventory = set()
        unsafe = set(self._runtime_data_drift()) | set(self._runtime_symlink_drift())
        for runtime_prefix in self.catalog.inventory_roots:
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
                    if name in self.catalog.ignored_runtime_directory_names:
                        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                            runtime_path = "/" + candidate.relative_to(
                                self.runtime_root
                            ).as_posix()
                            unsafe.add(runtime_path)
                        names.remove(name)
                        continue
                    runtime_directory = "/" + candidate.relative_to(
                        self.runtime_root
                    ).as_posix() + "/"
                    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                        runtime_path = "/" + candidate.relative_to(self.runtime_root).as_posix()
                        unsafe.add(runtime_path)
                        names.remove(name)
                        continue
                    if self.catalog.is_unmanaged_runtime(runtime_directory):
                        names.remove(name)
                for name in filenames:
                    candidate = directory_path / name
                    runtime_path = "/" + candidate.relative_to(self.runtime_root).as_posix()
                    info = os.lstat(candidate)
                    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                        unsafe.add(runtime_path)
                    elif not self.catalog.is_unmanaged_runtime(runtime_path):
                        inventory.add(runtime_path)
        for runtime_path in self.catalog.inventory_exact_paths:
            target = _mapped_path(self.runtime_root, runtime_path)
            try:
                info = os.lstat(target)
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                unsafe.add(runtime_path)
            elif not self.catalog.is_unmanaged_runtime(runtime_path):
                inventory.add(runtime_path)
        for unit in self.catalog.allowed_units:
            runtime_path = "/etc/systemd/system/" + unit
            target = _mapped_path(self.runtime_root, runtime_path)
            try:
                info = os.lstat(target)
            except FileNotFoundError:
                info = None
            if info is not None:
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                    unsafe.add(runtime_path)
                else:
                    inventory.add(runtime_path)
            dropin_path = runtime_path + ".d"
            dropin = _mapped_path(self.runtime_root, dropin_path)
            try:
                dropin_info = os.lstat(dropin)
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(dropin_info.st_mode) or not stat.S_ISDIR(dropin_info.st_mode):
                unsafe.add(dropin_path)
                continue
            for directory, names, filenames in os.walk(dropin, followlinks=False):
                directory_path = Path(directory)
                for name in list(names):
                    candidate = directory_path / name
                    child_path = "/" + candidate.relative_to(self.runtime_root).as_posix()
                    child_info = os.lstat(candidate)
                    if stat.S_ISLNK(child_info.st_mode) or not stat.S_ISDIR(child_info.st_mode):
                        unsafe.add(child_path)
                        names.remove(name)
                for name in filenames:
                    candidate = directory_path / name
                    child_path = "/" + candidate.relative_to(self.runtime_root).as_posix()
                    child_info = os.lstat(candidate)
                    if stat.S_ISLNK(child_info.st_mode) or not stat.S_ISREG(child_info.st_mode):
                        unsafe.add(child_path)
                    else:
                        inventory.add(child_path)
        for guard in self.catalog.inventory_name_guards:
            directory = _mapped_path(self.runtime_root, guard["directory"])
            try:
                directory_info = os.lstat(directory)
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(directory_info.st_mode) or not stat.S_ISDIR(directory_info.st_mode):
                unsafe.add(guard["directory"])
                continue
            for child in directory.iterdir():
                if (child.name.startswith(guard["prefix"])
                        and child.name not in guard["allowed_names"]):
                    unsafe.add(
                        guard["directory"].rstrip("/") + "/" + child.name
                    )
        return inventory, unsafe

    def _inventory_drift(self, expected_paths):
        inventory, unsafe = self._runtime_inventory()
        expected_inventory = {
            path for path in expected_paths
            if self.catalog.is_inventory_scoped(path)
            and not self.catalog.is_unmanaged_runtime(path)
        }
        return sorted((inventory - expected_inventory) | unsafe)

    def status(self):
        self.verify_identity()
        state = self.load_state()
        self._catalog_record(
            state["deployed_main_commit"], expected=state["runtime_catalog"],
        )
        if collect_impact_index(
                self.repo, self.catalog, state["deployed_main_commit"]
        ) != state["accepted_impacts"]:
            raise ReleaseError("deployment ledger accepted impacts differ from Git")
        drift = []
        for runtime_path, expected in sorted(state["runtime_hashes"].items()):
            if not self._runtime_matches(
                    runtime_path, expected, state["runtime_metadata"][runtime_path]):
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

    def _verify_service_preconditions(self, services):
        if not services:
            return
        self.inspector.validate_tool("/usr/bin/systemctl", self.catalog)
        invalid = []
        for service in services:
            policy = self.catalog.service_preconditions[service]
            if (policy == "active" and not self.inspector.is_active(service)
                    or policy == "loaded" and not self.inspector.is_loaded(service)):
                invalid.append(service)
        if invalid:
            raise ReleaseError(
                "required systemd unit preconditions failed: %s"
                % ", ".join(sorted(invalid))
            )

    def _verify_planning_snapshot(self, identity, state, target_commit, services):
        if self.verify_identity() != identity:
            raise ReleaseError("release identity changed during planning")
        if self.load_state() != state:
            raise ReleaseError("deployment ledger changed during planning")
        self.repo.verify_checkout(
            target_commit, self.catalog.target["origin_url"], verify_live_origin=True,
        )
        self._catalog_record(target_commit, expected=state["runtime_catalog"])
        if collect_impact_index(
                self.repo, self.catalog, state["deployed_main_commit"]
        ) != state["accepted_impacts"]:
            raise ReleaseError("release impacts changed during planning")
        mismatches = [
            path for path, expected in state["runtime_hashes"].items()
            if not self._runtime_matches(
                path, expected, state["runtime_metadata"][path],
            )
        ]
        unexpected = self._inventory_drift(set(state["managed_runtime_paths"]))
        if mismatches or unexpected:
            raise ReleaseError("runtime changed during release planning")
        self._verify_service_preconditions(services)

    def initialize(self, deployed_commit, confirmation, *, verify_live_origin=True):
        if confirmation != "test":
            raise ReleaseError("initialize requires exact test environment confirmation")
        # Read-only trust checks run before the lock path is touched, then are
        # repeated under the lock to close the time-of-check/time-of-use gap.
        self.verify_identity()
        self.repo.verify_checkout(
            deployed_commit, self.catalog.target["origin_url"],
            verify_live_origin=verify_live_origin,
        )
        self._catalog_record(deployed_commit)
        validate_catalog_coverage(self.repo, self.catalog, deployed_commit)
        with self._release_lock():
            if _read_regular(self.runtime_root, self._state_runtime_path("state.json")) is not None:
                raise ReleaseError("deployment ledger was initialized concurrently")
            identity = self.verify_identity()
            self.repo.verify_checkout(
                deployed_commit, self.catalog.target["origin_url"],
                verify_live_origin=False,
            )
            catalog_record = self._catalog_record(deployed_commit)
            validate_catalog_coverage(self.repo, self.catalog, deployed_commit)
            runtime_hashes, repository_paths, runtime_metadata = (
                self._expected_runtime(deployed_commit)
            )
            mismatches = [
                path for path, expected in runtime_hashes.items()
                if not self._runtime_matches(path, expected, runtime_metadata[path])
            ]
            unexpected = self._inventory_drift(set(runtime_hashes))
            if mismatches or unexpected:
                raise ReleaseError("runtime inventory does not exactly match deployed commit")
            accepted_impacts = collect_impact_index(
                self.repo, self.catalog, deployed_commit,
            )
            state = {
                "schema_version": SCHEMA_VERSION,
                "environment": identity["environment"],
                "host_id": identity["host_id"],
                "deployed_main_commit": deployed_commit,
                "runtime_catalog": catalog_record,
                "accepted_impacts": accepted_impacts,
                "runtime_hashes": runtime_hashes,
                "repository_paths": repository_paths,
                "runtime_metadata": runtime_metadata,
                "managed_runtime_paths": sorted(runtime_hashes),
                "last_release_id": None,
                "last_successful_release": None,
            }
            # Repeat every mutable observation immediately before the ledger write.
            if self.verify_identity() != identity:
                raise ReleaseError("release identity changed during initialization")
            self.repo.verify_checkout(
                deployed_commit, self.catalog.target["origin_url"],
                verify_live_origin=False,
            )
            self._catalog_record(deployed_commit, expected=catalog_record)
            if collect_impact_index(
                    self.repo, self.catalog, deployed_commit
            ) != accepted_impacts:
                raise ReleaseError("release impacts changed during initialization")
            mismatches = [
                path for path, expected in runtime_hashes.items()
                if not self._runtime_matches(path, expected, runtime_metadata[path])
            ]
            unexpected = self._inventory_drift(set(runtime_hashes))
            if mismatches or unexpected:
                raise ReleaseError("runtime changed during initialization")
            if _read_regular(self.runtime_root, self._state_runtime_path("state.json")) is not None:
                raise ReleaseError("deployment ledger was initialized concurrently")
            self._write_state(state)
        return {
            "ok": True,
            "status": "initialized",
            "deployed_main_commit": deployed_commit,
            "tracked_runtime_paths": len(runtime_hashes),
        }

    def build_plan(self, target_commit, reviewed_evidence=None):
        identity = self.verify_identity()
        state = self.load_state()
        current = self.status()
        if not current["ok"]:
            raise ReleaseError("deployment ledger runtime has drifted")
        if self.verify_identity() != identity or self.load_state() != state:
            raise ReleaseError("release trust state changed before planning")
        target_commit = str(target_commit or "")
        self.repo.require_commit(target_commit)
        older = state["deployed_main_commit"]
        self.repo.verify_checkout(
            target_commit, self.catalog.target["origin_url"], verify_live_origin=True,
        )
        self._catalog_record(target_commit, expected=state["runtime_catalog"])
        if older == target_commit:
            self._verify_planning_snapshot(identity, state, target_commit, [])
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
        validate_catalog_coverage(self.repo, self.catalog, target_commit)
        collected = collect_release_impact(self.repo, self.catalog, older, target_commit)
        impacts = collected["impacts"]
        evidence = verify_review_evidence(
            self.repo, self.catalog, older, target_commit, impacts,
            reviewed_evidence or {},
        )
        required_env = {}
        for impact in impacts:
            for service, names in impact["required_env"].items():
                required_env.setdefault(service, set()).update(names)
        missing = [
            "%s:%s" % (service, name)
            for service, names in sorted(required_env.items())
            for name in sorted(names)
            if name not in self._service_environment_names(service)
        ]
        if missing:
            raise ReleaseError("required environment variables are missing: %s" % ", ".join(missing))
        services = sorted({
            service for impact in impacts for service in impact["restart_services"]
        })
        self._verify_service_preconditions(services)
        pre_checks = sorted({
            check for impact in impacts for check in impact["pre_health_checks"]
        })
        post_checks = sorted({
            check for impact in impacts for check in impact["health_checks"]
        })
        files = []
        seen_runtime = set()
        total_bytes = 0
        daemon_reload_required = False
        planning_blockers = []
        for status_value, repository_path in collected["changed_paths"]:
            mappings = self.catalog.mappings(repository_path)
            if not mappings:
                continue
            before = self.repo.file_at(older, repository_path)
            after = self.repo.file_at(target_commit, repository_path)
            for mapping in mappings:
                if status_value == "D" and not mapping["delete_allowed"]:
                    raise ReleaseError("runtime deletion is not allowed by catalog")
                runtime_path = mapping["runtime_path"]
                if runtime_path in seen_runtime:
                    raise ReleaseError("multiple repository files map to one runtime path")
                seen_runtime.add(runtime_path)
                daemon_reload_required = (
                    daemon_reload_required or mapping["daemon_reload"]
                )
                if mapping["planning_blocker"]:
                    planning_blockers.append(
                        "%s: %s" % (repository_path, mapping["planning_blocker"])
                    )
                if state["repository_paths"].get(runtime_path) not in {None, repository_path}:
                    raise ReleaseError("catalog mapping changed relative to the deployment ledger")
                if not self._runtime_matches(
                        runtime_path, self._state_hash(before),
                        state["runtime_metadata"].get(runtime_path)):
                    raise ReleaseError("runtime drift detected before release planning")
                files.append({
                    "repository_path": repository_path,
                    "runtime_path": runtime_path,
                    "change": "delete" if after is None else "write",
                    "before": self._state_hash(before),
                    "after": self._state_hash(after),
                    "services": mapping.get("services", []),
                    "mode": mapping["mode"],
                    "owner": mapping["owner"],
                    "group": mapping["group"],
                    "daemon_reload": mapping["daemon_reload"],
                })
            total_bytes += (0 if before is None else len(before)) + (
                0 if after is None else len(after)
            )
        if planning_blockers:
            raise ReleaseError(
                "merged_not_releasable: " + "; ".join(sorted(planning_blockers))
            )
        free = shutil.disk_usage(self.state_root.parent).free
        required_free = max(self.catalog.min_free_bytes, total_bytes * 2)
        if free < required_free:
            raise ReleaseError("insufficient disk space for a future transactional release")
        self._verify_planning_snapshot(identity, state, target_commit, services)
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
            "daemon_reload_required": daemon_reload_required,
            "pre_health_probes": pre_checks,
            "post_health_probes": post_checks,
            "required_env": {
                service: sorted(names) for service, names in sorted(required_env.items())
            },
            "required_free_bytes": required_free,
        }


def _engine_from_args(args):
    source_root = Path(os.path.abspath(args.source_root))
    catalog = RuntimeCatalog.load(source_root, DEFAULT_CATALOG)
    return ReleaseEngine(
        source_root, "/", catalog,
        identity_path=DEFAULT_IDENTITY_FILE, state_root=DEFAULT_STATE_ROOT,
    )


def _add_runtime_arguments(parser):
    parser.add_argument("--source-root", default=DEFAULT_SOURCE_ROOT)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Huangque test release contract and read-only planning foundation",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    impact_parser = subparsers.add_parser("check-impact")
    impact_parser.add_argument("--source-root", default=".")
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
            catalog = RuntimeCatalog.load_from_git(
                repo, args.target, DEFAULT_CATALOG,
            )
            base_catalog = RuntimeCatalog.load_from_git(
                repo, args.base, DEFAULT_CATALOG, required=False,
            )
            validate_catalog_coverage(repo, catalog, args.target)
            if base_catalog is not None:
                validate_catalog_coverage(repo, base_catalog, args.base)
            result = collect_release_impact(
                repo, catalog, args.base, args.target,
                base_catalog=base_catalog, catalog_path=DEFAULT_CATALOG,
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
            _verify_runtime_entrypoint(args.source_root)
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
