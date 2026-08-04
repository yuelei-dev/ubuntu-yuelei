#!/usr/bin/env python3
"""Materialize and verify the PR #182 test-runtime overlay."""

import argparse
import hashlib
import json
import pathlib
import re
import subprocess
import sys
import tempfile


ROOT = pathlib.Path(__file__).resolve().parents[3]
HERE = pathlib.Path(__file__).resolve().parent
MANIFEST = HERE / "manifest.json"
SOURCE_MERGE = "bbfeac84d4e60811ba9988bcb468438a94921427"
SOURCE_BASE = "f827ab467bf691bd260d40c62c7f41f0748887e0"
SOURCE_TREE = "7e27c461d344019c941bb9f2548f366d4bb6e8cb"
APPROVED_HEAD = "4b956fd386eeda205d365bf6bb1fa71941b6b83e"


def load_manifest():
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def git(*args, input_bytes=None):
    return subprocess.check_output(
        ["git", *args], cwd=ROOT, input=input_bytes
    )


def blob(oid):
    return git("cat-file", "blob", oid)


def object_exists(oid, object_type):
    if not re.fullmatch(r"[0-9a-f]{40}", str(oid or "")):
        return False
    result = subprocess.run(
        ["git", "cat-file", "-e", "%s^{%s}" % (oid, object_type)],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def git_blob(data):
    header = ("blob %d\0" % len(data)).encode("ascii")
    return hashlib.sha1(header + data).hexdigest()


def capture_preimages():
    for entry in load_manifest()["deploy_files"]:
        if entry["expected_target_state"] == "absent":
            continue
        if not entry.get("preimage"):
            continue
        if not entry["preimage"].startswith("deploy/test-runtime/pr182/preimage/"):
            continue
        target = ROOT / entry["preimage"]
        if target.exists():
            raise SystemExit("refusing to overwrite preimage: %s" % entry["preimage"])
        data = blob(entry["expected_target_git_blob"])
        if sha256(data) != entry["expected_target_sha256"]:
            raise SystemExit("preimage sha256 mismatch: %s" % entry["target"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        print("captured %s" % entry["preimage"])


def merged_bytes(entry):
    ours = (ROOT / entry["preimage"]).read_bytes()
    # Keep controllable temporary artifacts on the workspace drive.
    with tempfile.TemporaryDirectory(dir=ROOT) as directory:
        paths = []
        for name, data in (
            ("ours", ours),
            ("base", blob(entry["base_git_blob"])),
            ("approved", blob(entry["approved_git_blob"])),
        ):
            path = pathlib.Path(directory) / name
            path.write_bytes(data)
            paths.append(path)
        result = subprocess.run(
            ["git", "merge-file", "-p", *map(str, paths)],
            cwd=ROOT,
            capture_output=True,
        )
    return result.returncode, result.stdout


def materialize(seed_manual=False):
    for entry in load_manifest()["deploy_files"]:
        target = ROOT / entry["source"]
        resolution = entry["resolution"]
        if resolution in ("dependency_passthrough", "deployment_infrastructure"):
            print("kept repository source %s" % entry["source"])
            continue
        if resolution == "manual" and target.exists() and not seed_manual:
            print("kept manual %s" % entry["source"])
            continue
        if target.exists():
            raise SystemExit("refusing to overwrite artifact: %s" % entry["source"])
        if resolution == "approved":
            data = blob(entry["approved_git_blob"])
        else:
            returncode, data = merged_bytes(entry)
            if returncode and not (resolution == "manual" and seed_manual):
                raise SystemExit("unexpected merge conflict: %s" % entry["feature_source_path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        print("materialized %s" % entry["source"])


def check():
    errors = []
    manifest = load_manifest()
    entries = manifest.get("deploy_files") or []
    if len(entries) != 9:
        errors.append("deploy_files must contain exactly nine PR182 runtime dependencies")
    if manifest.get("feature_merge_commit") != SOURCE_MERGE:
        errors.append("source merge commit changed")
    if manifest.get("feature_merge_tree") != SOURCE_TREE:
        errors.append("source merge tree changed")
    if manifest.get("approved_head") != APPROVED_HEAD:
        errors.append("approved feature head changed")
    source_history_available = object_exists(SOURCE_MERGE, "commit")
    if source_history_available:
        actual_tree = git("rev-parse", SOURCE_MERGE + "^{tree}").decode().strip()
        if actual_tree != SOURCE_TREE:
            errors.append("source merge tree mismatch")
        actual_parents = git("show", "-s", "--format=%P", SOURCE_MERGE).decode().split()
        if actual_parents != [SOURCE_BASE, APPROVED_HEAD]:
            errors.append("source merge parents mismatch")
    targets = set()
    for entry in entries:
        label = entry.get("feature_source_path") or "unknown"
        target_name = entry.get("target")
        if target_name in targets:
            errors.append("duplicate target: %s" % target_name)
        targets.add(target_name)
        approved_blob = entry.get("approved_git_blob")
        if approved_blob:
            if not re.fullmatch(r"[0-9a-f]{40}", approved_blob):
                errors.append("invalid approved source blob: %s" % label)
            elif source_history_available:
                try:
                    source_blob = git("rev-parse", "%s:%s" % (SOURCE_MERGE, label)).decode().strip()
                except Exception:
                    source_blob = ""
                if source_blob != approved_blob:
                    errors.append("approved source blob mismatch: %s" % label)
        if entry["expected_target_state"] == "sha256":
            if entry.get("preimage"):
                preimage = ROOT / entry["preimage"]
                data = preimage.read_bytes() if preimage.is_file() else b""
            elif entry.get("preimage_ref"):
                try:
                    data = git("show", entry["preimage_ref"])
                except Exception:
                    data = b""
            else:
                data = b""
            if sha256(data) != entry["expected_target_sha256"]:
                errors.append("preimage sha256 mismatch: %s" % label)
            if git_blob(data) != entry["expected_target_git_blob"]:
                errors.append("preimage git blob mismatch: %s" % label)
        elif entry["expected_target_state"] != "absent":
            errors.append("unsupported preimage state: %s" % label)
        artifact = ROOT / entry["source"]
        if not artifact.is_file():
            errors.append("missing artifact: %s" % label)
            continue
        data = artifact.read_bytes()
        if b"<<<<<<<" in data or b">>>>>>>" in data:
            errors.append("unresolved conflict marker: %s" % label)
        if sha256(data) != entry.get("sha256"):
            errors.append("postimage sha256 mismatch: %s" % label)
        if git_blob(data) != entry.get("git_blob"):
            errors.append("postimage git blob mismatch: %s" % label)
        if entry["resolution"] == "merge_clean":
            base_blob = entry.get("base_git_blob")
            if not re.fullmatch(r"[0-9a-f]{40}", str(base_blob or "")):
                errors.append("invalid base source blob: %s" % label)
            elif object_exists(base_blob, "blob") and object_exists(approved_blob, "blob"):
                returncode, regenerated = merged_bytes(entry)
                if returncode or regenerated != data:
                    errors.append("clean overlay is not reproducible: %s" % label)
        if entry["resolution"] == "manual" and not entry.get("manual_resolution_contract"):
            errors.append("manual overlay lacks contract: %s" % label)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("PR182 test overlay verification passed: 9 exact dependencies")
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--capture-preimages", action="store_true")
    parser.add_argument("--materialize", action="store_true")
    parser.add_argument("--seed-manual", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    selected = sum((args.capture_preimages, args.materialize, args.check))
    if selected != 1:
        parser.error("choose exactly one action")
    if args.capture_preimages:
        capture_preimages()
        return 0
    if args.materialize:
        materialize(seed_manual=args.seed_manual)
        return 0
    return check()


if __name__ == "__main__":
    raise SystemExit(main())
