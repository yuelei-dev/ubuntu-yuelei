#!/usr/bin/env python3
"""Capture, verify and build content-addressed Huangque runtime releases."""

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


TEXT_SUFFIXES = {
    ".conf", ".css", ".html", ".js", ".json", ".md", ".mjs", ".py",
    ".service", ".sh", ".svg", ".txt", ".xml", ".yaml", ".yml",
}
FILE_KEYS = {"source_path", "path", "sha256", "size", "mode"}


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


def relative_parts(path: Path | PurePosixPath) -> tuple[str, ...]:
    return tuple(part.lower() for part in path.as_posix().split("/") if part not in ("", "."))


def exclusion_reason(relative: Path | PurePosixPath, scope: dict) -> str | None:
    parts = relative_parts(relative)
    excluded_segments = {item.lower() for item in scope["exclude_path_segments"]}
    if any(part in excluded_segments for part in parts):
        return "excluded_path_segment"
    name = relative.name.lower()
    if name in {item.lower() for item in scope["exclude_basenames"]}:
        return "excluded_basename"
    if any(re.fullmatch(pattern, relative.name) for pattern in scope["exclude_name_patterns"]):
        return "excluded_backup_artifact"
    if any(name.endswith(suffix.lower()) for suffix in scope["exclude_suffixes"]):
        return "excluded_suffix"
    return None


def assert_safe_content(path: Path, scope: dict, patterns: list[re.Pattern[str]]) -> None:
    if path.suffix.lower() not in TEXT_SUFFIXES:
        return
    size = path.stat().st_size
    max_text_bytes = int(scope["max_text_file_bytes"])
    if size > max_text_bytes:
        raise GateError(f"text file exceeds scan limit ({max_text_bytes} bytes): {path}")
    decoder_buffer = b""
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            if b"\x00" in chunk:
                raise GateError(f"NUL byte in allowlisted text file: {path}")
            data = decoder_buffer + chunk
            text = data.decode("utf-8", errors="replace")
            decoder_buffer = data[-256:]
            for pattern in patterns:
                if pattern.search(text):
                    raise GateError(f"forbidden credential-like content: {path}")


def is_allowed_systemd_path(relative: PurePosixPath, allowlist: set[str]) -> bool:
    if len(relative.parts) == 1:
        return relative.name in allowlist
    return len(relative.parts) == 2 and relative.parts[0].endswith(".d") and (
        relative.parts[0][:-2] in allowlist
    )


def add_tree(
    *,
    physical_root: Path,
    source_prefix: PurePosixPath,
    target_prefix: PurePosixPath,
    kind: str,
    scope: dict,
    provenance: dict,
    patterns: list[re.Pattern[str]],
    mode_map: dict[str, str] | None,
    files: list[dict],
    exclusions: list[dict],
) -> dict:
    if not physical_root.is_dir():
        raise GateError(f"required runtime root missing: /{source_prefix.as_posix()}")
    allowed_suffixes = {item.lower() for item in scope["include_suffixes"]}
    systemd_allowlist = set(scope["systemd_unit_allowlist"])
    included_bytes = 0
    included_count = 0

    for physical_path in sorted(physical_root.rglob("*"), key=lambda item: item.as_posix()):
        relative = PurePosixPath(physical_path.relative_to(physical_root).as_posix())
        source_path = source_prefix / relative
        target_path = target_prefix / relative
        if kind == "startup-config" and source_prefix.as_posix() == "etc/systemd/system":
            if not is_allowed_systemd_path(relative, systemd_allowlist):
                if physical_path.is_file() or physical_path.is_symlink():
                    exclusions.append({
                        "source_path": source_path.as_posix(),
                        "reason": "systemd_unit_not_allowlisted",
                    })
                continue
        reason = exclusion_reason(source_path, scope)
        if reason:
            if physical_path.is_file() or physical_path.is_symlink():
                exclusions.append({"source_path": source_path.as_posix(), "reason": reason})
            continue
        if physical_path.is_symlink():
            raise GateError(f"symlink is forbidden in baseline: {source_path.as_posix()}")
        if not physical_path.is_file():
            continue
        if physical_path.suffix.lower() not in allowed_suffixes:
            exclusions.append({
                "source_path": source_path.as_posix(),
                "reason": "suffix_not_allowlisted",
            })
            continue
        assert_safe_content(physical_path, scope, patterns)
        if provenance["capture_kind"] == "repository-candidate":
            mode = 0o644
        elif mode_map is not None:
            mapped_mode = mode_map.get(source_path.as_posix())
            if mapped_mode is None or not re.fullmatch(r"0[0-7]{3}", mapped_mode):
                raise GateError(f"missing or invalid captured mode: {source_path.as_posix()}")
            mode = int(mapped_mode, 8)
        else:
            mode = stat.S_IMODE(physical_path.stat().st_mode)
        size = physical_path.stat().st_size
        files.append({
            "source_path": source_path.as_posix(),
            "path": target_path.as_posix(),
            "sha256": sha256_file(physical_path),
            "size": size,
            "mode": f"{mode:04o}",
        })
        included_count += 1
        included_bytes += size
    return {
        "source": f"/{source_prefix.as_posix()}",
        "target": target_prefix.as_posix(),
        "kind": kind,
        "included_file_count": included_count,
        "included_bytes": included_bytes,
    }


