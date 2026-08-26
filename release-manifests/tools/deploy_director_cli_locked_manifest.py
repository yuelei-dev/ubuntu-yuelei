#!/usr/bin/env python3
"""Transactionally deploy the Director Agent to HQ CLI bridge on test."""

import argparse
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time


_REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[2]
_BASE_EXECUTOR_PATH = _REPOSITORY_ROOT / "scripts/deploy_director_locked_manifest.py"
_BASE_EXECUTOR_SHA256 = "a319b3edf1ac66d44e1c3e1b10defc753eeb7a7b8e172f1f12cd89ebf17e0aa4"
_BASE_EXECUTOR_GIT_BLOB = "4805b8f79e0f650be3185b505253231520e74e63"
_BASE_EXECUTOR_DATA = _BASE_EXECUTOR_PATH.read_bytes()
if (hashlib.sha256(_BASE_EXECUTOR_DATA).hexdigest() != _BASE_EXECUTOR_SHA256
        or hashlib.sha1(
            b"blob %d\0" % len(_BASE_EXECUTOR_DATA) + _BASE_EXECUTOR_DATA
        ).hexdigest() != _BASE_EXECUTOR_GIT_BLOB):
    raise RuntimeError("locked Director Agent supporting executor mismatch")
_BASE_SPEC = importlib.util.spec_from_file_location(
    "hq_director_release_base", _BASE_EXECUTOR_PATH,
)
_BASE = importlib.util.module_from_spec(_BASE_SPEC)
_BASE_SPEC.loader.exec_module(_BASE)

ReleaseError = _BASE.ReleaseError
_CONTRACT = "director_agent_cli_bridge_v1"
_RUNTIME_ROOT = "/opt/huangque-repository/tools/hq-cli"
_CLI_FILES = {
    "src/hq_cli/__init__.py", "src/hq_cli/__main__.py",
    "src/hq_cli/catalog.py", "src/hq_cli/cli.py", "src/hq_cli/client.py",
}
_CAPABILITIES = {"script", "digital-presenter-capability", "assets-page"}
_FORBIDDEN_COMMANDS = {"run", "login", "upload", "confirm", "quote-token"}
_RUNTIME_FILES = {
    "/home/ubuntu/content-api/content_domains/director_agent.py",
    "/home/ubuntu/content-api/content_domains/director_cli.py",
}


