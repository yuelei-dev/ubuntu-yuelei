#!/usr/bin/env python3
"""Deploy the Director Agent digital-human guide contract as one locked test release.

This successor deliberately leaves the historical PR #276 executor untouched.
It backs up the four changed runtime files and the current feature row before
temporarily disabling the Agent.  Every failure after that point restores the
files, feature state, service health, and a durable audit record.
"""

import argparse
import hashlib
import importlib.util
import json
import os
import pathlib
import secrets
import shutil
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import closing


ROOT = pathlib.Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "release-manifests/test-runtime/director-digital-human-agent-v4-20260826.json"
BASE_EXECUTOR = ROOT / "scripts/deploy_director_locked_manifest.py"
CONTRACT = "director_digital_human_agent_four_file_v4"
REQUIRED_REPOSITORY_PATHS = {
    "server/content_domains/director_agent.py",
    "site/workbench/digital-human-oneclick.html",
    "site/workbench/script-agent.js",
    "site/workbench/script.html",
}
REQUIRED_RUNTIME_PATHS = {
    "server/content_domains/director_agent.py":
        "/home/ubuntu/content-api/content_domains/director_agent.py",
    "site/workbench/digital-human-oneclick.html":
        "/var/www/huangquechuanmei/workbench/digital-human-oneclick.html",
    "site/workbench/script-agent.js":
        "/var/www/huangquechuanmei/workbench/script-agent.js",
    "site/workbench/script.html":
        "/var/www/huangquechuanmei/workbench/script.html",
}
ALLOWED_REVIEW_DELTA = {
    MANIFEST.relative_to(ROOT).as_posix(),
}


def _load_base_executor():
    specification = importlib.util.spec_from_file_location(
        "director_agent_v2_release_base", BASE_EXECUTOR,
    )
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


