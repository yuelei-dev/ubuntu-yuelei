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
            (username, "/api/gen/breakdown/local-upload", key),
        )
        connection.commit()

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
    job_committed = False
    deducted = False
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
            jdb, user["username"], "/api/gen/breakdown/local-upload",
            idem_key, idem_body,
        )
        if idem_state == "replay":
            _remove(path)
            return handler._send(200, idem_response)
        if idem_state == "conflict":
            _remove(path)
            return handler._send(409, {
                "detail": "同一个 Idempotency-Key 不能用于不同文件",
                "code": "idempotency_conflict",
            })
        if idem_state == "processing":
            _remove(path)
            return handler._send(409, {
                "detail": "相同上传正在受理，请稍后查询",
                "code": "idempotency_in_progress", "retry_after_ms": 1000,
            })
        idem_started = idem_state == "new"
        payload = {
            "mode": "reverse_prompt",
            "upload_token": upload_token,
            "media_type": media_type,
            "source_title": title,
        }
        points_left = points_domain.deduct_points(
            user["username"], UPLOAD_COST, "job:breakdown"
        )
        deducted = True
        now = int(time.time())
        with closing(jdb()) as connection:
            breakdown._ensure_upload_table(connection)
            cur = connection.execute(
                "INSERT INTO jobs"
                "(kind,username,cost,payload,created_at,updated_at,owner)"
                " VALUES(?,?,?,?,?,?,?)",
                (
                    "breakdown", user["username"], UPLOAD_COST,
                    json.dumps(payload, ensure_ascii=False), now, now,
                    service_owner,
                ),
            )
            job_id = int(cur.lastrowid)
            connection.execute(
                "INSERT INTO breakdown_uploads"
                "(token,username,suffix,job_id,created_at,path,media_type)"
                " VALUES(?,?,?,?,?,?,?)",
                (
                    upload_token, user["username"], suffix, job_id, now,
                    str(path), media_type,
                ),
            )
            response = {
                "job_id": job_id, "cost": UPLOAD_COST,
                "points_left": points_left,
            }
            if idem_key:
                updated = connection.execute(
                    "UPDATE submission_idempotency"
                    " SET response_json=?,updated_at=?"
                    " WHERE username=? AND endpoint=? AND idem_key=?"
                    " AND response_json IS NULL",
                    (
                        json.dumps(response, ensure_ascii=False), now,
                        user["username"], "/api/gen/breakdown/local-upload",
                        idem_key,
                    ),
                )
                if updated.rowcount != 1:
                    raise RuntimeError("上传幂等记录已失效")
            connection.commit()
        job_committed = True
        if not enqueue_job(job_id, "breakdown", "local_reverse"):
            rejected = reject_pending_job(
                job_id, user["username"], UPLOAD_COST,
                "任务队列已满，请稍后再试",
            )
            if rejected:
                _remove_binding(
                    jdb, out_dir, upload_token, user["username"], job_id
                )
                _clear_idempotency(jdb, user["username"], idem_key)
            return handler._send(429, {"detail": "任务队列已满，请稍后再试",
                                       "code": "queue_full", "retry_after_ms": 4000})
        return handler._send(200, response)
    except ValueError as error:
        _remove(path)
        if idem_started:
            submission_idempotency.abort(
                jdb, user["username"], "/api/gen/breakdown/local-upload",
                idem_key,
            )
        return handler._send(400, {"detail": str(error)})
    except Exception as error:
        if job_committed:
            rejected = reject_pending_job(
                job_id, user["username"], UPLOAD_COST, "上传任务入队失败"
            )
            if rejected:
                _remove_binding(
                    jdb, out_dir, upload_token, user["username"], job_id
                )
                _clear_idempotency(jdb, user["username"], idem_key)
        elif deducted:
            points_domain.safe_refund_points(
                user["username"], UPLOAD_COST, "job:breakdown:insert_failed"
            )
            _remove(path)
        else:
            _remove(path)
        if idem_started and not job_committed:
            submission_idempotency.abort(
                jdb, user["username"], "/api/gen/breakdown/local-upload",
                idem_key,
            )
        status = int(getattr(error, "status", 500) or 500)
        if status == 402:
            return handler._send(402, {"detail": getattr(error, "detail", "点数不足"), "need": UPLOAD_COST})
        return handler._send(500, {"detail": "上传任务创建失败，请重试"})
