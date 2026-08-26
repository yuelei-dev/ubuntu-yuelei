#!/usr/bin/env python3
"""Read and stage media from the fixed Huangque test-server library."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys

SSH_TARGET = "ubuntu@8.148.158.106"
LIBRARY_ROOT = Path("/home/ubuntu/material-libraries/huangque-media")
ALLOWED_EXTENSIONS = {
    ".mp4", ".mov", ".m4v", ".webm",
    ".jpg", ".jpeg", ".png", ".webp",
    ".mp3", ".m4a", ".wav", ".aac", ".flac", ".ogg",
}
DEFAULT_SSH_KEY = Path(r"E:\AI\配置\SSH\huangque-test-material-readonly-v2_ed25519")
DEFAULT_KNOWN_HOSTS = Path(r"E:\AI\配置\SSH\known_hosts")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    list_parser = sub.add_parser("list", help="List supported material without copying it")
    list_parser.add_argument("--limit", type=int, default=300)
    fetch_parser = sub.add_parser("fetch", help="Copy selected material into a task workspace")
    fetch_parser.add_argument("--output-dir", type=Path, required=True)
    fetch_parser.add_argument("--path", action="append", required=True, dest="paths")
    return parser.parse_args()


def validate_relative_path(value: str) -> PurePosixPath:
    candidate = PurePosixPath(value)
    if not value or "\\" in value or "//" in value or candidate.is_absolute() or (len(value) >= 2 and value[1] == ":") or any(part in {"", ".", ".."} for part in candidate.parts):
        raise ValueError(f"invalid library-relative path: {value!r}")
    if candidate.suffix.lower() not in ALLOWED_EXTENSIONS:
        raise ValueError(f"unsupported material type: {candidate.suffix}")
    return candidate


def list_local(limit: int) -> dict[str, object] | None:
    if not LIBRARY_ROOT.is_dir():
        return None
    root = LIBRARY_ROOT.resolve(strict=True)
    items = []
    for path in sorted(root.rglob("*"), key=lambda item: str(item).casefold()):
        if len(items) >= limit:
            break
        if path.is_symlink() or not path.is_file() or path.suffix.lower() not in ALLOWED_EXTENSIONS:
            continue
        resolved = path.resolve(strict=True)
        if root not in resolved.parents:
            continue
        items.append({"path": resolved.relative_to(root).as_posix(), "size": resolved.stat().st_size})
    return {"root": "huangque-media", "mode": "local-read-only", "items": items}


def list_remote(limit: int) -> dict[str, object]:
    completed = subprocess.run(
        ["ssh", *ssh_options(), SSH_TARGET, f"list {limit}"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    payload = json.loads(completed.stdout)
    payload["mode"] = "ssh-read-only"
    return payload


def copy_one(relative: PurePosixPath, output_dir: Path) -> dict[str, object]:
    destination = output_dir.joinpath(*relative.parts)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if LIBRARY_ROOT.is_dir():
        root = LIBRARY_ROOT.resolve(strict=True)
        unresolved = root / Path(*relative.parts)
        if path_contains_symlink(root, unresolved):
            raise ValueError(f"symlink material is not allowed: {relative}")
        source = unresolved.resolve(strict=True)
        if root not in source.parents or not source.is_file():
            raise ValueError(f"material escapes or is missing from the library: {relative}")
        shutil.copy2(source, destination)
    else:
        encoded = base64.urlsafe_b64encode(str(relative).encode("utf-8")).decode("ascii")
        temporary = destination.with_name(f".{destination.name}.partial")
        try:
            with temporary.open("wb") as output:
                subprocess.run(
                    ["ssh", *ssh_options(), SSH_TARGET, f"fetch {encoded}"],
                    check=True,
                    stdout=output,
                    stderr=subprocess.PIPE,
                )
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
    digest = hash_file(destination)
    return {"path": str(relative), "local_path": str(destination.resolve()), "size": destination.stat().st_size, "sha256": digest}


def ssh_options() -> list[str]:
    key = Path(os.environ.get("HUANGQUE_MATERIAL_SSH_KEY", DEFAULT_SSH_KEY))
    known_hosts = Path(os.environ.get("HUANGQUE_MATERIAL_KNOWN_HOSTS", DEFAULT_KNOWN_HOSTS))
    if not key.is_file():
        raise FileNotFoundError("dedicated Huangque material SSH key is missing")
    known_hosts.parent.mkdir(parents=True, exist_ok=True)
    return [
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"UserKnownHostsFile={known_hosts}",
        "-i", str(key),
    ]


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def path_contains_symlink(root: Path, target: Path) -> bool:
    cursor = root
    for part in target.relative_to(root).parts:
        cursor /= part
        if cursor.is_symlink():
            return True
    return False


def main() -> int:
    args = parse_args()
    try:
        if args.command == "list":
            if args.limit <= 0 or args.limit > 5000:
                raise ValueError("--limit must be between 1 and 5000")
            payload = list_local(args.limit) or list_remote(args.limit)
        else:
            output_dir = args.output_dir.resolve()
            payload = {
                "root": "huangque-media",
                "mode": "local-read-only" if LIBRARY_ROOT.is_dir() else "ssh-read-only",
                "items": [copy_one(validate_relative_path(value), output_dir) for value in args.paths],
            }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, subprocess.CalledProcessError, json.JSONDecodeError) as error:
        print(f"material library unavailable: {type(error).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
