# -*- coding: utf-8 -*-
"""Stream local image/video uploads into the standard paid breakdown job flow."""
import hashlib
import json
import pathlib
import subprocess
import time
import urllib.parse
import uuid
from contextlib import closing

IMAGE_LIMIT = 20 * 1024 * 1024
VIDEO_LIMIT = 200 * 1024 * 1024
VIDEO_DURATION_LIMIT = 120.0
UPLOAD_COST = 20
UPLOAD_ENDPOINT = "/api/gen/breakdown/local-upload"
_PAYMENT_FIELD = "_local_upload_payment"
_REFUND_PROCESS_ID = uuid.uuid4().hex
_REFUND_CLAIM_TTL = 60
_IMAGE_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
_VIDEO_EXT = {"video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm"}

def _remove(path):
    try: pathlib.Path(path).unlink()
    except (FileNotFoundError, OSError): pass

def _upload_root(out_dir):
    root = (pathlib.Path(out_dir) / "_breakdown_uploads").resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root

def _remove_binding(jdb, out_dir, token, username, job_id):
    """Delete one exact DB binding and only its deterministic owned file."""
    from . import breakdown
    root = _upload_root(out_dir)
    suffix = ""
    with closing(jdb()) as connection:
        breakdown._ensure_upload_table(connection)
        row = connection.execute(
            "SELECT suffix FROM breakdown_uploads"
            " WHERE token=? AND username=? AND job_id=?",
            (token, username, int(job_id)),
        ).fetchone()
        if row:
            suffix = str(row["suffix"] or "")
            connection.execute(
                "DELETE FROM breakdown_uploads"
                " WHERE token=? AND username=? AND job_id=?",
                (token, username, int(job_id)),
            )
        connection.commit()
    if suffix:
        candidate = (root / (token + suffix)).resolve()
        if candidate.parent == root:
            _remove(candidate)
    return bool(row)

def _clear_idempotency(jdb, username, key):
    if not key:
        return
    with closing(jdb()) as connection:
        connection.execute(
            "DELETE FROM submission_idempotency"
            " WHERE username=? AND endpoint=? AND idem_key=?",
            (username, UPLOAD_ENDPOINT, key),
        )
        connection.commit()

def _job_payload(raw):
    try:
        payload = json.loads(raw or "{}")
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}

def _set_payment_state(jdb, job_id, state, **fields):
    """Persist the local-upload payment intent before any terminal transition."""
    with closing(jdb()) as connection:
        row = connection.execute(
            "SELECT payload FROM jobs WHERE id=?", (int(job_id),)
        ).fetchone()
        if not row:
            return False
        payload = _job_payload(row["payload"])
        payment = payload.get(_PAYMENT_FIELD)
        payment = dict(payment) if isinstance(payment, dict) else {}
        payment["state"] = str(state)
        payment.update(fields)
        payload[_PAYMENT_FIELD] = payment
        updated = connection.execute(
            "UPDATE jobs SET payload=?,updated_at=? WHERE id=?",
            (
                json.dumps(payload, ensure_ascii=False),
                int(time.time()),
                int(job_id),
            ),
        )
        connection.commit()
        return updated.rowcount == 1

def _find_idempotent_job(jdb, username, key):
    if not key:
        return None
    with closing(jdb()) as connection:
        row = connection.execute(
            "SELECT id,username,cost,status,payload,error,owner,"
            " COALESCE(refunded,0) AS refunded FROM jobs"
            " WHERE username=? AND kind='breakdown'"
            " AND json_extract("
            " CASE WHEN json_valid(payload) THEN payload ELSE '{}' END,"
            " '$._local_upload_payment.idempotency_key')=?"
            " ORDER BY id DESC LIMIT 1",
            (username, key),
        ).fetchone()
    return row

def _payment_keys(job_id, upload_token=""):
    job_id = int(job_id)
    nonce = str(upload_token or "")[:16]
    suffix = ("-%s" % nonce) if nonce else ""
    return (
        "breakdown-local-charge-%d%s" % (job_id, suffix),
        "breakdown-local-refund-%d%s" % (job_id, suffix),
    )

def _payment_for_job(jdb, job_id):
    with closing(jdb()) as connection:
        row = connection.execute(
            "SELECT payload FROM jobs WHERE id=?", (int(job_id),)
        ).fetchone()
    if not row:
        return {}
    payment = _job_payload(row["payload"]).get(_PAYMENT_FIELD)
    return dict(payment) if isinstance(payment, dict) else {}

