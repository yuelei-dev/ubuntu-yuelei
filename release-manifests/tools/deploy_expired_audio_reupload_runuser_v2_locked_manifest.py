#!/usr/bin/env python3
"""Transactional, test-only release for the expired-audio runuser successor.

The executor never connects to a remote host.  It consumes the reviewed
manifest locally on the Yuelei test server, verifies every manifest target before
the first write, then treats file installation, preflight and service restart
as one rollback unit.
"""
import argparse
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

try:
    import pwd
except ImportError:  # pragma: no cover - the real release runtime is Linux.
    pwd = None

import verify_content_whisper_deployment as manifest_verify


AUTHORIZED_TARGET = "test@8.148.158.106"
DEFAULT_BACKUP_ROOT = "/opt/huangque-deploy-backups"
MANIFEST_REPOSITORY_PATHS = {
    "release-manifests/test-runtime/expired-audio-reupload-runuser-v2-20260826.json",
}
HEALTH_PHASE_STATUS_FIELDS = {
    "pre-deployment": "pre_status_by_disposition",
    "post-deployment": "post_expected_status",
    "rollback": "rollback_status_by_disposition",
}
DIGITAL_HUMAN_REPOSITORY_PATH = "server/content_domains/digital_human_v2.py"
DIGITAL_HUMAN_RUNTIME_PATH = (
    "/home/ubuntu/content-api/content_domains/digital_human_v2.py"
)
START_DISPOSITIONS = frozenset({
    "needs_install", "already_installed", "unchanged",
})
APPROVED_HEALTH_CONTRACT = {
    "http://127.0.0.1:8096/api/gen/health": {
        "target_repository_path": DIGITAL_HUMAN_REPOSITORY_PATH,
        "target_runtime_path": DIGITAL_HUMAN_RUNTIME_PATH,
        "pre_status_by_disposition": {
            "needs_install": 200,
            "already_installed": 200,
            "unchanged": 200,
        },
        "post_expected_status": 200,
        "rollback_status_by_disposition": {
            "needs_install": 200,
            "already_installed": 200,
            "unchanged": 200,
        },
    },
    "http://127.0.0.1:8096/api/gen/history": {
        "target_repository_path": DIGITAL_HUMAN_REPOSITORY_PATH,
        "target_runtime_path": DIGITAL_HUMAN_RUNTIME_PATH,
        "pre_status_by_disposition": {
            "needs_install": 401,
            "already_installed": 401,
            "unchanged": 401,
        },
        "post_expected_status": 401,
        "rollback_status_by_disposition": {
            "needs_install": 401,
            "already_installed": 401,
            "unchanged": 401,
        },
    },
    "http://127.0.0.1:8096/api/gen/digital-human-v2/history": {
        "target_repository_path": DIGITAL_HUMAN_REPOSITORY_PATH,
        "target_runtime_path": DIGITAL_HUMAN_RUNTIME_PATH,
        "pre_status_by_disposition": {
            "needs_install": 404,
            "already_installed": 401,
            "unchanged": 401,
        },
        "post_expected_status": 401,
        "rollback_status_by_disposition": {
            "needs_install": 404,
            "already_installed": 401,
            "unchanged": 401,
        },
    },
}
APPROVED_HEALTH_URLS = frozenset(APPROVED_HEALTH_CONTRACT)
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
ENV_ASSIGNMENT_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")
ALLOWED_DEPLOYMENT_TOOLS = frozenset({
    "/usr/bin/env",
    "/usr/bin/python3",
    "/usr/bin/systemctl",
    "/usr/sbin/runuser",
})
RUNUSER_TOOL = "/usr/sbin/runuser"
RUNUSER_USER = "ubuntu"
SERVICE_USER_TEST_MODULE = "tests.test_digital_human_local_material_library"
SERVICE_USER_TEST_ARGV = (
    RUNUSER_TOOL, "-u", RUNUSER_USER, "--", "/usr/bin/python3",
    "-m", "unittest", SERVICE_USER_TEST_MODULE, "-v",
)


class ReleaseError(RuntimeError):
    pass


class RollbackError(ReleaseError):
    pass


class GitRunner:
    def run(self, arguments, *, source_root, allow_failure=False):
        result = subprocess.run(
            ["git", "-C", str(source_root)] + list(arguments),
            check=False, capture_output=True, text=True, timeout=60,
        )
        if result.returncode and not allow_failure:
            raise ReleaseError(
                "Git source checkout verification failed: %s" %
                " ".join(arguments)
            )
        return result


class CommandRunner:
    def run(self, stage, commands, *, source_root, runtime_root):
        for command in commands:
            argv = [
                self._expand(value, source_root, runtime_root)
                for value in command["argv"]
            ]
            cwd = self._expand(
                command.get("cwd", "{source:.}"), source_root, runtime_root,
            )
            environment = dict(os.environ)
            environment.update({
                key: self._expand(value, source_root, runtime_root)
                for key, value in command.get("env", {}).items()
            })
            subprocess.run(
                argv, cwd=cwd, env=environment, check=True,
                timeout=int(command.get("timeout_seconds", 900)),
            )

    @staticmethod
    def _expand(value, source_root, runtime_root):
        value = str(value)
        if value.startswith("{source:") and value.endswith("}"):
            relative = value[8:-1]
            return str(manifest_verify._safe_source_path(source_root, relative))
        if value.startswith("{runtime:") and value.endswith("}"):
            absolute = value[9:-1]
            return str(manifest_verify._safe_runtime_path(runtime_root, absolute))
        return value


