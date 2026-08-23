#!/usr/bin/env python3
"""Transactional, hash-locked test release for unified video voice cloning.

This versioned executor deliberately depends on the immutable Precision v5
executor for common filesystem, hash, HTTP, and health primitives.  It adds a
database snapshot/migration transaction without changing that historical file.
"""

import argparse
import importlib.util
import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing


CONTRACT = "digital_human_unified_voice_v6"
MANIFEST_PARTS = (
    "deploy", "test-runtime", "digital-human-unified-voice-v6-20260823.json",
)
BASE_EXECUTOR_PATH = "scripts/deploy_precision_director_v5_locked_manifest.py"
BASE_EXECUTOR_BLOB = "af6c4e6295ddff641091a4a2223abc8118b0857e"
BASE_EXECUTOR_SHA256 = "aa98b9b6288cf93b1b452ecf62b9ba51cac5eac4fe7c7c059c4313bc736035c1"
DATABASE_PATH = "/home/ubuntu/content-api/digital_human_oneclick.db"
REQUIRED_PATHS = {
    "server/content_domains/audio.py",
    "server/content_domains/core.py",
    "server/content_domains/digital_human_oneclick.py",
    "server/content_domains/video.py",
    "site/workbench/digital-human-one-click.html",
    "site/workbench/digital-human-oneclick.html",
    "site/workbench/digital-human-unified-state.js",
    "site/workbench/digital-human-unified.js",
    "site/workbench/script.html",
}
REQUIRED_CONSENT_COLUMNS = {
    "id", "username", "run_id", "consent_version", "purpose",
    "video_asset_id", "video_sha256", "sample_sha256", "slot_id",
    "slot_preimage_status", "slot_preimage_voice_name",
    "slot_preimage_voice_id", "slot_preimage_provider_voice",
    "slot_preimage_reclone_count", "slot_preimage_updated_at",
    "slot_preimage_voice_updated_at", "slot_preimage_version",
    "script_sha256", "token_hash", "created_at", "expires_at",
    "last_used_at",
}


def _load_base_executor(source_root):
    path = pathlib.Path(source_root).resolve() / BASE_EXECUTOR_PATH
    data = path.read_bytes()
    if (_sha256(data) != BASE_EXECUTOR_SHA256
            or _git_blob(data) != BASE_EXECUTOR_BLOB):
        raise ReleaseError("historical Precision v5 executor lock mismatch")
    specification = importlib.util.spec_from_file_location(
        "locked_precision_v5_prerequisite", path,
    )
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _sha256(data):
    import hashlib
    return hashlib.sha256(data).hexdigest()


def _git_blob(data):
    import hashlib
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


class ReleaseError(RuntimeError):
    pass


def _locked(item, phase, kind):
    return item.get("%s_%s" % (phase, kind))


def _validate_policy(executor, key, phase):
    policy = executor.get(key)
    if not isinstance(policy, dict):
        raise ReleaseError("v6 %s health policy is missing" % phase)
    timeout = policy.get("timeout_seconds")
    interval = policy.get("interval_seconds")
    if (not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
            or timeout <= 0 or timeout > 120):
        raise ReleaseError("v6 %s health timeout is invalid" % phase)
    if (not isinstance(interval, (int, float)) or isinstance(interval, bool)
            or interval <= 0 or interval > timeout):
        raise ReleaseError("v6 %s health interval is invalid" % phase)


