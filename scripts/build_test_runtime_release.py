#!/usr/bin/env python3
"""Build or verify the immutable test-runtime code tree from its manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path, PurePosixPath


REQUIRED_PREFIXES = ("server/", "site/")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_head(repo_root: Path) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        text=True,
    ).strip()


def validate_git_path(value: str) -> Path:
    posix = PurePosixPath(value)
    if (
        posix.is_absolute()
        or ".." in posix.parts
        or not value.startswith(REQUIRED_PREFIXES)
    ):
        raise ValueError(f"unsafe or unsupported git_path: {value}")
    return Path(*posix.parts)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--repo-root",
        default=Path(__file__).resolve().parents[1],
        type=Path,
    )
    parser.add_argument(
        "--manifest",
        default="deploy/runtime-manifest.json",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()

    repo_root = args.repo_root.resolve()
    manifest_path = (repo_root / args.manifest).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    if manifest.get("server_role") != "test-only":
        raise ValueError("runtime manifest is not marked test-only")
    if manifest.get("production_connected") is not False:
        raise ValueError("runtime manifest must state production_connected=false")
    if manifest.get("sensitive_files_not_written"):
        raise ValueError("manifest contains unresolved sensitive-file exclusions")
    if manifest.get("git_path_collisions"):
        raise ValueError("manifest contains unresolved git path collisions")

    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("runtime manifest has no files")

    output = args.output.resolve() if args.output else None
    if not args.verify_only:
        if output is None:
            raise ValueError("--output is required unless --verify-only is used")
        if output.exists() and any(output.iterdir()):
            raise ValueError(f"output directory is not empty: {output}")
        output.mkdir(parents=True, exist_ok=True)

    copied = []
    seen = set()
    for entry in files:
        git_path = entry["git_path"]
        relative = validate_git_path(git_path)
        if git_path in seen:
            raise ValueError(f"duplicate git_path: {git_path}")
        seen.add(git_path)
        source = repo_root / relative
        if not source.is_file():
            raise FileNotFoundError(f"manifest file missing: {git_path}")
        actual = sha256_file(source)
        expected = entry["sha256"]
        if actual != expected:
            raise ValueError(
                f"hash mismatch for {git_path}: expected {expected}, got {actual}"
            )
        if output is not None and not args.verify_only:
            destination = output / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            if sha256_file(destination) != expected:
                raise ValueError(f"post-copy hash mismatch: {git_path}")
        copied.append({"git_path": git_path, "sha256": expected})

    result = {
        "schema_version": 1,
        "server_role": "test-only",
        "source_commit": git_head(repo_root),
        "source_tree": subprocess.check_output(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD^{tree}"],
            text=True,
        ).strip(),
        "runtime_manifest": args.manifest,
        "file_count": len(copied),
        "files": copied,
        "external_state_required": manifest["excluded_external_state"],
    }
    if output is not None and not args.verify_only:
        (output / "release-manifest.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
