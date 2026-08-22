#!/usr/bin/env python3
"""Fail-closed deployment of a small, hash-locked runtime overlay.

The command is intentionally local to the target host and never opens SSH.
Every source/runtime hash and every candidate import is checked before the
first target replacement.  A manifest may require an authenticated, zero-cost
acceptance request; after the backup point, any failure restores every file and
feature row from the same snapshot.
"""

import argparse
import hashlib
import json
import os
import pathlib
import re
import secrets
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


_DIRECTOR_REVISION_PATTERN = re.compile(r"[a-f0-9]{8,32}")
_NODE_FALLBACK = "/home/ubuntu/.local/hq-node/bin/node"
_NODE_FALLBACK_ENVIRONMENT = "HQ_NODE_BINARY"
_PRECISION_CONTRACT = "digital_human_precision_director_v4"
_PRECISION_MANIFEST_PARTS = (
    "deploy", "test-runtime", "digital-human-precision-director-v4-20260822.json",
)


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


def _mapped_symlink_node(root, absolute_path):
    """Map a symlink node without following its final path component."""
    value = pathlib.PurePosixPath(str(absolute_path))
    if (not value.is_absolute() or ".." in value.parts
            or len(value.parts) < 2):
        raise ReleaseError("manifest runtime path must be absolute and normalized")
    root = pathlib.Path(root).resolve()
    parent = root.joinpath(*value.parts[1:-1]).resolve()
    if parent != root and root not in parent.parents:
        raise ReleaseError("manifest runtime path escapes target root")
    return parent / value.name


def _load_manifest(path):
    manifest_path = pathlib.Path(path)
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
    if executor.get("contract") != _PRECISION_CONTRACT:
        raise ReleaseError("Precision v4 executor rejects every other release contract")
    if tuple(manifest_path.parts[-3:]) != _PRECISION_MANIFEST_PARTS:
        raise ReleaseError("Precision v4 manifest must come from its locked source path")
    _validate_precision_contract(manifest)
    return manifest


def _validate_precision_contract(manifest):
    executor = manifest["release_executor"]
    expected = set(executor.get("required_repository_paths") or [])
    actual = {item.get("repository_path") for item in manifest["files"]}
    if not expected or actual != expected:
        raise ReleaseError("Precision v4 runtime inventory is incomplete")
    nginx = manifest.get("nginx_contract")
    if not isinstance(nginx, dict):
        raise ReleaseError("Precision v4 Yuelei Nginx contract is missing")
    if nginx.get("runtime_path") != "/etc/nginx/sites-available/yuelei-test.conf":
        raise ReleaseError("Precision v4 may only update the Yuelei test vhost")
    if nginx.get("enabled_runtime_path") != "/etc/nginx/sites-enabled/yuelei-test.conf":
        raise ReleaseError("Precision v4 Yuelei enabled-vhost path is invalid")
    if "huangquechuanmei" in str(nginx.get("runtime_path") or ""):
        raise ReleaseError("Precision v4 must not update the main-site vhost")
    if nginx.get("source_repository_path") != "deploy/nginx-huangquechuanmei.conf":
        raise ReleaseError("Precision v4 Nginx source path is invalid")
    if nginx.get("renderer_repository_path") != "deploy/render_yuelei_test_nginx.py":
        raise ReleaseError("Precision v4 Nginx renderer path is invalid")
    probe_paths = {
        probe.get("url") for probe in executor.get("unauthenticated_probes", [])
    }
    if ("https://yuelei.huangquechuanmei.com/api/gen/video/lipsync-import"
            not in probe_paths):
        raise ReleaseError("Precision v4 lipsync-import 401 probe is missing")
    policy = executor.get("rollback_health_policy")
    if not isinstance(policy, dict):
        raise ReleaseError("Precision v4 rollback health policy is missing")
    timeout, interval = policy.get("timeout_seconds"), policy.get("interval_seconds")
    if (not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
            or timeout <= 0 or timeout > 120):
        raise ReleaseError("Precision v4 rollback health timeout is invalid")
    if (not isinstance(interval, (int, float)) or isinstance(interval, bool)
            or interval <= 0 or interval > timeout):
        raise ReleaseError("Precision v4 rollback health interval is invalid")


