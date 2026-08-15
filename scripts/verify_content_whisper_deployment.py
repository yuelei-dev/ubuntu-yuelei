#!/usr/bin/env python3
"""Read-only verifier for the cumulative content Whisper deployment manifest."""
import argparse
import hashlib
import json
import os
import stat
from pathlib import Path, PurePosixPath


ABSENT_BYTES = b"ABSENT\n"


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _blob_id(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _safe_source_path(root, relative_path):
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("repository_path must stay inside source root")
    root = Path(os.path.abspath(root))
    candidate = Path(os.path.abspath(root / relative))
    if os.path.commonpath((str(root), str(candidate))) != str(root):
        raise ValueError("repository_path escaped source root")
    return candidate


def _safe_runtime_path(root, absolute_path):
    runtime = PurePosixPath(str(absolute_path))
    if not runtime.is_absolute() or ".." in runtime.parts:
        raise ValueError("runtime_path must be a normalized absolute path")
    root = Path(os.path.abspath(root))
    candidate = Path(os.path.abspath(root.joinpath(*runtime.parts[1:])))
    if os.path.commonpath((str(root), str(candidate))) != str(root):
        raise ValueError("runtime_path escaped runtime root")
    return candidate


def _lstat_no_symlink_chain(root, target):
    """Reject symlinks in every existing component without resolving them."""
    root = Path(os.path.abspath(root))
    target = Path(os.path.abspath(target))
    if os.path.commonpath((str(root), str(target))) != str(root):
        raise ValueError("target escaped verification root")
    root_info = os.lstat(root)
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise RuntimeError("verification root must be a real directory: %s" % root)
    current = root
    relative_parts = target.relative_to(root).parts
    for index, part in enumerate(relative_parts):
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            return current, False
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeError("symbolic link is forbidden in deployment path: %s" % current)
        if index < len(relative_parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("deployment parent is not a directory: %s" % current)
    return target, True


def _read_regular_file_no_follow(root, target):
    target, exists = _lstat_no_symlink_chain(root, target)
    if not exists:
        return None
    parent_descriptor = None
    if os.name == "posix":
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_flags |= getattr(os, "O_NOFOLLOW", 0)
        parent_descriptor = os.open(Path(os.path.abspath(root)), directory_flags)
        try:
            for part in target.parent.relative_to(
                    Path(os.path.abspath(root))).parts:
                next_descriptor = os.open(
                    part, directory_flags, dir_fd=parent_descriptor,
                )
                os.close(parent_descriptor)
                parent_descriptor = next_descriptor
        except Exception:
            os.close(parent_descriptor)
            raise
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(
        target.name if parent_descriptor is not None else target,
        flags,
        **({"dir_fd": parent_descriptor} if parent_descriptor is not None else {}),
    )
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError("deployment target is not a regular file: %s" % target)
        chunks = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(descriptor)
        if parent_descriptor is not None:
            os.close(parent_descriptor)
    _lstat_no_symlink_chain(root, target)
    return b"".join(chunks)


def load_manifest(path):
    manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1:
        raise ValueError("unsupported deployment manifest schema")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("deployment manifest has no files")
    repository_paths = [entry.get("repository_path") for entry in files]
    runtime_paths = [entry.get("runtime_path") for entry in files]
    if len(repository_paths) != len(set(repository_paths)):
        raise ValueError("duplicate repository_path in deployment manifest")
    if len(runtime_paths) != len(set(runtime_paths)):
        raise ValueError("duplicate runtime_path in deployment manifest")
    return manifest


def verify_sources(manifest, source_root):
    verified = []
    for entry in manifest["files"]:
        source = _safe_source_path(source_root, entry["repository_path"])
        data = _read_regular_file_no_follow(source_root, source)
        if data is None:
            raise RuntimeError("source file is missing: %s" % entry["repository_path"])
        if _sha256(data) != entry["source_sha256"]:
            raise RuntimeError("source SHA-256 mismatch: %s" % entry["repository_path"])
        if _blob_id(data) != entry["source_blob"]:
            raise RuntimeError("source Git blob mismatch: %s" % entry["repository_path"])
        if entry["expected_postimage_sha256"] != entry["source_sha256"]:
            raise RuntimeError("postimage SHA-256 is not source-locked: %s" % entry["repository_path"])
        if entry["expected_postimage_blob"] != entry["source_blob"]:
            raise RuntimeError("postimage blob is not source-locked: %s" % entry["repository_path"])
        verified.append(str(source))
    return verified


def _actual_target_digest(runtime_root, target):
    data = _read_regular_file_no_follow(runtime_root, target)
    if data is None:
        return "absent", _sha256(ABSENT_BYTES), None
    return "file", _sha256(data), _blob_id(data)


def verify_targets(manifest, runtime_root, phase):
    if phase not in {"preimage", "postimage"}:
        raise ValueError("phase must be preimage or postimage")
    verified = []
    for entry in manifest["files"]:
        target = _safe_runtime_path(runtime_root, entry["runtime_path"])
        state, digest, blob = _actual_target_digest(runtime_root, target)
        if phase == "preimage":
            expected_state = entry["target_preimage_state"]
            expected_digest = entry["target_preimage_sha256"]
            expected_blob = entry.get("target_preimage_blob")
        else:
            expected_state = "file"
            expected_digest = entry["expected_postimage_sha256"]
            expected_blob = entry["expected_postimage_blob"]
        if state != expected_state or digest != expected_digest:
            raise RuntimeError("%s mismatch: %s" % (phase, entry["runtime_path"]))
        if state == "file" and blob != expected_blob:
            raise RuntimeError("%s Git blob mismatch: %s" % (phase, entry["runtime_path"]))
        verified.append(str(target))
    return verified


def main():
    parser = argparse.ArgumentParser(
        description="Verify cumulative content Whisper deployment inputs without writing"
    )
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--runtime-root")
    parser.add_argument(
        "--phase", choices=("source", "preimage", "postimage"), required=True
    )
    args = parser.parse_args()
    manifest = load_manifest(args.manifest)
    if args.phase == "source":
        verified = verify_sources(manifest, args.source_root)
    else:
        if not args.runtime_root:
            parser.error("--runtime-root is required for target verification")
        verified = verify_targets(manifest, args.runtime_root, args.phase)
    print("verified %d %s entries" % (len(verified), args.phase))


if __name__ == "__main__":
    main()