def _refund_job_once(jdb, jobs_store, points_domain, job_id, username, cost):
    with closing(jdb()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT status,payload,COALESCE(refunded,0) AS refunded"
            " FROM jobs WHERE id=?",
            (int(job_id),),
        ).fetchone()
        if not row or str(row["status"]) != "error":
            connection.commit()
            return False
        payload = _job_payload(row["payload"])
        payment = payload.get(_PAYMENT_FIELD)
        payment = dict(payment) if isinstance(payment, dict) else {}
        refunded_state = int(row["refunded"] or 0)
        if payment.get("state") != "refund_pending" or refunded_state == 1:
            connection.commit()
            return False
        attempt_owner = str(payment.get("refund_attempt_owner") or "")
        attempt_at = int(payment.get("refund_attempt_at") or 0)
        now = int(time.time())
        if refunded_state == 0 and attempt_owner:
            claim_is_live = (
                attempt_owner == _REFUND_PROCESS_ID
                or now - attempt_at < _REFUND_CLAIM_TTL
            )
            if claim_is_live:
                connection.commit()
                return False
        if refunded_state not in {0, 2}:
            connection.commit()
            return False
        payment["refund_attempt_owner"] = _REFUND_PROCESS_ID
        payment["refund_attempt_at"] = now
        payload[_PAYMENT_FIELD] = payment
        claimed = connection.execute(
            "UPDATE jobs SET refunded=0,payload=?,updated_at=?"
            " WHERE id=? AND status='error' AND COALESCE(refunded,0)=?",
            (
                json.dumps(payload, ensure_ascii=False),
                now,
                int(job_id),
                refunded_state,
            ),
        )
        connection.commit()
        if claimed.rowcount != 1:
            return False

    refund_key = payment.get("refund_transaction_key") or _payment_keys(job_id)[1]
    def refund(target_username, amount):
        try:
            points_domain.refund_points(
                target_username, amount, "job#%d" % int(job_id),
                transaction_key=refund_key,
            )
            return True
        except Exception:
            return False
    refunded = jobs_store.refund_once(
        jdb, int(job_id), username, int(cost), refund
    )
    if refunded:
        _set_payment_state(
            jdb, job_id, "refunded",
            refund_attempt_owner="", refund_attempt_at=0,
        )
    else:
        with closing(jdb()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload,COALESCE(refunded,0) AS refunded"
                " FROM jobs WHERE id=? AND status='error'",
                (int(job_id),),
            ).fetchone()
            if row and int(row["refunded"] or 0) == 0:
                payload = _job_payload(row["payload"])
                payment = payload.get(_PAYMENT_FIELD)
                payment = dict(payment) if isinstance(payment, dict) else {}
                if (
                    payment.get("state") == "refund_pending"
                    and payment.get("refund_attempt_owner") == _REFUND_PROCESS_ID
                ):
                    payment["refund_attempt_owner"] = ""
                    payment["refund_attempt_at"] = 0
                    payload[_PAYMENT_FIELD] = payment
                    connection.execute(
                        "UPDATE jobs SET refunded=2,payload=?,updated_at=?"
                        " WHERE id=? AND status='error'"
                        " AND COALESCE(refunded,0)=0",
                        (
                            json.dumps(payload, ensure_ascii=False),
                            int(time.time()),
                            int(job_id),
                        ),
                    )
            connection.commit()
    return refunded

def reconcile_pending_refunds(jdb, jobs_store, points_domain, limit=50):
    """Retry only durable, paid local-upload refund intents."""
    remaining = max(1, int(limit))
    cursor = 0
    reconciled = 0
    while remaining > 0:
        page_size = min(25, remaining)
        with closing(jdb()) as connection:
            rows = connection.execute(
                "SELECT id,username,cost,payload FROM jobs"
                " WHERE id>? AND kind='breakdown' AND status='error'"
                " AND COALESCE(refunded,0) IN (0,2)"
                " AND json_extract("
                " CASE WHEN json_valid(payload) THEN payload ELSE '{}' END,"
                " '$._local_upload_payment.state')='refund_pending'"
                " ORDER BY id ASC LIMIT ?",
                (cursor, page_size),
            ).fetchall()
        if not rows:
            break
        for row in rows:
            cursor = int(row["id"])
            payment = _job_payload(row["payload"]).get(_PAYMENT_FIELD)
            if _refund_job_once(
                jdb, jobs_store, points_domain, row["id"], row["username"],
                row["cost"],
            ):
                _clear_idempotency(
                    jdb, row["username"], payment.get("idempotency_key")
                )
                reconciled += 1
        remaining -= len(rows)
    return reconciled

