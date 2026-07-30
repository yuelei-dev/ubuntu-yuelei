# -*- coding: utf-8 -*-
"""Stream local image/video uploads into the standard paid breakdown job flow."""
import json
import pathlib
import subprocess
import urllib.parse
import uuid

IMAGE_LIMIT = 20 * 1024 * 1024
VIDEO_LIMIT = 200 * 1024 * 1024
VIDEO_DURATION_LIMIT = 120.0
UPLOAD_COST = 20
_IMAGE_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
_VIDEO_EXT = {"video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm"}

def _remove(path):
    try: pathlib.Path(path).unlink()
    except (FileNotFoundError, OSError): pass

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
    with open(destination, "xb") as target:
        while remaining:
            chunk = handler.rfile.read(min(1024 * 1024, remaining))
            if not chunk: raise ValueError("文件读取不完整，请重新选择文件上传")
            target.write(chunk); remaining -= len(chunk)
    if pathlib.Path(destination).stat().st_size != expected_size:
        raise ValueError("文件读取不完整，请重新选择文件上传")

def handle_post(handler, *, verify, points_domain, jdb, jobs_store, enqueue_job,
                reject_pending_job, service_owner, out_dir, is_shutting_down,
                user_active_job_count, max_user_active_jobs, must_change_password):
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
    upload_dir = pathlib.Path(out_dir) / "reverse_uploads"; upload_dir.mkdir(parents=True, exist_ok=True)
    title = _safe_title(handler.headers.get("X-File-Name"))
    path = upload_dir / ("%s%s" % (uuid.uuid4().hex, allowed[content_type]))
    job_id = None
    try:
        _stream_body(handler, path, size)
        if media_type == "image":
            if _image_type(path) != content_type: raise ValueError("图片内容与文件格式不一致")
            duration = 0
        else: duration = _video_duration(path)
        payload = {"mode": "local_reverse", "local_media_path": str(path),
                   "local_media_type": media_type, "source_title": title, "duration": duration}
        job_id, points_left = jobs_store.create_paid_job(
            jdb, points_domain.deduct_points, points_domain.refund_points,
            "breakdown", user["username"], UPLOAD_COST, payload, service_owner)
        if not enqueue_job(job_id, "breakdown", "local_reverse"):
            reject_pending_job(job_id, user["username"], UPLOAD_COST, "任务队列已满，请稍后再试")
            _remove(path)
            return handler._send(429, {"detail": "任务队列已满，请稍后再试",
                                       "code": "queue_full", "retry_after_ms": 4000})
        return handler._send(200, {"job_id": job_id, "cost": UPLOAD_COST, "points_left": points_left})
    except ValueError as error:
        _remove(path); return handler._send(400, {"detail": str(error)})
    except Exception as error:
        if job_id is not None:
            reject_pending_job(job_id, user["username"], UPLOAD_COST, "上传任务入队失败")
        _remove(path)
        status = int(getattr(error, "status", 500) or 500)
        if status == 402:
            return handler._send(402, {"detail": getattr(error, "detail", "点数不足"), "need": UPLOAD_COST})
        return handler._send(500, {"detail": "上传任务创建失败，请重试"})
