#!/usr/bin/env python3
"""Compare two runtime releases by their complete relative-path/hash maps."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def file_map(root):
    result = {}
    for path in sorted(Path(root).rglob("*")):
        if path.is_file() and path.name != "release-manifest.json":
            relative = path.relative_to(root).as_posix()
            result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("first", type=Path)
    parser.add_argument("second", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    first = file_map(args.first)
    second = file_map(args.second)
    payload = {
        "schema_version": 1,
        "first_file_count": len(first),
        "second_file_count": len(second),
        "expected_file_count": 305,
        "paths_equal": set(first) == set(second),
        "hashes_equal": first == second,
        "only_first": sorted(set(first) - set(second)),
        "only_second": sorted(set(second) - set(first)),
        "hash_mismatches": sorted(
            path for path in set(first) & set(second) if first[path] != second[path]
        ),
        "files": first,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({key: value for key, value in payload.items() if key != "files"}))
    return 0 if len(first) == len(second) == 305 and first == second else 1


if __name__ == "__main__":
    raise SystemExit(main())
