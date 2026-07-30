#!/usr/bin/env python3
"""Enable the Seedance feature flag on the designated test server only."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import pathlib
import socket
import sqlite3


TEST_SERVER = "8.148.158.106"
TEST_HOSTNAME = "iZ7xv3be3lj4dxhvzegc5pZ"
FEATURE = "seedance_video"
DB_PATH = pathlib.Path("/home/ubuntu/content-api/feature_flags.db")
BACKUP_DIR = pathlib.Path("/home/ubuntu/backups")
ACTOR = "git-reviewed-test-seedance-enable"
REQUIRED_COLUMNS = {"feature", "enabled", "updated_by", "updated_at"}


def _verify_target(hostname: str, confirmation: str) -> None:
    if hostname != TEST_HOSTNAME:
        raise RuntimeError(
            "refusing to change feature flags outside the designated test host"
        )
    if confirmation != TEST_SERVER:
        raise RuntimeError("missing exact test-server confirmation")


def _backup_database(
    source: sqlite3.Connection,
    backup_path: pathlib.Path,
) -> None:
    if backup_path.exists():
        raise FileExistsError("refusing to overwrite an existing backup")
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(str(backup_path))) as destination:
        source.backup(destination)


def enable_seedance(
    db_path: pathlib.Path,
    backup_path: pathlib.Path,
    *,
    hostname: str,
    confirmation: str,
    now: int,
) -> dict:
    _verify_target(hostname, confirmation)
    if not db_path.is_file():
        raise FileNotFoundError("feature flag database does not exist")

    connection = sqlite3.connect(str(db_path), timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='feature_flags'"
        ).fetchone()
        if table is None:
            raise RuntimeError("feature_flags table is missing")
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(feature_flags)").fetchall()
        }
        if not REQUIRED_COLUMNS.issubset(columns):
            raise RuntimeError("feature_flags schema is incompatible")

        before = connection.execute(
            "SELECT enabled FROM feature_flags WHERE feature=?",
            (FEATURE,),
        ).fetchone()
        _backup_database(connection, backup_path)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """INSERT INTO feature_flags(feature, enabled, updated_by, updated_at)
               VALUES(?,?,?,?)
               ON CONFLICT(feature) DO UPDATE SET
                   enabled=excluded.enabled,
                   updated_by=excluded.updated_by,
                   updated_at=excluded.updated_at""",
            (FEATURE, 1, ACTOR, int(now)),
        )
        connection.commit()
        return {
            "feature": FEATURE,
            "enabled": True,
            "changed": before is None or not bool(before["enabled"]),
            "backup": str(backup_path),
            "cache_refresh_seconds": 5,
            "service_restart_required": False,
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Enable Seedance only on the designated Huangque test server."
    )
    parser.add_argument(
        "--confirm-test-server",
        required=True,
        help="Must exactly equal the designated test-server IP.",
    )
    args = parser.parse_args()
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    result = enable_seedance(
        DB_PATH,
        BACKUP_DIR / ("feature_flags.before-seedance-" + timestamp + ".db"),
        hostname=socket.gethostname(),
        confirmation=args.confirm_test_server,
        now=int(dt.datetime.now(dt.timezone.utc).timestamp()),
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