def _validate_contract(manifest):
    executor = manifest.get("release_executor", {})
    if executor.get("contract") != _CONTRACT:
        raise ReleaseError("manifest is not a Director Agent CLI release")
    if executor.get("repository_path") != (
        "release-manifests/tools/deploy_director_cli_locked_manifest.py"
    ):
        raise ReleaseError("Director Agent CLI executor path is invalid")
    if {item.get("runtime_path") for item in manifest["files"]} != _RUNTIME_FILES:
        raise ReleaseError("Director Agent CLI release must contain exactly two files")

    feature = manifest.get("feature_activation", {})
    if (feature.get("expected_preimage_enabled") is not True
            or feature.get("target_enabled") is not True
            or feature.get("disable_during_release") is not True):
        raise ReleaseError("Director Agent CLI feature lifecycle is invalid")

    acceptance = executor.get("authenticated_acceptance")
    request = acceptance.get("request") if isinstance(acceptance, dict) else None
    revision = request.get("page_revision") if isinstance(request, dict) else None
    if (not isinstance(revision, str)
            or _BASE._DIRECTOR_REVISION_PATTERN.fullmatch(revision) is None):
        raise ReleaseError(
            "Director Agent acceptance page_revision must match [a-f0-9]{8,32}"
        )
    policy = executor.get("rollback_health_policy")
    if not isinstance(policy, dict):
        raise ReleaseError("Director Agent rollback health policy is missing")
    timeout, interval = policy.get("timeout_seconds"), policy.get("interval_seconds")
    if (not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
            or timeout <= 0 or timeout > 120):
        raise ReleaseError("Director Agent rollback health timeout is invalid")
    if (not isinstance(interval, (int, float)) or isinstance(interval, bool)
            or interval <= 0 or interval > timeout):
        raise ReleaseError("Director Agent rollback health interval is invalid")

    supporting = executor.get("supporting_executor")
    if (not isinstance(supporting, dict)
            or supporting.get("repository_path")
            != "scripts/deploy_director_locked_manifest.py"
            or supporting.get("sha256") != _BASE_EXECUTOR_SHA256
            or supporting.get("git_blob") != _BASE_EXECUTOR_GIT_BLOB):
        raise ReleaseError("Director Agent supporting executor lock is invalid")

    dependency = executor.get("cli_dependency")
    if not isinstance(dependency, dict):
        raise ReleaseError("Director Agent CLI dependency lock is missing")
    if (dependency.get("runtime_root") != _RUNTIME_ROOT
            or dependency.get("repository_root") != "tools/hq-cli"):
        raise ReleaseError("Director Agent CLI root lock is invalid")
    if (not isinstance(dependency.get("version"), str)
            or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", dependency["version"])
            is None):
        raise ReleaseError("Director Agent CLI version lock is invalid")
    files = dependency.get("files")
    if (not isinstance(files, list) or len(files) != len(_CLI_FILES)
            or {item.get("path") for item in files if isinstance(item, dict)}
            != _CLI_FILES):
        raise ReleaseError("Director Agent CLI dependency file set is invalid")
    for item in files:
        if (not isinstance(item, dict)
                or re.fullmatch(r"[a-f0-9]{64}", str(item.get("sha256") or ""))
                is None
                or re.fullmatch(r"[a-f0-9]{40}", str(item.get("git_blob") or ""))
                is None):
            raise ReleaseError("Director Agent CLI dependency file lock is invalid")
    if set(dependency.get("forbidden_commands") or []) != _FORBIDDEN_COMMANDS:
        raise ReleaseError("Director Agent CLI forbidden command set is invalid")
    probes = dependency.get("probes")
    if not isinstance(probes, list):
        raise ReleaseError("Director Agent CLI probes are missing")
    actual = []
    for probe in probes:
        arguments = probe.get("arguments") if isinstance(probe, dict) else None
        if arguments == ["capabilities"]:
            actual.append(("capabilities", None))
        elif (isinstance(arguments, list) and len(arguments) == 2
              and arguments[0] == "describe" and arguments[1] in _CAPABILITIES):
            actual.append(("describe", arguments[1]))
        else:
            raise ReleaseError("Director Agent CLI probe command is not allowed")
    expected = [("capabilities", None)] + [
        ("describe", capability) for capability in sorted(_CAPABILITIES)
    ]
    if sorted(actual) != sorted(expected) or len(actual) != len(expected):
        raise ReleaseError("Director Agent CLI probes do not cover every page")


def _load_manifest(path):
    manifest = _BASE._load_manifest(path)
    _validate_contract(manifest)
    return manifest


class SystemHooks(_BASE.SystemHooks):
    def validate_cli(self, cli_root, probes, expected_version):
        cli_root = pathlib.Path(cli_root)
        source_root = cli_root / "src"
        run_module = (
            "import runpy,sys;"
            "sys.path.insert(0,sys.argv.pop(1));"
            "runpy.run_module('hq_cli',run_name='__main__')"
        )
        environment = {
            "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
            "PYTHONIOENCODING": "utf-8",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        }
        for name in ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP"):
            if os.environ.get(name):
                environment[name] = os.environ[name]
        for probe in probes:
            arguments = list(probe["arguments"])
            completed = subprocess.run(
                [sys.executable, "-I", "-X", "utf8", "-c", run_module,
                 str(source_root), *arguments, "--json"],
                cwd=str(cli_root), env=environment, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                encoding="utf-8", errors="replace", timeout=10, check=False,
            )
            if completed.returncode != 0:
                raise ReleaseError("Director Agent CLI preflight command failed")
            if (len(completed.stdout.encode("utf-8")) > 256 * 1024
                    or len(completed.stderr.encode("utf-8")) > 256 * 1024):
                raise ReleaseError("Director Agent CLI preflight output is too large")
            try:
                payload = json.loads(completed.stdout)
            except (TypeError, ValueError) as error:
                raise ReleaseError(
                    "Director Agent CLI preflight returned invalid JSON"
                ) from error
            if payload.get("cli_version") != expected_version:
                raise ReleaseError("Director Agent CLI version does not match lock")
            if arguments == ["capabilities"]:
                if payload.get("schema") != "hq.capabilities/v1":
                    raise ReleaseError("Director Agent CLI catalog schema is invalid")
                available = {
                    item.get("id") for item in payload.get("capabilities", [])
                    if isinstance(item, dict)
                }
                if not _CAPABILITIES.issubset(available):
                    raise ReleaseError("Director Agent CLI page capabilities are incomplete")
            else:
                capability = payload.get("capability")
                if (payload.get("schema") != "hq.describe/v1"
                        or not isinstance(capability, dict)
                        or capability.get("id") != arguments[1]):
                    raise ReleaseError("Director Agent CLI describe contract is invalid")