BASE = _load_base_executor()
ReleaseError = BASE.ReleaseError


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _git_blob(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _require_lock(value, length, label):
    if not isinstance(value, str) or len(value) != length:
        raise ReleaseError("%s lock is invalid" % label)
    try:
        int(value, 16)
    except ValueError as error:
        raise ReleaseError("%s lock is invalid" % label) from error


def _validate_policy(policy):
    required = {
        "production_server_write_allowed": False,
        "copy_environment_or_database": False,
        "backup_all_targets_before_first_write": True,
        "rollback_all_files_and_feature_state_as_one_unit": True,
        "require_merged_main": True,
    }
    if not isinstance(policy, dict):
        raise ReleaseError("deployment policy is missing")
    for key, expected in required.items():
        if policy.get(key) is not expected:
            raise ReleaseError("deployment policy must set %s=%s" % (key, expected))


def _validate_manifest(manifest):
    if manifest.get("schema_version") != 1:
        raise ReleaseError("unsupported manifest schema")
    if manifest.get("target", {}).get("role") != "test":
        raise ReleaseError("locked release only permits the test target")
    _validate_policy(manifest.get("deployment_policy"))
    source_commit = manifest.get("source", {}).get("code_source_commit")
    _require_lock(source_commit, 40, "code source commit")
    feature_preimage = manifest.get("expected_preimage", {}).get("feature_flag")
    if (not isinstance(feature_preimage, dict)
            or feature_preimage.get("state") != "row"
            or feature_preimage.get("enabled") is not True):
        raise ReleaseError("expected enabled feature preimage is missing")

    executor = manifest.get("release_executor")
    if not isinstance(executor, dict) or executor.get("contract") != CONTRACT:
        raise ReleaseError("digital-human Agent release contract is invalid")
    if executor.get("repository_path") != (
            "release-manifests/tools/deploy_director_digital_human_agent_v4_locked_manifest.py"):
        raise ReleaseError("release executor path is invalid")
    _require_lock(executor.get("git_blob"), 40, "release executor blob")
    _require_lock(executor.get("sha256"), 64, "release executor SHA-256")
    if set(executor.get("required_repository_paths") or []) != REQUIRED_REPOSITORY_PATHS:
        raise ReleaseError("release executor does not lock the four-file scope")

    base_lock = executor.get("locked_base_executor")
    if (not isinstance(base_lock, dict)
            or base_lock.get("repository_path") !=
            "scripts/deploy_director_locked_manifest.py"):
        raise ReleaseError("historical base executor lock is missing")
    _require_lock(base_lock.get("git_blob"), 40, "base executor blob")
    _require_lock(base_lock.get("sha256"), 64, "base executor SHA-256")

    files = manifest.get("files")
    if not isinstance(files, list) or len(files) != 4:
        raise ReleaseError("release must contain exactly four runtime files")
    paths = {item.get("repository_path") for item in files}
    if paths != REQUIRED_REPOSITORY_PATHS:
        raise ReleaseError("release file scope is incomplete")
    if len({item.get("runtime_path") for item in files}) != len(files):
        raise ReleaseError("release contains duplicate runtime paths")
    for item in files:
        path = item["repository_path"]
        if item.get("runtime_path") != REQUIRED_RUNTIME_PATHS[path]:
            raise ReleaseError("runtime path does not match locked repository path")
        if item.get("target_preimage_state") != "file":
            raise ReleaseError("successor release requires four existing preimages")
        for prefix in ("preimage", "postimage"):
            _require_lock(item.get(prefix + "_blob"), 40, path + " " + prefix)
            _require_lock(item.get(prefix + "_sha256"), 64, path + " " + prefix)

    contract_sources = manifest.get("release_contract_sources")
    if not isinstance(contract_sources, list) or len(contract_sources) != 3:
        raise ReleaseError("release contract sources are incomplete")
    expected_contract_paths = {
        "deploy/test-release/impacts/pr-293-digital-human-guide-v1.json",
        "release-manifests/tools/deploy_director_digital_human_agent_v4_locked_manifest.py",
        "tests/test_director_digital_human_agent_v4_release.py",
    }
    if {item.get("repository_path") for item in contract_sources} != expected_contract_paths:
        raise ReleaseError("release contract source scope is invalid")
    for item in contract_sources:
        _require_lock(item.get("git_blob"), 40, "contract source blob")
        _require_lock(item.get("sha256"), 64, "contract source SHA-256")

    feature = manifest.get("feature_activation")
    if (not isinstance(feature, dict)
            or feature.get("feature") != "director_agent"
            or feature.get("target_enabled") is not True
            or feature.get("database_path") !=
            "/home/ubuntu/content-api/feature_flags.db"):
        raise ReleaseError("feature activation contract is invalid")

    acceptance = executor.get("authenticated_acceptance")
    request = acceptance.get("request") if isinstance(acceptance, dict) else None
    context = request.get("page_context") if isinstance(request, dict) else None
    if (not isinstance(context, dict)
            or context.get("page") != "digital_human_oneclick"
            or request.get("source_page") != "digital_human_oneclick"):
        raise ReleaseError("digital-human authenticated acceptance is missing")
    revision = request.get("page_revision")
    if (not isinstance(revision, str)
            or BASE._DIRECTOR_REVISION_PATTERN.fullmatch(revision) is None):
        raise ReleaseError("acceptance page revision is invalid")

    markers = executor.get("html_required_markers")
    expected_markers = {
        "site/workbench/digital-human-oneclick.html": [
            'data-director-guide-contract="digital-human-oneclick-guide-v1"',
            "script-agent.js?v=b1c3f8c3",
        ],
        "site/workbench/script.html": ["script-agent.js?v=b1c3f8c3"],
    }
    if markers != expected_markers:
        raise ReleaseError("locked HTML cache marker is missing")
    probes = executor.get("static_probes")
    if not isinstance(probes, list) or len(probes) != 3:
        raise ReleaseError("release must probe both pages and the Agent script")
    policy = executor.get("rollback_health_policy")
    timeout = policy.get("timeout_seconds") if isinstance(policy, dict) else None
    interval = policy.get("interval_seconds") if isinstance(policy, dict) else None
    if (not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
            or timeout <= 0 or timeout > 120
            or not isinstance(interval, (int, float)) or isinstance(interval, bool)
            or interval <= 0 or interval > timeout):
        raise ReleaseError("rollback health policy is invalid")
    return manifest


def _load_manifest(path):
    path = pathlib.Path(path).resolve()
    if path != MANIFEST.resolve():
        raise ReleaseError("locked executor rejects every other manifest path")
    return _validate_manifest(json.loads(path.read_text(encoding="utf-8")))


def _lock_matches(path, lock):
    data = pathlib.Path(path).read_bytes()
    return _sha256(data) == lock.get("sha256") and _git_blob(data) == lock.get("git_blob")


def _verify_checkout(source_root, manifest, reviewed_head, merged_main):
    head = BASE._verify_director_checkout(
        source_root, manifest, reviewed_head, merged_main,
    )
    parent = BASE._run(
        ["git", "rev-parse", reviewed_head + "^"], cwd=source_root,
    )
    if parent != manifest["source"]["code_source_commit"]:
        raise ReleaseError("reviewed Head parent is not the locked code source commit")
    changed = set(filter(None, BASE._run(
        ["git", "diff", "--name-only", parent, reviewed_head], cwd=source_root,
    ).splitlines()))
    if changed != ALLOWED_REVIEW_DELTA:
        raise ReleaseError("reviewed Head is not the exact manifest-and-test lock commit")
    return head


def _validate_sources(source_root, target_root, manifest, hooks):
    source_root = pathlib.Path(source_root).resolve()
    executor = manifest["release_executor"]
    executor_path = source_root / executor["repository_path"]
    if not _lock_matches(executor_path, executor):
        raise ReleaseError("release executor lock does not match source")
    base_lock = executor["locked_base_executor"]
    if not _lock_matches(source_root / base_lock["repository_path"], base_lock):
        raise ReleaseError("historical base executor lock does not match source")
    for lock in manifest["release_contract_sources"]:
        if not _lock_matches(source_root / lock["repository_path"], lock):
            raise ReleaseError("release contract source lock does not match source")

    for item in manifest["files"]:
        source = (source_root / item["repository_path"]).resolve()
        if source_root not in source.parents:
            raise ReleaseError("repository path escapes source root")
        data = source.read_bytes()
        if (_sha256(data) != item["postimage_sha256"]
                or _git_blob(data) != item["postimage_blob"]):
            raise ReleaseError("candidate lock does not match source")
        if source.suffix == ".py":
            try:
                compile(data, str(source), "exec")
            except SyntaxError as error:
                raise ReleaseError("candidate Python compilation failed") from error
        elif source.suffix == ".js":
            hooks.validate_node(source)
        elif source.suffix == ".html":
            content = data.decode("utf-8")
            for marker in executor["html_required_markers"].get(
                    item["repository_path"], []):
                if marker not in content:
                    raise ReleaseError("candidate HTML cache marker is missing")

    runtime_python_root = BASE._mapped_path(
        target_root, executor["runtime_python_root"],
    )
    runtime_package = runtime_python_root / "content_domains"
    if not runtime_package.is_dir() or runtime_package.is_symlink():
        raise ReleaseError("runtime content_domains package is missing or unsafe")
    with tempfile.TemporaryDirectory(prefix="hq-dh-agent-import-") as directory:
        validation_root = pathlib.Path(directory)
        shutil.copytree(
            runtime_package, validation_root / "content_domains",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.db", "*.log"),
        )
        shutil.copy2(
            source_root / "server/content_domains/director_agent.py",
            validation_root / "content_domains/director_agent.py",
        )
        hooks.validate_import(validation_root, executor["import_modules"])


class SystemHooks(BASE.SystemHooks):
    """Use the historical system adapters with a v4-specific acceptance key."""

    def acceptance(self, specification):
        token_name = specification["token_environment"]
        token = str(os.environ.get(token_name, "")).strip()
        if not token:
            raise ReleaseError("authenticated acceptance token is missing")
        key = "release-dh-agent-v4-" + secrets.token_hex(16)
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
            try:
                with opener.open(request, timeout=30) as response:
                    data = response.read()
                    if response.status != 200:
                        raise ReleaseError(
                            "authenticated acceptance returned HTTP %s: %s"
                            % (response.status, BASE._http_error_detail(data))
                        )
                    return json.loads(data.decode("utf-8"))
            except urllib.error.HTTPError as error:
                raise ReleaseError(
                    "authenticated acceptance returned HTTP %s: %s"
                    % (error.code, BASE._http_error_detail(error.read()))
                ) from error

        first = request_json(specification["submit_url"], "POST")
        replay = request_json(specification["submit_url"], "POST")
        if not first.get("job_id") or replay.get("job_id") != first["job_id"]:
            raise ReleaseError("same-key acceptance did not replay the original job")
        if int(first.get("cost") or 0) != 0 or int(replay.get("cost") or 0) != 0:
            raise ReleaseError("Director Agent acceptance must remain zero-cost")
        status_url = specification["job_url_template"].format(
            job_id=int(first["job_id"]),
        )
        deadline = time.monotonic() + int(
            specification.get("job_timeout_seconds", 120)
        )
        while True:
            job = request_json(status_url)
            if int(job.get("id") or job.get("job_id") or 0) != int(first["job_id"]):
                raise ReleaseError("authenticated acceptance job is not queryable")
            if job.get("kind") != "director_agent" or int(job.get("cost") or 0) != 0:
                raise ReleaseError("authenticated acceptance returned the wrong job")
            status = str(job.get("status") or "")
            if status == "done":
                result = job.get("result")
                plan = result.get("plan") if isinstance(result, dict) else None
                actions = plan.get("actions") if isinstance(plan, dict) else None
                expected = specification.get("expected_action") or {}
                if (not isinstance(result, dict)
                        or result.get("type") != "director_agent"
                        or not isinstance(actions, list)
                        or plan.get("page_revision") !=
                        specification["request"]["page_revision"]
                        or not any(
                            action.get("type") == expected.get("type")
                            and action.get("field") == expected.get("field")
                            and action.get("value") == expected.get("value")
                            for action in actions if isinstance(action, dict)
                        )):
                    raise ReleaseError("digital-human Agent acceptance result is invalid")
                return
            if status in {"error", "failed"}:
                raise ReleaseError("Director Agent acceptance job failed")
            if status not in {"pending", "running"}:
                raise ReleaseError("Director Agent acceptance status is invalid")
            if time.monotonic() >= deadline:
                raise ReleaseError("Director Agent acceptance job timed out")
            time.sleep(1)


def _execute_manifest(
    manifest, source_root, target_root, backup_root, *, hooks=None,
    replace=os.replace, verify_repository=True, checkpoint=None,
    reviewed_head=None, merged_main=None,
):
    manifest = _validate_manifest(manifest)
    hooks = hooks or SystemHooks()
    checkpoint = checkpoint or (lambda name: None)
    source_root = pathlib.Path(source_root).resolve()
    target_root = pathlib.Path(target_root).resolve()
    backup_root = pathlib.Path(backup_root).resolve()
    if verify_repository:
        release_head = _verify_checkout(
            source_root, manifest, reviewed_head, merged_main,
        )
    else:
        release_head = merged_main or "test-double"
        reviewed_head = reviewed_head or "reviewed-test-double"

    _validate_sources(source_root, target_root, manifest, hooks)
    entries = []
    for item in manifest["files"]:
        source = source_root / item["repository_path"]
        target = BASE._mapped_path(target_root, item["runtime_path"])
        if not target.is_file() or target.is_symlink():
            raise ReleaseError("expected regular runtime preimage")
        old = target.read_bytes()
        if (item["preimage_sha256"] == item["postimage_sha256"]
                and item["preimage_blob"] == item["postimage_blob"]
                and _sha256(old) == item["preimage_sha256"]
                and _git_blob(old) == item["preimage_blob"]):
            start_state = "unchanged"
            start_prefix = "preimage"
        elif (_sha256(old) == item["preimage_sha256"]
                and _git_blob(old) == item["preimage_blob"]):
            start_state = "needs_install"
            start_prefix = "preimage"
        elif (_sha256(old) == item["postimage_sha256"]
                and _git_blob(old) == item["postimage_blob"]):
            start_state = "already_installed"
            start_prefix = "postimage"
        else:
            raise ReleaseError("runtime preimage lock mismatch")
        target_stat = target.stat()
        entries.append((
            item, source, target, target_stat.st_mode & 0o777,
            target_stat.st_uid, target_stat.st_gid, start_state, start_prefix,
        ))

    feature = manifest["feature_activation"]
    feature_db = BASE._mapped_path(target_root, feature["database_path"])
    feature_snapshot = BASE._capture_feature_row(feature_db, feature["feature"])
    expected_feature = manifest["expected_preimage"]["feature_flag"]
    if (feature_snapshot.get("state") != expected_feature["state"]
            or bool(feature_snapshot.get("enabled")) is not
            expected_feature["enabled"]):
        raise ReleaseError("feature flag preimage does not match locked state")
    service = manifest["target"]["service"]
    if not hooks.service_active(service):
        raise ReleaseError("target service is not active before release")

    backup_root.mkdir(parents=True, exist_ok=True)
    backup = pathlib.Path(tempfile.mkdtemp(
        prefix="director-dh-agent-%s-%s-" % (
            str(release_head)[:12], time.strftime("%Y%m%d%H%M%S"),
        ), dir=backup_root,
    ))
    os.chmod(backup, 0o700)
    backups = []
    executor = manifest["release_executor"]
    executor_data = (source_root / executor["repository_path"]).read_bytes()
    base_data = (source_root / executor["locked_base_executor"]["repository_path"]).read_bytes()
    audit = {
        "reviewed_head": reviewed_head, "merged_main": release_head,
        "executor_sha256": _sha256(executor_data),
        "executor_git_blob": _git_blob(executor_data),
        "base_executor_sha256": _sha256(base_data),
        "base_executor_git_blob": _git_blob(base_data),
        "feature_preimage": feature_snapshot, "files": [],
    }
    for index, (item, _, target, mode, uid, gid, start_state, start_prefix) in enumerate(entries):
        saved = backup / ("%02d-%s" % (index, target.name))
        shutil.copy2(target, saved)
        if os.name != "nt":
            os.chown(saved, uid, gid)
        saved_stat = saved.stat()
        if (_sha256(saved.read_bytes()) != item[start_prefix + "_sha256"]
                or (saved_stat.st_mode & 0o777) != mode
                or (os.name != "nt" and (
                    saved_stat.st_uid != uid or saved_stat.st_gid != gid
                ))):
            raise ReleaseError("runtime backup verification failed")
        backups.append(saved)
        audit["files"].append({
            "runtime_path": item["runtime_path"], "state": "file",
            "backup_file": saved.name, "mode": mode, "uid": uid, "gid": gid,
            "start_state": start_state,
            "preimage_sha256": item[start_prefix + "_sha256"],
            "postimage_sha256": item["postimage_sha256"],
        })
    BASE._write_audit(backup / "audit.json", audit)

    try:
        checkpoint("after_backup")
        BASE._set_feature_row(
            feature_db, feature["feature"], False,
            feature["actor"] + ":preflight",
        )
        checkpoint("after_disable")
        hooks.probe_feature(
            executor["health_url"], executor["health_feature_field"], False,
        )
        checkpoint("after_health_disabled_before_install")
        for index, (item, source, target, mode, uid, gid, start_state, _) in enumerate(entries):
            if start_state == "needs_install":
                BASE._atomic_install(source, target, mode, replace, uid, gid)
            checkpoint("after_replace_%d" % index)
        for item, _, target, _, _, _, _, _ in entries:
            if _sha256(target.read_bytes()) != item["postimage_sha256"]:
                raise ReleaseError("deployed postimage hash mismatch")
        _validate_sources(source_root, target_root, manifest, hooks)
        hooks.validate_import(
            BASE._mapped_path(target_root, executor["runtime_python_root"]),
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
        BASE._set_feature_row(
            feature_db, feature["feature"], feature["target_enabled"],
            feature["actor"],
        )
        checkpoint("after_activate")
        hooks.probe_feature(
            executor["health_url"], executor["health_feature_field"], True,
        )
        for probe in executor["static_probes"]:
            hooks.probe_static(
                probe["url"], probe.get("expected_status", 200),
                probe["expected_sha256"],
            )
        checkpoint("after_static")
        hooks.acceptance(executor["authenticated_acceptance"])
        checkpoint("after_acceptance")
        audit["status"] = "deployed"
        audit["feature_postimage"] = BASE._capture_feature_row(
            feature_db, feature["feature"],
        )
        audit["final_files"] = [
            {"runtime_path": item["runtime_path"], "state": "file",
             "sha256": _sha256(target.read_bytes())}
            for item, _, target, _, _, _, _, _ in entries
        ]
        BASE._write_audit(backup / "audit.json", audit)
        checkpoint("after_final_audit")
    except BaseException as forward_error:
        rollback_errors = []
        try:
            BASE._set_feature_row(
                feature_db, feature["feature"], False,
                feature["actor"] + ":rollback",
            )
        except BaseException as error:
            rollback_errors.append("disable:" + type(error).__name__)
        for (item, _, target, mode, uid, gid, _, start_prefix), saved in zip(entries, backups):
            try:
                BASE._atomic_install(saved, target, mode, os.replace, uid, gid)
                restored = target.read_bytes()
                if (_sha256(restored) != item[start_prefix + "_sha256"]
                        or _git_blob(restored) != item[start_prefix + "_blob"]):
                    raise ReleaseError("restored preimage hash mismatch")
            except BaseException as error:
                rollback_errors.append("file:" + type(error).__name__)
        try:
            BASE._restore_feature_row(feature_db, feature_snapshot)
        except BaseException as error:
            rollback_errors.append("feature:" + type(error).__name__)
        try:
            hooks.restart(service)
            BASE._wait_for_rollback_health(
                hooks, service, executor["health_url"],
                executor["rollback_health_policy"],
            )
            hooks.probe_feature(
                executor["health_url"], executor["health_feature_field"],
                bool(feature_snapshot.get("enabled")),
            )
        except BaseException as error:
            rollback_errors.append("service:" + type(error).__name__)
        audit["status"] = "rollback_failed" if rollback_errors else "rolled_back"
        audit["forward_error"] = type(forward_error).__name__
        audit["rollback_errors"] = rollback_errors
        try:
            audit["feature_final"] = BASE._capture_feature_row(
                feature_db, feature["feature"],
            )
            audit["final_files"] = [
                {"runtime_path": item["runtime_path"], "state": "file",
                 "sha256": _sha256(target.read_bytes())}
                for item, _, target, _, _, _, _, _ in entries
            ]
            BASE._write_audit(backup / "audit.json", audit)
        except BaseException as error:
            rollback_errors.append("audit:" + type(error).__name__)
        if rollback_errors:
            raise ReleaseError(
                "forward release failed and rollback failed: %s"
                % ",".join(rollback_errors)
            ) from forward_error
        raise

    return {"status": "deployed", "head": release_head, "backup": str(backup)}


def execute_locked_release(
    manifest_path, source_root, target_root, backup_root, *, hooks=None,
    replace=os.replace, verify_repository=True, checkpoint=None,
    reviewed_head=None, merged_main=None,
):
    return _execute_manifest(
        _load_manifest(manifest_path), source_root, target_root, backup_root,
        hooks=hooks, replace=replace, verify_repository=verify_repository,
        checkpoint=checkpoint, reviewed_head=reviewed_head,
        merged_main=merged_main,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Execute the locked Director digital-human Agent v4 test release.",
    )
    parser.add_argument("manifest", type=pathlib.Path)
    parser.add_argument("--source-root", type=pathlib.Path, required=True)
    parser.add_argument("--target-root", type=pathlib.Path, default=pathlib.Path("/"))
    parser.add_argument("--backup-root", type=pathlib.Path, required=True)
    parser.add_argument("--reviewed-head", required=True)
    parser.add_argument("--merged-main", required=True)
    args = parser.parse_args(argv)
    result = execute_locked_release(
        args.manifest, args.source_root, args.target_root, args.backup_root,
        reviewed_head=args.reviewed_head, merged_main=args.merged_main,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