def _validate_director_contract(manifest):
    executor = manifest.get("release_executor", {})
    if executor.get("contract") != "director_agent_seven_file_v2":
        return
    acceptance = executor.get("authenticated_acceptance")
    if not isinstance(acceptance, dict):
        raise ReleaseError("Director Agent acceptance contract is missing")
    request = acceptance.get("request")
    revision = request.get("page_revision") if isinstance(request, dict) else None
    if (not isinstance(revision, str)
            or _DIRECTOR_REVISION_PATTERN.fullmatch(revision) is None):
        raise ReleaseError(
            "Director Agent acceptance page_revision must match [a-f0-9]{8,32}"
        )
    policy = executor.get("rollback_health_policy")
    if not isinstance(policy, dict):
        raise ReleaseError("Director Agent rollback health policy is missing")
    timeout = policy.get("timeout_seconds")
    interval = policy.get("interval_seconds")
    if (not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
            or timeout <= 0 or timeout > 120):
        raise ReleaseError("Director Agent rollback health timeout is invalid")
    if (not isinstance(interval, (int, float)) or isinstance(interval, bool)
            or interval <= 0 or interval > timeout):
        raise ReleaseError("Director Agent rollback health interval is invalid")


def _resolve_node_binary(environment=None):
    environment = os.environ if environment is None else environment
    path_node = shutil.which("node", path=environment.get("PATH"))
    if path_node:
        return path_node
    configured = str(environment.get(_NODE_FALLBACK_ENVIRONMENT, "")).strip()
    candidates = []
    if configured:
        if not pathlib.PurePath(configured).is_absolute():
            raise ReleaseError(
                "%s must be an absolute path" % _NODE_FALLBACK_ENVIRONMENT
            )
        candidates.append(configured)
    candidates.append(_NODE_FALLBACK)
    for candidate in candidates:
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    checked = ", ".join(candidates)
    raise ReleaseError(
        "Node.js executable not found: PATH has no node; checked %s (%s)"
        % (_NODE_FALLBACK_ENVIRONMENT, checked)
    )


def _http_error_detail(data):
    text = bytes(data or b"").decode("utf-8", errors="replace").strip()
    if not text:
        return "no response body"
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return text[:500]
    if isinstance(payload, dict):
        nested = payload.get("error")
        values = nested if isinstance(nested, dict) else payload
        code = values.get("code") or values.get("error_code")
        message = values.get("message") or values.get("detail")
        if code and message:
            return "%s / %s" % (code, message)
        if code or message:
            return str(code or message)
    return text[:500]


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

    def validate_nginx(self):
        _run(["nginx", "-t"])

    def validate_nginx_candidate(self, candidate, reviewed_source):
        source = pathlib.Path(reviewed_source).read_text(encoding="utf-8")
        preamble = source.split("server {", 1)[0]
        candidate = pathlib.Path(candidate).resolve()
        with tempfile.TemporaryDirectory(prefix="hq-nginx-candidate-") as directory:
            directory = pathlib.Path(directory)
            wrapper = directory / "nginx.conf"
            wrapper.write_text(
                "pid %s;\nerror_log %s;\nevents {}\nhttp {\n%s\ninclude %s;\n}\n"
                % (directory / "nginx.pid", directory / "error.log",
                   preamble, candidate),
                encoding="utf-8",
            )
            _run(["nginx", "-t", "-c", str(wrapper)])

    def reload_nginx(self):
        _run(["systemctl", "reload", "nginx"])

    def link_state(self, path):
        path = pathlib.Path(path)
        if path.is_symlink():
            return {"state": "symlink", "target": os.readlink(path)}
        if path.exists():
            return {"state": "other", "target": None}
        return {"state": "absent", "target": None}

    def replace_symlink(self, path, target):
        path = pathlib.Path(path)
        state = self.link_state(path)
        if state["state"] == "other":
            raise ReleaseError("enabled-vhost path is not a symlink")
        temporary = path.with_name(
            ".%s.%s.tmp" % (path.name, secrets.token_hex(8))
        )
        try:
            os.symlink(target, temporary)
            os.replace(temporary, path)
        finally:
            if temporary.is_symlink() or temporary.exists():
                temporary.unlink()

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

    def probe_static(self, url, expected_status, expected_sha256):
        request = urllib.request.Request(url, method="GET")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=10) as response:
                status = response.status
                data = response.read()
        except urllib.error.HTTPError as error:
            status, data = error.code, error.read()
        if status != expected_status:
            raise ReleaseError(
                "static release probe returned HTTP %s, expected %s"
                % (status, expected_status)
            )
        if _sha256(data) != expected_sha256:
            raise ReleaseError("served static bytes do not match release source")

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
        _run([_resolve_node_binary(), "--check", str(path)])

    def acceptance(self, specification):
        token_name = specification["token_environment"]
        token = str(os.environ.get(token_name, "")).strip()
        if not token:
            raise ReleaseError("authenticated acceptance token is missing")
        key = "release-pr276-" + secrets.token_hex(16)
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
                            % (response.status, _http_error_detail(data))
                        )
                    return json.loads(data.decode("utf-8"))
            except urllib.error.HTTPError as error:
                raise ReleaseError(
                    "authenticated acceptance returned HTTP %s: %s"
                    % (error.code, _http_error_detail(error.read()))
                ) from error

        first = request_json(specification["submit_url"], "POST")
        replay = request_json(specification["submit_url"], "POST")
        if not first.get("job_id") or replay.get("job_id") != first["job_id"]:
            raise ReleaseError("same-key acceptance did not replay original job")
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
                if not isinstance(result, dict) or result.get("type") != "director_agent":
                    raise ReleaseError("Director Agent acceptance result is invalid")
                return
            if status in {"error", "failed"}:
                raise ReleaseError("Director Agent acceptance job failed")
            if status not in {"pending", "running"}:
                raise ReleaseError("Director Agent acceptance status is invalid")
            if time.monotonic() >= deadline:
                raise ReleaseError("Director Agent acceptance job timed out")
            time.sleep(1)


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


