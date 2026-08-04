#!/usr/bin/env python3
"""Validate and resolve an exact-file test-runtime deployment manifest."""

import argparse
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]
ALLOWED_TARGETS = ("/home/ubuntu/", "/var/www/huangquechuanmei/", "/etc/systemd/system/")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_blob(path):
    return subprocess.check_output(
        ["git", "hash-object", "--", str(path)], cwd=ROOT, text=True
    ).strip()


def load_manifest(path):
    manifest_path = (ROOT / path).resolve() if not pathlib.Path(path).is_absolute() else pathlib.Path(path)
    try:
        manifest_path.relative_to(ROOT)
    except ValueError as exc:
        raise ValueError("manifest must be inside the repository") from exc
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or data.get("target") != "test:8.148.158.106":
        raise ValueError("unsupported or non-test manifest")
    overrides = data.get("overrides", [])
    if (not isinstance(overrides, list)
            or any(not isinstance(item, str)
                   or not re.fullmatch(r"deploy/test-runtime/[^/]+/manifest\.json", item)
                   for item in overrides)
            or len(overrides) != len(set(overrides))
            or str(path).replace("\\", "/") in overrides):
        raise ValueError("invalid overlay overrides")
    entries = data.get("deploy_files")
    if not isinstance(entries, list) or not entries:
        raise ValueError("deploy_files must be a non-empty array")
    seen_sources, seen_targets = set(), set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError("deploy_files[%d] must be an object" % index)
        source, target = entry.get("source"), entry.get("target")
        if not isinstance(source, str) or source.startswith(("/", "\\")) or ".." in pathlib.PurePosixPath(source).parts:
            raise ValueError("invalid source at deploy_files[%d]" % index)
        if (not isinstance(target, str) or not target.startswith(ALLOWED_TARGETS)
                or target.endswith("/") or not re.fullmatch(r"[A-Za-z0-9._/-]+", target)):
            raise ValueError("invalid target for %s" % source)
        if os.path.basename(source) != os.path.basename(target):
            raise ValueError("source and target basename differ for %s" % source)
        if source in seen_sources or target in seen_targets:
            raise ValueError("duplicate source or target: %s" % source)
        seen_sources.add(source)
        seen_targets.add(target)
        services = entry.get("services")
        if not isinstance(services, list) or any(not re.fullmatch(r"huangque-[a-z0-9-]+", x or "") for x in services):
            raise ValueError("invalid services for %s" % source)
        state = entry.get("expected_target_state")
        if state == "sha256":
            if not SHA256_RE.fullmatch(entry.get("expected_target_sha256", "")):
                raise ValueError("invalid expected target sha256 for %s" % source)
        elif state == "absent":
            if entry.get("expected_target_sha256") is not None or entry.get("expected_target_git_blob") is not None:
                raise ValueError("absent target must not carry a preimage hash for %s" % source)
        else:
            raise ValueError("invalid expected_target_state for %s" % source)
        if not SHA256_RE.fullmatch(entry.get("sha256", "")):
            raise ValueError("invalid source sha256 for %s" % source)
        if not re.fullmatch(r"[0-9a-f]{40}", entry.get("git_blob", "")):
            raise ValueError("invalid source git blob for %s" % source)
    return manifest_path, data


def validate(path, requested):
    _, data = load_manifest(path)
    entries = data["deploy_files"]
    declared = [entry["source"] for entry in entries]
    if requested and (len(requested) != len(set(requested)) or set(requested) != set(declared)):
        missing = sorted(set(declared) - set(requested))
        extra = sorted(set(requested) - set(declared))
        raise ValueError("exact source set mismatch; missing=%s extra=%s" % (missing, extra))
    for entry in entries:
        source = ROOT / entry["source"]
        if not source.is_file():
            raise ValueError("missing source: %s" % entry["source"])
        if _sha256(source) != entry["sha256"]:
            raise ValueError("source sha256 mismatch: %s" % entry["source"])
        if _git_blob(source) != entry["git_blob"]:
            raise ValueError("source git blob mismatch: %s" % entry["source"])
    print("test-runtime manifest verified: %d exact files" % len(entries))


def resolve(path, source):
    _, data = load_manifest(path)
    matches = [entry for entry in data["deploy_files"] if entry["source"] == source]
    if len(matches) != 1:
        raise ValueError("source is not declared exactly once: %s" % source)
    entry = matches[0]
    print("\t".join((
        entry["target"],
        " ".join(entry["services"]) or "-",
        entry["expected_target_state"],
        entry.get("expected_target_sha256") or "-",
    )))


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("validate")
    check.add_argument("manifest")
    check.add_argument("sources", nargs="*")
    get = sub.add_parser("resolve")
    get.add_argument("manifest")
    get.add_argument("source")
    args = parser.parse_args()
    try:
        if args.command == "validate":
            validate(args.manifest, args.sources)
        else:
            resolve(args.manifest, args.source)
    except (OSError, ValueError, json.JSONDecodeError, subprocess.CalledProcessError) as exc:
        print("test-runtime manifest error: %s" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
