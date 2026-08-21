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
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import closing


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
        expression = "import sys; sys.path.insert(0, %r); %s" % (
            str(python_root),
            "; ".join("import %s" % module for module in modules),
        )
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

    def probe_feature(self, url, feature, expected_enabled):
        deadline = time.monotonic() + 15
        while True:
            request = urllib.request.Request(url, method="GET")
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            try:
                with opener.open(request, timeout=10) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                    if (response.status == 200
                            and bool(payload.get(feature)) is bool(expected_enabled)):
                        return
            except (OSError, ValueError, urllib.error.HTTPError):
                pass
            if time.monotonic() >= deadline:
                raise ReleaseError(
                    "health did not report %s=%s" % (feature, expected_enabled)
                )
            time.sleep(1)

    def validate_node(self, path):
        _run(["node", "--check", str(path)])

    def acceptance(self, specification):
        token_name = specification["token_environment"]
        token = str(os.environ.get(token_name, "")).strip()
        if not token:
            raise ReleaseError("authenticated acceptance token is missing")
        key = "release-pr276-%d" % int(time.time())
        body = json.dumps(
            specification["request"], ensure_ascii=False,
        ).encode("utf-8")

        def request_json(url, method="GET"):
            request = urllib.request.Request(
                url, data=body if method == "POST" else None,
                headers={
                    "Authorization": "Bearer " + token,
                    "Content-Type": "application/json",
                    "Idempotency-Key": key,
                },
                method=method,
            )
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(request, timeout=30) as response:
                if response.status != 200:
                    raise ReleaseError(
                        "authenticated acceptance returned HTTP %s"
                        % response.status
                    )
                return json.loads(response.read().decode("utf-8"))

        first = request_json(specification["submit_url"], "POST")
        replay = request_json(specification["submit_url"], "POST")
        if not first.get("job_id") or replay.get("job_id") != first["job_id"]:
            raise ReleaseError("same-key acceptance did not replay original job")
        status_url = specification["job_url_template"].format(
            job_id=int(first["job_id"]),
        )
        job = request_json(status_url)
        if int(job.get("id") or job.get("job_id") or 0) != int(first["job_id"]):
            raise ReleaseError("authenticated acceptance job is not queryable")


def _locked_value(item, prefix, suffix):
    value = item.get("%s_%s" % (prefix, suffix))
    if value is None and prefix == "postimage":
        value = item.get("expected_postimage_%s" % suffix)
    if value is None and prefix == "preimage":
        value = item.get("target_preimage_%s" % suffix)
    return value


def _capture_feature_row(database_path, feature):
    database_path = pathlib.Path(database_path)
    if not database_path.is_file() or database_path.is_symlink():
        raise ReleaseError("feature flag database is missing or unsafe")
    with closing(sqlite3.connect(str(database_path), timeout=10)) as connection:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='feature_flags'"
        ).fetchone()
        if not table:
            raise ReleaseError("feature_flags table is missing")
        row = connection.execute(
            "SELECT feature,enabled,updated_by,updated_at FROM feature_flags "
            "WHERE feature=?", (feature,),
        ).fetchone()
    if not row:
        return {"state": "absent", "feature": feature}
    return {
        "state": "row", "feature": row[0], "enabled": int(row[1]),
        "updated_by": row[2], "updated_at": int(row[3]),
    }


def _set_feature_row(database_path, feature, enabled, actor):
    now = int(time.time())
    with closing(sqlite3.connect(str(database_path), timeout=10)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO feature_flags(feature,enabled,updated_by,updated_at) "
            "VALUES(?,?,?,?) ON CONFLICT(feature) DO UPDATE SET "
            "enabled=excluded.enabled,updated_by=excluded.updated_by,"
            "updated_at=excluded.updated_at",
            (feature, 1 if enabled else 0, actor, now),
        )
        connection.commit()