def _write_audit(path, payload):
    path = pathlib.Path(path)
    temporary = path.with_name(".%s.hq-audit-%s" % (path.name, os.getpid()))
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
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


def _wait_for_rollback_health(hooks, service, url, policy):
    timeout = float(policy["timeout_seconds"])
    interval = float(policy["interval_seconds"])
    deadline = time.monotonic() + timeout
    last_error = None
    while True:
        try:
            if not hooks.service_active(service):
                raise ReleaseError("restored service is not active")
            hooks.probe(url, "GET", 200)
            return
        except Exception as error:
            last_error = error
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ReleaseError(
                "rollback service health readiness timed out after %ss; "
                "last error: %s: %s"
                % (timeout, type(last_error).__name__, last_error)
            ) from last_error
        time.sleep(min(interval, remaining))


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


def _verify_precision_checkout(source_root, manifest, reviewed_head, merged_main):
    head = _verify_director_checkout(
        source_root, manifest, reviewed_head, merged_main,
    )
    code_source = str(manifest.get("source", {}).get("code_source_commit") or "")
    try:
        reviewed_parent = _run(["git", "rev-parse", reviewed_head + "^"], cwd=source_root)
    except subprocess.CalledProcessError as error:
        raise ReleaseError("reviewed Precision Head has no source parent") from error
    if code_source != reviewed_parent:
        raise ReleaseError(
            "Precision code source must be the exact parent of the reviewed Head"
        )
    metadata_paths = set(_run(
        ["git", "diff", "--name-only", code_source, reviewed_head], cwd=source_root,
    ).splitlines())
    allowed = {"deploy/test-runtime/digital-human-precision-director-v4-20260822.json"}
    if not metadata_paths or not metadata_paths.issubset(allowed):
        raise ReleaseError(
            "reviewed Precision Head may only finalize the locked manifest metadata"
        )
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
    if source_root not in executor_source.parents:
        raise ReleaseError("release executor path escapes source root")
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
        "executor_git_blob": _git_blob(executor_data),
        "feature_preimage": feature_snapshot, "files": [],
    }
    for index, (item, _, target, mode, uid, gid) in enumerate(entries):
        saved = None
        if item["target_preimage_state"] == "file":
            saved = backup / ("%02d-%s" % (index, target.name))
            shutil.copy2(target, saved)
            if os.name != "nt":
                os.chown(saved, uid, gid)
            saved_stat = saved.stat()
            if (_sha256(saved.read_bytes()) != _locked_value(
                    item, "preimage", "sha256")
                    or (saved_stat.st_mode & 0o777) != mode
                    or (os.name != "nt" and (
                        saved_stat.st_uid != uid or saved_stat.st_gid != gid
                    ))):
                raise ReleaseError("runtime backup verification failed")
        backups.append(saved)
        audit["files"].append({
            "runtime_path": item["runtime_path"],
            "state": item["target_preimage_state"],
            "backup_file": saved.name if saved else None, "mode": mode,
            "uid": uid, "gid": gid,
            "preimage_sha256": _locked_value(item, "preimage", "sha256"),
            "postimage_sha256": _locked_value(item, "postimage", "sha256"),
        })
    _write_audit(backup / "audit.json", audit)
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
            hooks.probe_static(
                probe["url"], probe.get("expected_status", 200),
                probe["expected_sha256"],
            )
        checkpoint("after_health_enabled")
        hooks.acceptance(executor["authenticated_acceptance"])
        checkpoint("after_acceptance")
        audit["status"] = "deployed"
        audit["feature_postimage"] = _capture_feature_row(
            feature_db, feature["feature"],
        )
        audit["final_files"] = [
            {
                "runtime_path": item["runtime_path"],
                "state": "file",
                "sha256": _sha256(target.read_bytes()),
            }
            for item, _, target, _, _, _ in entries
        ]
        _write_audit(backup / "audit.json", audit)
        checkpoint("after_final_audit")
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
            policy = executor.get("rollback_health_policy", {
                "timeout_seconds": 15, "interval_seconds": 1,
            })
            _wait_for_rollback_health(
                hooks, service, executor["health_url"], policy,
            )
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
            _write_audit(backup / "audit.json", audit)
        except BaseException as error:
            rollback_errors.append("audit:" + type(error).__name__)
        if rollback_errors:
            raise ReleaseError(
                "forward release failed and rollback failed: %s"
                % ",".join(rollback_errors)
            ) from forward_error
        raise

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


