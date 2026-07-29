#!/usr/bin/env python3
"""Generate a value-free environment contract for the test runtime."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path, PurePosixPath


ENV_PATTERNS = (
    re.compile(r"""\bos\.getenv\(\s*["']([A-Z][A-Z0-9_]*)["']"""),
    re.compile(
        r"""\bos\.environ\.get\(\s*["']([A-Z][A-Z0-9_]*)["']"""
    ),
    re.compile(r"""\bos\.environ\[\s*["']([A-Z][A-Z0-9_]*)["']\s*\]"""),
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("deploy/runtime-test/env-contract.json"),
    )
    args = parser.parse_args()
    root = args.repo_root.resolve()
    manifest = json.loads(
        (root / "deploy/runtime-manifest.json").read_text(encoding="utf-8")
    )

    keys_by_file = {}
    all_keys = set()
    for entry in manifest["files"]:
        git_path = entry["git_path"]
        if not git_path.endswith(".py"):
            continue
        path = root / Path(*PurePosixPath(git_path).parts)
        text = path.read_text(encoding="utf-8", errors="replace")
        keys = sorted(
            {
                match.group(1)
                for pattern in ENV_PATTERNS
                for match in pattern.finditer(text)
            }
        )
        if keys:
            keys_by_file[git_path] = keys
            all_keys.update(keys)

    environment_files = {}
    systemd_root = root / "deploy/runtime-test/systemd"
    for path in sorted(systemd_root.glob("*.conf")):
        environment_files[path.name] = sorted(
            {
                line.split("=", 1)[1].lstrip("-").strip()
                for line in path.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines()
                if line.strip().startswith("EnvironmentFile=")
            }
        )

    result = {
        "schema_version": 1,
        "values_included": False,
        "server_role": "test-only",
        "environment_keys": sorted(all_keys),
        "keys_by_file": keys_by_file,
        "environment_file_path_contracts": environment_files,
        "note": (
            "Keys and external path contracts only. Runtime values, credentials, "
            "tokens, passwords and model keys are intentionally excluded."
        ),
    }
    output = args.output
    if not output.is_absolute():
        output = root / output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps({
        "output": str(output),
        "environment_key_count": len(all_keys),
        "files_with_environment_keys": len(keys_by_file),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
