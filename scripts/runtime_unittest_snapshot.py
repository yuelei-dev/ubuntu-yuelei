#!/usr/bin/env python3
"""Run the complete runtime suite and emit outcomes without hiding failures."""
import argparse
import io
import json
import os
import sys
import unittest
from pathlib import Path


class SnapshotResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.outcomes = {}

    def _put(self, test, outcome, detail=""):
        self.outcomes[test.id()] = {"outcome": outcome, "detail": detail[-4000:]}

    def addSuccess(self, test):
        super().addSuccess(test)
        self._put(test, "passed")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._put(test, "failed", self._exc_info_to_string(err, test))

    def addError(self, test, err):
        super().addError(test, err)
        self._put(test, "error", self._exc_info_to_string(err, test))

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._put(test, "skipped", str(reason))

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            outcome = "failed" if issubclass(err[0], test.failureException) else "error"
            self.outcomes[subtest.id()] = {
                "outcome": outcome,
                "detail": self._exc_info_to_string(err, test)[-4000:],
            }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    output = args.output.resolve()
    os.chdir(root)
    sys.path[:0] = [str(root), str(root / "server")]
    suite = unittest.defaultTestLoader.discover(str(root / "tests"), top_level_dir=str(root))
    result = unittest.TextTestRunner(
        stream=io.StringIO(), verbosity=0, resultclass=SnapshotResult,
    ).run(suite)
    payload = {
        "root": str(root),
        "tests_run": result.testsRun,
        "skipped": len(result.skipped),
        "failures": len(result.failures),
        "errors": len(result.errors),
        "outcomes": dict(sorted(result.outcomes.items())),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: payload[key] for key in ("tests_run", "skipped", "failures", "errors")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
