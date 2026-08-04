"""Durable cleanup outbox for temporary Gemini Files API resources.

Forward-ported from Tang's ``gemini_reverse.py`` cleanup lifecycle while
keeping Yue's existing reverse-analysis implementation unchanged.
"""

import hashlib
import json
import re
import threading
import time
from contextlib import closing


RETRY_DELAYS_SECONDS = (1.0, 3.0)
QUEUE_RETRY_BASE_SECONDS = 30
QUEUE_RETRY_MAX_SECONDS = 3600
QUEUE_MAX_ATTEMPTS = 12
QUEUE_RETENTION_SECONDS = 47 * 3600
QUEUE_LEASE_SECONDS = 60
QUEUE_SCAN_SECONDS = 30

_worker_lock = threading.Lock()
_worker_started = False


def _resource_name(value):
    name = str(value or "")
    if not re.fullmatch(
        r"files/[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?", name
    ):
        raise ValueError("Gemini Files API resource name is invalid")
    return name


def _safe_error(error):
    return type(error).__name__[:80]


def _audit(name, status, **fields):
    payload = {
        "resource_sha256": hashlib.sha256(
            str(name or "").encode("utf-8", "replace")
        ).hexdigest(),
        "status": status,
    }
    payload.update(fields)
    print(
        "[breakdown] gemini cleanup audit=%s"
        % json.dumps(payload, ensure_ascii=True),
        flush=True,
    )


def ensure_table(jdb, now=None):
    current = int(time.time() if now is None else now)
    with closing(jdb()) as connection:
        connection.execute("""CREATE TABLE IF NOT EXISTS gemini_file_cleanup_outbox(
            resource_name TEXT PRIMARY KEY,
            created_at INTEGER NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            next_retry_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            lease_until INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            updated_at INTEGER NOT NULL
        )""")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_gemini_cleanup_ready "
            "ON gemini_file_cleanup_outbox(status,next_retry_at,created_at)"
        )
        connection.execute(
            "UPDATE gemini_file_cleanup_outbox SET status='pending',"
            "lease_until=0,next_retry_at=?,updated_at=? "
            "WHERE status='deleting' AND lease_until<=?",
            (current, current, current),
        )
        connection.commit()