def _prepare_precision_nginx(
    manifest, source_root, target_root, backup_root, hooks,
):
    nginx = manifest["nginx_contract"]
    nginx_source = (source_root / nginx["source_repository_path"]).resolve()
    renderer_source = (source_root / nginx["renderer_repository_path"]).resolve()
    for path in (nginx_source, renderer_source):
        if source_root not in path.parents:
            raise ReleaseError("Nginx release source escapes source root")
    nginx_source_data = nginx_source.read_bytes()
    renderer_data = renderer_source.read_bytes()
    if (_sha256(nginx_source_data) != nginx["source_sha256"]
            or _git_blob(nginx_source_data) != nginx["source_blob"]):
        raise ReleaseError("reviewed main-site Nginx source lock mismatch")
    if (_sha256(renderer_data) != nginx["renderer_sha256"]
            or _git_blob(renderer_data) != nginx["renderer_blob"]):
        raise ReleaseError("Yuelei Nginx renderer lock mismatch")
    backup_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
            prefix="precision-nginx-candidate-", dir=backup_root) as candidate_dir:
        candidate = pathlib.Path(candidate_dir) / "yuelei-test.conf"
        _run([
            sys.executable, str(renderer_source),
            "--source", str(nginx_source), "--output", str(candidate),
        ], cwd=source_root)
        candidate_data = candidate.read_bytes()
        if (_sha256(candidate_data) != nginx["postimage_sha256"]
                or _git_blob(candidate_data) != nginx["postimage_blob"]):
            raise ReleaseError("rendered Yuelei Nginx candidate lock mismatch")
        text = candidate_data.decode("utf-8")
        required = (
            "server_name yuelei.huangquechuanmei.com;",
            "location = /api/gen/video/lipsync-import",
            "client_max_body_size 100m;",
        )
        if any(marker not in text for marker in required):
            raise ReleaseError("rendered Yuelei Nginx candidate is incomplete")
        if "server_name huangquechuanmei.com" in text:
            raise ReleaseError("rendered Yuelei Nginx candidate retained main-site names")
        hooks.validate_nginx_candidate(candidate, nginx_source)

    target = _mapped_path(target_root, nginx["runtime_path"])
    if not target.is_file() or target.is_symlink():
        raise ReleaseError("expected regular Yuelei Nginx preimage")
    preimage = target.read_bytes()
    if (_sha256(preimage) != nginx["preimage_sha256"]
            or _git_blob(preimage) != nginx["preimage_blob"]):
        raise ReleaseError("Yuelei Nginx preimage lock mismatch")
    target_stat = target.stat()
    item = {
        "repository_path": nginx["source_repository_path"],
        "runtime_path": nginx["runtime_path"],
        "target_preimage_state": "file",
        "preimage_sha256": nginx["preimage_sha256"],
        "preimage_blob": nginx["preimage_blob"],
        "postimage_sha256": nginx["postimage_sha256"],
        "postimage_blob": nginx["postimage_blob"],
    }
    enabled = _mapped_symlink_node(target_root, nginx["enabled_runtime_path"])
    enabled_actual = hooks.link_state(enabled)
    if (enabled_actual.get("state") != nginx["enabled_preimage_state"]
            or enabled_actual.get("target") != nginx.get("enabled_preimage_target")):
        raise ReleaseError("Yuelei enabled-vhost symlink preimage mismatch")
    return {
        "contract": nginx, "candidate_data": candidate_data,
        "entry": (item, None, target, target_stat.st_mode & 0o777,
                  target_stat.st_uid, target_stat.st_gid),
        "enabled_path": enabled,
    }