def _load_manifest(path):
    manifest_path = pathlib.Path(path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if tuple(manifest_path.parts[-3:]) != MANIFEST_PARTS:
        raise ReleaseError("v6 manifest must come from its locked source path")
    if manifest.get("schema_version") != 1:
        raise ReleaseError("unsupported manifest schema")
    target = manifest.get("target", {})
    if target.get("role") != "test":
        raise ReleaseError("v6 executor only permits the test target")
    policy = manifest.get("deployment_policy", {})
    required_policy = {
        "production_server_write_allowed": False,
        "require_merged_main": True,
        "fail_closed_on_preimage_mismatch": True,
        "backup_all_targets_before_first_write": True,
        "backup_live_database_before_first_write": True,
        "restore_database_on_failure": True,
        "install_all_files_before_restart": True,
        "restart_backend_once": True,
        "rollback_all_files_as_one_unit": True,
        "copy_environment_or_database": False,
    }
    if any(policy.get(key) is not value for key, value in required_policy.items()):
        raise ReleaseError("v6 deployment policy is incomplete")
    executor = manifest.get("release_executor", {})
    if executor.get("contract") != CONTRACT:
        raise ReleaseError("v6 executor rejects every other release contract")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise ReleaseError("v6 manifest has no files")
    actual = {item.get("repository_path") for item in files}
    if actual != REQUIRED_PATHS or set(executor.get("required_repository_paths") or []) != REQUIRED_PATHS:
        raise ReleaseError("v6 runtime inventory is incomplete")
    if len({item.get("runtime_path") for item in files}) != len(files):
        raise ReleaseError("v6 manifest contains duplicate runtime paths")
    for item in files:
        state = item.get("target_preimage_state")
        if state not in {"file", "absent"}:
            raise ReleaseError("v6 runtime preimage state is invalid")
        if state == "file" and not all(
                _locked(item, "preimage", kind) for kind in ("blob", "sha256")):
            raise ReleaseError("v6 file preimage lock is missing")
        if state == "absent" and any(
                _locked(item, "preimage", kind) is not None
                for kind in ("blob", "sha256")):
            raise ReleaseError("v6 absent preimage must not have content locks")
        if not all(_locked(item, "postimage", kind) for kind in ("blob", "sha256")):
            raise ReleaseError("v6 postimage lock is missing")
    database = manifest.get("database_backup", {})
    if (database.get("runtime_path") != DATABASE_PATH
            or database.get("preimage_state") != "sqlite_file"
            or database.get("backup_method") != "sqlite_online_backup"
            or database.get("migration_module") != "content_domains.digital_human_oneclick"
            or database.get("migration_callable") != "_ensure_unified_video_consent_table"
            or database.get("acceptance_table") != "digital_human_video_consents"
            or set(database.get("required_columns") or []) != REQUIRED_CONSENT_COLUMNS):
        raise ReleaseError("v6 authorization database contract is incomplete")
    base = executor.get("locked_base_executor", {})
    if (base.get("repository_path") != BASE_EXECUTOR_PATH
            or base.get("git_blob") != BASE_EXECUTOR_BLOB
            or base.get("sha256") != BASE_EXECUTOR_SHA256):
        raise ReleaseError("v6 historical executor prerequisite is not locked")
    _validate_policy(executor, "forward_health_policy", "forward")
    _validate_policy(executor, "rollback_health_policy", "rollback")
    static_urls = {probe.get("url") for probe in executor.get("static_probes", [])}
    required_static = {
        "https://yuelei.huangquechuanmei.com/workbench/digital-human-one-click.html",
        "https://yuelei.huangquechuanmei.com/workbench/digital-human-unified.js",
        "https://yuelei.huangquechuanmei.com/workbench/digital-human-unified-state.js",
    }
    if static_urls != required_static:
        raise ReleaseError("v6 static acceptance contract is incomplete")
    probes = executor.get("unauthenticated_probes", [])
    if probes != [{
        "url": "https://yuelei.huangquechuanmei.com/api/gen/video/lipsync-voice-sample",
        "method": "POST", "expected_status": 401,
    }]:
        raise ReleaseError("v6 authorization 401 acceptance is missing")
    return manifest


def _mapped(base, root, absolute_path):
    try:
        return base._mapped_path(root, absolute_path)
    except base.ReleaseError as error:
        raise ReleaseError(str(error)) from error


def _verify_checkout(base, source_root, manifest, reviewed_head, merged_main):
    if not reviewed_head or len(reviewed_head) != 40:
        raise ReleaseError("exact reviewed PR Head is required")
    if not merged_main or len(merged_main) != 40:
        raise ReleaseError("exact merged main commit is required")
    try:
        head = base._verify_repository(source_root, manifest)
    except base.ReleaseError as error:
        raise ReleaseError(str(error)) from error
    if head != merged_main:
        raise ReleaseError("checkout does not match locked merged main")
    try:
        base._run(["git", "merge-base", "--is-ancestor", reviewed_head, merged_main], cwd=source_root)
        parent = base._run(["git", "rev-parse", reviewed_head + "^"], cwd=source_root)
    except subprocess.CalledProcessError as error:
        raise ReleaseError("reviewed v6 Head is not contained in merged main") from error
    if parent != manifest.get("source", {}).get("code_source_commit"):
        raise ReleaseError("v6 code source must be the exact parent of reviewed Head")
    changed = set(base._run(
        ["git", "diff", "--name-only", parent, reviewed_head], cwd=source_root,
    ).splitlines())
    expected = {"deploy/test-runtime/digital-human-unified-voice-v6-20260823.json"}
    if changed != expected:
        raise ReleaseError("reviewed v6 Head must only finalize the locked manifest")
    return head


def _database_backup(source, destination):
    with closing(sqlite3.connect(str(source), timeout=10)) as live, \
            closing(sqlite3.connect(str(destination), timeout=10)) as saved:
        if live.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ReleaseError("authorization database preimage failed quick_check")
        live.backup(saved)
        saved.commit()
        if saved.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ReleaseError("authorization database backup failed verification")


def _database_restore(source, destination):
    with closing(sqlite3.connect(str(source), timeout=10)) as saved, \
            closing(sqlite3.connect(str(destination), timeout=10)) as live:
        saved.backup(live)
        live.commit()
        if live.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ReleaseError("authorization database restore failed verification")


def _verify_consent_schema(database_path, specification):
    with closing(sqlite3.connect(str(database_path), timeout=10)) as connection:
        rows = connection.execute(
            "PRAGMA table_info(%s)" % specification["acceptance_table"]
        ).fetchall()
    columns = {str(row[1]) for row in rows}
    required = set(specification["required_columns"])
    if not required.issubset(columns):
        raise ReleaseError("authorization consent migration is incomplete")


class SystemHooks:
    def __init__(self, base):
        self._base = base.SystemHooks()

    def __getattr__(self, name):
        return getattr(self._base, name)

    def migrate_consent(self, python_root, database_path, specification):
        expression = (
            "import importlib,sqlite3,sys;"
            "sys.path.insert(0,sys.argv[1]);"
            "m=importlib.import_module(sys.argv[2]);"
            "c=sqlite3.connect(sys.argv[3],timeout=10);"
            "getattr(m,sys.argv[4])(c);c.commit();c.close()"
        )
        subprocess.run([
            sys.executable, "-c", expression, str(python_root),
            specification["migration_module"], str(database_path),
            specification["migration_callable"],
        ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def execute_locked_release(
    manifest_path, source_root, target_root, backup_root, *, hooks=None,
    replace=os.replace, verify_repository=True, checkpoint=None,
    reviewed_head=None, merged_main=None,
):
    source_root = pathlib.Path(source_root).resolve()
    target_root = pathlib.Path(target_root).resolve()
    backup_root = pathlib.Path(backup_root).resolve()
    base = _load_base_executor(source_root)
    manifest = _load_manifest(manifest_path)
    hooks = hooks or SystemHooks(base)
    checkpoint = checkpoint or (lambda name: None)
    release_head = (
        _verify_checkout(base, source_root, manifest, reviewed_head, merged_main)
        if verify_repository else (merged_main or "test-double")
    )
    executor = manifest["release_executor"]
    executor_path = (source_root / executor["repository_path"]).resolve()
    if source_root not in executor_path.parents:
        raise ReleaseError("v6 executor path escapes source root")
    executor_data = executor_path.read_bytes()
    if (_sha256(executor_data) != executor.get("sha256")
            or _git_blob(executor_data) != executor.get("git_blob")):
        raise ReleaseError("v6 executor lock does not match source")

    entries = []
    for item in manifest["files"]:
        source = (source_root / item["repository_path"]).resolve()
        if source_root not in source.parents:
            raise ReleaseError("v6 repository path escapes source root")
        data = source.read_bytes()
        if (_sha256(data) != _locked(item, "postimage", "sha256")
                or _git_blob(data) != _locked(item, "postimage", "blob")):
            raise ReleaseError("v6 candidate lock does not match source")
        target = _mapped(base, target_root, item["runtime_path"])
        state = item["target_preimage_state"]
        if state == "file":
            if not target.is_file() or target.is_symlink():
                raise ReleaseError("expected regular v6 runtime preimage")
            old = target.read_bytes()
            if (_sha256(old) != _locked(item, "preimage", "sha256")
                    or _git_blob(old) != _locked(item, "preimage", "blob")):
                raise ReleaseError("v6 runtime preimage lock mismatch")
            stat_result = target.stat()
            mode, uid, gid = stat_result.st_mode & 0o777, stat_result.st_uid, stat_result.st_gid
        else:
            if target.exists() or target.is_symlink():
                raise ReleaseError("expected absent v6 runtime preimage")
            if not target.parent.is_dir() or target.parent.is_symlink():
                raise ReleaseError("v6 new target parent is missing or unsafe")
            stat_result = target.parent.stat()
            mode = int(str(item.get("install_mode", "0644")), 8)
            uid, gid = stat_result.st_uid, stat_result.st_gid
        entries.append((item, source, target, mode, uid, gid))

    try:
        base._validate_director_sources(source_root, manifest, hooks)
    except base.ReleaseError as error:
        raise ReleaseError(str(error)) from error
    database_spec = manifest["database_backup"]
    database = _mapped(base, target_root, database_spec["runtime_path"])
    if not database.is_file() or database.is_symlink():
        raise ReleaseError("authorization database preimage is missing or unsafe")
    if not hooks.service_active(manifest["target"]["service"]):
        raise ReleaseError("target service is not active before v6 release")

    backup_root.mkdir(parents=True, exist_ok=True)
    backup = pathlib.Path(tempfile.mkdtemp(
        prefix="unified-voice-v6-%s-%s-" % (
            str(release_head)[:12], time.strftime("%Y%m%d%H%M%S"),
        ), dir=backup_root,
    ))
    os.chmod(backup, 0o700)
    file_backups = []
    audit = {
        "reviewed_head": reviewed_head, "merged_main": release_head,
        "executor_sha256": _sha256(executor_data),
        "executor_git_blob": _git_blob(executor_data), "files": [],
        "database": {"runtime_path": database_spec["runtime_path"]},
    }
    for index, (item, _, target, mode, uid, gid) in enumerate(entries):
        saved = None
        if item["target_preimage_state"] == "file":
            saved = backup / ("%02d-%s" % (index, target.name))
            shutil.copy2(target, saved)
            if _sha256(saved.read_bytes()) != _locked(item, "preimage", "sha256"):
                raise ReleaseError("v6 file backup verification failed")
        file_backups.append(saved)
        audit["files"].append({
            "runtime_path": item["runtime_path"],
            "preimage_state": item["target_preimage_state"],
            "backup_file": saved.name if saved else None,
            "preimage_sha256": _locked(item, "preimage", "sha256"),
            "postimage_sha256": _locked(item, "postimage", "sha256"),
        })
    database_saved = backup / "digital_human_oneclick.db"
    _database_backup(database, database_saved)
    audit["database"]["backup_file"] = database_saved.name
    base._write_audit(backup / "audit.json", audit)
    checkpoint("after_backup")

    installed = []
    service = manifest["target"]["service"]
    try:
        for index, entry in enumerate(entries):
            item, source, target, mode, uid, gid = entry
            base._atomic_install(source, target, mode, replace, uid, gid)
            installed.append((entry, file_backups[index]))
            checkpoint("after_replace_%d" % index)
        for item, _, target, _, _, _ in entries:
            if _sha256(target.read_bytes()) != _locked(item, "postimage", "sha256"):
                raise ReleaseError("deployed v6 postimage hash mismatch")
        hooks.validate_import(
            _mapped(base, target_root, executor["runtime_python_root"]),
            executor["import_modules"],
        )
        hooks.migrate_consent(
            _mapped(base, target_root, executor["runtime_python_root"]),
            database, database_spec,
        )
        _verify_consent_schema(database, database_spec)
        checkpoint("after_migration")
        hooks.restart(service)
        checkpoint("after_restart")
        base._wait_for_health(
            hooks, service, executor["health_url"],
            executor["forward_health_policy"], "forward",
        )
        for probe in executor["static_probes"]:
            hooks.probe_static(probe["url"], probe["expected_status"], probe["expected_sha256"])
        for probe in executor["unauthenticated_probes"]:
            hooks.probe(probe["url"], probe["method"], probe["expected_status"])
        checkpoint("after_acceptance")
        audit["status"] = "deployed"
        audit["database"]["migration_verified"] = True
        audit["final_files"] = [
            {"runtime_path": item["runtime_path"], "sha256": _sha256(target.read_bytes())}
            for item, _, target, _, _, _ in entries
        ]
        base._write_audit(backup / "audit.json", audit)
    except BaseException as forward_error:
        rollback_errors = []
        for (item, _, target, mode, uid, gid), saved in reversed(installed):
            try:
                if saved is None:
                    if target.is_symlink() or (target.exists() and not target.is_file()):
                        raise ReleaseError("created v6 target became unsafe")
                    target.unlink(missing_ok=True)
                else:
                    base._atomic_install(saved, target, mode, os.replace, uid, gid)
                if item["target_preimage_state"] == "file":
                    if _sha256(target.read_bytes()) != _locked(item, "preimage", "sha256"):
                        raise ReleaseError("restored v6 preimage hash mismatch")
                elif target.exists() or target.is_symlink():
                    raise ReleaseError("absent v6 preimage was not restored")
            except BaseException as error:
                rollback_errors.append("file:" + type(error).__name__)
        try:
            _database_restore(database_saved, database)
        except BaseException as error:
            rollback_errors.append("database:" + type(error).__name__)
        try:
            hooks.restart(service)
            base._wait_for_health(
                hooks, service, executor["health_url"],
                executor["rollback_health_policy"], "rollback",
            )
        except BaseException as error:
            rollback_errors.append("service:" + type(error).__name__)
        audit["status"] = "rollback_failed" if rollback_errors else "rolled_back"
        audit["forward_error"] = type(forward_error).__name__
        audit["rollback_errors"] = rollback_errors
        try:
            base._write_audit(backup / "audit.json", audit)
        except BaseException as error:
            rollback_errors.append("audit:" + type(error).__name__)
        if rollback_errors:
            raise ReleaseError(
                "v6 forward release failed and rollback failed: %s"
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
    parser.add_argument("--reviewed-head", required=True)
    parser.add_argument("--merged-main", required=True)
    arguments = parser.parse_args(argv)
    result = execute_locked_release(
        arguments.manifest, arguments.source_root, arguments.target_root,
        arguments.backup_root, reviewed_head=arguments.reviewed_head,
        merged_main=arguments.merged_main,
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