def persist(jdb, name, attempts, now=None):
    name = _resource_name(name)
    current = int(time.time() if now is None else now)
    ensure_table(jdb, now=current)
    with closing(jdb()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT OR IGNORE INTO gemini_file_cleanup_outbox("
            "resource_name,created_at,attempts,next_retry_at,expires_at,"
            "status,lease_until,last_error,updated_at) "
            "VALUES(?,?,?,?,?,'pending',0,'delete_failed',?)",
            (
                name,
                current,
                int(attempts),
                current + QUEUE_RETRY_BASE_SECONDS,
                current + QUEUE_RETENTION_SECONDS,
                current,
            ),
        )
        connection.execute(
            "UPDATE gemini_file_cleanup_outbox SET attempts=MAX(attempts,?),"
            "status='pending',lease_until=0,next_retry_at=MIN(next_retry_at,?),"
            "last_error='delete_failed',updated_at=? WHERE resource_name=?",
            (
                int(attempts),
                current + QUEUE_RETRY_BASE_SECONDS,
                current,
                name,
            ),
        )
        connection.commit()


def _claim(jdb, now=None):
    current = int(time.time() if now is None else now)
    ensure_table(jdb, now=current)
    with closing(jdb()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "DELETE FROM gemini_file_cleanup_outbox "
            "WHERE attempts>=? OR expires_at<=?",
            (QUEUE_MAX_ATTEMPTS, current),
        )
        row = connection.execute(
            "SELECT resource_name,created_at,attempts,expires_at "
            "FROM gemini_file_cleanup_outbox WHERE status='pending' "
            "AND next_retry_at<=? ORDER BY next_retry_at,created_at LIMIT 1",
            (current,),
        ).fetchone()
        claimed = None
        if row:
            name = row["resource_name"]
            cursor = connection.execute(
                "UPDATE gemini_file_cleanup_outbox SET status='deleting',"
                "attempts=attempts+1,lease_until=?,updated_at=? "
                "WHERE resource_name=? AND status='pending'",
                (current + QUEUE_LEASE_SECONDS, current, name),
            )
            if cursor.rowcount == 1:
                claimed = {
                    "resource_name": name,
                    "created_at": int(row["created_at"] or 0),
                    "attempts": int(row["attempts"] or 0) + 1,
                    "expires_at": int(row["expires_at"] or 0),
                }
        connection.commit()
    return claimed


def _complete(jdb, row, provider_result, now=None):
    current = int(time.time() if now is None else now)
    with closing(jdb()) as connection:
        connection.execute(
            "DELETE FROM gemini_file_cleanup_outbox "
            "WHERE resource_name=? AND status='deleting'",
            (row["resource_name"],),
        )
        connection.commit()
    _audit(
        row["resource_name"],
        "already_absent_by_recovery"
        if provider_result == "already_absent"
        else "deleted_by_recovery",
        attempts=row["attempts"],
        completed_at=current,
        cleanup_pending=False,
    )


def _reschedule(jdb, row, error, now=None):
    current = int(time.time() if now is None else now)
    attempts = int(row["attempts"] or 0)
    expired = attempts >= QUEUE_MAX_ATTEMPTS or current >= row["expires_at"]
    delay = min(
        QUEUE_RETRY_MAX_SECONDS,
        QUEUE_RETRY_BASE_SECONDS * (2 ** min(attempts, 10)),
    )
    with closing(jdb()) as connection:
        if expired:
            connection.execute(
                "DELETE FROM gemini_file_cleanup_outbox "
                "WHERE resource_name=? AND status='deleting'",
                (row["resource_name"],),
            )
        else:
            connection.execute(
                "UPDATE gemini_file_cleanup_outbox SET status='pending',"
                "lease_until=0,next_retry_at=?,last_error=?,updated_at=? "
                "WHERE resource_name=? AND status='deleting'",
                (
                    current + delay,
                    _safe_error(error),
                    current,
                    row["resource_name"],
                ),
            )
        connection.commit()
    _audit(
        row["resource_name"],
        "retry_window_exhausted" if expired else "recovery_retry_scheduled",
        attempts=attempts,
        cleanup_pending=not expired,
        error=_safe_error(error),
    )


def drain_once(jdb, delete_resource, now=None):
    row = _claim(jdb, now=now)
    if not row:
        return False
    try:
        result = delete_resource(row["resource_name"])
    except Exception as error:
        _reschedule(jdb, row, error, now=now)
    else:
        _complete(jdb, row, result, now=now)
    return True


def scanner(jdb, delete_resource, interval=QUEUE_SCAN_SECONDS):
    while True:
        try:
            while drain_once(jdb, delete_resource):
                pass
        except Exception as error:
            print(
                "[breakdown] gemini cleanup scanner error=%s"
                % _safe_error(error),
                flush=True,
            )
        time.sleep(interval)


def start_worker(jdb, delete_resource):
    global _worker_started
    ensure_table(jdb)
    with _worker_lock:
        if _worker_started:
            return False
        _worker_started = True
    try:
        threading.Thread(
            target=scanner,
            args=(jdb, delete_resource),
            name="gemini-file-cleanup-recover",
            daemon=True,
        ).start()
    except Exception:
        with _worker_lock:
            _worker_started = False
        raise
    return True


def delete_file(jdb, name, delete_resource, sleep=time.sleep):
    name = _resource_name(name)
    attempts = len(RETRY_DELAYS_SECONDS) + 1
    persisted = False
    for attempt in range(attempts):
        try:
            result = delete_resource(name)
            _audit(
                name,
                "already_absent" if result == "already_absent" else "deleted",
                attempt=attempt + 1,
                cleanup_pending=False,
            )
            return {"status": "deleted", "attempts": attempt + 1}
        except Exception as error:
            final = attempt + 1 == attempts
            retry_in = None if final else RETRY_DELAYS_SECONDS[attempt]
            if final:
                try:
                    persist(jdb, name, attempts)
                    persisted = True
                except Exception as persist_error:
                    _audit(
                        name,
                        "persistence_failed",
                        attempt=attempt + 1,
                        cleanup_pending=True,
                        error=_safe_error(persist_error),
                    )
            _audit(
                name,
                "pending_provider_cleanup",
                attempt=attempt + 1,
                cleanup_pending=True,
                persisted=persisted,
                retry_in_seconds=retry_in,
                error=_safe_error(error),
            )
            if not final:
                sleep(retry_in)
    return {
        "status": "pending_provider_cleanup",
        "attempts": attempts,
        "persisted": persisted,
    }