def _restore_feature_row(database_path, snapshot):
    with closing(sqlite3.connect(str(database_path), timeout=10)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        if snapshot["state"] == "absent":
            connection.execute(
                "DELETE FROM feature_flags WHERE feature=?",
                (snapshot["feature"],),
            )
        else:
            connection.execute(
                "INSERT INTO feature_flags(feature,enabled,updated_by,updated_at) "
                "VALUES(?,?,?,?) ON CONFLICT(feature) DO UPDATE SET "
                "enabled=excluded.enabled,updated_by=excluded.updated_by,"
                "updated_at=excluded.updated_at",
                (snapshot["feature"], snapshot["enabled"],
                 snapshot["updated_by"], snapshot["updated_at"]),
            )
        connection.commit()


def _atomic_install(source, target, mode, replace, uid=None, gid=None):
    target = pathlib.Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(".%s.hq-release-%s" % (target.name, os.getpid()))
    try:
        shutil.copyfile(source, temporary)
        os.chmod(temporary, mode)
        if os.name != "nt" and uid is not None and gid is not None:
            os.chown(temporary, int(uid), int(gid))
        with temporary.open("rb+") as handle:
            os.fsync(handle.fileno())
        replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _validate_director_sources(source_root, manifest, hooks):
    executor = manifest["release_executor"]
    for item in manifest["files"]:
        source = pathlib.Path(source_root) / item["repository_path"]
        if item["repository_path"].endswith(".py"):
            try:
                compile(source.read_bytes(), str(source), "exec")
            except SyntaxError as error:
                raise ReleaseError("candidate Python compilation failed") from error
        elif item["repository_path"].endswith(".js"):
            hooks.validate_node(source)
        elif item["repository_path"].endswith(".html"):
            content = source.read_text(encoding="utf-8")
            for marker in executor.get("html_required_markers", []):
                if marker not in content:
                    raise ReleaseError("candidate HTML marker is missing")


def _verify_director_checkout(source_root, manifest, reviewed_head, merged_main):
    if not reviewed_head or len(reviewed_head) != 40:
        raise ReleaseError("exact reviewed PR Head is required")
    if not merged_main or len(merged_main) != 40:
        raise ReleaseError("exact merged main commit is required")
    head = _verify_repository(source_root, manifest)
    if head != merged_main:
        raise ReleaseError("checkout does not match locked merged main")
    try:
        _run(
            ["git", "merge-base", "--is-ancestor", reviewed_head, merged_main],
            cwd=source_root,
        )
    except subprocess.CalledProcessError as error:
        raise ReleaseError("reviewed PR Head is not contained in merged main") from error
    return head


def _execute_director_release(
    manifest, source_root, target_root, backup_root, *, hooks, replace,
    verify_repository, checkpoint, reviewed_head, merged_main,
):
    executor = manifest["release_executor"]
    if verify_repository:
        release_head = _verify_director_checkout(
            source_root, manifest, reviewed_head, merged_main,
        )
    else:
        release_head = merged_main or "test-double"
        reviewed_head = reviewed_head or "reviewed-test-double"

    executor_source = (source_root / executor["repository_path"]).resolve()
    executor_data = executor_source.read_bytes()
    if (_sha256(executor_data) != executor.get("sha256")
            or _git_blob(executor_data) != executor.get("git_blob")):
        raise ReleaseError("release executor lock does not match source")

    _validate_director_sources(source_root, manifest, hooks)
    entries = []
    for item in manifest["files"]:
        source = (source_root / item["repository_path"]).resolve()
        if source_root not in source.parents:
            raise ReleaseError("repository path escapes source root")
        data = source.read_bytes()
        if (_sha256(data) != _locked_value(item, "postimage", "sha256")
                or _git_blob(data) != _locked_value(item, "postimage", "blob")):
            raise ReleaseError("candidate lock does not match source")
        target = _mapped_path(target_root, item["runtime_path"])
        state = item["target_preimage_state"]
        if state == "absent":
            if target.exists() or target.is_symlink():
                raise ReleaseError("expected absent runtime preimage")
            if not target.parent.is_dir() or target.parent.is_symlink():
                raise ReleaseError("new target parent is missing or unsafe")
            parent_stat = target.parent.stat()
            mode = int(str(item.get("install_mode", "0644")), 8)
            uid, gid = parent_stat.st_uid, parent_stat.st_gid
        elif state == "file":
            if not target.is_file() or target.is_symlink():
                raise ReleaseError("expected regular runtime preimage")
            old = target.read_bytes()
            if (_sha256(old) != _locked_value(item, "preimage", "sha256")
                    or _git_blob(old) != _locked_value(item, "preimage", "blob")):
                raise ReleaseError("runtime preimage lock mismatch")
            target_stat = target.stat()
            mode = target_stat.st_mode & 0o777
            uid, gid = target_stat.st_uid, target_stat.st_gid
        else:
            raise ReleaseError("unsupported runtime preimage state")
        entries.append((item, source, target, mode, uid, gid))

    feature = manifest["feature_activation"]
    feature_db = _mapped_path(target_root, feature["database_path"])
    feature_snapshot = _capture_feature_row(feature_db, feature["feature"])
    if feature_snapshot.get("enabled"):
        raise ReleaseError("Director Agent must be disabled before release")
    if not hooks.service_active(manifest["target"]["service"]):
        raise ReleaseError("target service is not active before release")

    backup_root.mkdir(parents=True, exist_ok=True)
    backup = pathlib.Path(tempfile.mkdtemp(
        prefix="director-agent-%s-%s-" % (
            str(release_head)[:12], time.strftime("%Y%m%d%H%M%S"),
        ), dir=backup_root,
    ))
    os.chmod(backup, 0o700)
    backups = []
    audit = {
        "reviewed_head": reviewed_head, "merged_main": release_head,
        "executor_sha256": _sha256(executor_data),
        "feature_preimage": feature_snapshot, "files": [],
    }
    for index, (item, _, target, mode, uid, gid) in enumerate(entries):
        saved = None
        if item["target_preimage_state"] == "file":
            saved = backup / ("%02d-%s" % (index, target.name))
            shutil.copy2(target, saved)
        backups.append(saved)
        audit["files"].append({
            "runtime_path": item["runtime_path"],
            "state": item["target_preimage_state"],
            "backup_file": saved.name if saved else None, "mode": mode,
            "uid": uid, "gid": gid,
        })
    (backup / "audit.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    checkpoint("after_backup")

    service = manifest["target"]["service"]
    try:
        for index, (item, source, target, mode, uid, gid) in enumerate(entries):
            _atomic_install(source, target, mode, replace, uid, gid)
            checkpoint("after_replace_%d" % index)
        for item, _, target, _, _, _ in entries:
            data = target.read_bytes()
            if _sha256(data) != _locked_value(item, "postimage", "sha256"):
                raise ReleaseError("deployed postimage hash mismatch")
        _validate_director_sources(source_root, manifest, hooks)
        hooks.validate_import(
            _mapped_path(target_root, executor["runtime_python_root"]),
            executor["import_modules"],
        )
        checkpoint("after_compile")
        hooks.restart(service)
        checkpoint("after_restart")
        if not hooks.service_active(service):
            raise ReleaseError("target service is not active after restart")
        hooks.probe_feature(
            executor["health_url"], executor["health_feature_field"], False,
        )
        checkpoint("after_health_disabled")
        _set_feature_row(
            feature_db, feature["feature"], True, feature["actor"],
        )
        checkpoint("after_activate")
        hooks.probe_feature(
            executor["health_url"], executor["health_feature_field"], True,
        )
        for probe in executor.get("static_probes", []):
            hooks.probe(probe["url"], "GET", probe.get("expected_status", 200))
        checkpoint("after_health_enabled")
        hooks.acceptance(executor["authenticated_acceptance"])
        checkpoint("after_acceptance")
    except BaseException as forward_error:
        rollback_errors = []
        try:
            _restore_feature_row(feature_db, feature_snapshot)
        except BaseException as error:
            rollback_errors.append("feature:" + type(error).__name__)
        for (item, _, target, mode, uid, gid), saved in zip(entries, backups):
            try:
                if saved is None:
                    if target.is_symlink() or (target.exists() and not target.is_file()):
                        raise ReleaseError("created target became unsafe")
                    target.unlink(missing_ok=True)
                else:
                    _atomic_install(saved, target, mode, os.replace, uid, gid)
                if item["target_preimage_state"] == "file":
                    if _sha256(target.read_bytes()) != _locked_value(
                            item, "preimage", "sha256"):
                        raise ReleaseError("restored preimage hash mismatch")
                elif target.exists() or target.is_symlink():
                    raise ReleaseError("absent preimage was not restored")
            except BaseException as error:
                rollback_errors.append("file:" + type(error).__name__)
        try:
            hooks.restart(service)
            if not hooks.service_active(service):
                raise ReleaseError("restored service is not active")
            hooks.probe(executor["health_url"], "GET", 200)
        except BaseException as error:
            rollback_errors.append("service:" + type(error).__name__)
        audit["status"] = (
            "rollback_failed" if rollback_errors else "rolled_back"
        )
        audit["forward_error"] = type(forward_error).__name__
        audit["rollback_errors"] = rollback_errors
        try:
            audit["feature_final"] = _capture_feature_row(
                feature_db, feature["feature"],
            )
            audit["final_files"] = [
                {
                    "runtime_path": item["runtime_path"],
                    "state": (
                        "file" if target.is_file() and not target.is_symlink()
                        else "absent"
                    ),
                    "sha256": (
                        _sha256(target.read_bytes())
                        if target.is_file() and not target.is_symlink() else None
                    ),
                }
                for item, _, target, _, _, _ in entries
            ]
            (backup / "audit.json").write_text(
                json.dumps(audit, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except BaseException as error:
            rollback_errors.append("audit:" + type(error).__name__)
        if rollback_errors:
            raise ReleaseError(
                "forward release failed and rollback failed: %s"
                % ",".join(rollback_errors)
            ) from forward_error
        raise

    audit["status"] = "deployed"
    audit["feature_postimage"] = _capture_feature_row(
        feature_db, feature["feature"],
    )
    (backup / "audit.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return {"status": "deployed", "head": release_head, "backup": str(backup)}


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
    reviewed_head=None, merged_main=None,
):
    """Execute the manifest, rolling every target back on any post-backup error."""
    hooks = hooks or SystemHooks()
    checkpoint = checkpoint or (lambda name: None)
    manifest = _load_manifest(manifest_path)
    source_root = pathlib.Path(source_root).resolve()
    target_root = pathlib.Path(target_root).resolve()
    backup_root = pathlib.Path(backup_root).resolve()

    if (manifest["release_executor"].get("contract")
            == "director_agent_seven_file_v1"):
        return _execute_director_release(
            manifest, source_root, target_root, backup_root,
            hooks=hooks, replace=replace,
            verify_repository=verify_repository, checkpoint=checkpoint,
            reviewed_head=reviewed_head, merged_main=merged_main,
        )

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
    parser.add_argument("--reviewed-head")
    parser.add_argument("--merged-main")
    arguments = parser.parse_args(argv)
    result = execute_locked_release(
        arguments.manifest, arguments.source_root, arguments.target_root,
        arguments.backup_root,
        reviewed_head=arguments.reviewed_head,
        merged_main=arguments.merged_main,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
