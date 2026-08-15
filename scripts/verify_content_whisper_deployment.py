#!/usr/bin/env python3
"""Read-only verifier for the cumulative content Whisper deployment manifest."""
import argparse
import hashlib
import json
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
    root = Path(root).resolve()
    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("repository_path escaped source root")
    return candidate


def _safe_runtime_path(root, absolute_path):
    runtime = PurePosixPath(str(absolute_path))
    if not runtime.is_absolute() or ".." in runtime.parts:
        raise ValueError("runtime_path must be a normalized absolute path")
    root = Path(root).resolve()
    candidate = root.joinpath(*runtime.parts[1:]).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("runtime_path escaped runtime root")
    return candidate


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
        data = source.read_bytes()
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


def _actual_target_digest(target):
    if not target.exists():
        return "absent", _sha256(ABSENT_BYTES), None
    if not target.is_file() or target.is_symlink():
        raise RuntimeError("deployment target is not a regular file: %s" % target)
    data = target.read_bytes()
    return "file", _sha256(data), _blob_id(data)


def verify_targets(manifest, runtime_root, phase):
    if phase not in {"preimage", "postimage"}:
        raise ValueError("phase must be preimage or postimage")
    verified = []
    for entry in manifest["files"]:
        target = _safe_runtime_path(runtime_root, entry["runtime_path"])
        state, digest, blob = _actual_target_digest(target)
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