def inventory(
    source_root: Path,
    scope: dict,
    provenance: dict,
    mode_map: dict[str, str] | None = None,
) -> dict:
    if not source_root.is_dir():
        raise GateError(f"source root does not exist: {source_root}")
    patterns = [re.compile(item) for item in scope["forbidden_content_patterns"]]
    files: list[dict] = []
    exclusions: list[dict] = []
    roots: list[dict] = []

    if provenance["capture_kind"] == "repository-candidate":
        for prefix_text in scope["repository_include_prefixes"]:
            prefix = PurePosixPath(prefix_text)
            physical = source_root.joinpath(*prefix.parts)
            if physical.is_file():
                reason = exclusion_reason(prefix, scope)
                if reason:
                    raise GateError(f"required repository runtime file is excluded: {prefix}")
                if physical.suffix.lower() not in {
                    item.lower() for item in scope["include_suffixes"]
                }:
                    raise GateError(f"required repository runtime suffix is not allowed: {prefix}")
                assert_safe_content(physical, scope, patterns)
                size = physical.stat().st_size
                files.append({
                    "source_path": prefix.as_posix(),
                    "path": prefix.as_posix(),
                    "sha256": sha256_file(physical),
                    "size": size,
                    "mode": "0644",
                })
                roots.append({
                    "source": f"/{prefix.as_posix()}",
                    "target": prefix.as_posix(),
                    "kind": "repository-runtime",
                    "included_file_count": 1,
                    "included_bytes": size,
                })
            else:
                roots.append(add_tree(
                    physical_root=physical,
                    source_prefix=prefix,
                    target_prefix=prefix,
                    kind="repository-runtime",
                    scope=scope,
                    provenance=provenance,
                    patterns=patterns,
                    mode_map=mode_map,
                    files=files,
                    exclusions=exclusions,
                ))
    elif provenance["capture_kind"] == "server-read-only":
        for root in scope["roots"]:
            source_prefix = PurePosixPath(root["source"].lstrip("/"))
            target_prefix = PurePosixPath(root["target"])
            roots.append(add_tree(
                physical_root=source_root.joinpath(*source_prefix.parts),
                source_prefix=source_prefix,
                target_prefix=target_prefix,
                kind=root["kind"],
                scope=scope,
                provenance=provenance,
                patterns=patterns,
                mode_map=mode_map,
                files=files,
                exclusions=exclusions,
            ))
    else:
        raise GateError(f"unsupported capture kind: {provenance['capture_kind']}")

    files.sort(key=lambda item: item["path"])
    exclusions.sort(key=lambda item: (item["source_path"], item["reason"]))
    paths = [item["path"] for item in files]
    if not files:
        raise GateError("baseline contains no included files")
    if len(paths) != len(set(paths)):
        raise GateError("server-to-release path mapping collision")
    payload = {
        "schema_version": 3,
        "provenance": provenance,
        "scope_sha256": hashlib.sha256(canonical_json(scope)).hexdigest(),
        "roots": roots,
        "files": files,
        "excluded": exclusions,
    }
    payload["content_id"] = hashlib.sha256(canonical_json(payload)).hexdigest()
    return payload


def validate_manifest(manifest: dict, scope: dict, require_server_verified: bool) -> None:
    required = {
        "schema_version", "provenance", "scope_sha256", "roots",
        "files", "excluded", "content_id",
    }
    if set(manifest) != required:
        raise GateError(f"manifest keys must be exactly {sorted(required)}")
    if manifest["schema_version"] != 3:
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
    if provenance.get("server_address") == scope["production_address"]:
        raise GateError("production server manifest is forbidden")
    if provenance.get("production_accessed") is not False:
        raise GateError("production_accessed must be false")
    expected_id_payload = dict(manifest)
    content_id = expected_id_payload.pop("content_id")
    if not isinstance(content_id, str) or (
        hashlib.sha256(canonical_json(expected_id_payload)).hexdigest() != content_id
    ):
        raise GateError("manifest content_id mismatch")
    paths: set[str] = set()
    for item in manifest["files"]:
        if not isinstance(item, dict) or set(item) != FILE_KEYS:
            raise GateError("invalid file entry keys")
        path = item["path"]
        source_path = item["source_path"]
        if not isinstance(path, str) or not isinstance(source_path, str):
            raise GateError("manifest paths must be strings")
        pure = PurePosixPath(path)
        source_pure = PurePosixPath(source_path)
        if (
            pure.is_absolute() or source_pure.is_absolute()
            or ".." in pure.parts or ".." in source_pure.parts or path in paths
        ):
            raise GateError(f"unsafe or duplicate manifest path: {path}")
        if exclusion_reason(source_pure, scope):
            raise GateError(f"excluded path leaked into manifest: {source_path}")
        if not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]):
            raise GateError(f"invalid sha256: {path}")
        if not isinstance(item["size"], int) or item["size"] < 0:
            raise GateError(f"invalid size: {path}")
        if not isinstance(item["mode"], str) or not re.fullmatch(r"0[0-7]{3}", item["mode"]):
            raise GateError(f"invalid mode: {path}")
        paths.add(path)
    if not paths:
        raise GateError("empty manifest")
    if [item["path"] for item in manifest["files"]] != sorted(paths):
        raise GateError("manifest files are not sorted")


