#!/usr/bin/env python3
"""Verify a server-behavior baseline import without reading server secrets."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath


FORBIDDEN_PARTS = {
    "__pycache__",
    "browser_data",
    "content_out",
    "data",
    "logs",
}
FORBIDDEN_SUFFIXES = {
    ".crt",
    ".db",
    ".env",
    ".key",
    ".log",
    ".pem",
    ".pyc",
    ".sqlite",
    ".sqlite3",
}
ALLOWED_TARGET_ROOTS = {"server", "site", "scripts"}


def load_manifest(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def validate_manifest(manifest: dict) -> list[str]:
    errors: list[str] = []
    records = manifest.get("synchronized_files")
    if not isinstance(records, list) or not records:
        return ["manifest must contain a non-empty synchronized_files list"]

    seen: set[str] = set()
    for index, record in enumerate(records):
        label = f"synchronized_files[{index}]"
        if not isinstance(record, dict):
            errors.append(f"{label} must be an object")
            continue
        source = str(record.get("source") or "")
        target = str(record.get("target") or "")
        digest = str(record.get("sha256") or "")
        target_path = PurePosixPath(target)
        source_path = PurePosixPath(source)

        if not source or source_path.is_absolute() or ".." in source_path.parts:
            errors.append(f"{label}.source is unsafe")
        if (
            not target
            or target_path.is_absolute()
            or ".." in target_path.parts
            or not target_path.parts
            or target_path.parts[0] not in ALLOWED_TARGET_ROOTS
        ):
            errors.append(f"{label}.target is unsafe")
        lower_parts = {part.lower() for part in source_path.parts + target_path.parts}
        if lower_parts & FORBIDDEN_PARTS:
            errors.append(f"{label} contains a forbidden runtime-data path")
        if source_path.suffix.lower() in FORBIDDEN_SUFFIXES:
            errors.append(f"{label}.source has a forbidden suffix")
        if target_path.suffix.lower() in FORBIDDEN_SUFFIXES:
            errors.append(f"{label}.target has a forbidden suffix")
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            errors.append(f"{label}.sha256 is invalid")
        if target in seen:
            errors.append(f"{label}.target is duplicated")
        seen.add(target)
    return errors


def verify_targets(repo_root: Path, manifest: dict) -> list[str]:
    errors = validate_manifest(manifest)
    if errors:
        return errors
    resolved_root = repo_root.resolve()
    for record in manifest["synchronized_files"]:
        relative = PurePosixPath(record["target"])
        target = (resolved_root / Path(*relative.parts)).resolve()
        try:
            target.relative_to(resolved_root)
        except ValueError:
            errors.append(f"target escapes repository: {relative}")
            continue
        if not target.is_file():
            errors.append(f"target is missing: {relative}")
            continue
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        if digest != record["sha256"]:
            errors.append(f"target hash differs: {relative}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "manifest",
        nargs="?",
        default="docs/baselines/test-production-behavior-20260729.json",
    )
    parser.add_argument("--repo-root", default=None)
    args = parser.parse_args()

    script_root = Path(__file__).resolve().parents[1]
    repo_root = Path(args.repo_root).resolve() if args.repo_root else script_root
    manifest_path = Path(args.manifest)
    if not manifest_path.is_absolute():
        manifest_path = repo_root / manifest_path

    errors = verify_targets(repo_root, load_manifest(manifest_path))
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print(
        "baseline import verified: "
        f"{len(load_manifest(manifest_path)['synchronized_files'])} files"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
