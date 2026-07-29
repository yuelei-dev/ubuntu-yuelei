#!/usr/bin/env python3
"""Run unittest suites and retain one structured record per test.

Runtime/test PRs use this for two different purposes:

* blocking runtime-baseline contracts;
* non-blocking diagnostics for the unmodified main-branch suite.

The latter intentionally exits successfully with ``--non-blocking`` while the
JSON still records every failure, error, skip and traceback.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def classify(test_id: str, detail: str) -> str:
    text = (test_id + "\n" + detail).lower()
    rules = (
        (("breakdown", "_reverse_frame_pair_ssim", "readiness"), "future_breakdown_contract"),
        (("imggen", "thinking_budget", "cleanup"), "future_imggen_contract"),
        (("zhipu", "_chat_multimodal"), "future_text_contract"),
        (("refund", "jobs_store", "refunded"), "jobs_refund_version_drift"),
        (("video", "seedance", "heygen"), "future_video_contract"),
        (("script", "ui", "html"), "frontend_contract_drift"),
        (("ship_", "systemd", "nginx"), "deployment_environment_contract"),
    )
    for needles, category in rules:
        if any(needle in text for needle in needles):
            return category
    return "other_contract_drift"


class JsonResult(unittest.TestResult):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[dict[str, object]] = []
        self._started: dict[int, float] = {}
        self._index: dict[int, int] = {}

    def startTest(self, test: unittest.case.TestCase) -> None:
        super().startTest(test)
        key = id(test)
        self._started[key] = time.perf_counter()
        self._index[key] = len(self.records)
        self.records.append(
            {
                "id": test.id(),
                "module": test.__class__.__module__,
                "class": test.__class__.__qualname__,
                "method": getattr(test, "_testMethodName", ""),
                "status": "running",
                "duration_seconds": 0.0,
                "traceback": "",
                "category": "",
            }
        )

    def stopTest(self, test: unittest.case.TestCase) -> None:
        key = id(test)
        record = self.records[self._index[key]]
        record["duration_seconds"] = round(
            time.perf_counter() - self._started[key], 6
        )
        if record["status"] == "running":
            record["status"] = "pass"
        super().stopTest(test)

    def _finish(
        self, test: unittest.case.TestCase, status: str, detail: str = ""
    ) -> None:
        record = self.records[self._index[id(test)]]
        record["status"] = status
        record["traceback"] = detail
        record["category"] = (
            classify(str(record["id"]), detail)
            if status in {"failure", "error", "unexpected_success"}
            else ""
        )

    def addSuccess(self, test: unittest.case.TestCase) -> None:
        self._finish(test, "pass")
        super().addSuccess(test)

    def addFailure(self, test: unittest.case.TestCase, err) -> None:
        self._finish(test, "failure", "".join(traceback.format_exception(*err)))
        super().addFailure(test, err)

    def addError(self, test: unittest.case.TestCase, err) -> None:
        self._finish(test, "error", "".join(traceback.format_exception(*err)))
        super().addError(test, err)

    def addSkip(self, test: unittest.case.TestCase, reason: str) -> None:
        self._finish(test, "skip", reason)
        super().addSkip(test, reason)

    def addExpectedFailure(self, test: unittest.case.TestCase, err) -> None:
        self._finish(
            test, "expected_failure", "".join(traceback.format_exception(*err))
        )
        super().addExpectedFailure(test, err)

    def addUnexpectedSuccess(self, test: unittest.case.TestCase) -> None:
        self._finish(test, "unexpected_success")
        super().addUnexpectedSuccess(test)

    def addSubTest(self, test, subtest, err) -> None:
        if err is not None:
            detail = "".join(traceback.format_exception(*err))
            status = (
                "failure"
                if issubclass(err[0], test.failureException)
                else "error"
            )
            record = self.records[self._index[id(test)]]
            prior = str(record["traceback"])
            record["status"] = status
            record["traceback"] = (
                prior + ("\n" if prior else "") + f"SUBTEST {subtest}:\n" + detail
            )
            record["category"] = classify(
                str(record["id"]), str(record["traceback"])
            )
        super().addSubTest(test, subtest, err)


def load_suite(args: argparse.Namespace) -> unittest.TestSuite:
    loader = unittest.defaultTestLoader
    if args.discover:
        suite = loader.discover(args.start_directory, pattern=args.pattern)
        if args.exclude_prefix:
            filtered = unittest.TestSuite()

            def collect(item):
                for child in item:
                    if isinstance(child, unittest.TestSuite):
                        collect(child)
                    elif not any(
                        child.id().startswith(prefix)
                        for prefix in args.exclude_prefix
                    ):
                        filtered.addTest(child)

            collect(suite)
            return filtered
        return suite
    names = list(args.tests)
    if args.targets_file:
        for raw in Path(args.targets_file).read_text(encoding="utf-8").splitlines():
            name = raw.strip()
            if name and not name.startswith("#"):
                names.append(name)
    if not names:
        raise SystemExit("provide test names, --targets-file, or --discover")
    return loader.loadTestsFromNames(names)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("tests", nargs="*")
    parser.add_argument("--targets-file")
    parser.add_argument("--discover", action="store_true")
    parser.add_argument("--start-directory", default="tests")
    parser.add_argument("--pattern", default="test*.py")
    parser.add_argument("--exclude-prefix", action="append", default=[])
    parser.add_argument("--output", required=True)
    parser.add_argument("--label", default="unittest")
    parser.add_argument("--non-blocking", action="store_true")
    args = parser.parse_args()

    suite = load_suite(args)
    started = time.perf_counter()
    result = JsonResult()
    suite.run(result)
    duration = round(time.perf_counter() - started, 6)
    counts: dict[str, int] = {}
    categories: dict[str, int] = {}
    for record in result.records:
        status = str(record["status"])
        counts[status] = counts.get(status, 0) + 1
        category = str(record["category"])
        if category:
            categories[category] = categories.get(category, 0) + 1
    payload = {
        "schema_version": 1,
        "label": args.label,
        "blocking": not args.non_blocking,
        "tests_run": result.testsRun,
        "duration_seconds": duration,
        "counts": counts,
        "failure_categories": categories,
        "successful": result.wasSuccessful(),
        "records": result.records,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps({key: value for key, value in payload.items() if key != "records"}))
    return 0 if result.wasSuccessful() or args.non_blocking else 1


if __name__ == "__main__":
    raise SystemExit(main())