def _reject_reserved_job(reject_pending_job, jdb, out_dir, token, username,
                         job_id, idem_key, reason, payment_state):
    _set_payment_state(jdb, job_id, payment_state)
    # cost=0 makes _reject_pending_job perform only the pending->error CAS.
    # The exact paid refund is handled below with jobs_store.refund_once so a
    # failed refund leaves refunded=0 for reconciliation.
    rejected = reject_pending_job(job_id, username, 0, reason)
    _remove_binding(jdb, out_dir, token, username, job_id)
    return rejected

def _mark_refund_pending_error(jdb, job_id, reason):
    """Atomically publish the error terminal state and its durable refund intent."""
    with closing(jdb()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT status,payload,COALESCE(refunded,0) AS refunded"
            " FROM jobs WHERE id=?",
            (int(job_id),),
        ).fetchone()
        if not row:
            connection.commit()
            return False
        payload = _job_payload(row["payload"])
        payment = payload.get(_PAYMENT_FIELD)
        payment = dict(payment) if isinstance(payment, dict) else {}
        if str(row["status"]) == "pending":
            payment["state"] = "refund_pending"
            payment["refund_attempt_owner"] = ""
            payment["refund_attempt_at"] = 0
            payload[_PAYMENT_FIELD] = payment
            updated = connection.execute(
                "UPDATE jobs SET status='error',error=?,payload=?,"
                " refunded=2,updated_at=?"
                " WHERE id=? AND status='pending'",
                (
                    str(reason)[:1000],
                    json.dumps(payload, ensure_ascii=False),
                    int(time.time()),
                    int(job_id),
                ),
            )
            connection.commit()
            return updated.rowcount == 1
        already_pending = (
            str(row["status"]) in {"error", "failed"}
            and payment.get("state") == "refund_pending"
            and int(row["refunded"] or 0) in {0, 2}
        )
        connection.commit()
        return already_pending

def _reject_paid_job(reject_pending_job, jdb, jobs_store, points_domain,
                     out_dir, token, username, job_id, idem_key, reason):
    rejected = _mark_refund_pending_error(jdb, job_id, reason)
    _remove_binding(jdb, out_dir, token, username, job_id)
    refunded = _refund_job_once(
        jdb, jobs_store, points_domain, job_id, username, UPLOAD_COST
    )
    if refunded:
        _clear_idempotency(jdb, username, idem_key)
    return rejected, refunded

def _activate_reserved_job(jdb, job_id, reserved_owner, service_owner, payload,
                           username, idem_key, response):
    now = int(time.time())
    with closing(jdb()) as connection:
        activated = connection.execute(
            "UPDATE jobs SET payload=?,updated_at=?,owner=?"
            " WHERE id=? AND status='pending' AND owner=?",
            (
                json.dumps(payload, ensure_ascii=False), now, service_owner,
                int(job_id), reserved_owner,
            ),
        )
        if activated.rowcount != 1:
            raise RuntimeError("上传任务激活失败")
        if idem_key:
            updated = connection.execute(
                "UPDATE submission_idempotency"
                " SET response_json=?,updated_at=?"
                " WHERE username=? AND endpoint=? AND idem_key=?"
                " AND response_json IS NULL",
                (
                    json.dumps(response, ensure_ascii=False), now,
                    username, UPLOAD_ENDPOINT, idem_key,
                ),
            )
            if updated.rowcount != 1:
                raise RuntimeError("上传幂等记录已失效")
        connection.commit()

def _binding_is_readable(jdb, out_dir, job_id, username, token):
    from . import breakdown
    root = _upload_root(out_dir)
    with closing(jdb()) as connection:
        breakdown._ensure_upload_table(connection)
        row = connection.execute(
            "SELECT suffix,path FROM breakdown_uploads"
            " WHERE job_id=? AND username=? AND token=?",
            (int(job_id), username, token),
        ).fetchone()
    if not row:
        return False
    expected = (root / (str(token) + str(row["suffix"] or ""))).resolve()
    recorded = pathlib.Path(str(row["path"] or "")).resolve()
    return expected.parent == root and recorded == expected and expected.is_file()

