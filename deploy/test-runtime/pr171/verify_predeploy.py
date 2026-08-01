#!/usr/bin/env python3
"""Fail-closed source and target preimage verification for the PR171 overlay."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys


OVERLAY_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = OVERLAY_DIR.parents[3]
MANIFEST_PATH = OVERLAY_DIR / "manifest.json"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _git_blob(data: bytes) -> str:
    header = f"blob {len(data)}\0".encode("ascii")
    return hashlib.sha1(header + data).hexdigest()


def _target_path(target_root: pathlib.Path, absolute_target: str) -> pathlib.Path:
    target = pathlib.PurePosixPath(absolute_target)
    if not target.is_absolute() or ".." in target.parts:
        raise ValueError(f"invalid absolute deployment target: {absolute_target}")
    return target_root.joinpath(*target.parts[1:])


def verify_predeploy(
    manifest_path: pathlib.Path = MANIFEST_PATH,
    repo_root: pathlib.Path = REPO_ROOT,
    target_root: pathlib.Path = pathlib.Path("/"),
) -> list[str]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    errors: list[str] = []
    entries = manifest.get("deploy_files")
    if not isinstance(entries, list) or not entries:
        return ["manifest deploy_files must be a non-empty list"]

    for index, entry in enumerate(entries):
        label = f"deploy_files[{index}]"
        required = (
            "source",
            "target",
            "sha256",
            "git_blob",
            "expected_target_sha256",
            "expected_target_git_blob",
        )
        missing = [key for key in required if not entry.get(key)]
        if missing:
            errors.append(f"{label} missing required fields: {', '.join(missing)}")
            continue

        source_relative = pathlib.PurePosixPath(entry["source"])
        if source_relative.is_absolute() or ".." in source_relative.parts:
            errors.append(f"{label}: invalid repository source: {entry['source']}")
            continue
        source = repo_root.joinpath(*source_relative.parts)
        try:
            target = _target_path(target_root, entry["target"])
        except ValueError as exc:
            errors.append(f"{label}: {exc}")
            continue

        for kind, path, expected_sha, expected_blob in (
            ("source", source, entry["sha256"], entry["git_blob"]),
            (
                "target preimage",
                target,
                entry["expected_target_sha256"],
                entry["expected_target_git_blob"],
            ),
        ):
            if not path.is_file():
                errors.append(f"{label} {kind} missing: {path}")
                continue
            data = path.read_bytes()
            actual_sha = _sha256(data)
            actual_blob = _git_blob(data)
            if actual_sha != expected_sha:
                errors.append(
                    f"{label} {kind} sha256 mismatch: expected {expected_sha}, got {actual_sha}"
                )
            if actual_blob != expected_blob:
                errors.append(
                    f"{label} {kind} git blob mismatch: expected {expected_blob}, got {actual_blob}"
                )
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=pathlib.Path, default=MANIFEST_PATH)
    parser.add_argument("--repo-root", type=pathlib.Path, default=REPO_ROOT)
    parser.add_argument("--target-root", type=pathlib.Path, default=pathlib.Path("/"))
    args = parser.parse_args(argv)
    errors = verify_predeploy(args.manifest, args.repo_root, args.target_root)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("PR171 overlay source and target preimage verification passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
