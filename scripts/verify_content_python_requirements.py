#!/usr/bin/env python3
"""Fail-closed, read-only verification for content-service Python pins."""
import argparse
import importlib.metadata
import re
import subprocess
import sys
from pathlib import Path


PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)==([^\s;]+)$")
ALLOWED_BROKEN = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9_.-]*)==([^\s:]+):"
    r"([A-Za-z0-9][A-Za-z0-9_.-]*)$"
)
PIP_MISSING = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9_.-]*) ([^\s]+) requires "
    r"([A-Za-z0-9][A-Za-z0-9_.-]*), which is not installed\.$",
    re.IGNORECASE,
)


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


def read_allowed_broken(values, pins):
    allowed = set()
    for raw in values or ():
        match = ALLOWED_BROKEN.fullmatch(str(raw).strip())
        if not match:
            raise RuntimeError("invalid allowed broken requirement: %s" % raw)
        name, version, dependency = match.groups()
        canonical = canonical_name(name)
        if canonical in pins:
            raise RuntimeError(
                "pinned content requirement cannot be ignored: %s" % name
            )
        try:
            actual = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError as exc:
            raise RuntimeError(
                "allowed broken package is missing: %s" % name
            ) from exc
        if actual != version:
            raise RuntimeError(
                "allowed broken package %s is %s (expected %s)" %
                (name, actual, version)
            )
        allowed.add((canonical, version, canonical_name(dependency)))
    return allowed


def _pip_check_problems(result):
    output = "\n".join(
        value for value in (
            getattr(result, "stdout", "") or "",
            getattr(result, "stderr", "") or "",
        ) if value
    )
    return [line.strip() for line in output.splitlines() if line.strip()]


def verify_installed(path, allowed_broken=()):
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
    allowed = read_allowed_broken(allowed_broken, pins)
    result = subprocess.run(
        [sys.executable, "-m", "pip", "check"],
        check=False, capture_output=True, text=True, timeout=120,
    )
    if result.returncode:
        unexpected = []
        for problem in _pip_check_problems(result):
            match = PIP_MISSING.fullmatch(problem)
            key = None
            if match:
                name, version, dependency = match.groups()
                key = (
                    canonical_name(name), version,
                    canonical_name(dependency),
                )
            if key not in allowed:
                unexpected.append(problem)
        if unexpected or not _pip_check_problems(result):
            raise RuntimeError(
                "pip check reported an inconsistent environment: %s" %
                ("; ".join(unexpected) or "unclassified failure")
            )
    return len(pins)


def main():
    parser = argparse.ArgumentParser(
        description="Verify exact content-service dependencies without installing"
    )
    parser.add_argument("--requirements", required=True)
    parser.add_argument(
        "--allow-broken-requirement", action="append", default=[],
        help=(
            "Exact unrelated pip-check exception as "
            "package==version:missing_dependency"
        ),
    )
    args = parser.parse_args()
    count = verify_installed(
        args.requirements,
        allowed_broken=args.allow_broken_requirement,
    )
    print("verified %d exact content dependency pins" % count)


if __name__ == "__main__":
    main()