def _continue_reserved_job(
    jdb, jobs_store, points_domain, enqueue_job, reject_pending_job,
    service_owner, out_dir, row, username, idem_key,
):
    """Replay the stable charge, then activate/enqueue or compensate exactly once."""
    job_id = int(row["id"])
    payload = _job_payload(row["payload"])
    payment = payload.get(_PAYMENT_FIELD)
    payment = dict(payment) if isinstance(payment, dict) else {}
    token = str(payload.get("upload_token") or "")
    reserved_owner = "%s:local-upload-reserved" % service_owner
    if payment.get("idempotency_key") != idem_key or row["username"] != username:
        return 404, {"detail": "未找到对应上传任务", "code": "idempotency_not_found"}
    if str(row["status"]) in {"error", "failed"}:
        refund_confirmed = int(row["refunded"] or 0) == 1
        if payment.get("state") == "refund_pending" and not refund_confirmed:
            _refund_job_once(
                jdb, jobs_store, points_domain, job_id, username, row["cost"],
            )
            refreshed = _find_idempotent_job(jdb, username, idem_key)
            if refreshed is not None:
                row = refreshed
                payload = _job_payload(row["payload"])
                payment = payload.get(_PAYMENT_FIELD)
                payment = dict(payment) if isinstance(payment, dict) else {}
                refund_confirmed = int(row["refunded"] or 0) == 1
        if payment.get("state") == "refund_pending" and not refund_confirmed:
            return 202, {
                "job_id": job_id,
                "detail": "退款结果正在确认，请稍后继续查询",
                "code": "idempotency_in_progress",
                "payment_state": "refund_pending",
                "retry_after_ms": 1000,
            }
        return 200, {
            "job_id": job_id, "status": "error",
            "error": str(row["error"] or "上传任务失败"),
            "refunded": refund_confirmed,
            "payment_state": payment.get("state") or "",
        }
    if str(row["status"]) in {"running", "done"}:
        return 200, {"job_id": job_id, "status": str(row["status"])}
    if row["owner"] == service_owner:
        if not enqueue_job(job_id, "breakdown", "local_reverse"):
            _reject_paid_job(
                reject_pending_job, jdb, jobs_store, points_domain, out_dir,
                token, username, job_id, idem_key,
                "任务队列已满，请稍后再试",
            )
            return 429, {
                "detail": "任务队列已满，请稍后再试",
                "code": "queue_full", "retry_after_ms": 4000,
            }
        return 200, {
            "job_id": job_id, "cost": UPLOAD_COST,
            "points_left": int(points_domain.get_points(username) or 0),
        }
    if row["owner"] != reserved_owner:
        return 409, {
            "detail": "上传任务状态冲突", "code": "idempotency_conflict",
        }

    charge_key = payment.get("charge_transaction_key") or _payment_keys(job_id, token)[0]
    refund_key = payment.get("refund_transaction_key") or _payment_keys(job_id, token)[1]
    try:
        points_left = points_domain.deduct_points(
            username, UPLOAD_COST, "job:breakdown",
            transaction_key=charge_key,
        )
    except Exception as error:
        status = int(getattr(error, "status", 500) or 500)
        if 400 <= status < 500:
            _reject_reserved_job(
                reject_pending_job, jdb, out_dir, token, username, job_id,
                idem_key, "上传任务扣点被拒绝", "charge_rejected",
            )
            _clear_idempotency(jdb, username, idem_key)
            response = {
                "detail": getattr(error, "detail", "上传任务扣点被拒绝"),
            }
            if status == 402:
                response["need"] = UPLOAD_COST
            return status, response
        return 202, {
            "detail": "扣点结果正在确认，请稍后继续查询",
            "code": "idempotency_in_progress",
            "retry_after_ms": 1000,
            "job_id": job_id,
        }

    payload[_PAYMENT_FIELD] = {
        **payment,
        "state": "charged",
        "idempotency_key": idem_key,
        "charge_transaction_key": charge_key,
        "refund_transaction_key": refund_key,
    }
    try:
        if not _set_payment_state(
            jdb, job_id, "charged",
            charge_transaction_key=charge_key,
            refund_transaction_key=refund_key,
        ):
            raise RuntimeError("上传任务支付状态不存在")
        if not _binding_is_readable(jdb, out_dir, job_id, username, token):
            _reject_paid_job(
                reject_pending_job, jdb, jobs_store, points_domain, out_dir,
                token, username, job_id, idem_key, "上传文件已丢失",
            )
            return 500, {"detail": "上传文件已丢失，点数将自动退回"}
        payload[_PAYMENT_FIELD]["state"] = "paid"
        response = {
            "job_id": job_id, "cost": UPLOAD_COST,
            "points_left": points_left,
        }
        _activate_reserved_job(
            jdb, job_id, reserved_owner, service_owner, payload,
            username, idem_key, response,
        )
        if not enqueue_job(job_id, "breakdown", "local_reverse"):
            _reject_paid_job(
                reject_pending_job, jdb, jobs_store, points_domain, out_dir,
                token, username, job_id, idem_key,
                "任务队列已满，请稍后再试",
            )
            return 429, {
                "detail": "任务队列已满，请稍后再试",
                "code": "queue_full", "retry_after_ms": 4000,
            }
        return 200, response
    except Exception:
        _reject_paid_job(
            reject_pending_job, jdb, jobs_store, points_domain, out_dir,
            token, username, job_id, idem_key, "上传任务入队失败",
        )
        return 500, {"detail": "上传任务创建失败，请重试"}

