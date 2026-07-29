#!/usr/bin/env python3
"""Convert a preserved ``unittest -q`` log into structured failure evidence."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from run_unittest_json import classify


BLOCK = re.compile(
    r"^={70}\n(ERROR|FAIL): (.*?)\n-{70}\n(.*?)(?=^={70}\n|^-{70}\nRan )",
    re.MULTILINE | re.DOTALL,
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("log", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--encoding", default="utf-16")
    args = parser.parse_args()
    text = args.log.read_text(
        encoding=args.encoding, errors="replace"
    ).replace("\r\n", "\n")
    records = []
    for kind, wrapped_header, detail in BLOCK.findall(text):
        header = wrapped_header.replace("\n", "")
        match = re.search(r"\(([^()]+)\)\s*$", header)
        test_id = match.group(1) if match else header
        status = "error" if kind == "ERROR" else "failure"
        records.append(
            {
                "id": test_id,
                "status": status,
                "traceback": detail.rstrip(),
                "category": classify(test_id, detail),
            }
        )
    summary = re.search(
        r"Ran\s+(\d+)\s+tests.*?FAILED\s+\(failures=(\d+),\s*errors=(\d+)\)",
        text,
        re.DOTALL,
    )
    if not summary:
        raise SystemExit("unittest summary not found")
    tests_run, failures, errors = map(int, summary.groups())
    if len(records) != failures + errors:
        raise SystemExit(
            f"parsed {len(records)} failure blocks, expected {failures + errors}"
        )
    categories = {}
    for record in records:
        category = record["category"]
        categories[category] = categories.get(category, 0) + 1
    payload = {
        "schema_version": 1,
        "source_format": "preserved unittest -q text log",
        "tests_run": tests_run,
        "counts": {
            "pass": tests_run - failures - errors,
            "failure": failures,
            "error": errors,
        },
        "failure_categories": categories,
        "records": records,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({key: value for key, value in payload.items() if key != "records"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
