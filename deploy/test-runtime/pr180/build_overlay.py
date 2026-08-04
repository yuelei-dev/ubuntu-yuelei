#!/usr/bin/env python3
"""Materialize and verify the PR #180 test-server forward-port artifacts."""

import argparse
import hashlib
import json
import pathlib
import subprocess
import sys
import tempfile


ROOT = pathlib.Path(__file__).resolve().parents[3]
MANIFEST = pathlib.Path(__file__).with_name("manifest.json")


def _blob(oid):
    return subprocess.check_output(["git", "cat-file", "blob", oid], cwd=ROOT)


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def load_manifest():
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def seed():
    for entry in load_manifest()["deploy_files"]:
        artifact = entry.get("artifact")
        preimage = entry.get("preimage")
        if not artifact or not preimage:
            continue
        target = ROOT / artifact
        if target.exists():
            raise SystemExit("refusing to overwrite existing artifact: %s" % artifact)
        data = (ROOT / preimage).read_bytes()
        expected = entry["expected_target_sha256"]
        if _sha256(data) != expected:
            raise SystemExit("preimage sha256 mismatch: %s" % artifact)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        print("seeded %s" % artifact)


def capture_preimages():
    """One-time capture from the audited local snapshot object database."""
    for entry in load_manifest()["deploy_files"]:
        preimage_path = entry.get("preimage")
        blob = entry.get("expected_target_git_blob")
        if not preimage_path or not blob:
            continue
        target = ROOT / preimage_path
        if target.exists():
            raise SystemExit("refusing to overwrite committed preimage: %s" % preimage_path)
        data = _blob(blob)
        if _sha256(data) != entry.get("expected_target_sha256"):
            raise SystemExit("preimage sha256 mismatch: %s" % preimage_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        print("captured %s" % preimage_path)


def merge_clean():
    for entry in load_manifest()["deploy_files"]:
        if not entry.get("artifact"):
            continue
        if entry.get("resolution") == "manual":
            continue
        target = ROOT / entry["artifact"]
        if not target.is_file():
            raise SystemExit("missing seeded artifact: %s" % entry["artifact"])
        ours = target.read_bytes()
        if subprocess.check_output(["git", "hash-object", "--stdin"], cwd=ROOT,
                                   input=ours, text=False).decode().strip() != entry["expected_target_git_blob"]:
            raise SystemExit("seeded artifact changed before merge: %s" % entry["artifact"])
        with tempfile.TemporaryDirectory() as directory:
            paths = []
            for name, data in (("ours", ours), ("base", _blob(entry["base_git_blob"])),
                               ("approved", _blob(entry["approved_git_blob"]))):
                path = pathlib.Path(directory) / name
                path.write_bytes(data)
                paths.append(path)
            merged = subprocess.run(["git", "merge-file", "-p", *map(str, paths)],
                                    cwd=ROOT, capture_output=True)
        if merged.returncode:
            raise SystemExit("unexpected merge conflict: %s" % entry["artifact"])
        target.write_bytes(merged.stdout)
        print("merged %s" % entry["artifact"])


def check():
    errors = []
    manifest = load_manifest()
    for entry in manifest["deploy_files"]:
        source = ROOT / entry["source"]
        if not source.is_file():
            errors.append("missing source: %s" % entry["source"])
            continue
        data = source.read_bytes()
        if _sha256(data) != entry["sha256"]:
            errors.append("source sha256 mismatch: %s" % entry["source"])
        blob = subprocess.check_output(["git", "hash-object", "--", str(source)], cwd=ROOT,
                                       text=True).strip()
        if blob != entry["git_blob"]:
            errors.append("source git blob mismatch: %s" % entry["source"])
        preimage_path = entry.get("preimage")
        if entry.get("expected_target_state") == "sha256":
            if not preimage_path or not (ROOT / preimage_path).is_file():
                errors.append("missing committed preimage: %s" % entry["target"])
            else:
                old = (ROOT / preimage_path).read_bytes()
                if _sha256(old) != entry.get("expected_target_sha256"):
                    errors.append("preimage sha256 mismatch: %s" % entry["target"])
                old_blob = subprocess.check_output(["git", "hash-object", "--stdin"], cwd=ROOT,
                                                   input=old, text=False).decode().strip()
                if old_blob != entry.get("expected_target_git_blob"):
                    errors.append("preimage git blob mismatch: %s" % entry["target"])
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("PR180 test overlay verification passed: %d files" % len(manifest["deploy_files"]))
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--capture-preimages", action="store_true")
    parser.add_argument("--seed", action="store_true")
    parser.add_argument("--merge-clean", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if sum((args.capture_preimages, args.seed, args.merge_clean, args.check)) != 1:
        parser.error("choose exactly one action")
    if args.capture_preimages:
        return capture_preimages()
    if args.seed:
        return seed()
    if args.merge_clean:
        return merge_clean()
    return check()


if __name__ == "__main__":
    raise SystemExit(main())
