#!/usr/bin/env python3
"""Transactional, test-only release for the cumulative Whisper runtime.

The executor never connects to a remote host.  It consumes the reviewed
manifest locally on the Yuelei test server, verifies all seven targets before
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
import urllib.request
import uuid
from pathlib import Path

import verify_content_whisper_deployment as manifest_verify


AUTHORIZED_TARGET = "test@8.148.158.106"
DEFAULT_BACKUP_ROOT = "/opt/huangque-deploy-backups"
MANIFEST_REPOSITORY_PATH = (
    "deploy/test-runtime/digital-human-whisper-runtime-20260815.json"
)
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")


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

    def mode(self, runtime_path):
        target = self.path(runtime_path)
        manifest_verify._lstat_no_symlink_chain(self.root, target)
        info = os.lstat(target)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ReleaseError("target is not a regular file: %s" % target)
        return stat.S_IMODE(info.st_mode)

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

    def atomic_write(self, runtime_path, data, mode):
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
            reviewed_main_commit=None):
        self.manifest = manifest
        self.source_root = Path(os.path.abspath(source_root))
        self.runtime = RuntimeFiles(runtime_root)
        self.backup_root = Path(os.path.abspath(backup_root))
        self.runner = runner or CommandRunner()
        self.git_runner = git_runner or GitRunner()
        self.reviewed_source_commit = reviewed_source_commit
        self.reviewed_main_commit = reviewed_main_commit
        self.health_getter = health_getter or self._http_status
        self.checkpoint = checkpoint or (lambda _name: None)
        self.backup_path = None
        self.backup_entries = []
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
            "scripts/deploy_content_whisper_runtime.py": Path(__file__),
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
        expected_manifest = Path(os.path.abspath(
            self.source_root / MANIFEST_REPOSITORY_PATH
        ))
        if manifest_path != expected_manifest:
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

    def _backup_all(self):
        self.backup_path = self._create_backup_directory()
        entries = []
        for index, entry in enumerate(self.manifest["files"], 1):
            data = self.runtime.read(entry["runtime_path"])
            record = {
                "repository_path": entry["repository_path"],
                "runtime_path": entry["runtime_path"],
                "state": "absent" if data is None else "file",
                "mode": None,
                "backup_file": None,
                "sha256": manifest_verify._sha256(
                    manifest_verify.ABSENT_BYTES if data is None else data
                ),
                "blob": None if data is None else manifest_verify._blob_id(data),
            }
            if data is not None:
                record["mode"] = self.runtime.mode(entry["runtime_path"])
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

    def _install_all(self, payloads):
        for index, entry in enumerate(self.manifest["files"], 1):
            manifest_verify.verify_targets(
                {"files": [entry]}, self.runtime.root, "preimage",
            )
            data, mode = payloads[entry["repository_path"]]
            self.runtime.atomic_write(entry["runtime_path"], data, mode)
            self.checkpoint("write_%d" % index)
        manifest_verify.verify_targets(self.manifest, self.runtime.root, "postimage")

    def _run_stage(self, stage):
        commands = self.manifest["release_commands"].get(stage)
        if not commands:
            raise ReleaseError("manifest has no executable stage: %s" % stage)
        self.runner.run(
            stage, commands, source_root=self.source_root,
            runtime_root=self.runtime.root,
        )

    def _verify_health(self, prefix=""):
        for check in self.manifest["health_checks"]:
            status = self.health_getter(check["url"])
            if status != int(check["expected_status"]):
                raise ReleaseError(
                    "%shealth status mismatch for %s: %s" %
                    (prefix, check["url"], status)
                )

    @staticmethod
    def _http_status(url):
        request = urllib.request.Request(url, headers={"User-Agent": "hq-release-probe"})
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                return int(response.status)
        except urllib.error.HTTPError as exc:
            return int(exc.code)

    def _restore_all(self):
        failures = []
        for record in self.backup_entries:
            try:
                if record["state"] == "absent":
                    self.runtime.remove(record["runtime_path"])
                else:
                    data = (self.backup_path / record["backup_file"]).read_bytes()
                    self.runtime.atomic_write(
                        record["runtime_path"], data, int(record["mode"]),
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
                if state != record["state"] or digest != record["sha256"]:
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
                self._verify_health("rollback ")
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
        payloads = self._source_payloads()
        manifest_verify.verify_targets(self.manifest, self.runtime.root, "preimage")
        self.checkpoint("preimage_complete")
        self._run_stage("pre_service_active")
        self._backup_all()
        manifest_verify.verify_targets(self.manifest, self.runtime.root, "preimage")
        try:
            self._install_all(payloads)
            for stage in (
                    "dependencies", "cache", "offline", "font", "no_charge"):
                self._run_stage(stage)
            self.daemon_reload_attempted = True
            self._run_stage("daemon_reload")
            self.restart_attempted = True
            self._run_stage("restart")
            self._run_stage("service_active")
            self._verify_health()
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
                "release failed and all seven targets were restored: %s" % release_error
            ) from release_error
        return {
            "ok": True,
            "backup": str(self.backup_path),
            "files": len(self.manifest["files"]),
            "restart_count": 1,
        }


def main():
    parser = argparse.ArgumentParser(
        description="Deploy the reviewed cumulative Whisper runtime on Yuelei test only"
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