def _locked_repository_file(source_root, lock, label):
    path = (source_root / lock["repository_path"]).resolve()
    if source_root not in path.parents or not path.is_file() or path.is_symlink():
        raise ReleaseError("%s path is unsafe" % label)
    data = path.read_bytes()
    if (_BASE._sha256(data) != lock.get("sha256")
            or _BASE._git_blob(data) != lock.get("git_blob")):
        raise ReleaseError("%s lock does not match source" % label)
    return data


def _verify_cli_dependency(source_root, target_root, executor, hooks):
    dependency = executor["cli_dependency"]
    repository_root = (source_root / dependency["repository_root"]).resolve()
    runtime_root = _BASE._mapped_path(target_root, dependency["runtime_root"])
    if (source_root not in repository_root.parents
            or not repository_root.is_dir() or repository_root.is_symlink()):
        raise ReleaseError("Director Agent CLI repository root is unsafe")
    if not runtime_root.is_dir() or runtime_root.is_symlink():
        raise ReleaseError("Director Agent CLI runtime root is unsafe")
    audit = []
    for item in dependency["files"]:
        relative = pathlib.PurePosixPath(str(item["path"]))
        if relative.is_absolute() or ".." in relative.parts:
            raise ReleaseError("Director Agent CLI dependency path is unsafe")
        source = repository_root.joinpath(*relative.parts).resolve()
        runtime = runtime_root.joinpath(*relative.parts).resolve()
        if repository_root not in source.parents or runtime_root not in runtime.parents:
            raise ReleaseError("Director Agent CLI dependency path escapes root")
        if (not source.is_file() or source.is_symlink()
                or not runtime.is_file() or runtime.is_symlink()):
            raise ReleaseError("Director Agent CLI dependency file is unsafe")
        source_data, runtime_data = source.read_bytes(), runtime.read_bytes()
        if (_BASE._sha256(source_data) != item["sha256"]
                or _BASE._git_blob(source_data) != item["git_blob"]
                or _BASE._sha256(runtime_data) != item["sha256"]
                or _BASE._git_blob(runtime_data) != item["git_blob"]):
            raise ReleaseError("Director Agent CLI dependency lock mismatch")
        audit.append(dict(item))
    hooks.validate_cli(runtime_root, dependency["probes"], dependency["version"])
    return audit