def _exact_status(
    jdb, jobs_store, points_domain, enqueue_job, reject_pending_job,
    service_owner, out_dir, username, idem_key,
):
    from . import submission_idempotency
    row = _find_idempotent_job(jdb, username, idem_key)
    if row:
        return _continue_reserved_job(
            jdb, jobs_store, points_domain, enqueue_job, reject_pending_job,
            service_owner, out_dir, row, username, idem_key,
        )
    with closing(jdb()) as connection:
        submission_idempotency.ensure_table(connection)
        idem = connection.execute(
            "SELECT response_json FROM submission_idempotency"
            " WHERE username=? AND endpoint=? AND idem_key=?",
            (username, UPLOAD_ENDPOINT, idem_key),
        ).fetchone()
    if not idem:
        return 404, {
            "detail": "未找到对应提交凭证", "code": "idempotency_not_found",
        }
    return 202, {
        "detail": "上传请求正在建立任务",
        "code": "idempotency_in_progress", "retry_after_ms": 1000,
    }

def cleanup_stale_uploads(jdb, out_dir):
    """Reap terminal/orphaned/missing upload bindings without touching live jobs."""
    from . import breakdown
    root = _upload_root(out_dir)
    stale = []
    with closing(jdb()) as connection:
        breakdown._ensure_upload_table(connection)
        rows = connection.execute(
            "SELECT b.token,b.username,b.suffix,b.job_id,j.status"
            " FROM breakdown_uploads b LEFT JOIN jobs j ON j.id=b.job_id"
            " ORDER BY b.created_at LIMIT 200"
        ).fetchall()
        for row in rows:
            candidate = (root / (str(row["token"]) + str(row["suffix"]))).resolve()
            terminal = row["status"] is None or str(row["status"]) in {
                "done", "error", "failed",
            }
            if terminal or candidate.parent != root or not candidate.is_file():
                stale.append((
                    str(row["token"]), str(row["username"]), int(row["job_id"]),
                    candidate if candidate.parent == root else None,
                ))
        for token, username, job_id, _ in stale:
            connection.execute(
                "DELETE FROM breakdown_uploads"
                " WHERE token=? AND username=? AND job_id=?",
                (token, username, job_id),
            )
        connection.commit()
    for _, _, _, candidate in stale:
        if candidate is not None:
            _remove(candidate)
    return len(stale)

def _safe_title(raw):
    normalized = urllib.parse.unquote(str(raw or "本地素材")).replace("\\", "/")
    title = pathlib.PurePosixPath(normalized).name
    return title[:120] or "本地素材"

def _image_type(path):
    with open(path, "rb") as source: head = source.read(16)
    if head.startswith(b"\xff\xd8\xff"): return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"): return "image/png"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP": return "image/webp"
    raise ValueError("图片格式不受支持，请上传 JPG、PNG 或 WEBP")

def _video_duration(path):
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "json", str(path)], check=True, timeout=20,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        duration = float((json.loads(result.stdout or b"{}").get("format") or {}).get("duration") or 0)
    except Exception:
        raise ValueError("无法读取视频，请上传完整的 MP4、MOV 或 WEBM 文件")
    if duration <= 0: raise ValueError("无法读取视频时长")
    if duration > VIDEO_DURATION_LIMIT + 0.05: raise ValueError("视频最长支持 2 分钟")
    return round(duration, 3)