def _execute_precision_release(
    manifest, source_root, target_root, backup_root, *, hooks, replace,
    verify_repository, checkpoint, reviewed_head, merged_main,
):
    """Install the complete Precision Director overlay as one rollback unit."""
    executor = manifest["release_executor"]
    if verify_repository:
        release_head = _verify_precision_checkout(
            source_root, manifest, reviewed_head, merged_main,
        )
    else:
        release_head = merged_main or "test-double"
        reviewed_head = reviewed_head or "reviewed-test-double"

    executor_source = (source_root / executor["repository_path"]).resolve()
    if source_root not in executor_source.parents:
        raise ReleaseError("release executor path escapes source root")
    executor_data = executor_source.read_bytes()
    if (_sha256(executor_data) != executor.get("sha256")
            or _git_blob(executor_data) != executor.get("git_blob")):
        raise ReleaseError("release executor lock does not match source")

    nginx_release = _prepare_precision_nginx(
        manifest, source_root, target_root, backup_root, hooks,
    )
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

    entries.append(nginx_release["entry"])
    service = manifest["target"]["service"]
    if not hooks.service_active(service):
        raise ReleaseError("target service is not active before release")
    backup_root.mkdir(parents=True, exist_ok=True)
    backup = pathlib.Path(tempfile.mkdtemp(
        prefix="precision-director-%s-%s-" % (
            str(release_head)[:12], time.strftime("%Y%m%d%H%M%S"),
        ), dir=backup_root,
    ))
    os.chmod(backup, 0o700)
    backups = []
    audit = {
        "reviewed_head": reviewed_head, "merged_main": release_head,
        "executor_sha256": _sha256(executor_data),
        "executor_git_blob": _git_blob(executor_data), "files": [],
        "nginx_enabled_preimage": hooks.link_state(
            nginx_release["enabled_path"]
        ),
    }
    for index, (item, _, target, mode, uid, gid) in enumerate(entries):
        saved = None
        if item["target_preimage_state"] == "file":
            saved = backup / ("%02d-%s" % (index, target.name))
            shutil.copy2(target, saved)
            if os.name != "nt":
                os.chown(saved, uid, gid)
            if _sha256(saved.read_bytes()) != _locked_value(item, "preimage", "sha256"):
                raise ReleaseError("runtime backup verification failed")
        backups.append(saved)
        audit["files"].append({
            "runtime_path": item["runtime_path"],
            "state": item["target_preimage_state"],
            "backup_file": saved.name if saved else None,
            "preimage_sha256": _locked_value(item, "preimage", "sha256"),
            "postimage_sha256": _locked_value(item, "postimage", "sha256"),
        })
    _write_audit(backup / "audit.json", audit)
    candidate_source = backup / "yuelei-test.conf.candidate"
    candidate_source.write_bytes(nginx_release["candidate_data"])
    os.chmod(candidate_source, 0o600)
    entries = [
        (item, candidate_source if source is None else source,
         target, mode, uid, gid)
        for item, source, target, mode, uid, gid in entries
    ]
    nginx = nginx_release["contract"]
    enabled_target = nginx_release["enabled_path"]
    enabled_preimage_state = nginx["enabled_preimage_state"]
    enabled_preimage_target = nginx.get("enabled_preimage_target")
    checkpoint("after_backup")

    try:
        for index, (item, source, target, mode, uid, gid) in enumerate(entries):
            _atomic_install(source, target, mode, replace, uid, gid)
            checkpoint("after_replace_%d" % index)
        for item, _, target, _, _, _ in entries:
            if _sha256(target.read_bytes()) != _locked_value(item, "postimage", "sha256"):
                raise ReleaseError("deployed postimage hash mismatch")
        _validate_director_sources(source_root, manifest, hooks)
        hooks.validate_import(
            _mapped_path(target_root, executor["runtime_python_root"]),
            executor["import_modules"],
        )
        hooks.validate_nginx()
        checkpoint("after_compile")
        if nginx["enabled_postimage_state"] == "symlink":
            post_target = nginx["enabled_postimage_target"]
            current_link = hooks.link_state(enabled_target)
            if current_link.get("state") == "symlink":
                if current_link.get("target") != post_target:
                    raise ReleaseError("Yuelei enabled-vhost symlink postimage mismatch")
            elif current_link.get("state") != "absent":
                raise ReleaseError("Yuelei enabled-vhost postimage became unsafe")
            else:
                hooks.replace_symlink(enabled_target, post_target)
        else:
            raise ReleaseError("unsupported Yuelei enabled-vhost postimage state")
        hooks.restart(service)
        hooks.reload_nginx()
        checkpoint("after_restart")
        if not hooks.service_active(service):
            raise ReleaseError("target service is not active after restart")
        hooks.probe(executor["health_url"], "GET", 200)
        for probe in executor.get("static_probes", []):
            hooks.probe_static(
                probe["url"], probe.get("expected_status", 200),
                probe["expected_sha256"],
            )
        for probe in executor.get("unauthenticated_probes", []):
            hooks.probe(
                probe["url"], probe.get("method", "GET"),
                probe["expected_status"],
            )
        checkpoint("after_health")
        audit["status"] = "deployed"
        audit["nginx_enabled_postimage"] = hooks.link_state(enabled_target)
        audit["final_files"] = [
            {"runtime_path": item["runtime_path"], "state": "file",
             "sha256": _sha256(target.read_bytes())}
            for item, _, target, _, _, _ in entries
        ]
        _write_audit(backup / "audit.json", audit)
    except BaseException as forward_error:
        rollback_errors = []
        try:
            current_link = hooks.link_state(enabled_target)
            if current_link.get("state") == "other":
                raise ReleaseError("Yuelei enabled-vhost rollback target is unsafe")
            if enabled_preimage_state == "symlink":
                hooks.replace_symlink(enabled_target, enabled_preimage_target)
            elif current_link.get("state") == "symlink":
                enabled_target.unlink()
        except BaseException as error:
            rollback_errors.append("nginx-link:" + type(error).__name__)
        for (item, _, target, mode, uid, gid), saved in zip(entries, backups):
            try:
                if saved is None:
                    if target.is_symlink() or (target.exists() and not target.is_file()):
                        raise ReleaseError("created target became unsafe")
                    target.unlink(missing_ok=True)
                else:
                    _atomic_install(saved, target, mode, os.replace, uid, gid)
                if item["target_preimage_state"] == "file":
                    if _sha256(target.read_bytes()) != _locked_value(item, "preimage", "sha256"):
                        raise ReleaseError("restored preimage hash mismatch")
                elif target.exists() or target.is_symlink():
                    raise ReleaseError("absent preimage was not restored")
            except BaseException as error:
                rollback_errors.append("file:" + type(error).__name__)
        try:
            hooks.validate_nginx()
            hooks.restart(service)
            hooks.reload_nginx()
            _wait_for_rollback_health(
                hooks, service, executor["health_url"],
                executor["rollback_health_policy"],
            )
        except BaseException as error:
            rollback_errors.append("service:" + type(error).__name__)
        audit["status"] = "rollback_failed" if rollback_errors else "rolled_back"
        audit["forward_error"] = type(forward_error).__name__
        audit["rollback_errors"] = rollback_errors
        audit["nginx_enabled_final"] = hooks.link_state(enabled_target)
        try:
            _write_audit(backup / "audit.json", audit)
        except BaseException as error:
            rollback_errors.append("audit:" + type(error).__name__)
        if rollback_errors:
            raise ReleaseError(
                "forward release failed and rollback failed: %s"
                % ",".join(rollback_errors)
            ) from forward_error
        raise
    return {"status": "deployed", "head": release_head, "backup": str(backup)}


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

    if manifest["release_executor"].get("contract") == _PRECISION_CONTRACT:
        return _execute_precision_release(
            manifest, source_root, target_root, backup_root,
            hooks=hooks, replace=replace,
            verify_repository=verify_repository, checkpoint=checkpoint,
            reviewed_head=reviewed_head, merged_main=merged_main,
        )

    if manifest["release_executor"].get("contract") in {
        "director_agent_seven_file_v1", "director_agent_seven_file_v2",
    }:
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
