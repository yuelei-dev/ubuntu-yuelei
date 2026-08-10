import hashlib
import json
import re
import sqlite3
import time
from contextlib import closing

_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")

_ATTEMPT_COLUMNS = {
    "attempt_payload_json": "TEXT",
    "attempt_cost": "INTEGER",
    "charge_transaction_key": "TEXT",
    "attempt_state": "TEXT",
    "points_left": "INTEGER",
    "job_id": "INTEGER",
}


def _table_has_column(connection, name):
    return any(
        row[1] == name
        for row in connection.execute(
            "PRAGMA table_info(submission_idempotency)"
        ).fetchall()
    )


def _add_column_if_missing(connection, name, declaration):
    try:
        connection.execute(
            "ALTER TABLE submission_idempotency ADD COLUMN %s %s"
            % (name, declaration)
        )
    except sqlite3.OperationalError as error:
        # Another connection may have completed the same migration after our
        # schema snapshot. Only accept SQLite's duplicate-column error after
        # verifying that the required column is now present; all other errors
        # remain fatal.
        if (
            str(error).strip().lower()
            != ("duplicate column name: %s" % name).lower()
            or not _table_has_column(connection, name)
        ):
            raise


def ensure_table(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS submission_idempotency(
        username TEXT NOT NULL, endpoint TEXT NOT NULL, idem_key TEXT NOT NULL,
        request_hash TEXT NOT NULL, response_json TEXT, created_at INTEGER, updated_at INTEGER,
        PRIMARY KEY(username, endpoint, idem_key))""")
    existing = {
        row[1] for row in connection.execute(
            "PRAGMA table_info(submission_idempotency)"
        ).fetchall()
    }
    for name, declaration in _ATTEMPT_COLUMNS.items():
        if name not in existing:
            _add_column_if_missing(connection, name, declaration)
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_submission_idempotency_job "
        "ON submission_idempotency(job_id) WHERE job_id IS NOT NULL"
    )

def clean_key(raw):
    key = str(raw or "").strip()
    if key and not _KEY_RE.fullmatch(key):
        raise ValueError("Idempotency-Key 需为 8-128 位字母、数字或 . _ : -")
    return key

def _request_hash(body):
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

def lookup(db_factory, username, endpoint, key, body):
    """Inspect an existing claim without creating one.

    This lets callers replay a completed request before running versioned
    server-side derivation such as the smart-montage planner.
    """
    if not key:
        return "disabled", None
    digest = _request_hash(body)
    with closing(db_factory()) as connection:
        ensure_table(connection)
        connection.commit()
        row = connection.execute(
            "SELECT request_hash,response_json FROM submission_idempotency "
            "WHERE username=? AND endpoint=? AND idem_key=?",
            (username, endpoint, key)).fetchone()
        if not row:
            return "missing", None
        if row["request_hash"] != digest:
            return "conflict", None
        return ("replay", json.loads(row["response_json"])) if row["response_json"] else ("processing", None)

def begin(db_factory, username, endpoint, key, body):
    if not key:
        return "disabled", None
    digest, now = _request_hash(body), int(time.time())
    with closing(db_factory()) as connection:
        ensure_table(connection)
        inserted = connection.execute(
            "INSERT OR IGNORE INTO submission_idempotency"
            "(username,endpoint,idem_key,request_hash,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (username, endpoint, key, digest, now, now)).rowcount
        connection.commit()
        if inserted == 1:
            return "new", None
        row = connection.execute(
            "SELECT request_hash,response_json FROM submission_idempotency "
            "WHERE username=? AND endpoint=? AND idem_key=?",
            (username, endpoint, key)).fetchone()
        if not row:
            raise RuntimeError("idempotency claim disappeared")
        if row["request_hash"] != digest:
            return "conflict", None
        return ("replay", json.loads(row["response_json"])) if row["response_json"] else ("processing", None)


def _attempt_from_row(row):
    if not row or not row["attempt_payload_json"]:
        return None
    payload = json.loads(row["attempt_payload_json"])
    if not isinstance(payload, dict):
        raise RuntimeError("idempotency attempt payload is invalid")
    return {
        "payload": payload,
        "cost": int(row["attempt_cost"] or 0),
        "charge_transaction_key": str(row["charge_transaction_key"] or ""),
        "state": str(row["attempt_state"] or "frozen"),
        "points_left": (
            int(row["points_left"]) if row["points_left"] is not None else None
        ),
        "job_id": int(row["job_id"]) if row["job_id"] is not None else None,
    }


def load_attempt(db_factory, username, endpoint, key, body):
    """Load a durable paid-submission attempt after verifying its request hash."""
    if not key:
        return None
    digest = _request_hash(body)
    with closing(db_factory()) as connection:
        ensure_table(connection)
        connection.commit()
        row = connection.execute(
            "SELECT request_hash,attempt_payload_json,attempt_cost,"
            "charge_transaction_key,attempt_state,points_left,job_id "
            "FROM submission_idempotency WHERE username=? AND endpoint=? AND idem_key=?",
            (username, endpoint, key),
        ).fetchone()
    if not row:
        return None
    if row["request_hash"] != digest:
        raise ValueError("idempotency request hash conflict")
    return _attempt_from_row(row)


def begin_attempt(db_factory, username, endpoint, key, body, payload, cost,
                  charge_transaction_key):
    """Atomically create a processing claim with its frozen paid payload.

    Smart-montage material files are frozen before this call.  Consequently a
    committed processing row is always self-contained and can be resumed after
    the short-lived upload IDs expire.
    """
    if not key:
        raise ValueError("durable attempt requires an idempotency key")
    if not isinstance(payload, dict):
        raise ValueError("durable attempt payload must be an object")
    cost = int(cost or 0)
    if cost < 0:
        raise ValueError("durable attempt cost is invalid")
    transaction_key = str(charge_transaction_key or "").strip()
    if not transaction_key or len(transaction_key) > 160:
        raise ValueError("durable attempt charge key is invalid")
    digest, now = _request_hash(body), int(time.time())
    payload_json = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    with closing(db_factory()) as connection:
        ensure_table(connection)
        inserted = connection.execute(
            "INSERT OR IGNORE INTO submission_idempotency"
            "(username,endpoint,idem_key,request_hash,created_at,updated_at,"
            "attempt_payload_json,attempt_cost,charge_transaction_key,attempt_state) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (username, endpoint, key, digest, now, now, payload_json, cost,
             transaction_key, "frozen"),
        ).rowcount
        connection.commit()
        row = connection.execute(
            "SELECT request_hash,response_json,attempt_payload_json,attempt_cost,"
            "charge_transaction_key,attempt_state,points_left,job_id "
            "FROM submission_idempotency WHERE username=? AND endpoint=? AND idem_key=?",
            (username, endpoint, key),
        ).fetchone()
    if not row:
        raise RuntimeError("idempotency attempt disappeared")
    if row["request_hash"] != digest:
        return "conflict", None
    if row["response_json"]:
        return "replay", json.loads(row["response_json"])
    attempt = _attempt_from_row(row)
    if attempt is None:
        raise RuntimeError("processing idempotency claim has no durable attempt")
    return ("new" if inserted == 1 else "processing"), attempt


def mark_charged(db_factory, username, endpoint, key, charge_transaction_key,
                 points_left):
    """Persist Auth confirmation before the local job transaction starts."""
    now = int(time.time())
    points_left = int(points_left)
    with closing(db_factory()) as connection:
        ensure_table(connection)
        cursor = connection.execute(
            "UPDATE submission_idempotency SET attempt_state='charged',"
            "points_left=?,updated_at=? WHERE username=? AND endpoint=? AND idem_key=? "
            "AND response_json IS NULL AND charge_transaction_key=? "
            "AND attempt_payload_json IS NOT NULL AND attempt_state IN ('frozen','charged')",
            (points_left, now, username, endpoint, key, charge_transaction_key),
        )
        connection.commit()
        if cursor.rowcount == 1:
            return True
        row = connection.execute(
            "SELECT attempt_state,points_left,charge_transaction_key FROM "
            "submission_idempotency WHERE username=? AND endpoint=? AND idem_key=?",
            (username, endpoint, key),
        ).fetchone()
    if (row and row["attempt_state"] in {"charged", "linked"}
            and row["charge_transaction_key"] == charge_transaction_key
            and int(row["points_left"]) == points_left):
        return True
    raise RuntimeError("durable charge confirmation could not be recorded")


def link_job(connection, username, endpoint, key, charge_transaction_key,
             job_id, points_left):
    """Bind the paid attempt to its job in the same SQLite transaction."""
    ensure_table(connection)
    job_id, points_left, now = int(job_id), int(points_left), int(time.time())
    cursor = connection.execute(
        "UPDATE submission_idempotency SET attempt_state='linked',job_id=?,"
        "points_left=?,updated_at=? WHERE username=? AND endpoint=? AND idem_key=? "
        "AND response_json IS NULL AND charge_transaction_key=? "
        "AND attempt_payload_json IS NOT NULL AND attempt_state IN ('charged','linked') "
        "AND (job_id IS NULL OR job_id=?)",
        (job_id, points_left, now, username, endpoint, key,
         charge_transaction_key, job_id),
    )
    if cursor.rowcount == 1:
        return True
    row = connection.execute(
        "SELECT attempt_state,job_id,points_left,charge_transaction_key FROM "
        "submission_idempotency WHERE username=? AND endpoint=? AND idem_key=?",
        (username, endpoint, key),
    ).fetchone()
    if (row and row["attempt_state"] == "linked" and int(row["job_id"]) == job_id
            and int(row["points_left"]) == points_left
            and row["charge_transaction_key"] == charge_transaction_key):
        return True
    raise RuntimeError("durable paid job could not be linked")

def complete(db_factory, username, endpoint, key, response):
    if key:
        with closing(db_factory()) as connection:
            connection.execute(
                "UPDATE submission_idempotency SET response_json=?,updated_at=? WHERE username=? AND endpoint=? AND idem_key=?",
                (json.dumps(response, ensure_ascii=False), int(time.time()), username, endpoint, key))
            connection.commit()

def abort(db_factory, username, endpoint, key):
    if key:
        with closing(db_factory()) as connection:
            connection.execute(
                "DELETE FROM submission_idempotency WHERE username=? AND endpoint=? AND idem_key=? "
                "AND response_json IS NULL AND "
                "(attempt_payload_json IS NULL OR attempt_state='frozen')",
                (username, endpoint, key))
            connection.commit()
