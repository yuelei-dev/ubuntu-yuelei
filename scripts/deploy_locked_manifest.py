#!/usr/bin/env python3
"""Fail-closed deployment of a small, hash-locked runtime overlay.

The command is intentionally local to the target host.  It never opens SSH and
it never calls an authenticated or paid endpoint.  Every source/runtime hash
and every candidate import is checked before the first target replacement.
After that point, any failure restores every file from the same backup set.
"""

import argparse
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request


class ReleaseError(RuntimeError):
    pass


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _git_blob(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _run(command, *, cwd=None, env=None):
    return subprocess.run(
        command, cwd=cwd, env=env, check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout.strip()


def _mapped_path(root, absolute_path):
    value = pathlib.PurePosixPath(str(absolute_path))
    if not value.is_absolute() or ".." in value.parts:
        raise ReleaseError("manifest runtime path must be absolute and normalized")
    root = pathlib.Path(root).resolve()
    target = root.joinpath(*value.parts[1:]).resolve()
    if target != root and root not in target.parents:
        raise ReleaseError("manifest runtime path escapes target root")
    return target


def _load_manifest(path):
    manifest = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1:
        raise ReleaseError("unsupported manifest schema")
    if manifest.get("target", {}).get("role") != "test":
        raise ReleaseError("locked overlay only permits the test target")
    policy = manifest.get("deployment_policy", {})
    if policy.get("production_server_write_allowed") is not False:
        raise ReleaseError("manifest must explicitly forbid production writes")
    if policy.get("copy_environment_or_database") is not False:
        raise ReleaseError("manifest must forbid environment/database copies")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise ReleaseError("manifest has no files")
    if len({item.get("runtime_path") for item in files}) != len(files):
        raise ReleaseError("manifest contains duplicate runtime paths")
    executor = manifest.get("release_executor")
    if not isinstance(executor, dict):
        raise ReleaseError("manifest has no executable release contract")
    return manifest


def _verify_repository(source_root, manifest):
    source_root = pathlib.Path(source_root).resolve()
    if _run(["git", "branch", "--show-current"], cwd=source_root) != "main":
        raise ReleaseError("release source must be the main branch")
    if _run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=source_root):
        raise ReleaseError("release source worktree is dirty")
    head = _run(["git", "rev-parse", "HEAD"], cwd=source_root)
    remote = _run(
        ["git", "ls-remote", "--exit-code", "origin", "refs/heads/main"],
        cwd=source_root,
    ).split()[0]
    if head != remote:
        raise ReleaseError("release source is not the live origin/main commit")
    code_source = manifest.get("source", {}).get("code_source_commit", "")
    if not code_source:
        raise ReleaseError("manifest has no code source commit")
    try:
        _run(["git", "merge-base", "--is-ancestor", code_source, head], cwd=source_root)
    except subprocess.CalledProcessError as error:
        raise ReleaseError("locked code source is not contained in release HEAD") from error
    return head


class SystemHooks:
    def validate_import(self, python_root, modules):
        expression = "; ".join("import %s" % module for module in modules)
        _run([sys.executable, "-c", expression], cwd=python_root)

    def service_active(self, service):
        return subprocess.run(
            ["systemctl", "is-active", "--quiet", service], check=False,
        ).returncode == 0

    def restart(self, service):
        _run(["systemctl", "restart", service])

    def probe(self, url, method, expected_status):
        request = urllib.request.Request(
            url, data=b"{}" if method == "POST" else None,
            headers={"Content-Type": "application/json"}, method=method,
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=10) as response:
                status = response.status
        except urllib.error.HTTPError as error:
            status = error.code
        if status != expected_status:
            raise ReleaseError(
                "unauthenticated release probe returned HTTP %s, expected %s"
                % (status, expected_status)
            )


def _build_validation_tree(target_root, source_root, manifest, destination):
    executor = manifest["release_executor"]
    runtime_python_root = _mapped_path(
        target_root, executor["runtime_python_root"],
    )
    package_name = executor.get("runtime_package", "content_domains")
    source_package = runtime_python_root / package_name
    if not source_package.is_dir():
        raise ReleaseError("runtime package required for isolated import is missing")
    validation_root = pathlib.Path(destination) / "python-root"
    shutil.copytree(
        source_package, validation_root / package_name,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.db", "*.log"),
    )
    for item in manifest["files"]:
        repository_path = pathlib.PurePosixPath(item["repository_path"])
        try:
            package_offset = repository_path.parts.index(package_name)
        except ValueError as error:
            raise ReleaseError("candidate is outside the declared runtime package") from error
        destination_path = validation_root.joinpath(*repository_path.parts[package_offset:])
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(pathlib.Path(source_root) / item["repository_path"], destination_path)
    return validation_root


def _validate_candidates(target_root, source_root, manifest, hooks, workspace):
    for item in manifest["files"]:
        path = pathlib.Path(source_root) / item["repository_path"]
        data = path.read_bytes()
        try:
            compile(data, str(path), "exec")
        except SyntaxError as error:
            raise ReleaseError("candidate syntax validation failed") from error
    validation_root = _build_validation_tree(
        target_root, source_root, manifest, workspace,
    )
    hooks.validate_import(
        validation_root, manifest["release_executor"]["import_modules"],
    )


def _atomic_copy(source, target, replace):
    target = pathlib.Path(target)
    temporary = target.with_name(".%s.hq-release-%s" % (target.name, os.getpid()))
    try:
        shutil.copy2(source, temporary)
        os.chmod(temporary, target.stat().st_mode)
        # Windows requires a writable descriptor for fsync; the deployed bytes
        # are unchanged, and Linux keeps the same durability semantics.
        with temporary.open("rb+") as handle:
            os.fsync(handle.fileno())
        replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def execute_locked_release(
    manifest_path, source_root, target_root, backup_root, *, hooks=None,
    replace=os.replace, verify_repository=True, checkpoint=None,
):
    """Execute the manifest, rolling every target back on any post-backup error."""
    hooks = hooks or SystemHooks()
    checkpoint = checkpoint or (lambda name: None)
    manifest = _load_manifest(manifest_path)
    source_root = pathlib.Path(source_root).resolve()
    target_root = pathlib.Path(target_root).resolve()
    backup_root = pathlib.Path(backup_root).resolve()

    if verify_repository:
        release_head = _verify_repository(source_root, manifest)
    else:
        release_head = "test-double"

    executor = manifest["release_executor"]
    executor_source = (source_root / executor["repository_path"]).resolve()
    if source_root not in executor_source.parents:
        raise ReleaseError("release executor path escapes source root")
    executor_data = executor_source.read_bytes()
    if _sha256(executor_data) != executor.get("sha256"):
        raise ReleaseError("release executor SHA-256 does not match manifest")
    if _git_blob(executor_data) != executor.get("git_blob"):
        raise ReleaseError("release executor Git blob does not match manifest")

    entries = []
    for item in manifest["files"]:
        source = (source_root / item["repository_path"]).resolve()
        if source_root not in source.parents:
            raise ReleaseError("repository path escapes source root")
        target = _mapped_path(target_root, item["runtime_path"])
        source_data = source.read_bytes()
        if _sha256(source_data) != item["postimage_sha256"]:
            raise ReleaseError("candidate SHA-256 does not match manifest")
        if _git_blob(source_data) != item["postimage_blob"]:
            raise ReleaseError("candidate Git blob does not match manifest")
        target_data = target.read_bytes()
        if _sha256(target_data) != item["preimage_sha256"]:
            raise ReleaseError("runtime preimage does not match manifest")
        if _git_blob(target_data) != item["preimage_blob"]:
            raise ReleaseError("runtime preimage Git blob does not match manifest")
        entries.append((item, source, target))

    backup_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="hq-locked-validate-", dir=backup_root) as workspace:
        _validate_candidates(target_root, source_root, manifest, hooks, workspace)

    service = manifest["target"]["service"]
    was_active = hooks.service_active(service)
    if not was_active:
        raise ReleaseError("target service is not active before release")

    backup = pathlib.Path(tempfile.mkdtemp(
        prefix="ip12-egress-%s-%s-" % (
            release_head[:12], time.strftime("%Y%m%d%H%M%S"),
        ),
        dir=backup_root,
    ))
    os.chmod(backup, 0o700)
    backups = []
    for index, (_, _, target) in enumerate(entries):
        saved = backup / ("%02d-%s" % (index, target.name))
        shutil.copy2(target, saved)
        backups.append(saved)
    checkpoint("after_backup")

    try:
        for index, (_, source, target) in enumerate(entries):
            _atomic_copy(source, target, replace)
            checkpoint("after_replace_%d" % index)
        checkpoint("before_postimage_verify")
        for item, _, target in entries:
            if _sha256(target.read_bytes()) != item["postimage_sha256"]:
                raise ReleaseError("deployed postimage does not match manifest")

        with tempfile.TemporaryDirectory(prefix="hq-locked-post-", dir=backup_root) as workspace:
            _validate_candidates(target_root, source_root, manifest, hooks, workspace)
        checkpoint("after_import")
        hooks.restart(service)
        checkpoint("after_restart")
        if not hooks.service_active(service):
            raise ReleaseError("target service is not active after restart")
        executor = manifest["release_executor"]
        hooks.probe(executor["health_url"], "GET", 200)
        probe = executor["unauthenticated_probe"]
        hooks.probe(probe["url"], probe["method"], probe["expected_status"])
        checkpoint("after_health")
    except BaseException as forward_error:
        rollback_errors = []
        for (item, _, target), saved in zip(entries, backups):
            try:
                _atomic_copy(saved, target, os.replace)
                if _sha256(target.read_bytes()) != item["preimage_sha256"]:
                    raise ReleaseError("restored preimage hash mismatch")
            except BaseException as error:
                rollback_errors.append(type(error).__name__)
        try:
            hooks.restart(service)
            if not hooks.service_active(service):
                raise ReleaseError("restored service is not active")
            executor = manifest["release_executor"]
            hooks.probe(executor["health_url"], "GET", 200)
            probe = executor["unauthenticated_probe"]
            hooks.probe(probe["url"], probe["method"], probe["expected_status"])
        except BaseException as error:
            rollback_errors.append(type(error).__name__)
        if rollback_errors:
            raise ReleaseError(
                "forward release failed and rollback verification failed: %s"
                % ",".join(rollback_errors)
            ) from forward_error
        raise

    return {"status": "deployed", "head": release_head, "backup": str(backup)}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--source-root", default=".")
    parser.add_argument("--target-root", default="/")
    parser.add_argument("--backup-root", required=True)
    arguments = parser.parse_args(argv)
    result = execute_locked_release(
        arguments.manifest, arguments.source_root, arguments.target_root,
        arguments.backup_root,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
