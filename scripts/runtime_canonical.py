#!/usr/bin/env python3
"""Build and verify a secret-free, content-addressed runtime release."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath


class GateError(RuntimeError):
    pass


def canonical_json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GateError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise GateError(f"expected JSON object: {path}")
    return value


def relative_parts(path: Path) -> tuple[str, ...]:
    return tuple(part.lower() for part in path.as_posix().split("/") if part not in ("", "."))


def exclusion_reason(relative: Path, scope: dict) -> str | None:
    parts = relative_parts(relative)
    excluded_segments = {item.lower() for item in scope["exclude_path_segments"]}
    if any(part in excluded_segments for part in parts):
        return "excluded_path_segment"
    name = relative.name.lower()
    if name in {item.lower() for item in scope["exclude_basenames"]}:
        return "excluded_basename"
    if any(name.endswith(suffix.lower()) for suffix in scope["exclude_suffixes"]):
        return "excluded_suffix"
    return None


def assert_safe_content(path: Path, patterns: list[re.Pattern[str]]) -> None:
    if path.stat().st_size > 8 * 1024 * 1024:
        return
    data = path.read_bytes()
    if b"\x00" in data:
        return
    text = data.decode("utf-8", errors="replace")
    for pattern in patterns:
        if pattern.search(text):
            raise GateError(f"forbidden credential-like content: {path}")


def inventory(source_root: Path, scope: dict, provenance: dict) -> dict:
    if not source_root.is_dir():
        raise GateError(f"source root does not exist: {source_root}")
    patterns = [re.compile(item) for item in scope["forbidden_content_patterns"]]
    allowed_suffixes = {item.lower() for item in scope["include_suffixes"]}
    files: list[dict] = []
    exclusions: list[dict] = []
    repository_prefixes = tuple(
        PurePosixPath(item).parts for item in scope.get("repository_include_prefixes", [])
    )

    for path in sorted(source_root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(source_root)
        if provenance.get("capture_kind") == "repository-candidate" and repository_prefixes:
            relative_posix_parts = PurePosixPath(relative.as_posix()).parts
            if not any(
                relative_posix_parts[:len(prefix)] == prefix
                for prefix in repository_prefixes
            ):
                continue
        reason = exclusion_reason(relative, scope)
        if reason:
            if path.is_file() or path.is_symlink():
                exclusions.append({"path": relative.as_posix(), "reason": reason})
            continue
        if path.is_symlink():
            raise GateError(f"symlink is forbidden in baseline: {relative.as_posix()}")
        if not path.is_file():
            continue
        if path.suffix.lower() not in allowed_suffixes:
            exclusions.append({"path": relative.as_posix(), "reason": "suffix_not_allowlisted"})
            continue
        assert_safe_content(path, patterns)
        mode = stat.S_IMODE(path.stat().st_mode)
        files.append({
            "path": relative.as_posix(),
            "sha256": sha256_file(path),
            "size": path.stat().st_size,
            "mode": f"{mode:04o}",
        })

    if not files:
        raise GateError("baseline contains no included files")
    payload = {
        "schema_version": 2,
        "provenance": provenance,
        "scope_sha256": hashlib.sha256(canonical_json(scope)).hexdigest(),
        "files": files,
        "excluded": exclusions,
    }
    payload["content_id"] = hashlib.sha256(canonical_json(payload)).hexdigest()
    return payload


def validate_manifest(manifest: dict, scope: dict, require_server_verified: bool) -> None:
    required = {"schema_version", "provenance", "scope_sha256", "files", "excluded", "content_id"}
    if set(manifest) != required:
        raise GateError(f"manifest keys must be exactly {sorted(required)}")
    if manifest["schema_version"] != 2:
        raise GateError("unsupported manifest schema")
    if manifest["scope_sha256"] != hashlib.sha256(canonical_json(scope)).hexdigest():
        raise GateError("scope hash mismatch")
    provenance = manifest["provenance"]
    if require_server_verified and provenance.get("capture_kind") != "server-read-only":
        raise GateError("server-verified capture is required")
    if provenance.get("server_role") != "test":
        raise GateError("manifest is not for the test server")
    if provenance.get("server_address") != scope["server_address"]:
        raise GateError("test server address mismatch")
    if provenance.get("production_accessed") is not False:
        raise GateError("production_accessed must be false")
    expected_id_payload = dict(manifest)
    content_id = expected_id_payload.pop("content_id")
    if hashlib.sha256(canonical_json(expected_id_payload)).hexdigest() != content_id:
        raise GateError("manifest content_id mismatch")
    paths: set[str] = set()
    for item in manifest["files"]:
        path = item.get("path", "")
        pure = PurePosixPath(path)
        if pure.is_absolute() or ".." in pure.parts or path in paths:
            raise GateError(f"unsafe or duplicate manifest path: {path}")
        if exclusion_reason(Path(path), scope):
            raise GateError(f"excluded path leaked into manifest: {path}")
        if not re.fullmatch(r"[0-9a-f]{64}", item.get("sha256", "")):
            raise GateError(f"invalid sha256: {path}")
        paths.add(path)
    if not paths:
        raise GateError("empty manifest")
    if [item["path"] for item in manifest["files"]] != sorted(paths):
        raise GateError("manifest files are not sorted")


def verify_tree(source_root: Path, manifest: dict, scope: dict) -> None:
    validate_manifest(manifest, scope, require_server_verified=False)
    actual = inventory(source_root, scope, manifest["provenance"])
    expected_files = manifest["files"]
    if actual["files"] != expected_files:
        expected = {item["path"]: item for item in expected_files}
        found = {item["path"]: item for item in actual["files"]}
        missing = sorted(expected.keys() - found.keys())
        extra = sorted(found.keys() - expected.keys())
        changed = sorted(path for path in expected.keys() & found.keys() if expected[path] != found[path])
        raise GateError(f"runtime drift: missing={missing}, extra={extra}, changed={changed}")


def build_release(source_root: Path, manifest: dict, scope: dict, releases_root: Path) -> Path:
    verify_tree(source_root, manifest, scope)
    content_id = manifest["content_id"]
    release = releases_root / content_id
    if release.exists():
        raise GateError(f"immutable release already exists: {release}")
    staging = releases_root / f".{content_id}.staging-{os.getpid()}"
    if staging.exists():
        raise GateError(f"staging path already exists: {staging}")
    staging.mkdir(parents=True)
    try:
        for item in manifest["files"]:
            source = source_root / Path(item["path"])
            target = staging / Path(item["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            os.chmod(target, int(item["mode"], 8) & 0o755)
        (staging / "MANIFEST.json").write_bytes(canonical_json(manifest))
        verify_tree(staging, manifest, scope)
        os.replace(staging, release)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return release


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scope", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    inv = sub.add_parser("inventory")
    inv.add_argument("--source", type=Path, required=True)
    inv.add_argument("--output", type=Path, required=True)
    inv.add_argument("--capture-kind", choices=["repository-candidate", "server-read-only"], required=True)
    inv.add_argument("--source-revision", required=True)
    val = sub.add_parser("validate-manifest")
    val.add_argument("--manifest", type=Path, required=True)
    val.add_argument("--require-server-verified", action="store_true")
    verify = sub.add_parser("verify")
    verify.add_argument("--source", type=Path, required=True)
    verify.add_argument("--manifest", type=Path, required=True)
    build = sub.add_parser("build-release")
    build.add_argument("--source", type=Path, required=True)
    build.add_argument("--manifest", type=Path, required=True)
    build.add_argument("--releases", type=Path, required=True)
    args = parser.parse_args()

    try:
        scope = load_json(args.scope)
        if args.command == "inventory":
            provenance = {
                "capture_kind": args.capture_kind,
                "captured_at_utc": datetime.now(timezone.utc).isoformat(),
                "server_role": "test",
                "server_address": scope["server_address"],
                "production_accessed": False,
                "source_revision": args.source_revision,
            }
            result = inventory(args.source, scope, provenance)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_bytes(canonical_json(result))
        elif args.command == "validate-manifest":
            validate_manifest(load_json(args.manifest), scope, args.require_server_verified)
        elif args.command == "verify":
            verify_tree(args.source, load_json(args.manifest), scope)
        elif args.command == "build-release":
            release = build_release(args.source, load_json(args.manifest), scope, args.releases)
            print(release)
    except GateError as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