class RuntimeFiles:
    def __init__(self, runtime_root):
        self.root = Path(os.path.abspath(runtime_root))
        manifest_verify._lstat_no_symlink_chain(self.root, self.root)
        self.created_directories = []

    def path(self, runtime_path):
        return manifest_verify._safe_runtime_path(self.root, runtime_path)

    def read(self, runtime_path):
        target = self.path(runtime_path)
        return manifest_verify._read_regular_file_no_follow(self.root, target)

    def metadata(self, runtime_path):
        target = self.path(runtime_path)
        manifest_verify._lstat_no_symlink_chain(self.root, target)
        info = os.lstat(target)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ReleaseError("target is not a regular file: %s" % target)
        return {
            "mode": stat.S_IMODE(info.st_mode),
            "uid": getattr(info, "st_uid", None),
            "gid": getattr(info, "st_gid", None),
        }

    def mode(self, runtime_path):
        return self.metadata(runtime_path)["mode"]

    def _ensure_parent_directories(self, target):
        current = self.root
        for part in target.parent.relative_to(self.root).parts:
            current = current / part
            try:
                info = os.lstat(current)
            except FileNotFoundError:
                os.mkdir(current, 0o755)
                self.created_directories.append(current)
                info = os.lstat(current)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise ReleaseError("deployment parent is not a real directory: %s" % current)
        manifest_verify._lstat_no_symlink_chain(self.root, target.parent)

    def _open_parent_descriptor(self, target):
        if os.name != "posix":
            return None
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.root, flags)
        try:
            for part in target.parent.relative_to(self.root).parts:
                next_descriptor = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = next_descriptor
        except Exception:
            os.close(descriptor)
            raise
        return descriptor

    def atomic_write(self, runtime_path, data, mode, uid=None, gid=None):
        target = self.path(runtime_path)
        self._ensure_parent_directories(target)
        try:
            info = os.lstat(target)
        except FileNotFoundError:
            info = None
        if info is not None and (
                stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)):
            raise ReleaseError("refusing to replace non-regular target: %s" % target)
        parent_descriptor = self._open_parent_descriptor(target)
        temporary_name = ".hq-release-%s" % uuid.uuid4().hex
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        temporary = target.parent / temporary_name
        descriptor = None
        try:
            descriptor = os.open(
                temporary_name if parent_descriptor is not None else temporary,
                flags, 0o600,
                **({"dir_fd": parent_descriptor} if parent_descriptor is not None else {}),
            )
            view = memoryview(data)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fsync(descriptor)
            descriptor_info = os.fstat(descriptor)
            if (hasattr(os, "fchown") and uid is not None and gid is not None
                    and (descriptor_info.st_uid != int(uid)
                         or descriptor_info.st_gid != int(gid))):
                os.fchown(descriptor, int(uid), int(gid))
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, int(mode))
            os.close(descriptor)
            descriptor = None
            if not hasattr(os, "fchmod"):
                os.chmod(temporary, int(mode))
            if parent_descriptor is not None:
                os.replace(
                    temporary_name, target.name,
                    src_dir_fd=parent_descriptor, dst_dir_fd=parent_descriptor,
                )
            else:
                os.replace(temporary, target)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                if parent_descriptor is not None:
                    os.unlink(temporary_name, dir_fd=parent_descriptor)
                elif temporary.exists():
                    temporary.unlink()
            except FileNotFoundError:
                pass
            if parent_descriptor is not None:
                os.close(parent_descriptor)
        actual = self.read(runtime_path)
        if actual != data:
            raise ReleaseError("atomic write verification failed: %s" % runtime_path)

    def remove(self, runtime_path):
        target = self.path(runtime_path)
        _, exists = manifest_verify._lstat_no_symlink_chain(self.root, target)
        if not exists:
            return
        parent_descriptor = self._open_parent_descriptor(target)
        try:
            info = os.stat(
                target.name if parent_descriptor is not None else target,
                **({"dir_fd": parent_descriptor, "follow_symlinks": False}
                   if parent_descriptor is not None else {"follow_symlinks": False}),
            )
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise ReleaseError("refusing to remove non-regular target: %s" % target)
            os.unlink(
                target.name if parent_descriptor is not None else target,
                **({"dir_fd": parent_descriptor} if parent_descriptor is not None else {}),
            )
        finally:
            if parent_descriptor is not None:
                os.close(parent_descriptor)

    def remove_created_empty_directories(self):
        for directory in reversed(self.created_directories):
            try:
                os.rmdir(directory)
            except FileNotFoundError:
                pass
            except OSError:
                continue


