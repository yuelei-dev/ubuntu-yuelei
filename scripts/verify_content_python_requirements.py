#!/usr/bin/env python3
"""Fail-closed, read-only verification for content-service Python pins."""
import argparse
import importlib.metadata
import re
import subprocess
import sys
from pathlib import Path


PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)==([^\s;]+)$")


def canonical_name(value):
    return re.sub(r"[-_.]+", "-", value).lower()


def read_exact_pins(path):
    pins = {}
    for number, raw_line in enumerate(
            Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        match = PIN.fullmatch(line)
        if not match:
            raise RuntimeError(
                "requirements line %d is not an exact name==version pin" % number
            )
        name, version = match.groups()
        canonical = canonical_name(name)
        if canonical in pins:
            raise RuntimeError("duplicate requirement: %s" % name)
        pins[canonical] = (name, version)
    if not pins:
        raise RuntimeError("requirements file contains no exact pins")
    return pins


def verify_installed(path):
    pins = read_exact_pins(path)
    errors = []
    for _canonical, (name, expected) in sorted(pins.items()):
        try:
            actual = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            errors.append("%s is missing (expected %s)" % (name, expected))
            continue
        if actual != expected:
            errors.append(
                "%s is %s (expected %s)" % (name, actual, expected)
            )
    if errors:
        raise RuntimeError("; ".join(errors))
    result = subprocess.run(
        [sys.executable, "-m", "pip", "check"],
        check=False, capture_output=True, text=True, timeout=120,
    )
    if result.returncode:
        raise RuntimeError("pip check reported an inconsistent environment")
    return len(pins)


def main():
    parser = argparse.ArgumentParser(
        description="Verify exact content-service dependencies without installing"
    )
    parser.add_argument("--requirements", required=True)
    args = parser.parse_args()
    count = verify_installed(args.requirements)
    print("verified %d exact content dependency pins" % count)


if __name__ == "__main__":
    main()