def verify_tree(source_root: Path, manifest: dict, scope: dict) -> None:
    validate_manifest(manifest, scope, require_server_verified=False)
    captured_modes = None
    if manifest["provenance"]["capture_kind"] == "server-read-only":
        captured_modes = {item["source_path"]: item["mode"] for item in manifest["files"]}
    actual = inventory(
        source_root,
        scope,
        manifest["provenance"],
        mode_map=captured_modes,
    )
    for key in ("roots", "files", "excluded"):
        if actual[key] != manifest[key]:
            raise GateError(f"runtime drift in {key}")


def verify_release(
    release: Path,
    manifest: dict,
    scope: dict,
    require_server_verified: bool,
    require_matching_directory: bool = True,
    expect_read_only: bool = True,
) -> None:
    validate_manifest(manifest, scope, require_server_verified)
    if require_matching_directory and release.name != manifest["content_id"]:
        raise GateError("release directory does not match manifest content_id")
    internal_manifest = release / "MANIFEST.json"
    if not internal_manifest.is_file() or internal_manifest.read_bytes() != canonical_json(manifest):
        raise GateError("release MANIFEST.json does not match verified manifest")
    expected = {item["path"]: item for item in manifest["files"]}
    found: set[str] = set()
    for path in sorted(release.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(release).as_posix()
        if relative == "MANIFEST.json":
            continue
        if path.is_symlink():
            raise GateError(f"symlink in release: {relative}")
        if path.is_dir():
            continue
        if not path.is_file() or relative not in expected:
            raise GateError(f"unexpected release entry: {relative}")
        item = expected[relative]
        if path.stat().st_size != item["size"] or sha256_file(path) != item["sha256"]:
            raise GateError(f"release file mismatch: {relative}")
        mode = stat.S_IMODE(path.stat().st_mode)
        expected_mode = int(item["mode"], 8)
        if expect_read_only:
            expected_mode &= ~0o222
        if os.name == "posix" and mode != expected_mode:
            raise GateError(f"release mode mismatch: {relative}")
        found.add(relative)
    missing = sorted(set(expected) - found)
    if missing:
        raise GateError(f"release files missing: {missing}")


def make_read_only(root: Path) -> None:
    if os.name != "posix":
        return
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_file():
            path.chmod(stat.S_IMODE(path.stat().st_mode) & ~0o222)
        elif path.is_dir():
            path.chmod(0o555)
    root.chmod(0o555)


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
            source = source_root.joinpath(*PurePosixPath(item["source_path"]).parts)
            target = staging.joinpath(*PurePosixPath(item["path"]).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            os.chmod(target, int(item["mode"], 8))
        (staging / "MANIFEST.json").write_bytes(canonical_json(manifest))
        verify_release(
            staging,
            manifest,
            scope,
            require_server_verified=False,
            require_matching_directory=False,
            expect_read_only=False,
        )
        os.replace(staging, release)
        make_read_only(release)
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
    inv.add_argument("--mode-map", type=Path)
    val = sub.add_parser("validate-manifest")
    val.add_argument("--manifest", type=Path, required=True)
    val.add_argument("--require-server-verified", action="store_true")
    verify = sub.add_parser("verify")
    verify.add_argument("--source", type=Path, required=True)
    verify.add_argument("--manifest", type=Path, required=True)
    verify_release_parser = sub.add_parser("verify-release")
    verify_release_parser.add_argument("--release", type=Path, required=True)
    verify_release_parser.add_argument("--manifest", type=Path)
    verify_release_parser.add_argument("--require-server-verified", action="store_true")
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
            mode_map = None
            if args.mode_map:
                mode_map = load_json(args.mode_map)
            if args.capture_kind == "server-read-only" and mode_map is None:
                raise GateError("server-read-only inventory requires --mode-map")
            result = inventory(args.source, scope, provenance, mode_map=mode_map)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_bytes(canonical_json(result))
        elif args.command == "validate-manifest":
            validate_manifest(load_json(args.manifest), scope, args.require_server_verified)
        elif args.command == "verify":
            verify_tree(args.source, load_json(args.manifest), scope)
        elif args.command == "verify-release":
            manifest_path = args.manifest or (args.release / "MANIFEST.json")
            verify_release(
                args.release, load_json(manifest_path), scope, args.require_server_verified
            )
        elif args.command == "build-release":
            release = build_release(args.source, load_json(args.manifest), scope, args.releases)
            print(release)
    except (GateError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
