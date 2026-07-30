#!/usr/bin/env python3
"""Block runtime PRs on any base-to-head test regression."""
import argparse
import json
from pathlib import Path


BAD = {"failed", "error"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True, type=Path)
    parser.add_argument("--head", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    base = json.loads(args.base.read_text(encoding="utf-8"))
    head = json.loads(args.head.read_text(encoding="utf-8"))
    bo, ho = base["outcomes"], head["outcomes"]
    base_pass_regressions = sorted(
        name for name, value in bo.items()
        if value["outcome"] == "passed"
        and (name not in ho or ho[name]["outcome"] != "passed")
    )
    head_only_bad = sorted(
        name for name, value in ho.items()
        if name not in bo and value["outcome"] in BAD
    )
    gemini = {
        name: value["outcome"] for name, value in ho.items()
        if "test_breakdown_gemini31" in name
    }
    checks = {
        "test_discovery_not_reduced": head["tests_run"] >= base["tests_run"],
        "skip_count_not_increased": head["skipped"] <= base["skipped"],
        "base_passes_do_not_regress": not base_pass_regressions,
        "head_only_failure_error_zero": not head_only_bad,
        "gemini_tests_discovered": len(gemini) >= 22,
        "gemini_tests_all_pass": bool(gemini) and set(gemini.values()) == {"passed"},
    }
    report = {
        "base": {key: base[key] for key in ("tests_run", "skipped", "failures", "errors")},
        "head": {key: head[key] for key in ("tests_run", "skipped", "failures", "errors")},
        "checks": checks,
        "base_pass_regressions": base_pass_regressions,
        "head_only_failure_error": head_only_bad,
        "gemini_test_count": len(gemini),
        "gemini_outcomes": gemini,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not all(checks.values()):
        raise SystemExit("runtime base-to-head regression gate failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