class ContentWhisperRelease:
    def __init__(
            self, manifest, source_root, runtime_root, backup_root,
            *, runner=None, health_getter=None, checkpoint=None,
            git_runner=None, reviewed_source_commit=None,
            reviewed_main_commit=None, monotonic=None, sleeper=None,
            deployment_tool_root=None, service_environment_getter=None,
            local_library_probe_runner=None):
        self.manifest = manifest
        self.source_root = Path(os.path.abspath(source_root))
        self.runtime = RuntimeFiles(runtime_root)
        self.backup_root = Path(os.path.abspath(backup_root))
        self.runner = runner or CommandRunner()
        self.git_runner = git_runner or GitRunner()
        self.reviewed_source_commit = reviewed_source_commit
        self.reviewed_main_commit = reviewed_main_commit
        self.health_getter = health_getter or self._http_status
        self.monotonic = monotonic or time.monotonic
        self.sleeper = sleeper or time.sleep
        self.service_environment_getter = (
            service_environment_getter or self._service_environment
        )
        self.local_library_probe_runner = (
            local_library_probe_runner or self._run_local_library_probe
        )
        self.deployment_tool_root = Path(os.path.abspath(
            deployment_tool_root or os.sep
        ))
        self.checkpoint = checkpoint or (lambda _name: None)
        self.backup_path = None
        self.backup_entries = []
        self.start_states = []
        self.modified_runtime_paths = set()
        self.daemon_reload_attempted = False
        self.restart_attempted = False

    @staticmethod
    def _git_blob(data):
        return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()

    def _read_locked_source(self, repository_path):
        relative = Path(repository_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ReleaseError("release tool path must stay inside source root")
        target = Path(os.path.abspath(self.source_root / relative))
        if os.path.commonpath((str(self.source_root), str(target))) != str(
                self.source_root):
            raise ReleaseError("release tool path escaped source root")
        current = self.source_root
        root_info = os.lstat(current)
        if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
            raise ReleaseError("source root must be a real directory")
        for index, part in enumerate(target.relative_to(self.source_root).parts):
            current = current / part
            try:
                info = os.lstat(current)
            except FileNotFoundError as exc:
                raise ReleaseError(
                    "locked release tool is missing: %s" % repository_path
                ) from exc
            if stat.S_ISLNK(info.st_mode):
                raise ReleaseError(
                    "locked release tool path contains a symbolic link: %s" %
                    repository_path
                )
            if index < len(target.relative_to(self.source_root).parts) - 1:
                if not stat.S_ISDIR(info.st_mode):
                    raise ReleaseError(
                        "locked release tool parent is not a directory: %s" %
                        repository_path
                    )
            elif not stat.S_ISREG(info.st_mode):
                raise ReleaseError(
                    "locked release tool is not a regular file: %s" %
                    repository_path
                )
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
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

    def _verify_release_tools(self):
        executor = self.manifest.get("executor", {})
        verifier = executor.get("verifier", {})
        requirements_verifier = executor.get("requirements_verifier", {})
        locked = (executor, verifier, requirements_verifier)
        expected_runtime_paths = {
            "release-manifests/tools/deploy_expired_audio_reupload_runuser_v2_locked_manifest.py": Path(__file__),
            "scripts/verify_content_whisper_deployment.py": Path(
                manifest_verify.__file__
            ),
        }
        for entry in locked:
            repository_path = entry.get("repository_path")
            if not repository_path:
                raise ReleaseError("manifest release tool lock is incomplete")
            data = self._read_locked_source(repository_path)
            if hashlib.sha256(data).hexdigest() != entry.get("source_sha256"):
                raise ReleaseError(
                    "release tool SHA-256 mismatch: %s" % repository_path
                )
            if self._git_blob(data) != entry.get("source_blob"):
                raise ReleaseError(
                    "release tool Git blob mismatch: %s" % repository_path
                )
            loaded_path = expected_runtime_paths.get(repository_path)
            if loaded_path is not None and Path(os.path.abspath(loaded_path)) != Path(
                    os.path.abspath(self.source_root / repository_path)):
                raise ReleaseError(
                    "loaded release tool is outside the locked checkout: %s" %
                    repository_path
                )

    def _git_output(self, arguments):
        result = self.git_runner.run(arguments, source_root=self.source_root)
        return result.stdout.strip()

    def _verify_source_checkout(
            self, reviewed_source_commit, reviewed_main_commit):
        if not COMMIT_PATTERN.fullmatch(str(reviewed_source_commit or "")):
            raise ReleaseError("exact reviewed source commit is required")
        if not COMMIT_PATTERN.fullmatch(str(reviewed_main_commit or "")):
            raise ReleaseError("exact reviewed main commit is required")
        manifest_path = Path(os.path.abspath(self.manifest.get("_manifest_path", "")))
        allowed_manifests = {
            Path(os.path.abspath(self.source_root / relative))
            for relative in MANIFEST_REPOSITORY_PATHS
        }
        if manifest_path not in allowed_manifests:
            raise ReleaseError("manifest must come from the locked source checkout")
        if self._git_output(["status", "--porcelain", "--untracked-files=normal"]):
            raise ReleaseError("source checkout must be clean")
        if self._git_output(["symbolic-ref", "--short", "HEAD"]) != "main":
            raise ReleaseError("source checkout branch must be main")
        head = self._git_output(["rev-parse", "HEAD"])
        origin_main = self._git_output(["rev-parse", "refs/remotes/origin/main"])
        remote_line = self._git_output([
            "ls-remote", "--exit-code", "origin", "refs/heads/main",
        ])
        remote_main = remote_line.split()[0] if remote_line else ""
        if not COMMIT_PATTERN.fullmatch(remote_main):
            raise ReleaseError("live origin/main could not be verified")
        if head != reviewed_main_commit or head != origin_main or head != remote_main:
            raise ReleaseError(
                "HEAD, reviewed main, local origin/main and live origin/main must match"
            )
        ancestor = self.git_runner.run(
            ["merge-base", "--is-ancestor", reviewed_source_commit, head],
            source_root=self.source_root, allow_failure=True,
        )
        if ancestor.returncode != 0:
            raise ReleaseError(
                "reviewed source commit is missing or not contained in live main"
            )

    @staticmethod
    def _deployment_tools_from_argv(argv):
        if (not isinstance(argv, list) or not argv
                or any(not isinstance(value, str) or not value for value in argv)):
            raise ReleaseError(
                "deployment command argv must be a non-empty string list"
            )
        tools = [argv[0]]
        if argv[0] == RUNUSER_TOOL:
            if tuple(argv) != SERVICE_USER_TEST_ARGV:
                raise ReleaseError(
                    "runuser deployment command must match the locked service-user test"
                )
            tools.append(argv[4])
            return tools
        if argv[0] == "/usr/bin/env":
            index = 1
            while (index < len(argv)
                   and ENV_ASSIGNMENT_PATTERN.fullmatch(argv[index])):
                index += 1
            if index >= len(argv):
                raise ReleaseError(
                    "undeclared or unavailable deployment tool: /usr/bin/env"
                )
            tools.append(argv[index])
        return tools

    def _validate_deployment_tool(self, tool):
        error = "undeclared or unavailable deployment tool: %s" % tool
        if tool not in ALLOWED_DEPLOYMENT_TOOLS:
            raise ReleaseError(error)
        posix_path = PurePosixPath(tool)
        if not posix_path.is_absolute():
            raise ReleaseError(error)
        target = self.deployment_tool_root.joinpath(*posix_path.parts[1:])
        try:
            if os.path.commonpath((
                    str(self.deployment_tool_root), str(target),
            )) != str(self.deployment_tool_root):
                raise ReleaseError(error)
            current = self.deployment_tool_root
            root_info = os.lstat(current)
            if (stat.S_ISLNK(root_info.st_mode)
                    or not stat.S_ISDIR(root_info.st_mode)):
                raise ReleaseError(error)
            parts = target.relative_to(self.deployment_tool_root).parts
            for index, part in enumerate(parts):
                current = current / part
                info = os.lstat(current)
                if index < len(parts) - 1:
                    if (stat.S_ISLNK(info.st_mode)
                            or not stat.S_ISDIR(info.st_mode)):
                        raise ReleaseError(error)
                    continue
                if stat.S_ISLNK(info.st_mode):
                    allowed_directory = (
                        self.deployment_tool_root / "usr" / "bin"
                    )
                    resolved = Path(os.path.realpath(current))
                    if os.path.commonpath((
                            str(allowed_directory), str(resolved),
                    )) != str(allowed_directory):
                        raise ReleaseError(error)
                    info = os.stat(current)
                if (not stat.S_ISREG(info.st_mode)
                        or (os.name == "posix" and not info.st_mode & (
                            stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
                        ))):
                    raise ReleaseError(error)
        except (OSError, ValueError) as exc:
            raise ReleaseError(error) from exc

    def _preflight_release_commands(self):
        stages = self.manifest.get("release_commands")
        if not isinstance(stages, dict) or not stages:
            raise ReleaseError("manifest release commands are missing")
        verified = set()
        service_user_test_count = 0
        for stage, commands in stages.items():
            if (not isinstance(stage, str)
                    or not isinstance(commands, list) or not commands):
                raise ReleaseError(
                    "manifest release command stage is invalid"
                )
            for command in commands:
                if not isinstance(command, dict):
                    raise ReleaseError("manifest release command is invalid")
                argv = command.get("argv")
                if (isinstance(argv, list)
                        and SERVICE_USER_TEST_MODULE in argv):
                    if tuple(argv) != SERVICE_USER_TEST_ARGV:
                        raise ReleaseError(
                            "local-material permission test must run as the locked service user"
                        )
                    service_user_test_count += 1
                for tool in self._deployment_tools_from_argv(
                        argv):
                    if tool not in verified:
                        self._validate_deployment_tool(tool)
                        verified.add(tool)
        if service_user_test_count != 1:
            raise ReleaseError(
                "local-material permission test must appear exactly once"
            )
        self.checkpoint("tools_preflight_complete")

    def _validate_target(self, confirmation):
        target = self.manifest.get("target", {})
        if target.get("role") != "test" or target.get("host") != "8.148.158.106":
            raise ReleaseError("manifest is not authorized for the Yuelei test server")
        if self.manifest["deployment_policy"].get("production_server_write_allowed"):
            raise ReleaseError("production writes must remain forbidden")
        if confirmation != AUTHORIZED_TARGET:
            raise ReleaseError("exact test target confirmation is required")
        if (self.runtime.root == Path(os.path.abspath(os.sep))
                and hasattr(os, "geteuid") and os.geteuid() != 0):
            raise ReleaseError("root privileges are required for the real test runtime")

    def _source_payloads(self):
        manifest_verify.verify_sources(self.manifest, self.source_root)
        for contract in self.manifest.get("release_contract_sources", []):
            source = manifest_verify._safe_source_path(
                self.source_root, contract["repository_path"],
            )
            data = manifest_verify._read_regular_file_no_follow(
                self.source_root, source,
            )
            if data is None:
                raise ReleaseError(
                    "release contract source is missing: %s" %
                    contract["repository_path"]
                )
            if manifest_verify._sha256(data) != contract["source_sha256"]:
                raise ReleaseError(
                    "release contract SHA-256 mismatch: %s" %
                    contract["repository_path"]
                )
            if manifest_verify._blob_id(data) != contract["source_blob"]:
                raise ReleaseError(
                    "release contract Git blob mismatch: %s" %
                    contract["repository_path"]
                )
        payloads = {}
        for entry in self.manifest["files"]:
            source = manifest_verify._safe_source_path(
                self.source_root, entry["repository_path"],
            )
            data = manifest_verify._read_regular_file_no_follow(
                self.source_root, source,
            )
            mode = stat.S_IMODE(os.lstat(source).st_mode)
            payloads[entry["repository_path"]] = (data, mode)
        return payloads

    def _create_backup_directory(self):
        anchor = Path(self.backup_root.anchor)
        current = anchor
        for part in self.backup_root.relative_to(anchor).parts:
            current = current / part
            try:
                info = os.lstat(current)
            except FileNotFoundError:
                os.mkdir(current, 0o700)
                info = os.lstat(current)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise ReleaseError("backup path contains a symlink or non-directory: %s" % current)
        name = "pr248-whisper-%s-%s" % (
            time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()), uuid.uuid4().hex[:8],
        )
        backup = self.backup_root / name
        os.mkdir(backup, 0o700)
        info = os.lstat(backup)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ReleaseError("backup directory must be a real directory")
        return backup

    def _backup_all(self, start_states):
        self.backup_path = self._create_backup_directory()
        state_by_runtime_path = {
            item["runtime_path"]: item for item in start_states
        }
        entries = []
        for index, entry in enumerate(self.manifest["files"], 1):
            data = self.runtime.read(entry["runtime_path"])
            start_state = state_by_runtime_path[entry["runtime_path"]]
            record = {
                "repository_path": entry["repository_path"],
                "runtime_path": entry["runtime_path"],
                "disposition": start_state["disposition"],
                "state": "absent" if data is None else "file",
                "mode": None,
                "uid": None,
                "gid": None,
                "backup_file": None,
                "sha256": manifest_verify._sha256(
                    manifest_verify.ABSENT_BYTES if data is None else data
                ),
                "blob": None if data is None else manifest_verify._blob_id(data),
            }
            if any(record[key] != start_state[key]
                   for key in ("state", "sha256", "blob")):
                raise ReleaseError(
                    "target changed while backup started: %s" %
                    entry["runtime_path"]
                )
            if data is not None:
                metadata = self.runtime.metadata(entry["runtime_path"])
                record.update(metadata)
                record["backup_file"] = "%02d.bin" % index
                backup_file = self.backup_path / record["backup_file"]
                with open(backup_file, "xb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
            entries.append(record)
            self.checkpoint("backup_%d" % index)
        self.backup_entries = entries
        state_path = self.backup_path / "backup-state.json"
        state_path.write_text(
            json.dumps({"files": entries}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        shutil.copyfile(
            Path(self.manifest["_manifest_path"]), self.backup_path / "release-manifest.json",
        )
        self.checkpoint("backup_complete")

    def _install_all(self, payloads, start_states):
        state_by_runtime_path = {
            item["runtime_path"]: item for item in start_states
        }
        backup_by_runtime_path = {
            item["runtime_path"]: item for item in self.backup_entries
        }
        installed = 0
        for index, entry in enumerate(self.manifest["files"], 1):
            start_state = state_by_runtime_path[entry["runtime_path"]]
            if start_state["disposition"] in {
                    "already_installed", "unchanged"}:
                manifest_verify.verify_targets(
                    {"files": [entry]}, self.runtime.root, "postimage",
                )
                self.checkpoint("skip_%d" % index)
                continue
            manifest_verify.verify_targets(
                {"files": [entry]}, self.runtime.root, "preimage",
            )
            data, mode = payloads[entry["repository_path"]]
            backup = backup_by_runtime_path[entry["runtime_path"]]
            self.modified_runtime_paths.add(entry["runtime_path"])
            self.runtime.atomic_write(
                entry["runtime_path"], data, mode,
                uid=backup["uid"], gid=backup["gid"],
            )
            installed += 1
            self.checkpoint("write_%d" % index)
        manifest_verify.verify_targets(self.manifest, self.runtime.root, "postimage")
        return installed

    def _run_stage(self, stage):
        commands = self.manifest["release_commands"].get(stage)
        if not commands:
            raise ReleaseError("manifest has no executable stage: %s" % stage)
        self.runner.run(
            stage, commands, source_root=self.source_root,
            runtime_root=self.runtime.root,
        )

    def _health_probe_policy(self):
        policy = self.manifest.get("health_probe_policy", {})
        timeout = float(policy.get("startup_timeout_seconds", 60))
        interval = float(policy.get("interval_seconds", 1))
        if not 1 <= timeout <= 120:
            raise ReleaseError("health startup timeout must be between 1 and 120 seconds")
        if not 0.1 <= interval <= 5 or interval > timeout:
            raise ReleaseError("health retry interval must be between 0.1 and 5 seconds")
        return timeout, interval

    @staticmethod
    def _status_value(value, label):
        if type(value) is not int or not 100 <= value <= 599:
            raise ReleaseError("%s must be an HTTP status integer" % label)
        return value

    def _validated_start_disposition(self, check, start_states):
        if not isinstance(start_states, list):
            raise ReleaseError("health start states must be an explicit list")
        manifest_targets = {
            (entry["repository_path"], entry["runtime_path"]): entry
            for entry in self.manifest.get("files", [])
        }
        if len(manifest_targets) != len(self.manifest.get("files", [])):
            raise ReleaseError("manifest runtime target mapping is duplicated")
        actual_targets = {}
        for record in start_states:
            if not isinstance(record, dict):
                raise ReleaseError("health start state record must be an object")
            key = (record.get("repository_path"), record.get("runtime_path"))
            if key not in manifest_targets:
                raise ReleaseError("health start state has an unknown runtime mapping")
            if key in actual_targets:
                raise ReleaseError("health start state runtime mapping is duplicated")
            disposition = record.get("disposition")
            if disposition not in START_DISPOSITIONS:
                raise ReleaseError("health start state disposition is unknown")
            entry = manifest_targets[key]
            if disposition == "needs_install":
                expected = (
                    entry["target_preimage_state"],
                    entry["target_preimage_sha256"],
                    entry.get("target_preimage_blob"),
                )
            elif disposition == "already_installed":
                expected = (
                    "file", entry["expected_postimage_sha256"],
                    entry["expected_postimage_blob"],
                )
            else:
                preimage = (
                    entry["target_preimage_state"],
                    entry["target_preimage_sha256"],
                    entry.get("target_preimage_blob"),
                )
                postimage = (
                    "file", entry["expected_postimage_sha256"],
                    entry["expected_postimage_blob"],
                )
                if preimage != postimage:
                    raise ReleaseError(
                        "unchanged disposition conflicts with manifest file locks"
                    )
                expected = preimage
            actual = (
                record.get("state"), record.get("sha256"), record.get("blob"),
            )
            if actual != expected:
                raise ReleaseError(
                    "health start state does not match manifest file lock"
                )
            actual_targets[key] = record
        if set(actual_targets) != set(manifest_targets):
            raise ReleaseError("health start state runtime mapping is incomplete")
        target_key = (
            check["target_repository_path"], check["target_runtime_path"],
        )
        if target_key not in actual_targets:
            raise ReleaseError("health check target runtime mapping is missing")
        return actual_targets[target_key]["disposition"]

    def _health_expected_status(self, check, phase, status_field, start_states):
        if HEALTH_PHASE_STATUS_FIELDS.get(phase) != status_field:
            raise ReleaseError("health phase and status field do not match")
        if status_field == "post_expected_status":
            if start_states is not None:
                raise ReleaseError("post-deployment health must not use start states")
            return self._status_value(
                check.get(status_field), status_field,
            )
        disposition = self._validated_start_disposition(check, start_states)
        statuses = check.get(status_field)
        if not isinstance(statuses, dict) or set(statuses) != START_DISPOSITIONS:
            raise ReleaseError(
                "%s must lock every start disposition" % status_field
            )
        return self._status_value(statuses[disposition], status_field)

    def _validate_health_contract(self):
        checks = self.manifest.get("health_checks")
        expected_fields = {
            "url", "target_repository_path", "target_runtime_path",
            "pre_status_by_disposition", "post_expected_status",
            "rollback_status_by_disposition",
        }
        if (not isinstance(checks, list) or len(checks) != 3
                or any(not isinstance(check, dict)
                       or set(check) != expected_fields for check in checks)):
            raise ReleaseError("health checks must use the exact phase-aware contract")
        urls = [str(check["url"]) for check in checks]
        if len(set(urls)) != len(urls) or set(urls) != APPROVED_HEALTH_URLS:
            raise ReleaseError("health checks must lock the three approved local URLs")
        actual_contract = {
            check["url"]: {
                key: value for key, value in check.items() if key != "url"
            }
            for check in checks
        }
        if actual_contract != APPROVED_HEALTH_CONTRACT:
            raise ReleaseError("health phase statuses do not match the locked contract")
        for check in checks:
            self._health_expected_status(
                check, "post-deployment", "post_expected_status", None,
            )

    def _verify_health(self, *, phase, status_field, start_states):
        self._validate_health_contract()
        timeout, interval = self._health_probe_policy()
        deadline = self.monotonic() + timeout
        for check in self.manifest["health_checks"]:
            expected = self._health_expected_status(
                check, phase, status_field, start_states,
            )
            last_status = None
            last_error = None
            while True:
                try:
                    last_status = self.health_getter(check["url"])
                    last_error = None
                except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                    last_status = None
                    last_error = exc
                if last_status == expected:
                    break
                remaining = deadline - self.monotonic()
                if remaining <= 0:
                    detail = (
                        "connection unavailable" if last_error is not None
                        else "status %s" % last_status
                    )
                    raise ReleaseError(
                        "%s health readiness timeout for %s: expected %s, last %s" %
                        (phase, check["url"], expected, detail)
                    )
                self.sleeper(min(interval, remaining))

    def _http_status(self, url):
        locked_urls = APPROVED_HEALTH_URLS
        parsed = urllib.parse.urlsplit(url)
        if (url not in locked_urls
                or parsed.scheme != "http"
                or parsed.hostname != "127.0.0.1"
                or parsed.port is None
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
                or parsed.path not in {
                    "/api/gen/health",
                    "/api/gen/history",
                    "/api/gen/digital-human-v2/history",
                }):
            raise ReleaseError("health probe URL is not an approved local endpoint")
        request = urllib.request.Request(url, headers={"User-Agent": "hq-release-probe"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=8) as response:
                return int(response.status)
        except urllib.error.HTTPError as exc:
            return int(exc.code)

    def _service_environment(self):
        contract = self.manifest.get("configuration_requirements", {}).get(
            "service_runtime", {}
        )
        service = str(self.manifest.get("target", {}).get("service") or "")
        expected_user = str(contract.get("user") or "")
        environment_file = str(contract.get("environment_file") or "")
        if (service != "huangque-content.service" or expected_user != "ubuntu"
                or environment_file != "/home/ubuntu/content-api/content.env"):
            raise ReleaseError("locked service runtime contract is missing or invalid")

        def show(property_name):
            try:
                result = subprocess.run(
                    ["/usr/bin/systemctl", "show", service,
                     "--property=" + property_name, "--value"],
                    check=False, capture_output=True, text=True, timeout=30,
                )
            except OSError as exc:
                raise ReleaseError(
                    "could not inspect the locked service runtime"
                ) from exc
            if result.returncode:
                raise ReleaseError(
                    "could not inspect the locked service runtime: %s" % property_name
                )
            return result.stdout.strip()

        if show("User") != expected_user:
            raise ReleaseError("service runtime user does not match the locked contract")
        if environment_file not in show("EnvironmentFiles"):
            raise ReleaseError(
                "service EnvironmentFile does not match the locked contract"
            )
        try:
            pid = int(show("MainPID"))
            if pwd is None:
                raise ReleaseError("service identity inspection requires Linux")
            account = pwd.getpwnam(expected_user)
            process = Path("/proc") / str(pid)
            if pid <= 0 or os.stat(process).st_uid != account.pw_uid:
                raise ReleaseError(
                    "service process identity does not match the locked runtime user"
                )
            raw = (process / "environ").read_bytes()
        except (KeyError, OSError, ValueError) as exc:
            raise ReleaseError(
                "could not read the active service environment fail-closed"
            ) from exc
        if len(raw) > 1024 * 1024:
            raise ReleaseError("active service environment is unexpectedly large")
        environment = {}
        for item in raw.split(b"\0"):
            if not item:
                continue
            key, separator, value = item.partition(b"=")
            if not separator:
                continue
            try:
                environment[key.decode("utf-8")] = value.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ReleaseError(
                    "active service environment contains invalid text"
                ) from exc
        return environment

    def _local_library_contract(self):
        config = self.manifest.get("configuration_requirements", {}).get(
            "local_library", {}
        )
        expected = {
            "root_environment_name": "DIGITAL_HUMAN_LOCAL_MATERIAL_LIBRARY_ROOT",
            "required_root": "/home/ubuntu/material-libraries/huangque-media",
            "index_relative_path": "index.jsonl",
            "files_relative_root": "files",
            "expected_count": 204,
            "expected_type_counts": {"image": 88, "video": 100, "bgm": 16},
            "operational_probe_required": True,
            "read_only": True,
        }
        if config != expected:
            raise ReleaseError("local material library contract is invalid")
        return config

    def _run_local_library_probe(self, root, expected_count):
        source_server = manifest_verify._safe_source_path(
            self.source_root, "server"
        )
        program = (
            "import json,sys;sys.path.insert(0,sys.argv[1]);"
            "from content_domains.digital_human_v2 import "
            "local_material_library_operational_probe as probe;"
            "print(json.dumps(probe(int(sys.argv[2])),sort_keys=True))"
        )
        environment = "DIGITAL_HUMAN_LOCAL_MATERIAL_LIBRARY_ROOT=" + root
        try:
            result = subprocess.run([
                "/usr/sbin/runuser", "-u", "ubuntu", "--",
                "/usr/bin/env", "-i", environment, "PATH=/usr/bin:/bin",
                "/usr/bin/python3", "-I", "-E", "-s", "-B", "-c", program,
                str(source_server), str(expected_count),
            ], check=False, capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ReleaseError(
                "local material library operational probe could not run"
            ) from exc
        if result.returncode:
            raise ReleaseError("local material library operational probe failed")
        try:
            payload = json.loads(result.stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ReleaseError(
                "local material library operational probe returned invalid output"
            ) from exc
        return payload

    def _verify_local_library_operational(self, phase):
        config = self._local_library_contract()
        environment = self.service_environment_getter()
        root_name = config["root_environment_name"]
        active_root = str(environment.get(root_name) or "").strip()
        if active_root != config["required_root"]:
            raise ReleaseError(
                "%s local material library root does not match the locked service configuration"
                % phase
            )
        result = self.local_library_probe_runner(
            active_root, config["expected_count"]
        )
        if (not isinstance(result, dict) or result.get("ok") is not True
                or result.get("count") != config["expected_count"]
                or result.get("types") != config["expected_type_counts"]):
            raise ReleaseError(
                "%s local material library operational result is invalid" % phase
            )

    def _restore_all(self):
        failures = []
        for record in self.backup_entries:
            if record["runtime_path"] not in self.modified_runtime_paths:
                continue
            try:
                if record["state"] == "absent":
                    self.runtime.remove(record["runtime_path"])
                else:
                    data = (self.backup_path / record["backup_file"]).read_bytes()
                    self.runtime.atomic_write(
                        record["runtime_path"], data, int(record["mode"]),
                        uid=record["uid"], gid=record["gid"],
                    )
            except Exception as exc:
                failures.append("%s: %s" % (record["runtime_path"], exc))
        self.runtime.remove_created_empty_directories()
        for record in self.backup_entries:
            try:
                data = self.runtime.read(record["runtime_path"])
                state = "absent" if data is None else "file"
                digest = manifest_verify._sha256(
                    manifest_verify.ABSENT_BYTES if data is None else data
                )
                blob = None if data is None else manifest_verify._blob_id(data)
                mismatch = (
                    state != record["state"]
                    or digest != record["sha256"]
                    or blob != record["blob"]
                )
                if data is not None:
                    metadata = self.runtime.metadata(record["runtime_path"])
                    mismatch = mismatch or any(
                        metadata[key] != record[key]
                        for key in ("mode", "uid", "gid")
                    )
                if mismatch:
                    failures.append(
                        "rollback verification mismatch: %s" % record["runtime_path"]
                    )
            except Exception as exc:
                failures.append(
                    "rollback verification %s: %s" %
                    (record["runtime_path"], exc)
                )
        if self.daemon_reload_attempted or self.restart_attempted:
            try:
                self._run_stage("rollback_daemon_reload")
            except Exception as exc:
                failures.append("rollback daemon-reload: %s" % exc)
        if self.restart_attempted:
            try:
                self._run_stage("rollback_restart")
                self._run_stage("rollback_service_active")
                self._verify_health(
                    phase="rollback",
                    status_field="rollback_status_by_disposition",
                    start_states=self.start_states,
                )
            except Exception as exc:
                failures.append("rollback service: %s" % exc)
        if failures:
            raise RollbackError("; ".join(failures))

    def execute(
            self, confirmation, reviewed_source_commit=None,
            reviewed_main_commit=None):
        self._validate_target(confirmation)
        self._verify_source_checkout(
            reviewed_source_commit or self.reviewed_source_commit,
            reviewed_main_commit or self.reviewed_main_commit,
        )
        self._verify_release_tools()
        self._preflight_release_commands()
        payloads = self._source_payloads()
        self._health_probe_policy()
        self._validate_health_contract()
        self.start_states = manifest_verify.classify_start_states(
            self.manifest, self.runtime.root,
        )
        self.checkpoint("start_state_complete")
        self._run_stage("pre_service_active")
        self._verify_health(
            phase="pre-deployment",
            status_field="pre_status_by_disposition",
            start_states=self.start_states,
        )
        self._verify_local_library_operational("pre-deployment")
        self.checkpoint("pre_health")
        self._backup_all(self.start_states)
        if manifest_verify.classify_start_states(
                self.manifest, self.runtime.root) != self.start_states:
            raise ReleaseError("deployment targets changed after backup")
        try:
            installed = self._install_all(payloads, self.start_states)
            if installed:
                for stage in (
                        "dependencies", "cache", "offline", "font", "no_charge"):
                    self._run_stage(stage)
                self.daemon_reload_attempted = True
                self._run_stage("daemon_reload")
                self.restart_attempted = True
                self._run_stage("restart")
                self._run_stage("service_active")
            self._verify_health(
                phase="post-deployment",
                status_field="post_expected_status",
                start_states=None,
            )
            self._verify_local_library_operational("post-restart")
            self.checkpoint("health")
        except Exception as release_error:
            try:
                self._restore_all()
            except Exception as rollback_error:
                raise RollbackError(
                    "release failed (%s); rollback failed (%s)" %
                    (release_error, rollback_error)
                ) from rollback_error
            raise ReleaseError(
                "release failed and all manifest targets were restored: %s" % release_error
            ) from release_error
        already_installed = sum(
            item["disposition"] == "already_installed"
            for item in self.start_states
        )
        unchanged = sum(
            item["disposition"] == "unchanged"
            for item in self.start_states
        )
        return {
            "ok": True,
            "status": "deployed" if installed else "already_deployed",
            "backup": str(self.backup_path),
            "files": len(self.manifest["files"]),
            "installed_files": installed,
            "already_installed_files": already_installed,
            "unchanged_files": unchanged,
            "restart_count": 1 if installed else 0,
        }


def main():
    parser = argparse.ArgumentParser(
        description="Deploy the reviewed expired-audio successor on Yuelei test only"
    )
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--runtime-root", default="/")
    parser.add_argument("--backup-root", default=DEFAULT_BACKUP_ROOT)
    parser.add_argument("--confirm-target", required=True)
    parser.add_argument("--reviewed-source-commit", required=True)
    parser.add_argument("--reviewed-main-commit", required=True)
    args = parser.parse_args()
    manifest = manifest_verify.load_manifest(args.manifest)
    manifest["_manifest_path"] = str(Path(args.manifest).resolve())
    result = ContentWhisperRelease(
        manifest, args.source_root, args.runtime_root, args.backup_root,
    ).execute(
        args.confirm_target,
        reviewed_source_commit=args.reviewed_source_commit,
        reviewed_main_commit=args.reviewed_main_commit,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
