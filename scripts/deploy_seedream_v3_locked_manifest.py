#!/usr/bin/env python3
"""Transactional, test-only release for the Seedream v3 successor manifest.

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
    "deploy/test-runtime/digital-human-material-seedream-v3-20260821.json",
    "docs/release-manifests/digital-human-material-feishu-priority-20260823.json",
}
HEALTH_PHASE_STATUS_FIELDS = {
    "pre-deployment": "pre_expected_statuses",
    "post-deployment": "post_expected_status",
    "rollback": "rollback_expected_statuses",
}
APPROVED_HEALTH_CONTRACT = {
    "http://127.0.0.1:8096/api/gen/health": {
        "pre_expected_statuses": [200],
        "post_expected_status": 200,
        "rollback_expected_statuses": [200],
    },
    "http://127.0.0.1:8096/api/gen/history": {
        "pre_expected_statuses": [401],
        "post_expected_status": 401,
        "rollback_expected_statuses": [401],
    },
    "http://127.0.0.1:8096/api/gen/digital-human-v2/history": {
        "pre_expected_statuses": [404, 401],
        "post_expected_status": 401,
        "rollback_expected_statuses": [404, 401],
    },
}
APPROVED_HEALTH_URLS = frozenset(APPROVED_HEALTH_CONTRACT)
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
ENV_ASSIGNMENT_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")
ALLOWED_DEPLOYMENT_TOOLS = frozenset({
    "/usr/bin/env",
    "/usr/bin/python3",
    "/usr/bin/systemctl",
})


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
            feishu_json_getter=None, feishu_media_getter=None):
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
        self.feishu_json_getter = feishu_json_getter or self._read_feishu_json
        self.feishu_media_getter = feishu_media_getter or self._read_feishu_media
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
            "scripts/deploy_seedream_v3_locked_manifest.py": Path(__file__),
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
        for stage, commands in stages.items():
            if (not isinstance(stage, str)
                    or not isinstance(commands, list) or not commands):
                raise ReleaseError(
                    "manifest release command stage is invalid"
                )
            for command in commands:
                if not isinstance(command, dict):
                    raise ReleaseError("manifest release command is invalid")
                for tool in self._deployment_tools_from_argv(
                        command.get("argv")):
                    if tool not in verified:
                        self._validate_deployment_tool(tool)
                        verified.add(tool)
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

    def _health_expected_statuses(self, check, phase, status_field):
        if HEALTH_PHASE_STATUS_FIELDS.get(phase) != status_field:
            raise ReleaseError("health phase and status field do not match")
        if status_field == "post_expected_status":
            return {self._status_value(
                check.get(status_field), status_field,
            )}
        values = check.get(status_field)
        if (not isinstance(values, list) or not values
                or len(values) != len(set(values))):
            raise ReleaseError("%s must be a non-empty unique status list" % status_field)
        return {
            self._status_value(value, status_field) for value in values
        }

    def _validate_health_contract(self):
        checks = self.manifest.get("health_checks")
        expected_fields = {
            "url", "pre_expected_statuses", "post_expected_status",
            "rollback_expected_statuses",
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
            for phase, status_field in HEALTH_PHASE_STATUS_FIELDS.items():
                self._health_expected_statuses(check, phase, status_field)

    def _verify_health(self, *, phase, status_field):
        self._validate_health_contract()
        timeout, interval = self._health_probe_policy()
        deadline = self.monotonic() + timeout
        for check in self.manifest["health_checks"]:
            expected = self._health_expected_statuses(
                check, phase, status_field,
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
                if last_status in expected:
                    break
                remaining = deadline - self.monotonic()
                if remaining <= 0:
                    detail = (
                        "connection unavailable" if last_error is not None
                        else "status %s" % last_status
                    )
                    raise ReleaseError(
                        "%s health readiness timeout for %s: expected one of %s, last %s" %
                        (phase, check["url"], sorted(expected), detail)
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

    @staticmethod
    def _feishu_proxy_handler(environment):
        proxies = {}
        for scheme in ("http", "https"):
            value = str(
                environment.get(scheme + "_proxy")
                or environment.get(scheme.upper() + "_PROXY") or ""
            ).strip()
            if value:
                proxies[scheme] = value
        return urllib.request.ProxyHandler(proxies)

    @staticmethod
    def _read_feishu_json(request, environment):
        opener = urllib.request.build_opener(
            ContentWhisperRelease._feishu_proxy_handler(environment)
        )
        try:
            with opener.open(request, timeout=15) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            raise ReleaseError(
                "Feishu operational preflight HTTP status %s" % exc.code
            ) from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            raise ReleaseError(
                "Feishu operational preflight connection failed: %s" %
                type(exc).__name__
            ) from exc
        if len(raw) > 2 * 1024 * 1024:
            raise ReleaseError("Feishu operational preflight response is too large")
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReleaseError(
                "Feishu operational preflight returned invalid JSON"
            ) from exc
        if not isinstance(result, dict):
            raise ReleaseError("Feishu operational preflight returned invalid JSON")
        return result

    @staticmethod
    def _read_feishu_media(request, environment):
        allowed_mimes = {
            "image/jpeg", "image/png", "image/webp", "video/mp4",
            "video/webm", "video/quicktime",
        }
        opener = urllib.request.build_opener(
            ContentWhisperRelease._feishu_proxy_handler(environment)
        )
        try:
            with opener.open(request, timeout=30) as response:
                mime = str(response.headers.get("Content-Type") or "").split(
                    ";", 1
                )[0].lower()
                raw = response.read(20 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            raise ReleaseError(
                "Feishu attachment preflight HTTP status %s" % exc.code
            ) from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            raise ReleaseError(
                "Feishu attachment preflight connection failed: %s" %
                type(exc).__name__
            ) from exc
        if not raw or len(raw) > 20 * 1024 * 1024:
            raise ReleaseError("Feishu attachment is empty or exceeds 20MB")
        if mime not in allowed_mimes:
            raise ReleaseError("Feishu attachment MIME is not an approved material type")
        return mime

    @staticmethod
    def _attachment_tokens(value, output):
        if isinstance(value, dict):
            token = str(value.get("file_token") or "").strip()
            if token:
                output.append(token)
            for child in value.values():
                ContentWhisperRelease._attachment_tokens(child, output)
        elif isinstance(value, list):
            for child in value:
                ContentWhisperRelease._attachment_tokens(child, output)

    def _verify_feishu_operational(self, phase):
        requirements = self.manifest.get("configuration_requirements", {})
        config = requirements.get("feishu", {})
        if config.get("operational_probe_required") is not True:
            raise ReleaseError("Feishu operational probe must be required")
        environment = self.service_environment_getter()
        credential_names = config.get("secret_environment_names")
        if credential_names != ["FEISHU_APP_ID", "FEISHU_APP_SECRET"]:
            raise ReleaseError("Feishu credential contract is invalid")
        credentials = {
            name: str(environment.get(name) or "").strip()
            for name in credential_names
        }
        if any(not value for value in credentials.values()):
            raise ReleaseError(
                "%s Feishu credentials are missing from the service environment" % phase
            )
        app_token = str(environment.get(
            config.get("app_token_environment_name"),
            config.get("app_token_default") or "",
        ) or "").strip()
        table_id = str(environment.get(
            config.get("table_environment_name"),
            config.get("table_default") or "",
        ) or "").strip()
        view_id = str(environment.get(
            config.get("view_environment_name"),
            config.get("view_default") or "",
        ) or "").strip()
        if not app_token or not table_id or not view_id:
            raise ReleaseError("%s Feishu app, table or view is missing" % phase)
        token_result = self.feishu_json_getter(urllib.request.Request(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            data=json.dumps({
                "app_id": credentials["FEISHU_APP_ID"],
                "app_secret": credentials["FEISHU_APP_SECRET"],
            }).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        ), environment)
        if int(token_result.get("code") or 0) != 0:
            raise ReleaseError("%s Feishu credential verification failed" % phase)
        tenant_token = str(token_result.get("tenant_access_token") or "").strip()
        if not tenant_token:
            raise ReleaseError("%s Feishu credential verification returned no token" % phase)
        attachment_tokens = []
        page_token = ""
        for page_index in range(20):
            query = {"page_size": 100, "view_id": view_id}
            if page_token:
                query["page_token"] = page_token
            url = (
                "https://open.feishu.cn/open-apis/bitable/v1/apps/%s/tables/%s/records?%s" %
                (urllib.parse.quote(app_token, safe=""),
                 urllib.parse.quote(table_id, safe=""),
                 urllib.parse.urlencode(query))
            )
            result = self.feishu_json_getter(urllib.request.Request(
                url, headers={"Authorization": "Bearer " + tenant_token},
            ), environment)
            if int(result.get("code") or 0) != 0:
                raise ReleaseError(
                    "%s Feishu table or view permission verification failed" % phase
                )
            data = result.get("data") or {}
            if not isinstance(data, dict) or not isinstance(data.get("items", []), list):
                raise ReleaseError("%s Feishu records response is invalid" % phase)
            for record in data.get("items", []):
                self._attachment_tokens(record, attachment_tokens)
            if not data.get("has_more"):
                break
            next_page = str(data.get("page_token") or "").strip()
            if not next_page or next_page == page_token:
                raise ReleaseError("%s Feishu pagination cursor is invalid" % phase)
            page_token = next_page
            if page_index == 19:
                raise ReleaseError("%s Feishu pagination exceeds the safety limit" % phase)
        if not attachment_tokens:
            raise ReleaseError("%s Feishu view contains no downloadable attachment" % phase)
        media_url = (
            "https://open.feishu.cn/open-apis/drive/v1/medias/%s/download" %
            urllib.parse.quote(attachment_tokens[0], safe="")
        )
        self.feishu_media_getter(urllib.request.Request(
            media_url, headers={"Authorization": "Bearer " + tenant_token},
        ), environment)
        self.checkpoint("%s_feishu_operational" % phase)

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
                    status_field="rollback_expected_statuses",
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
            status_field="pre_expected_statuses",
        )
        self._verify_feishu_operational("pre-deployment")
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
            )
            self._verify_feishu_operational("post-restart")
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
        description="Deploy the reviewed Seedream v3 successor on Yuelei test only"
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