def _stream_body(handler, destination, expected_size):
    remaining = expected_size
    digest = hashlib.sha256()
    with open(destination, "xb") as target:
        while remaining:
            chunk = handler.rfile.read(min(1024 * 1024, remaining))
            if not chunk: raise ValueError("文件读取不完整，请重新选择文件上传")
            target.write(chunk); digest.update(chunk); remaining -= len(chunk)
    if pathlib.Path(destination).stat().st_size != expected_size:
        raise ValueError("文件读取不完整，请重新选择文件上传")
    return digest.hexdigest()

def handle_post(handler, *, verify, points_domain, jdb, jobs_store, enqueue_job,
                reject_pending_job, service_owner, out_dir, is_shutting_down,
                user_active_job_count, max_user_active_jobs, must_change_password):
    from . import breakdown, submission_idempotency

    user = verify(handler._token())
    if not user: return handler._send(401, {"detail": "未登录或登录已过期"})
    if must_change_password(user):
        return handler._send(403, {"detail": "请先修改初始密码"})
    from . import feature_flags
    try: feature_flags.require_enabled("breakdown")
    except feature_flags.FeatureDisabled as error: return handler._send(503, {"detail": str(error)})
    if is_shutting_down():
        return handler._send(503, {"detail": "服务正在更新，请稍后重试（未扣点）",
                                   "code": "shutting_down", "retry_after_ms": 5000})
    try:
        idem_key = submission_idempotency.clean_key(
            handler.headers.get("Idempotency-Key")
        )
    except ValueError as error:
        return handler._send(400, {"detail": str(error)})
    try:
        cleanup_stale_uploads(jdb, out_dir)
    except Exception:
        pass
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(handler.path).query)
    action = str((query.get("action") or [""])[0]).lower()
    if action == "status":
        if not idem_key:
            return handler._send(400, {"detail": "缺少 Idempotency-Key"})
        status, response = _exact_status(
            jdb, jobs_store, points_domain, enqueue_job, reject_pending_job,
            service_owner, out_dir, user["username"], idem_key,
        )
        return handler._send(status, response)
    media_type = str((query.get("media_type") or [""])[0]).lower()
    content_type = str(handler.headers.get("Content-Type") or "").split(";", 1)[0].lower()
    allowed = _IMAGE_EXT if media_type == "image" else _VIDEO_EXT if media_type == "video" else {}
    if not allowed or content_type not in allowed:
        return handler._send(400, {"detail": "请选择受支持的本地图片或视频"})
    limit = IMAGE_LIMIT if media_type == "image" else VIDEO_LIMIT
    try: size = int(handler.headers.get("Content-Length") or 0)
    except (TypeError, ValueError): size = 0
    if size <= 0: return handler._send(400, {"detail": "上传文件为空"})
    if size > limit:
        label = "20MB" if media_type == "image" else "200MB"
        return handler._send(413, {"detail": "%s不能超过 %s" % ("图片" if media_type == "image" else "视频", label)})
    points = int(points_domain.get_points(user["username"]) or 0)
    if points < UPLOAD_COST:
        return handler._send(402, {"detail": "点数不足", "need": UPLOAD_COST, "points": points})
    active = int(user_active_job_count(user["username"]) or 0)
    if active >= max_user_active_jobs:
        return handler._send(429, {"detail": "您有 %d 个任务正在排队/生成，完成后再提交" % active,
            "code": "active_job_cap", "active_jobs": active, "max_active_jobs": max_user_active_jobs,
            "retry_after_ms": 4000})
    upload_dir = _upload_root(out_dir)
    title = _safe_title(handler.headers.get("X-File-Name"))
    upload_token = uuid.uuid4().hex
    suffix = allowed[content_type]
    path = (upload_dir / (upload_token + suffix)).resolve()
    job_id = None
    job_reserved = False
    idem_started = False
    try:
        digest = _stream_body(handler, path, size)
        if media_type == "image":
            if _image_type(path) != content_type: raise ValueError("图片内容与文件格式不一致")
            duration = 0
        else: duration = _video_duration(path)
        idem_body = {
            "media_type": media_type,
            "content_type": content_type,
            "size": size,
            "title": title,
            "sha256": digest,
        }
        idem_state, idem_response = submission_idempotency.begin(
            jdb, user["username"], UPLOAD_ENDPOINT,
            idem_key, idem_body,
        )
        if idem_state == "replay":
            _remove(path)
            replay_job = _find_idempotent_job(
                jdb, user["username"], idem_key
            )
            if replay_job is not None:
                status, replay_response = _continue_reserved_job(
                    jdb, jobs_store, points_domain, enqueue_job,
                    reject_pending_job, service_owner, out_dir,
                    replay_job, user["username"], idem_key,
                )
                return handler._send(status, replay_response)
            return handler._send(200, idem_response)
        if idem_state == "conflict":
            _remove(path)
            return handler._send(409, {
                "detail": "同一个 Idempotency-Key 不能用于不同文件",
                "code": "idempotency_conflict",
            })
        if idem_state == "processing":
            _remove(path)
            processing = {
                "detail": "相同上传正在受理，请稍后查询",
                "code": "idempotency_in_progress", "retry_after_ms": 1000,
            }
            processing_job = _find_idempotent_job(
                jdb, user["username"], idem_key
            )
            if processing_job is not None:
                status, resumed = _continue_reserved_job(
                    jdb, jobs_store, points_domain, enqueue_job,
                    reject_pending_job, service_owner, out_dir,
                    processing_job, user["username"], idem_key,
                )
                if status != 202:
                    return handler._send(status, resumed)
                processing["job_id"] = int(processing_job["id"])
            return handler._send(409, processing)
        idem_started = idem_state == "new"
        payload = {
            "mode": "reverse_prompt",
            "upload_token": upload_token,
            "media_type": media_type,
            "source_title": title,
            _PAYMENT_FIELD: {
                "state": "charge_pending",
                "idempotency_key": idem_key,
            },
        }
        now = int(time.time())
        reserved_owner = "%s:local-upload-reserved" % service_owner
        with closing(jdb()) as connection:
            breakdown._ensure_upload_table(connection)
            cur = connection.execute(
                "INSERT INTO jobs"
                "(kind,username,cost,status,payload,created_at,updated_at,owner)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (
                    "breakdown", user["username"], UPLOAD_COST, "pending",
                    json.dumps(payload, ensure_ascii=False), now, now,
                    reserved_owner,
                ),
            )
            job_id = int(cur.lastrowid)
            charge_key, refund_key = _payment_keys(job_id, upload_token)
            payload[_PAYMENT_FIELD].update({
                "charge_transaction_key": charge_key,
                "refund_transaction_key": refund_key,
            })
            connection.execute(
                "UPDATE jobs SET payload=? WHERE id=?",
                (json.dumps(payload, ensure_ascii=False), job_id),
            )
            connection.execute(
                "INSERT INTO breakdown_uploads"
                "(token,username,suffix,job_id,created_at,path,media_type)"
                " VALUES(?,?,?,?,?,?,?)",
                (
                    upload_token, user["username"], suffix, job_id, now,
                    str(path), media_type,
                ),
            )
            connection.commit()
        job_reserved = True
        row = _find_idempotent_job(jdb, user["username"], idem_key)
        if not row:
            raise RuntimeError("上传任务预留记录不存在")
        status, response = _continue_reserved_job(
            jdb, jobs_store, points_domain, enqueue_job, reject_pending_job,
            service_owner, out_dir, row, user["username"], idem_key,
        )
        return handler._send(status, response)
    except ValueError as error:
        _remove(path)
        if idem_started:
            submission_idempotency.abort(
                jdb, user["username"], UPLOAD_ENDPOINT,
                idem_key,
            )
        return handler._send(400, {"detail": str(error)})
    except Exception as error:
        if not job_reserved:
            _remove(path)
        if idem_started and not job_reserved:
            submission_idempotency.abort(
                jdb, user["username"], UPLOAD_ENDPOINT,
                idem_key,
            )
        if job_reserved:
            return handler._send(202, {
                "detail": "上传任务状态正在确认，请使用原提交凭证继续查询",
                "code": "idempotency_in_progress",
                "retry_after_ms": 1000,
                "job_id": job_id,
            })
        status = int(getattr(error, "status", 500) or 500)
        if status == 402:
            return handler._send(402, {"detail": getattr(error, "detail", "点数不足"), "need": UPLOAD_COST})
        return handler._send(500, {"detail": "上传任务创建失败，请重试"})