def _execute(manifest, source_root, target_root, backup_root, *, hooks, replace,
             verify_repository, checkpoint, reviewed_head, merged_main):
    executor = manifest["release_executor"]
    if verify_repository:
        release_head = _BASE._verify_director_checkout(
            source_root, manifest, reviewed_head, merged_main,
        )
    else:
        release_head = merged_main or "test-double"
        reviewed_head = reviewed_head or "reviewed-test-double"

    executor_data = _locked_repository_file(source_root, executor, "release executor")
    supporting_data = _locked_repository_file(
        source_root, executor["supporting_executor"], "supporting executor",
    )
    _BASE._validate_director_sources(source_root, manifest, hooks)
    cli_audit = _verify_cli_dependency(source_root, target_root, executor, hooks)

    entries = []
    for item in manifest["files"]:
        source = (source_root / item["repository_path"]).resolve()
        if source_root not in source.parents:
            raise ReleaseError("repository path escapes source root")
        data = source.read_bytes()
        if (_BASE._sha256(data) != _BASE._locked_value(item, "postimage", "sha256")
                or _BASE._git_blob(data)
                != _BASE._locked_value(item, "postimage", "blob")):
            raise ReleaseError("candidate lock does not match source")
        target = _BASE._mapped_path(target_root, item["runtime_path"])
        state = item["target_preimage_state"]
        if state == "absent":
            if target.exists() or target.is_symlink():
                raise ReleaseError("expected absent runtime preimage")
            if not target.parent.is_dir() or target.parent.is_symlink():
                raise ReleaseError("new target parent is missing or unsafe")
            stat = target.parent.stat()
            mode, uid, gid = int(str(item.get("install_mode", "0644")), 8), stat.st_uid, stat.st_gid
        elif state == "file":
            if not target.is_file() or target.is_symlink():
                raise ReleaseError("expected regular runtime preimage")
            old = target.read_bytes()
            if (_BASE._sha256(old) != _BASE._locked_value(item, "preimage", "sha256")
                    or _BASE._git_blob(old)
                    != _BASE._locked_value(item, "preimage", "blob")):
                raise ReleaseError("runtime preimage lock mismatch")
            stat = target.stat()
            mode, uid, gid = stat.st_mode & 0o777, stat.st_uid, stat.st_gid
        else:
            raise ReleaseError("unsupported runtime preimage state")
        entries.append((item, source, target, mode, uid, gid))

    feature = manifest["feature_activation"]
    feature_db = _BASE._mapped_path(target_root, feature["database_path"])
    feature_snapshot = _BASE._capture_feature_row(feature_db, feature["feature"])
    if feature_snapshot.get("state") != "row" or feature_snapshot.get("enabled") != 1:
        raise ReleaseError("Director Agent feature preimage does not match manifest")
    service = manifest["target"]["service"]
    if not hooks.service_active(service):
        raise ReleaseError("target service is not active before release")

    backup_root.mkdir(parents=True, exist_ok=True)
    backup = pathlib.Path(tempfile.mkdtemp(
        prefix="director-agent-cli-%s-%s-" % (
            str(release_head)[:12], time.strftime("%Y%m%d%H%M%S"),
        ), dir=backup_root,
    ))
    os.chmod(backup, 0o700)
    backups = []
    audit = {
        "reviewed_head": reviewed_head, "merged_main": release_head,
        "executor_sha256": _BASE._sha256(executor_data),
        "executor_git_blob": _BASE._git_blob(executor_data),
        "supporting_executor_sha256": _BASE._sha256(supporting_data),
        "supporting_executor_git_blob": _BASE._git_blob(supporting_data),
        "feature_preimage": feature_snapshot, "cli_dependency": cli_audit,
        "files": [],
    }
    for index, (item, _, target, mode, uid, gid) in enumerate(entries):
        saved = None
        if item["target_preimage_state"] == "file":
            saved = backup / ("%02d-%s" % (index, target.name))
            shutil.copy2(target, saved)
            if os.name != "nt":
                os.chown(saved, uid, gid)
            saved_stat = saved.stat()
            if (_BASE._sha256(saved.read_bytes())
                    != _BASE._locked_value(item, "preimage", "sha256")
                    or (saved_stat.st_mode & 0o777) != mode
                    or (os.name != "nt" and (
                        saved_stat.st_uid != uid or saved_stat.st_gid != gid
                    ))):
                raise ReleaseError("runtime backup verification failed")
        backups.append(saved)
        audit["files"].append({
            "runtime_path": item["runtime_path"],
            "state": item["target_preimage_state"],
            "backup_file": saved.name if saved else None,
            "mode": mode, "uid": uid, "gid": gid,
            "preimage_sha256": _BASE._locked_value(item, "preimage", "sha256"),
            "postimage_sha256": _BASE._locked_value(item, "postimage", "sha256"),
        })
    _BASE._write_audit(backup / "audit.json", audit)
    checkpoint("after_backup")

    try:
        _BASE._set_feature_row(feature_db, feature["feature"], False, feature["actor"])
        checkpoint("after_deactivate")
        hooks.probe_feature(executor["health_url"], executor["health_feature_field"], False)
        checkpoint("after_health_disabled_preinstall")
        for index, (_, source, target, mode, uid, gid) in enumerate(entries):
            _BASE._atomic_install(source, target, mode, replace, uid, gid)
            checkpoint("after_replace_%d" % index)
        for item, _, target, _, _, _ in entries:
            if _BASE._sha256(target.read_bytes()) != _BASE._locked_value(
                    item, "postimage", "sha256"):
                raise ReleaseError("deployed postimage hash mismatch")
        _BASE._validate_director_sources(source_root, manifest, hooks)
        hooks.validate_import(
            _BASE._mapped_path(target_root, executor["runtime_python_root"]),
            executor["import_modules"],
        )
        checkpoint("after_compile")
        hooks.restart(service)
        checkpoint("after_restart")
        if not hooks.service_active(service):
            raise ReleaseError("target service is not active after restart")
        hooks.probe_feature(executor["health_url"], executor["health_feature_field"], False)
        checkpoint("after_health_disabled")
        _BASE._set_feature_row(feature_db, feature["feature"], True, feature["actor"])
        checkpoint("after_activate")
        hooks.probe_feature(executor["health_url"], executor["health_feature_field"], True)
        checkpoint("after_health_enabled")
        hooks.acceptance(executor["authenticated_acceptance"])
        checkpoint("after_acceptance")
        audit["status"] = "deployed"
        audit["feature_postimage"] = _BASE._capture_feature_row(
            feature_db, feature["feature"],
        )
        audit["final_files"] = [{
            "runtime_path": item["runtime_path"], "state": "file",
            "sha256": _BASE._sha256(target.read_bytes()),
        } for item, _, target, _, _, _ in entries]
        _BASE._write_audit(backup / "audit.json", audit)
        checkpoint("after_final_audit")
    except BaseException as forward_error:
        rollback_errors = []
        try:
            _BASE._restore_feature_row(feature_db, feature_snapshot)
        except BaseException as error:
            rollback_errors.append("feature:" + type(error).__name__)
        for (item, _, target, mode, uid, gid), saved in zip(entries, backups):
            try:
                if saved is None:
                    if target.is_symlink() or (target.exists() and not target.is_file()):
                        raise ReleaseError("created target became unsafe")
                    target.unlink(missing_ok=True)
                else:
                    _BASE._atomic_install(saved, target, mode, os.replace, uid, gid)
                if item["target_preimage_state"] == "file":
                    if _BASE._sha256(target.read_bytes()) != _BASE._locked_value(
                            item, "preimage", "sha256"):
                        raise ReleaseError("restored preimage hash mismatch")
                elif target.exists() or target.is_symlink():
                    raise ReleaseError("absent preimage was not restored")
            except BaseException as error:
                rollback_errors.append("file:" + type(error).__name__)
        try:
            hooks.restart(service)
            _BASE._wait_for_rollback_health(
                hooks, service, executor["health_url"],
                executor["rollback_health_policy"],
            )
            hooks.probe_feature(
                executor["health_url"], executor["health_feature_field"], True,
            )
        except BaseException as error:
            rollback_errors.append("service:" + type(error).__name__)
        audit["status"] = "rollback_failed" if rollback_errors else "rolled_back"
        audit["forward_error"] = type(forward_error).__name__
        audit["rollback_errors"] = rollback_errors
        try:
            audit["feature_final"] = _BASE._capture_feature_row(
                feature_db, feature["feature"],
            )
            audit["final_files"] = [{
                "runtime_path": item["runtime_path"],
                "state": "file" if target.is_file() and not target.is_symlink() else "absent",
                "sha256": (_BASE._sha256(target.read_bytes())
                           if target.is_file() and not target.is_symlink() else None),
            } for item, _, target, _, _, _ in entries]
            _BASE._write_audit(backup / "audit.json", audit)
        except BaseException as error:
            rollback_errors.append("audit:" + type(error).__name__)
        if rollback_errors:
            raise ReleaseError(
                "forward release failed and rollback failed: %s"
                % ",".join(rollback_errors)
            ) from forward_error
        raise
    return {"status": "deployed", "head": release_head, "backup": str(backup)}


def execute_locked_release(manifest_path, source_root, target_root, backup_root, *,
                           hooks=None, replace=os.replace, verify_repository=True,
                           checkpoint=None, reviewed_head=None, merged_main=None):
    manifest = _load_manifest(manifest_path)
    return _execute(
        manifest, pathlib.Path(source_root).resolve(), pathlib.Path(target_root).resolve(),
        pathlib.Path(backup_root).resolve(), hooks=hooks or SystemHooks(),
        replace=replace, verify_repository=verify_repository,
        checkpoint=checkpoint or (lambda name: None),
        reviewed_head=reviewed_head, merged_main=merged_main,
    )


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
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
