# -*- coding: utf-8 -*-
"""Duration-driven digital-human workflow (v2).

Paid provider jobs still pass through the existing job, points, refund and
idempotency boundary.  This module freezes a server-authored plan, binds every
child job to one consent record, and performs only the zero-cost local compose.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import subprocess
import time
import urllib.parse
import urllib.request

from .core import OUT_DIR, closing, jdb
from . import digital_human_oneclick as legacy
from . import digital_human_timeline as timeline


PIPELINE = timeline.PIPELINE
PLAN_PATH = "/api/gen/digital-human-v2/plan"
CONSENT_PATH = "/api/gen/digital-human-v2/consent"
AUDIO_UPLOAD_PATH = "/api/gen/digital-human-v2/audio-upload"
VIDEO_UPLOAD_PATH = "/api/gen/digital-human-v2/video-upload"
MATERIAL_RESOLVE_PATH = "/api/gen/digital-human-v2/material-resolve"
CONSENT_VERSION = "digital-human-material-v2"
CONSENT_PURPOSE = "digital_human_material_v2"
CONSENT_TTL_SECONDS = legacy.CONSENT_TTL_SECONDS
DigitalHumanRequestError = legacy.DigitalHumanRequestError

_STAGE_KINDS = {
    "gesture": "image",
    "material": "image",
    "talking": "video",
    "compose": "script_to_video",
}
_GESTURE_PROMPTS = (
    "人物保持与参考照片完全一致，竖屏腰部以上口播照，双手自然放在身体前方，右手轻抬作开场讲解手势",
    "人物保持与参考照片完全一致，竖屏腰部以上口播照，双手在身体前方自然展开作对比说明手势",
    "人物保持与参考照片完全一致，竖屏腰部以上口播照，一手轻指前方作总结强调手势，另一手自然放松",
)
_AUDIO_UPLOAD_ID_RE = re.compile(r"^dha_[0-9a-f]{32}$")
_AUDIO_MIMES = {
    "audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/wav": ".wav",
    "audio/x-wav": ".wav", "audio/mp4": ".m4a", "audio/aac": ".m4a",
    "audio/x-m4a": ".m4a",
}
_MAX_AUDIO_UPLOAD_BYTES = 30 * 1024 * 1024
_AUDIO_UPLOAD_TTL_SECONDS = 24 * 60 * 60
_VIDEO_UPLOAD_ID_RE = re.compile(r"^dhv_[0-9a-f]{32}$")
_VIDEO_MIMES = {
    "video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm",
}
_MAX_VIDEO_UPLOAD_BYTES = 500 * 1024 * 1024
_VIDEO_UPLOAD_TTL_SECONDS = 24 * 60 * 60
_MATERIAL_ASSET_ID_RE = re.compile(r"^dhm_[0-9a-f]{32}$")
_MAX_MATERIAL_BYTES = 20 * 1024 * 1024
_MATERIAL_TTL_SECONDS = 24 * 60 * 60
_FEISHU_APP_TOKEN = os.environ.get("DIGITAL_HUMAN_FEISHU_APP_TOKEN", "RRiFbxY9CaJLV2saos2cyyhLnhe").strip()
_FEISHU_TABLES = (
    (os.environ.get("DIGITAL_HUMAN_FEISHU_TABLE_1", "tbliH1WHvDwhvFMi").strip(),
     os.environ.get("DIGITAL_HUMAN_FEISHU_VIEW_1", "vewPfJzNVq").strip()),
    (os.environ.get("DIGITAL_HUMAN_FEISHU_TABLE_2", "tblVAOaI3CTAkTaL").strip(),
     os.environ.get("DIGITAL_HUMAN_FEISHU_VIEW_2", "vewAu1VvKG").strip()),
)
_MATERIAL_MIMES = {
    "image/jpeg": ("image", ".jpg"), "image/png": ("image", ".png"),
    "image/webp": ("image", ".webp"), "video/mp4": ("video", ".mp4"),
    "video/webm": ("video", ".webm"), "video/quicktime": ("video", ".mov"),
}


def _ensure_audio_table(connection):
    connection.execute(
        """CREATE TABLE IF NOT EXISTS digital_human_audio_uploads(
            asset_id TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            run_id TEXT NOT NULL,
            source_sha256 TEXT NOT NULL,
            source_file TEXT NOT NULL,
            duration REAL NOT NULL,
            transcript TEXT NOT NULL,
            slices_json TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            UNIQUE(username, run_id)
        )"""
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_digital_human_audio_owner "
        "ON digital_human_audio_uploads(username, created_at DESC)"
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS digital_human_video_uploads(
            asset_id TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            run_id TEXT NOT NULL,
            source_sha256 TEXT NOT NULL,
            source_file TEXT NOT NULL,
            mime TEXT NOT NULL,
            duration REAL NOT NULL,
            width INTEGER NOT NULL,
            height INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            UNIQUE(username, run_id)
        )"""
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_digital_human_video_owner "
        "ON digital_human_video_uploads(username, created_at DESC)"
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS digital_human_material_assets(
            asset_id TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            run_id TEXT NOT NULL,
            plan_digest TEXT NOT NULL,
            item_index INTEGER NOT NULL,
            file TEXT NOT NULL,
            mime TEXT NOT NULL,
            media_type TEXT NOT NULL,
            provider TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            UNIQUE(username, run_id, item_index)
        )"""
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_digital_human_material_owner "
        "ON digital_human_material_assets(username, run_id, item_index)"
    )


def _audio_db(db_factory=None):
    db_factory = db_factory or legacy.cdb
    connection = db_factory()
    _ensure_audio_table(connection)
    connection.commit()
    return connection


def _safe_audio_upload_id(value):
    value = str(value or "").strip().lower()
    if not _AUDIO_UPLOAD_ID_RE.fullmatch(value):
        raise DigitalHumanRequestError("录音上传记录无效，请重新上传", "audio_upload_invalid", 409)
    return value


def _load_audio_asset(asset_id, username, now=None, db_factory=None):
    asset_id = _safe_audio_upload_id(asset_id)
    now = int(time.time() if now is None else now)
    with closing(_audio_db(db_factory)) as connection:
        row = connection.execute(
            "SELECT * FROM digital_human_audio_uploads WHERE asset_id=? AND username=?",
            (asset_id, str(username or "").strip()),
        ).fetchone()
    if not row:
        raise DigitalHumanRequestError(
            "录音不存在或不属于当前账号，请重新上传", "audio_upload_invalid", 409,
        )
    asset = dict(row)
    if int(asset["expires_at"]) <= now:
        raise DigitalHumanRequestError("录音已过期，请重新上传", "audio_upload_expired", 409)
    try:
        asset["slices"] = json.loads(asset.pop("slices_json"))
    except Exception as exc:
        raise DigitalHumanRequestError(
            "录音切段记录损坏，请重新上传", "audio_upload_invalid", 409,
        ) from exc
    if not isinstance(asset["slices"], list) or not asset["slices"]:
        raise DigitalHumanRequestError("录音切段记录无效，请重新上传", "audio_upload_invalid", 409)
    return asset


def _safe_video_upload_id(value):
    value = str(value or "").strip().lower()
    if not _VIDEO_UPLOAD_ID_RE.fullmatch(value):
        raise DigitalHumanRequestError(
            "真人视频上传记录无效，请重新上传", "video_upload_invalid", 409,
        )
    return value


def _load_video_asset(asset_id, username, now=None, db_factory=None):
    asset_id = _safe_video_upload_id(asset_id)
    now = int(time.time() if now is None else now)
    with closing(_audio_db(db_factory)) as connection:
        row = connection.execute(
            "SELECT * FROM digital_human_video_uploads WHERE asset_id=? AND username=?",
            (asset_id, str(username or "").strip()),
        ).fetchone()
    if not row:
        raise DigitalHumanRequestError(
            "真人视频不存在或不属于当前账号，请重新上传", "video_upload_invalid", 409,
        )
    asset = dict(row)
    if int(asset["expires_at"]) <= now:
        raise DigitalHumanRequestError(
            "真人视频已过期，请重新上传", "video_upload_expired", 409,
        )
    try:
        path = (OUT_DIR / asset["source_file"]).resolve()
        path.relative_to(OUT_DIR.resolve())
    except Exception:
        path = None
    if not path or not path.is_file() or path.stat().st_size <= 0:
        raise DigitalHumanRequestError(
            "真人视频文件已不可用，请重新上传", "video_upload_invalid", 409,
        )
    return asset


def _probe_audio_duration(path):
    process = subprocess.run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ], check=False, timeout=60, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        duration = float((process.stdout or b"").decode("ascii", "replace").strip())
    except (TypeError, ValueError):
        duration = 0.0
    if process.returncode != 0 or duration <= 0:
        raise DigitalHumanRequestError("无法读取录音时长，请更换音频", "audio_probe_failed")
    if duration < timeline.MIN_AUDIO_SECONDS:
        raise DigitalHumanRequestError("完整录音不能少于 6 秒", "audio_duration_invalid")
    if duration > timeline.MAX_DURATION_SECONDS:
        raise DigitalHumanRequestError("完整录音不能超过 180 秒", "audio_duration_invalid")
    return round(duration, 3)


def _transcribe_audio(path):
    try:
        from . import video as video_domain
        video_domain.subtitle_runtime_preflight()
        model = video_domain._get_whisper_model()
        with video_domain._whisper_sem:
            result, _info = model.transcribe(
                str(path), beam_size=1, vad_filter=True, language="zh",
            )
            segments = [{
                "start": max(0.0, float(item.start)),
                "end": max(0.0, float(item.end)),
                "text": str(item.text or "").strip(),
            } for item in result if str(item.text or "").strip()]
    except DigitalHumanRequestError:
        raise
    except Exception as exc:
        raise DigitalHumanRequestError(
            "录音转写失败，请稍后重试", "audio_transcribe_failed", 503,
        ) from exc
    if not segments:
        raise DigitalHumanRequestError("录音中没有识别到有效口播", "audio_transcript_empty")
    return segments


def _slice_intervals(transcript_segments, duration):
    intervals = []
    start = 0.0
    while duration - start > timeline.MAX_APPEARANCE_INTERVAL:
        candidates = [float(item["end"]) for item in transcript_segments
                      if start + timeline.MIN_APPEARANCE_INTERVAL <= float(item["end"])
                      <= start + timeline.MAX_APPEARANCE_INTERVAL]
        cut = (min(candidates, key=lambda value: abs(
            value - (start + timeline.TARGET_APPEARANCE_INTERVAL)
        )) if candidates else min(duration, start + timeline.TARGET_APPEARANCE_INTERVAL))
        intervals.append((round(start, 3), round(cut, 3)))
        start = cut
    intervals.append((round(start, 3), round(duration, 3)))
    if len(intervals) > 1 and intervals[-1][1] - intervals[-1][0] < 8.0:
        previous = intervals[-2]
        intervals[-2:] = [(previous[0], intervals[-1][1])]
    return intervals


def _slice_text(transcript_segments, start, end):
    text = "".join(item["text"] for item in transcript_segments
                   if float(item["end"]) > start and float(item["start"]) < end)
    return re.sub(r"\s+", " ", text).strip()


def store_audio_upload(stream, length, username, run_id, content_type,
                       claimed_sha256, db_factory=None):
    if not legacy._RUN_ID_RE.fullmatch(str(run_id or "").strip()):
        raise DigitalHumanRequestError("本次制作流程编号无效，请重新开始")
    if type(length) is not int or length <= 0 or length > _MAX_AUDIO_UPLOAD_BYTES:
        raise DigitalHumanRequestError("录音文件必须小于 30MB", "audio_upload_size_invalid")
    extension = _AUDIO_MIMES.get(str(content_type or "").split(";", 1)[0].strip().lower())
    if not extension:
        raise DigitalHumanRequestError("仅支持 MP3、WAV、M4A 或 AAC 录音", "audio_upload_type_invalid")
    claimed = legacy._required_sha256(claimed_sha256, "完整录音")
    username = str(username or "").strip()
    with closing(_audio_db(db_factory)) as connection:
        existing = connection.execute(
            "SELECT asset_id,source_sha256 FROM digital_human_audio_uploads "
            "WHERE username=? AND run_id=?", (username, str(run_id).strip()),
        ).fetchone()
    if existing:
        if not hmac.compare_digest(str(existing["source_sha256"]), claimed):
            raise DigitalHumanRequestError(
                "同一制作流程不能更换完整录音，请重新开始",
                "audio_upload_binding_conflict", 409,
            )
        return _load_audio_asset(existing["asset_id"], username, db_factory=db_factory)
    asset_id = "dha_" + secrets.token_hex(16)
    owner = hashlib.sha256(username.encode("utf-8")).hexdigest()[:20]
    directory = OUT_DIR / "digital_human_audio" / owner / asset_id
    directory.mkdir(parents=True, exist_ok=False)
    source = directory / ("source" + extension)
    digest = hashlib.sha256()
    remaining = length
    try:
        with source.open("wb") as output:
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise DigitalHumanRequestError("录音上传不完整，请重新上传", "audio_upload_incomplete")
                output.write(chunk)
                digest.update(chunk)
                remaining -= len(chunk)
        if not hmac.compare_digest(digest.hexdigest(), claimed):
            raise DigitalHumanRequestError("录音校验失败，请重新上传", "audio_upload_digest_mismatch")
        duration = _probe_audio_duration(source)
        transcript_segments = _transcribe_audio(source)
        slices = []
        for index, (start, end) in enumerate(_slice_intervals(transcript_segments, duration)):
            target = directory / ("slice_%02d.m4a" % index)
            legacy._run([
                "ffmpeg", "-y", "-ss", "%.3f" % start, "-to", "%.3f" % end,
                "-i", str(source), "-vn", "-c:a", "aac", "-b:a", "192k",
                "-ar", "48000", "-ac", "2", str(target),
            ], timeout=180)
            raw = target.read_bytes()
            slices.append({
                "index": index, "start": start, "end": end,
                "duration": round(end - start, 3),
                "text": _slice_text(transcript_segments, start, end),
                "file": target.resolve().relative_to(OUT_DIR.resolve()).as_posix(),
                "sha256": hashlib.sha256(raw).hexdigest(),
            })
        transcript = "".join(item["text"] for item in transcript_segments).strip()
        if len(transcript) < 4:
            raise DigitalHumanRequestError("录音中没有识别到足够的口播内容", "audio_transcript_empty")
        now = int(time.time())
        try:
            with closing(_audio_db(db_factory)) as connection:
                connection.execute(
                    """INSERT INTO digital_human_audio_uploads(
                        asset_id,username,run_id,source_sha256,source_file,duration,
                        transcript,slices_json,created_at,expires_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (asset_id, username, str(run_id).strip(), claimed,
                     source.resolve().relative_to(OUT_DIR.resolve()).as_posix(), duration,
                     transcript, json.dumps(slices, ensure_ascii=False), now,
                     now + _AUDIO_UPLOAD_TTL_SECONDS),
                )
                connection.commit()
        except sqlite3.IntegrityError:
            # A browser retry can race the first upload. Reuse the committed,
            # owner-bound asset instead of returning a transient server error.
            with closing(_audio_db(db_factory)) as connection:
                winner = connection.execute(
                    "SELECT asset_id,source_sha256 FROM digital_human_audio_uploads "
                    "WHERE username=? AND run_id=?",
                    (username, str(run_id).strip()),
                ).fetchone()
            if winner and hmac.compare_digest(str(winner["source_sha256"]), claimed):
                import shutil
                shutil.rmtree(str(directory), ignore_errors=True)
                return _load_audio_asset(winner["asset_id"], username, db_factory=db_factory)
            raise DigitalHumanRequestError(
                "同一制作流程不能更换完整录音，请重新开始",
                "audio_upload_binding_conflict", 409,
            )
        return _load_audio_asset(asset_id, username, db_factory=db_factory)
    except Exception:
        # Leave only committed, owner-bound uploads.  Uncommitted provider input
        # is safe to remove because the browser still owns the source file.
        import shutil
        shutil.rmtree(str(directory), ignore_errors=True)
        raise


def audio_upload_response(stream, length, username, run_id, content_type,
                          claimed_sha256, db_factory=None):
    asset = store_audio_upload(
        stream, length, username, run_id, content_type, claimed_sha256,
        db_factory=db_factory,
    )
    return {
        "ok": True, "audio_upload_id": asset["asset_id"],
        "duration": round(float(asset["duration"]), 3),
        "transcript": asset["transcript"], "slice_count": len(asset["slices"]),
        "expires_at": int(asset["expires_at"]), "source_sha256": asset["source_sha256"],
    }


def _probe_video(path):
    process = subprocess.run([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height:format=duration",
        "-of", "json", str(path),
    ], check=False, timeout=60, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        data = json.loads((process.stdout or b"").decode("utf-8", "replace"))
        stream = (data.get("streams") or [])[0]
        duration = float((data.get("format") or {}).get("duration") or 0)
        width, height = int(stream.get("width") or 0), int(stream.get("height") or 0)
    except (IndexError, TypeError, ValueError, json.JSONDecodeError):
        duration, width, height = 0.0, 0, 0
    if process.returncode != 0 or duration <= 0 or width < 16 or height < 16:
        raise DigitalHumanRequestError(
            "无法读取真人视频，请重新导出后上传", "video_probe_failed",
        )
    if duration < timeline.MIN_AUDIO_SECONDS:
        raise DigitalHumanRequestError(
            "真人视频不能少于 6 秒", "video_duration_invalid",
        )
    if duration > timeline.MAX_DURATION_SECONDS:
        raise DigitalHumanRequestError(
            "真人视频不能超过 180 秒", "video_duration_invalid",
        )
    return round(duration, 3), width, height


def store_video_upload(stream, length, username, run_id, content_type,
                       claimed_sha256, db_factory=None):
    if not legacy._RUN_ID_RE.fullmatch(str(run_id or "").strip()):
        raise DigitalHumanRequestError("本次制作流程编号无效，请重新开始")
    if type(length) is not int or length <= 0 or length > _MAX_VIDEO_UPLOAD_BYTES:
        raise DigitalHumanRequestError(
            "真人视频必须小于 500MB", "video_upload_size_invalid",
        )
    mime = str(content_type or "").split(";", 1)[0].strip().lower()
    extension = _VIDEO_MIMES.get(mime)
    if not extension:
        raise DigitalHumanRequestError(
            "仅支持 MP4、MOV 或 WebM 真人视频", "video_upload_type_invalid",
        )
    claimed = legacy._required_sha256(claimed_sha256, "真人视频")
    username = str(username or "").strip()
    run_id = str(run_id).strip()
    with closing(_audio_db(db_factory)) as connection:
        existing = connection.execute(
            "SELECT asset_id,source_sha256 FROM digital_human_video_uploads "
            "WHERE username=? AND run_id=?", (username, run_id),
        ).fetchone()
    if existing:
        if not hmac.compare_digest(str(existing["source_sha256"]), claimed):
            raise DigitalHumanRequestError(
                "同一制作流程不能更换真人视频，请重新开始",
                "video_upload_binding_conflict", 409,
            )
        return _load_video_asset(existing["asset_id"], username, db_factory=db_factory)
    asset_id = "dhv_" + secrets.token_hex(16)
    owner = hashlib.sha256(username.encode("utf-8")).hexdigest()[:20]
    directory = OUT_DIR / "digital_human_video" / owner / asset_id
    directory.mkdir(parents=True, exist_ok=False)
    source = directory / ("source" + extension)
    digest = hashlib.sha256()
    remaining = length
    try:
        with source.open("wb") as output:
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise DigitalHumanRequestError(
                        "真人视频上传不完整，请重新上传", "video_upload_incomplete",
                    )
                output.write(chunk)
                digest.update(chunk)
                remaining -= len(chunk)
        if not hmac.compare_digest(digest.hexdigest(), claimed):
            raise DigitalHumanRequestError(
                "真人视频校验失败，请重新上传", "video_upload_digest_mismatch",
            )
        duration, width, height = _probe_video(source)
        now = int(time.time())
        relative = source.resolve().relative_to(OUT_DIR.resolve()).as_posix()
        try:
            with closing(_audio_db(db_factory)) as connection:
                connection.execute(
                    """INSERT INTO digital_human_video_uploads(
                        asset_id,username,run_id,source_sha256,source_file,mime,
                        duration,width,height,created_at,expires_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (asset_id, username, run_id, claimed, relative, mime, duration,
                     width, height, now, now + _VIDEO_UPLOAD_TTL_SECONDS),
                )
                connection.commit()
        except sqlite3.IntegrityError:
            with closing(_audio_db(db_factory)) as connection:
                winner = connection.execute(
                    "SELECT asset_id,source_sha256 FROM digital_human_video_uploads "
                    "WHERE username=? AND run_id=?", (username, run_id),
                ).fetchone()
            if winner and hmac.compare_digest(str(winner["source_sha256"]), claimed):
                import shutil
                shutil.rmtree(str(directory), ignore_errors=True)
                return _load_video_asset(winner["asset_id"], username, db_factory=db_factory)
            raise DigitalHumanRequestError(
                "同一制作流程不能更换真人视频，请重新开始",
                "video_upload_binding_conflict", 409,
            )
        return _load_video_asset(asset_id, username, db_factory=db_factory)
    except Exception:
        import shutil
        shutil.rmtree(str(directory), ignore_errors=True)
        raise


def video_upload_response(stream, length, username, run_id, content_type,
                          claimed_sha256, db_factory=None):
    asset = store_video_upload(
        stream, length, username, run_id, content_type, claimed_sha256,
        db_factory=db_factory,
    )
    return {
        "ok": True, "video_upload_id": asset["asset_id"],
        "duration": round(float(asset["duration"]), 3),
        "width": int(asset["width"]), "height": int(asset["height"]),
        "expires_at": int(asset["expires_at"]),
        "source_sha256": asset["source_sha256"],
    }


def _read_http_json(request, timeout=12):
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("素材接口响应过大")
    return json.loads(raw.decode("utf-8"))


def _read_http_media(request, timeout=30):
    with urllib.request.urlopen(request, timeout=timeout) as response:
        content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].lower()
        raw = response.read(_MAX_MATERIAL_BYTES + 1)
    if not raw or len(raw) > _MAX_MATERIAL_BYTES:
        raise ValueError("素材文件为空或超过 20MB")
    if content_type not in _MATERIAL_MIMES:
        raise ValueError("素材文件格式不受支持")
    return raw, content_type


def _keywords(text):
    compact = re.sub(r"\s+", "", str(text or "").lower())
    latin = re.findall(r"[a-z0-9]{2,}", compact)
    cjk = "".join(re.findall(r"[\u4e00-\u9fff]", compact))
    return set(latin + [cjk[index:index + 2] for index in range(max(0, len(cjk) - 1))])


def _flatten_fields(value, texts, attachments):
    if isinstance(value, dict):
        if value.get("file_token") and (value.get("name") or value.get("type")):
            attachments.append(value)
        for child in value.values():
            _flatten_fields(child, texts, attachments)
    elif isinstance(value, list):
        for child in value:
            _flatten_fields(child, texts, attachments)
    elif isinstance(value, (str, int, float)):
        texts.append(str(value))


def _feishu_token():
    app_id = os.environ.get("FEISHU_APP_ID", "").strip()
    app_secret = os.environ.get("FEISHU_APP_SECRET", "").strip()
    if not app_id or not app_secret or not _FEISHU_APP_TOKEN:
        return ""
    request = urllib.request.Request(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        data=json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"}, method="POST",
    )
    result = _read_http_json(request)
    return str(result.get("tenant_access_token") or "").strip()


def _feishu_material(query, preferred_type):
    token = _feishu_token()
    if not token:
        return None
    query_words = _keywords(query)
    best = None
    for table_id, view_id in _FEISHU_TABLES:
        if not table_id:
            continue
        page_token = ""
        for _page in range(5):
            query_params = {"page_size": 100}
            if view_id:
                query_params["view_id"] = view_id
            if page_token:
                query_params["page_token"] = page_token
            params = urllib.parse.urlencode(query_params)
            url = ("https://open.feishu.cn/open-apis/bitable/v1/apps/%s/tables/%s/records?%s" %
                   (urllib.parse.quote(_FEISHU_APP_TOKEN, safe=""),
                    urllib.parse.quote(table_id, safe=""), params))
            result = _read_http_json(urllib.request.Request(
                url, headers={"Authorization": "Bearer " + token},
            ))
            data = result.get("data") or {}
            for record in (data.get("items") or []):
                texts, attachments = [], []
                _flatten_fields(record.get("fields") or {}, texts, attachments)
                score = len(query_words & _keywords(" ".join(texts)))
                for attachment in attachments:
                    mime = str(attachment.get("type") or attachment.get("mime_type") or "").lower()
                    media_type = _MATERIAL_MIMES.get(mime, ("", ""))[0]
                    if not media_type or score <= 0:
                        continue
                    type_bonus = 3 if media_type == preferred_type else 0
                    candidate = (score + type_bonus, attachment, mime)
                    if best is None or candidate[0] > best[0]:
                        best = candidate
            if not data.get("has_more"):
                break
            next_page = str(data.get("page_token") or "").strip()
            if not next_page or next_page == page_token:
                break
            page_token = next_page
    if not best or not best[1].get("file_token"):
        return None
    url = ("https://open.feishu.cn/open-apis/drive/v1/medias/%s/download" %
           urllib.parse.quote(str(best[1]["file_token"]), safe=""))
    raw, mime = _read_http_media(urllib.request.Request(
        url, headers={"Authorization": "Bearer " + token},
    ))
    return raw, mime, "feishu"


def _wikimedia_material(query, preferred_type):
    search = str(query or "").strip()[:120]
    if not search:
        return None
    if preferred_type == "video":
        search += " filetype:video"
    params = urllib.parse.urlencode({
        "action": "query", "generator": "search", "gsrsearch": search,
        "gsrnamespace": 6, "gsrlimit": 12, "prop": "imageinfo",
        "iiprop": "url|mime|extmetadata", "iiurlwidth": 1080,
        "format": "json", "formatversion": 2,
    })
    result = _read_http_json(urllib.request.Request(
        "https://commons.wikimedia.org/w/api.php?" + params,
        headers={"User-Agent": "HuangqueDigitalHuman/2.0"},
    ))
    candidates = []
    for page in ((result.get("query") or {}).get("pages") or []):
        info = ((page.get("imageinfo") or [{}])[0])
        metadata = info.get("extmetadata") or {}
        license_name = str((metadata.get("LicenseShortName") or {}).get("value") or "").lower()
        if not ("public domain" in license_name or "cc0" in license_name):
            continue
        mime = str(info.get("mime") or "").lower()
        media_type = _MATERIAL_MIMES.get(mime, ("", ""))[0]
        if not media_type:
            continue
        url = str((info.get("url") if media_type == "video" else info.get("thumburl"))
                  or info.get("url") or "")
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != "upload.wikimedia.org":
            continue
        candidates.append((3 if media_type == preferred_type else 0, url, mime))
    if not candidates:
        return None
    _score, url, expected_mime = max(candidates, key=lambda item: item[0])
    raw, mime = _read_http_media(urllib.request.Request(
        url, headers={"User-Agent": "HuangqueDigitalHuman/2.0"},
    ))
    if mime != expected_mime and _MATERIAL_MIMES[mime][0] != _MATERIAL_MIMES[expected_mime][0]:
        raise ValueError("公开素材响应格式发生变化")
    return raw, mime, "public_web"


def _store_material_asset(raw, mime, provider, username, run_id, plan_digest,
                          item_index, db_factory=None):
    media_type, extension = _MATERIAL_MIMES[mime]
    asset_id = "dhm_" + secrets.token_hex(16)
    owner = hashlib.sha256(str(username).encode("utf-8")).hexdigest()[:20]
    directory = OUT_DIR / "digital_human_materials" / owner / hashlib.sha256(
        str(run_id).encode("utf-8")).hexdigest()[:20]
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (asset_id + extension)
    target.write_bytes(raw)
    probe = subprocess.run([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=codec_type,width,height", "-of", "json",
        str(target),
    ], check=False, timeout=60, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        streams = json.loads((probe.stdout or b"{}").decode("utf-8")).get("streams") or []
        valid_stream = (probe.returncode == 0 and streams
                        and int(streams[0].get("width") or 0) > 0
                        and int(streams[0].get("height") or 0) > 0)
    except Exception:
        valid_stream = False
    if not valid_stream:
        target.unlink(missing_ok=True)
        raise ValueError("素材文件无法解码")
    now = int(time.time())
    relative = target.resolve().relative_to(OUT_DIR.resolve()).as_posix()
    try:
        with closing(_audio_db(db_factory)) as connection:
            connection.execute(
                """INSERT INTO digital_human_material_assets(
                    asset_id,username,run_id,plan_digest,item_index,file,mime,
                    media_type,provider,created_at,expires_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (asset_id, username, run_id, plan_digest, item_index, relative,
                 mime, media_type, provider, now, now + _MATERIAL_TTL_SECONDS),
            )
            connection.commit()
    except sqlite3.IntegrityError:
        target.unlink(missing_ok=True)
        with closing(_audio_db(db_factory)) as connection:
            winner = connection.execute(
                "SELECT asset_id FROM digital_human_material_assets WHERE username=? "
                "AND run_id=? AND plan_digest=? AND item_index=?",
                (username, run_id, plan_digest, int(item_index)),
            ).fetchone()
        if winner:
            asset = _load_material_asset(
                winner["asset_id"], username, run_id, plan_digest, item_index,
                db_factory=db_factory,
            )
            return {
                "asset_id": asset["asset_id"], "media_type": asset["media_type"],
                "provider": asset["provider"], "expires_at": asset["expires_at"],
            }
        raise
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return {
        "asset_id": asset_id, "media_type": media_type,
        "provider": provider, "expires_at": now + _MATERIAL_TTL_SECONDS,
    }


def _load_material_asset(asset_id, username, run_id, plan_digest, item_index,
                         now=None, db_factory=None):
    asset_id = str(asset_id or "").strip().lower()
    if not _MATERIAL_ASSET_ID_RE.fullmatch(asset_id):
        raise DigitalHumanRequestError("正文素材记录无效", "material_asset_invalid", 409)
    now = int(time.time() if now is None else now)
    with closing(_audio_db(db_factory)) as connection:
        row = connection.execute(
            "SELECT * FROM digital_human_material_assets WHERE asset_id=? AND username=? "
            "AND run_id=? AND plan_digest=? AND item_index=?",
            (asset_id, username, run_id, plan_digest, int(item_index)),
        ).fetchone()
    if not row or int(row["expires_at"]) <= now:
        raise DigitalHumanRequestError(
            "正文素材不存在、已过期或不属于本次方案", "material_asset_invalid", 409,
        )
    asset = dict(row)
    try:
        path = (OUT_DIR / asset["file"]).resolve()
        path.relative_to(OUT_DIR.resolve())
    except Exception:
        path = None
    if not path or not path.is_file() or path.stat().st_size <= 0:
        raise DigitalHumanRequestError("正文素材文件已不可用", "material_asset_invalid", 409)
    return asset


def resolve_material_response(payload, username, db_factory=None):
    if not isinstance(payload, dict):
        raise DigitalHumanRequestError("请求体必须是 JSON 对象")
    allowed = {
        "digital_human_pipeline", "digital_human_stage", "digital_human_run_id",
        "digital_human_plan_digest", "digital_human_consent_token",
        "digital_human_script", "digital_human_gesture_count",
        "digital_human_narration_mode", "digital_human_audio_upload_id",
        "digital_human_item_index",
    }
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise DigitalHumanRequestError("素材检索包含不支持字段：" + ", ".join(unknown))
    if str(payload.get("digital_human_pipeline") or "") != CONSENT_PURPOSE:
        raise DigitalHumanRequestError("素材检索流程标识无效")
    if str(payload.get("digital_human_stage") or "") != "material_resolve":
        raise DigitalHumanRequestError("素材检索步骤无效")
    record = _load_v2_consent(username, payload.get("digital_human_consent_token"))
    if (str(payload.get("digital_human_run_id") or "") != record["run_id"]
            or str(payload.get("digital_human_plan_digest") or "") != record["plan_digest"]):
        raise DigitalHumanRequestError("素材检索与授权方案不一致", "consent_binding_mismatch", 403)
    frozen = _authoritative_plan(payload, username)
    if frozen["plan_digest"] != record["plan_digest"]:
        raise DigitalHumanRequestError("素材检索方案已变化", "consent_plan_mismatch", 409)
    try:
        item_index = int(payload.get("digital_human_item_index"))
        material = frozen["materials"][item_index]
    except (TypeError, ValueError, IndexError, KeyError):
        raise DigitalHumanRequestError("正文素材步骤编号无效", "consent_plan_mismatch", 409)
    with closing(_audio_db(db_factory)) as connection:
        existing = connection.execute(
            "SELECT asset_id FROM digital_human_material_assets WHERE username=? "
            "AND run_id=? AND item_index=?", (username, record["run_id"], item_index),
        ).fetchone()
    if existing:
        asset = _load_material_asset(
            existing["asset_id"], username, record["run_id"], record["plan_digest"],
            item_index, db_factory=db_factory,
        )
        return {"ok": True, "source": asset["provider"],
                "material_asset_id": asset["asset_id"], "media_type": asset["media_type"]}
    preferred = "video" if material["scene_type"] == "video" else "image"
    fetched = None
    failures = []
    for provider in ("feishu", "public_web"):
        try:
            fetched = (_feishu_material(material["material_query"], preferred)
                       if provider == "feishu" else
                       _wikimedia_material(material["material_query"], preferred))
        except Exception as exc:
            failures.append(provider + ":" + str(exc)[:80])
            fetched = None
        if fetched:
            break
    if not fetched:
        return {"ok": True, "source": "ai", "ai_fallback": True,
                "retryable_sources": bool(failures)}
    raw, mime, provider = fetched
    try:
        asset = _store_material_asset(
            raw, mime, provider, username, record["run_id"], record["plan_digest"],
            item_index, db_factory=db_factory,
        )
    except Exception:
        # A remote result that cannot be decoded or committed is not safe to
        # use. Continue with the existing paid AI fallback instead of leaving
        # the whole customer run in a half-finished material state.
        return {"ok": True, "source": "ai", "ai_fallback": True,
                "retryable_sources": True}
    return {"ok": True, "source": provider,
            "material_asset_id": asset["asset_id"], "media_type": asset["media_type"]}


def _as_request_error(exc):
    if isinstance(exc, DigitalHumanRequestError):
        return exc
    return DigitalHumanRequestError(
        str(exc), str(getattr(exc, "code", "invalid_digital_human_plan")),
        int(getattr(exc, "status", 400) or 400),
    )


def _audio_plan(asset, selected_gesture_count):
    gestures = timeline.gesture_count(selected_gesture_count)
    duration = round(float(asset["duration"]), 3)
    slices = list(asset["slices"])
    segment_durations = [round(float(item["duration"]), 3) for item in slices]
    windows = timeline.presenter_windows(segment_durations, duration)
    planned_slots = timeline.material_slots(windows, duration)
    infographic_limit = 1 if duration < 75 else 2
    infographic_indexes = {max(0, len(planned_slots) // 3)} if planned_slots else set()
    if infographic_limit == 2 and planned_slots:
        infographic_indexes.add(min(len(planned_slots) - 1, (len(planned_slots) * 2) // 3))
    roles = ("hook", "explain", "cta")
    segments = []
    for index, item in enumerate(slices):
        role = "hook" if index == 0 else "cta" if index == len(slices) - 1 else "explain"
        segments.append({
            "index": index, "text": str(item["text"] or "").strip(),
            "start": round(float(item["start"]), 3),
            "end": round(float(item["end"]), 3),
            "duration": round(float(item["duration"]), 3),
            "gesture_index": index % gestures, "role": role,
            "audio_slice_sha256": item["sha256"],
        })
    gestures_plan = [{"index": index, "role": roles[min(index, 2)]}
                     for index in range(gestures)]
    materials = []
    excerpts = [item["text"] for item in segments if item["text"]] or [asset["transcript"]]
    for slot in planned_slots:
        excerpt = excerpts[slot["index"] % len(excerpts)][:220]
        scene_type = "infographic" if slot["index"] in infographic_indexes else (
            "video" if slot["index"] % 3 == 1 else "image"
        )
        prefix = ("为竖屏知识短视频制作一张简洁的信息图表，只展示本段关键关系，"
                  if scene_type == "infographic" else
                  "为竖屏知识短视频制作真实、自然、具有现场感的内容画面，")
        materials.append(dict(slot, **{
            "scene_type": scene_type, "material_query": excerpt,
            "prompt": prefix + "不要出现数字人口播人物、文字水印或品牌标识。画面准确表达：" + excerpt,
            "source_priority": list(timeline.SOURCE_PRIORITY),
        }))
    core = {
        "pipeline": PIPELINE, "workflow_version": timeline.WORKFLOW_VERSION,
        "narration_mode": "audio", "audio_upload_id": asset["asset_id"],
        "source_audio_sha256": asset["source_sha256"],
        "copy": asset["transcript"], "ratio": "9:16",
        "expected_duration": duration, "gesture_count": gestures,
        "gestures": gestures_plan, "segments": segments,
        "presenter_windows": windows, "materials": materials,
        "infographic_limit": infographic_limit,
        "source_priority": list(timeline.SOURCE_PRIORITY),
    }
    return dict(core, segment_count=len(segments), material_count=len(materials),
                plan_digest=timeline._digest(core))


def _precision_plan(asset, script):
    copy = timeline.clean_script(script)
    expected_duration = timeline.estimate_duration(copy)
    source_duration = round(float(asset["duration"]), 3)
    delta = round(expected_duration - source_duration, 3)
    tolerance = max(1.5, source_duration * 0.08)
    mismatch = abs(delta) > tolerance
    warning = ""
    if mismatch:
        direction = "长" if delta > 0 else "短"
        warning = (
            "预计新配音比原视频%s约 %.1f 秒；Precision 会动态调整时长，"
            "请确认后再生成。" % (direction, abs(delta))
        )
    segment = {
        "index": 0, "text": copy, "start": 0.0,
        "end": expected_duration, "duration": expected_duration,
        "gesture_index": 0, "role": "full_video",
    }
    core = {
        "pipeline": PIPELINE, "workflow_version": timeline.WORKFLOW_VERSION,
        "narration_mode": "precision",
        "video_upload_id": asset["asset_id"],
        "source_video_sha256": asset["source_sha256"],
        "source_video_duration": source_duration,
        "source_video_width": int(asset["width"]),
        "source_video_height": int(asset["height"]),
        "copy": copy, "ratio": "9:16", "expected_duration": expected_duration,
        "duration_delta": delta, "duration_mismatch": mismatch,
        "duration_warning": warning,
        "gesture_count": 0, "gestures": [], "segments": [segment],
        "presenter_windows": [[0.0, expected_duration]], "materials": [],
        "infographic_limit": 0, "source_priority": [],
    }
    return dict(core, segment_count=1, material_count=0,
                plan_digest=timeline._digest(core))


def plan_response(payload, username=None):
    if not isinstance(payload, dict):
        raise DigitalHumanRequestError("请求体必须是 JSON 对象")
    mode = str(payload.get("narration_mode") or "text").strip().lower()
    try:
        if mode == "audio":
            allowed = {"narration_mode", "audio_upload_id", "gesture_count"}
            unknown = sorted(set(payload) - allowed)
            if unknown:
                raise DigitalHumanRequestError("方案提交包含不支持字段：" + ", ".join(unknown))
            asset = _load_audio_asset(payload.get("audio_upload_id"), username)
            return {"ok": True, "plan": _audio_plan(asset, payload.get("gesture_count"))}
        if mode == "precision":
            allowed = {"narration_mode", "video_upload_id", "script"}
            unknown = sorted(set(payload) - allowed)
            if unknown:
                raise DigitalHumanRequestError("方案提交包含不支持字段：" + ", ".join(unknown))
            asset = _load_video_asset(payload.get("video_upload_id"), username)
            return {"ok": True, "plan": _precision_plan(asset, payload.get("script"))}
        return timeline.plan_response(payload)
    except Exception as exc:
        raise _as_request_error(exc) from exc


def _authoritative_plan(payload, username=None):
    try:
        mode = str(payload.get("digital_human_narration_mode") or
                   payload.get("narration_mode") or "text").strip().lower()
        if mode == "audio":
            asset = _load_audio_asset(
                payload.get("digital_human_audio_upload_id") or payload.get("audio_upload_id"),
                username,
            )
            return _audio_plan(
                asset,
                payload.get("digital_human_gesture_count")
                if "digital_human_gesture_count" in payload else payload.get("gesture_count"),
            )
        if mode == "precision":
            asset = _load_video_asset(
                payload.get("digital_human_video_upload_id") or payload.get("video_upload_id"),
                username,
            )
            return _precision_plan(
                asset, payload.get("digital_human_script") or payload.get("script")
            )
        return timeline.plan_text(
            payload.get("digital_human_script") or payload.get("script"),
            payload.get("digital_human_gesture_count")
            if "digital_human_gesture_count" in payload else payload.get("gesture_count"),
        )
    except Exception as exc:
        raise _as_request_error(exc) from exc


def create_consent(payload, username, signing_secret, now=None, db_factory=None):
    if not isinstance(payload, dict):
        raise DigitalHumanRequestError("请求体必须是 JSON 对象")
    allowed = {
        "confirmed", "consent_version", "purpose", "run_id", "plan_digest",
        "script", "gesture_count", "photo_sha256", "voice_mode", "voice_ref",
        "voice_sha256", "narration_mode", "audio_upload_id",
        "video_upload_id", "video_sha256",
    }
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise DigitalHumanRequestError("授权提交包含不支持字段：" + ", ".join(unknown))
    if payload.get("confirmed") is not True:
        raise DigitalHumanRequestError("请先确认照片与声音授权", "consent_required", 403)
    if str(payload.get("consent_version") or "") != CONSENT_VERSION:
        raise DigitalHumanRequestError("授权条款版本已更新，请重新确认", "consent_version_mismatch", 409)
    if str(payload.get("purpose") or "") != CONSENT_PURPOSE:
        raise DigitalHumanRequestError("授权用途无效", "consent_purpose_invalid")
    narration_mode = str(payload.get("narration_mode") or "text").strip().lower()
    if narration_mode not in {"text", "audio", "precision"}:
        raise DigitalHumanRequestError("声音驱动方式无效")
    username = str(username or "").strip()
    if not username:
        raise DigitalHumanRequestError("未登录或登录已过期", "unauthorized", 401)
    run_id = str(payload.get("run_id") or "").strip()
    if not legacy._RUN_ID_RE.fullmatch(run_id):
        raise DigitalHumanRequestError("本次制作流程编号无效，请重新开始")
    try:
        audio_asset = (_load_audio_asset(payload.get("audio_upload_id"), username)
                       if narration_mode == "audio" else None)
        video_asset = (_load_video_asset(payload.get("video_upload_id"), username)
                       if narration_mode == "precision" else None)
        frozen = (_audio_plan(audio_asset, payload.get("gesture_count"))
                  if audio_asset else _precision_plan(video_asset, payload.get("script"))
                  if video_asset else timeline.plan_text(
                      payload.get("script"), payload.get("gesture_count")))
    except Exception as exc:
        raise _as_request_error(exc) from exc
    plan_digest = legacy._required_sha256(payload.get("plan_digest"), "制作方案")
    if not hmac.compare_digest(plan_digest, frozen["plan_digest"]):
        raise DigitalHumanRequestError(
            "制作方案与服务端时长拆分结果不一致，请重新分析方案",
            "consent_plan_mismatch", 409,
        )
    if narration_mode == "precision":
        supplied_video_sha = legacy._required_sha256(
            payload.get("video_sha256"), "真人视频",
        )
        if not hmac.compare_digest(supplied_video_sha, video_asset["source_sha256"]):
            raise DigitalHumanRequestError(
                "真人视频与上传记录不一致，请重新上传", "consent_video_mismatch", 403,
            )
        if str(payload.get("video_upload_id") or "") != video_asset["asset_id"]:
            raise DigitalHumanRequestError(
                "真人视频与制作方案不一致，请重新分析", "consent_video_mismatch", 403,
            )
        if payload.get("photo_sha256"):
            raise DigitalHumanRequestError("Precision 模式不应上传人物照片校验值")
        # The existing consent ledger column is retained for compatibility. In
        # precision mode it binds the authorized real-person source video hash.
        photo_sha256 = video_asset["source_sha256"]
    else:
        photo_sha256 = legacy._required_sha256(payload.get("photo_sha256"), "人物照片")
    if narration_mode == "audio":
        voice_mode = "audio"
        voice_ref = audio_asset["asset_id"]
        voice_sha256 = audio_asset["source_sha256"]
        if payload.get("voice_ref") or payload.get("voice_sha256"):
            raise DigitalHumanRequestError("录音驱动模式不应选择或复刻音色")
    else:
        voice_mode = str(payload.get("voice_mode") or "").strip().lower()
        if voice_mode not in {"existing", "clone"}:
            raise DigitalHumanRequestError("声音授权类型无效")
        voice_ref = str(payload.get("voice_ref") or "").strip()
        if not voice_ref or len(voice_ref) > 180:
            raise DigitalHumanRequestError("声音资产标识无效")
        voice_sha256 = str(payload.get("voice_sha256") or "").strip().lower()
        if voice_mode == "clone":
            voice_sha256 = legacy._required_sha256(voice_sha256, "声音样本")
        elif voice_sha256:
            raise DigitalHumanRequestError("复用已有声音时不应上传样音校验值")
    now = int(time.time() if now is None else now)
    consent_id = "dhc_" + hmac.new(
        str(signing_secret or "").encode("utf-8"),
        (username + "|" + run_id).encode("utf-8"), hashlib.sha256,
    ).hexdigest()[:32]
    candidate = {
        "id": consent_id, "username": username, "run_id": run_id,
        "consent_version": CONSENT_VERSION, "purpose": CONSENT_PURPOSE,
        "plan_digest": plan_digest, "photo_sha256": photo_sha256,
        "voice_mode": voice_mode, "voice_ref": voice_ref,
        "voice_sha256": voice_sha256, "created_at": now,
        "expires_at": now + CONSENT_TTL_SECONDS,
    }
    legacy._consent_signature(candidate, signing_secret)
    db_factory = db_factory or legacy.cdb
    legacy.init_db(db_factory)
    with closing(db_factory()) as connection:
        row = connection.execute(
            "SELECT * FROM digital_human_consents WHERE username=? AND run_id=?",
            (username, run_id),
        ).fetchone()
        if row:
            existing = dict(row)
            comparable = (
                "consent_version", "purpose", "plan_digest", "photo_sha256",
                "voice_mode", "voice_ref", "voice_sha256",
            )
            if any(str(existing[key]) != str(candidate[key]) for key in comparable):
                raise DigitalHumanRequestError(
                    "本次流程的照片、声音或方案已经变化，请重新开始并授权",
                    "consent_binding_conflict", 409,
                )
            if int(existing["expires_at"]) <= now:
                raise DigitalHumanRequestError(
                    "本次授权已过期，请重新开始并授权", "consent_expired", 409,
                )
            candidate = existing
        token = candidate["id"] + "." + legacy._consent_signature(candidate, signing_secret)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        if row:
            if not hmac.compare_digest(str(candidate["token_hash"]), token_hash):
                raise DigitalHumanRequestError(
                    "授权存证签名已变化，请重新开始", "consent_signature_changed", 409,
                )
        else:
            connection.execute(
                """INSERT INTO digital_human_consents(
                    id,username,run_id,consent_version,purpose,plan_digest,
                    photo_sha256,voice_mode,voice_ref,voice_sha256,token_hash,
                    created_at,expires_at,last_used_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    candidate["id"], candidate["username"], candidate["run_id"],
                    candidate["consent_version"], candidate["purpose"],
                    candidate["plan_digest"], candidate["photo_sha256"],
                    candidate["voice_mode"], candidate["voice_ref"],
                    candidate["voice_sha256"], token_hash, candidate["created_at"],
                    candidate["expires_at"], now,
                ),
            )
            connection.commit()
    return legacy._public_consent(candidate, token)


def consent_response(payload, username, signing_secret, db_factory=None):
    return {"ok": True, "consent": create_consent(
        payload, username, signing_secret, db_factory=db_factory,
    )}


def _load_v2_consent(username, token):
    record = legacy._load_consent(username, token)
    if (record.get("purpose") != CONSENT_PURPOSE
            or record.get("consent_version") != CONSENT_VERSION):
        raise DigitalHumanRequestError(
            "授权记录不属于当前数字人成片流程", "consent_binding_mismatch", 403,
        )
    return record


def verify_clone_submission(payload, username):
    """Bind a v2 voice-clone submission to its signed consent and plan."""
    if not isinstance(payload, dict):
        raise DigitalHumanRequestError("请求体必须是 JSON 对象")
    if str(payload.get("digital_human_pipeline") or "").strip().lower() != CONSENT_PURPOSE:
        raise DigitalHumanRequestError("数字人成片流程标识无效")
    if str(payload.get("digital_human_stage") or "").strip().lower() != "voice_clone":
        raise DigitalHumanRequestError("声音复刻步骤标识无效")
    record = _load_v2_consent(username, payload.get("digital_human_consent_token"))
    if str(payload.get("digital_human_run_id") or "") != record["run_id"]:
        raise DigitalHumanRequestError(
            "授权与本次制作流程不匹配", "consent_binding_mismatch", 403,
        )
    digest = str(payload.get("digital_human_plan_digest") or "").strip().lower()
    if not hmac.compare_digest(digest, record["plan_digest"]):
        raise DigitalHumanRequestError(
            "授权与制作方案不匹配", "consent_binding_mismatch", 403,
        )
    frozen = _authoritative_plan(payload, username)
    if not hmac.compare_digest(frozen["plan_digest"], record["plan_digest"]):
        raise DigitalHumanRequestError(
            "声音复刻文案与本次授权方案不一致，请重新开始",
            "consent_plan_mismatch", 409,
        )
    if record["voice_mode"] != "clone":
        raise DigitalHumanRequestError(
            "当前授权未允许重新复刻声音", "consent_voice_mismatch", 403,
        )
    if str(payload.get("slot_id") or "").strip() != record["voice_ref"]:
        raise DigitalHumanRequestError(
            "音色槽位与授权记录不一致", "consent_voice_mismatch", 403,
        )
    actual = hashlib.sha256(
        legacy._decode_b64_bytes(payload.get("audio"), "声音样本")
    ).hexdigest()
    if not hmac.compare_digest(actual, record["voice_sha256"]):
        raise DigitalHumanRequestError(
            "声音样本与授权记录不一致", "consent_voice_mismatch", 403,
        )
    cleaned = dict(payload)
    cleaned.pop("digital_human_consent_token", None)
    cleaned["digital_human_consent_id"] = record["id"]
    return cleaned


def _binding(payload_text, job_id, expected_stage, record, expected_index=None):
    try:
        payload = json.loads(payload_text or "")
    except Exception as exc:
        raise DigitalHumanRequestError(
            "子任务 #%d 的授权记录损坏，请重新生成" % job_id,
            "child_consent_binding_invalid", 409,
        ) from exc
    expected = {
        "digital_human_pipeline": CONSENT_PURPOSE,
        "digital_human_stage": expected_stage,
        "digital_human_consent_id": record["id"],
        "digital_human_run_id": record["run_id"],
        "digital_human_plan_digest": record["plan_digest"],
    }
    if any(str(payload.get(key) or "") != str(value) for key, value in expected.items()):
        raise DigitalHumanRequestError(
            "子任务 #%d 不属于本次授权制作流程，请重新生成" % job_id,
            "child_consent_binding_mismatch", 409,
        )
    if expected_index is not None:
        try:
            actual = int(payload.get("digital_human_item_index"))
        except (TypeError, ValueError):
            actual = -1
        if actual != int(expected_index):
            raise DigitalHumanRequestError(
                "子任务 #%d 的方案位置不匹配，请重新生成" % job_id,
                "child_consent_binding_mismatch", 409,
            )


def _gesture_prompt(index):
    return (_GESTURE_PROMPTS[index] +
            "；神态亲切自然，眼神稳定直视镜头，嘴唇自然闭合。服装、发型、眼镜、面部特征和背景保持一致，双手完整可见，真实摄影，不添加文字。")


def verify_child_submission_with_record(payload, username, kind):
    if not isinstance(payload, dict):
        return payload, None
    if str(payload.get("digital_human_pipeline") or "").strip().lower() != CONSENT_PURPOSE:
        return payload, None
    stage = str(payload.get("digital_human_stage") or "").strip().lower()
    if _STAGE_KINDS.get(stage) != str(kind or ""):
        raise DigitalHumanRequestError("数字人成片步骤与任务类型不匹配")
    record = _load_v2_consent(username, payload.get("digital_human_consent_token"))
    if str(payload.get("digital_human_run_id") or "") != record["run_id"]:
        raise DigitalHumanRequestError("授权与本次制作流程不匹配", "consent_binding_mismatch", 403)
    if str(payload.get("digital_human_plan_digest") or "").lower() != record["plan_digest"]:
        raise DigitalHumanRequestError("授权与制作方案不匹配", "consent_binding_mismatch", 403)
    frozen = _authoritative_plan(payload, username)
    if not hmac.compare_digest(frozen["plan_digest"], record["plan_digest"]):
        raise DigitalHumanRequestError(
            "子任务文案与本次授权方案不一致，请重新开始",
            "consent_plan_mismatch", 409,
        )
    cleaned = dict(payload)
    cleaned.pop("digital_human_consent_token", None)
    cleaned["digital_human_consent_id"] = record["id"]
    raw_index = payload.get("digital_human_item_index")
    if isinstance(raw_index, bool):
        raw_index = None
    try:
        item_index = int(raw_index)
    except (TypeError, ValueError):
        item_index = -1
    if stage == "gesture":
        if not 0 <= item_index < frozen["gesture_count"]:
            raise DigitalHumanRequestError("手势照步骤编号无效", "consent_plan_mismatch", 409)
        references = payload.get("reference_images")
        if not isinstance(references, list) or len(references) != 1:
            raise DigitalHumanRequestError(
                "手势照必须且只能使用本次授权的一张人物照片",
                "consent_photo_mismatch", 403,
            )
        actual = hashlib.sha256(legacy._decode_b64_bytes(references[0], "人物照片")).hexdigest()
        if not hmac.compare_digest(actual, record["photo_sha256"]):
            raise DigitalHumanRequestError(
                "人物照片与授权记录不一致，请重新开始并授权",
                "consent_photo_mismatch", 403,
            )
        cleaned.pop("reference_images", None)
        cleaned["images"] = [references[0]]
        cleaned.update({
            "prompt": _gesture_prompt(item_index), "provider": "banana",
            "model": "nb2", "quality": "std",
            "digital_human_item_index": item_index,
        })
    elif stage == "material":
        if not 0 <= item_index < frozen["material_count"]:
            raise DigitalHumanRequestError("正文素材步骤编号无效", "consent_plan_mismatch", 409)
        material = frozen["materials"][item_index]
        references = cleaned.pop("reference_images", None)
        cleaned.pop("images", None)
        if references is not None:
            cleaned["images"] = references
        cleaned.update({
            "prompt": material["prompt"], "provider": "banana",
            "model": "nb2", "quality": "std",
            "digital_human_item_index": item_index,
        })
    elif stage == "talking":
        if not 0 <= item_index < frozen["segment_count"]:
            raise DigitalHumanRequestError("口播步骤编号无效", "consent_plan_mismatch", 409)
        segment = frozen["segments"][item_index]
        audio_mode = frozen.get("narration_mode") == "audio"
        precision_mode = frozen.get("narration_mode") == "precision"
        expected_voice = ""
        audio_asset = None
        if audio_mode:
            if record["voice_mode"] != "audio":
                raise DigitalHumanRequestError(
                    "录音驱动授权与当前方案不一致", "consent_voice_mismatch", 403,
                )
            audio_asset = _load_audio_asset(record["voice_ref"], username)
            if (audio_asset["asset_id"] != frozen.get("audio_upload_id")
                    or not hmac.compare_digest(
                        audio_asset["source_sha256"], record["voice_sha256"])):
                raise DigitalHumanRequestError(
                    "完整录音与授权记录不一致", "consent_voice_mismatch", 403,
                )
        else:
            expected_voice = (record["voice_ref"] if record["voice_mode"] == "existing"
                              else legacy._expected_cloned_voice(record["voice_ref"]))
            if str(payload.get("voice") or "").strip() != expected_voice:
                raise DigitalHumanRequestError(
                    "口播声音与授权记录不一致，请重新开始并授权",
                    "consent_voice_mismatch", 403,
                )
        if precision_mode:
            video_asset = _load_video_asset(frozen.get("video_upload_id"), username)
            if (not hmac.compare_digest(video_asset["source_sha256"], record["photo_sha256"])
                    or not hmac.compare_digest(
                        video_asset["source_sha256"], frozen.get("source_video_sha256") or "")):
                raise DigitalHumanRequestError(
                    "真人视频与授权记录不一致，请重新开始",
                    "consent_video_mismatch", 403,
                )
            cleaned.update({
                "mode": "precision", "text": segment["text"],
                "voice": expected_voice,
                "source_video_file": video_asset["source_file"],
                "source_video_mime": video_asset["mime"],
                "motion": "medium", "speed": 1.0, "pitch": 0,
                "volume": 1, "delivery": "natural",
                "digital_human_item_index": item_index,
            })
            cleaned.pop("gesture_job_id", None)
        else:
            try:
                gesture_job_id = int(payload.get("gesture_job_id"))
            except (TypeError, ValueError):
                gesture_job_id = 0
            if gesture_job_id <= 0:
                raise DigitalHumanRequestError(
                    "口播缺少本次授权的手势照任务编号",
                    "talking_gesture_binding_invalid", 409,
                )
            with closing(jdb()) as connection:
                row = connection.execute(
                    "SELECT id,kind,status,payload,result FROM jobs WHERE id=? AND username=? "
                    "AND COALESCE(deleted,0)=0", (gesture_job_id, str(username or "").strip()),
                ).fetchone()
            if not row or row["kind"] != "image" or row["status"] != "done":
                raise DigitalHumanRequestError(
                    "口播手势照不存在、未完成或不属于当前账号",
                    "talking_gesture_binding_invalid", 409,
                )
            _binding(row["payload"], gesture_job_id, "gesture", record, segment["gesture_index"])
            try:
                result = json.loads(row["result"] or "{}")
            except Exception:
                result = {}
            relative_file = legacy._result_file(result, "image")
            try:
                gesture_path = (OUT_DIR / relative_file).resolve()
                gesture_path.relative_to(OUT_DIR.resolve())
            except Exception:
                gesture_path = None
            if not gesture_path or not gesture_path.is_file() or gesture_path.stat().st_size <= 0:
                raise DigitalHumanRequestError(
                    "口播手势照文件已不可用，请重新生成手势照",
                    "talking_gesture_binding_invalid", 409,
                )
            mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(gesture_path.suffix.lower())
            if not mime:
                raise DigitalHumanRequestError("口播手势照格式不受支持", "talking_gesture_binding_invalid", 409)
            cleaned.update({
                "image_data": "data:%s;base64,%s" % (
                    mime, base64.b64encode(gesture_path.read_bytes()).decode("ascii"),
                ),
                "motion": "high" if segment["role"] in {"hook", "cta"} else "medium",
                "speed": 1.04 if segment["role"] in {"hook", "cta"} else 1.0,
                "pitch": 0, "volume": 1, "delivery": "natural",
                "digital_human_item_index": item_index,
            })
        if audio_mode:
            audio_slice = audio_asset["slices"][item_index]
            if not hmac.compare_digest(
                    str(audio_slice.get("sha256") or ""),
                    str(segment.get("audio_slice_sha256") or "")):
                raise DigitalHumanRequestError(
                    "录音切段与制作方案不一致，请重新上传",
                    "audio_slice_binding_mismatch", 409,
                )
            try:
                audio_path = (OUT_DIR / audio_slice["file"]).resolve()
                audio_path.relative_to(OUT_DIR.resolve())
            except Exception:
                audio_path = None
            if not audio_path or not audio_path.is_file() or audio_path.stat().st_size <= 0:
                raise DigitalHumanRequestError(
                    "录音切段文件已不可用，请重新上传", "audio_slice_unavailable", 409,
                )
            cleaned.update({
                "mode": "audio", "text": segment["text"], "voice": "",
                "audio_data": "data:audio/mp4;base64," + base64.b64encode(
                    audio_path.read_bytes()).decode("ascii"),
            })
            cleaned.pop("audio_file", None)
        elif not precision_mode:
            cleaned.update({
                "mode": "text", "text": segment["text"], "voice": expected_voice,
            })
        cleaned.pop("gesture_job_id", None)
        try:
            from . import video as video_domain
            video_domain.subtitle_runtime_preflight()
        except Exception as exc:
            raise DigitalHumanRequestError(
                str(exc)[:220], str(getattr(exc, "code", "subtitle_runtime_unavailable")),
                int(getattr(exc, "status", 503) or 503),
            ) from exc
    elif stage == "compose":
        if str(payload.get("plan_digest") or "").lower() != record["plan_digest"]:
            raise DigitalHumanRequestError("成片方案与授权记录不一致", "consent_binding_mismatch", 403)
    cleaned.pop("digital_human_script", None)
    return cleaned, record


def _owned_completed_files(username, ids, kind, stage, record, expected):
    if not isinstance(ids, list) or any(isinstance(item, bool) for item in ids):
        raise DigitalHumanRequestError("子任务编号格式无效")
    try:
        normalized = [int(item) for item in ids]
    except (TypeError, ValueError):
        raise DigitalHumanRequestError("子任务编号格式无效")
    if len(normalized) != expected or len(set(normalized)) != expected:
        raise DigitalHumanRequestError("子任务数量不完整或包含重复任务")
    placeholders = ",".join("?" for _ in normalized)
    with closing(jdb()) as connection:
        rows = connection.execute(
            "SELECT id,kind,status,payload,result FROM jobs WHERE username=? "
            "AND COALESCE(deleted,0)=0 AND id IN (%s)" % placeholders,
            [username] + normalized,
        ).fetchall()
    by_id = {int(row["id"]): row for row in rows}
    files = []
    for index, job_id in enumerate(normalized):
        row = by_id.get(job_id)
        if not row or row["kind"] != kind or row["status"] != "done":
            raise DigitalHumanRequestError(
                "子任务 #%d 不存在、未完成或不属于当前账号" % job_id,
                "child_job_unavailable", 409,
            )
        _binding(row["payload"], job_id, stage, record, index)
        try:
            result = json.loads(row["result"] or "{}")
        except Exception:
            result = {}
        rel = legacy._result_file(result, kind)
        try:
            path = (OUT_DIR / rel).resolve()
            path.relative_to(OUT_DIR.resolve())
        except Exception:
            path = None
        if not path or not path.is_file() or path.stat().st_size <= 0:
            raise DigitalHumanRequestError(
                "子任务 #%d 的本地文件已不可用" % job_id,
                "child_file_unavailable", 409,
            )
        files.append(path.relative_to(OUT_DIR.resolve()).as_posix())
    return normalized, files


def _owned_material_files(username, job_ids, asset_ids, record, expected,
                          db_factory=None):
    if not isinstance(job_ids, list) or not isinstance(asset_ids, list):
        raise DigitalHumanRequestError("正文素材提交格式无效")
    if len(job_ids) != expected or len(asset_ids) != expected:
        raise DigitalHumanRequestError("正文素材数量不完整")
    normalized_jobs, normalized_assets, files, media_types = [], [], [], []
    seen_jobs = set()
    for index, (raw_job_id, raw_asset_id) in enumerate(zip(job_ids, asset_ids)):
        asset_id = str(raw_asset_id or "").strip().lower()
        try:
            job_id = int(raw_job_id or 0)
        except (TypeError, ValueError):
            job_id = -1
        if asset_id:
            if job_id != 0:
                raise DigitalHumanRequestError("同一个正文镜头不能同时绑定任务和素材")
            asset = _load_material_asset(
                asset_id, username, record["run_id"], record["plan_digest"], index,
                db_factory=db_factory,
            )
            normalized_jobs.append(0)
            normalized_assets.append(asset_id)
            files.append(asset["file"])
            media_types.append(asset["media_type"])
            continue
        if job_id <= 0 or job_id in seen_jobs:
            raise DigitalHumanRequestError("AI 补图任务编号无效或重复")
        seen_jobs.add(job_id)
        with closing(jdb()) as connection:
            row = connection.execute(
                "SELECT id,kind,status,payload,result FROM jobs WHERE id=? AND username=? "
                "AND COALESCE(deleted,0)=0", (job_id, username),
            ).fetchone()
        if not row or row["kind"] != "image" or row["status"] != "done":
            raise DigitalHumanRequestError(
                "AI 补图任务 #%d 不存在、未完成或不属于当前账号" % job_id,
                "child_job_unavailable", 409,
            )
        _binding(row["payload"], job_id, "material", record, index)
        try:
            result = json.loads(row["result"] or "{}")
        except Exception:
            result = {}
        rel = legacy._result_file(result, "image")
        try:
            path = (OUT_DIR / rel).resolve()
            path.relative_to(OUT_DIR.resolve())
        except Exception:
            path = None
        if not path or not path.is_file() or path.stat().st_size <= 0:
            raise DigitalHumanRequestError(
                "AI 补图任务 #%d 的文件已不可用" % job_id,
                "child_file_unavailable", 409,
            )
        normalized_jobs.append(job_id)
        normalized_assets.append("")
        files.append(path.relative_to(OUT_DIR.resolve()).as_posix())
        media_types.append("image")
    return normalized_jobs, normalized_assets, files, media_types


def prepare_compose_payload(payload, username, consent_record=None):
    if not isinstance(payload, dict):
        raise DigitalHumanRequestError("请求体必须是 JSON 对象")
    allowed = {
        "pipeline", "mode", "script", "plan_digest", "video_job_ids",
        "material_job_ids", "material_asset_ids", "digital_human_pipeline", "digital_human_stage",
        "digital_human_run_id", "digital_human_plan_digest",
        "digital_human_consent_id", "digital_human_script",
        "digital_human_gesture_count", "digital_human_item_index",
        "digital_human_narration_mode", "digital_human_audio_upload_id",
        "digital_human_video_upload_id",
    }
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise DigitalHumanRequestError("提交包含不支持字段：" + ", ".join(unknown))
    if str(payload.get("pipeline") or "").strip().lower() != PIPELINE:
        raise DigitalHumanRequestError("pipeline 无效")
    frozen = _authoritative_plan(payload, username)
    if str(payload.get("plan_digest") or "").lower() != frozen["plan_digest"]:
        raise DigitalHumanRequestError("制作方案已变化，请重新开始生成", "plan_digest_mismatch", 409)
    if not isinstance(consent_record, dict) or consent_record.get("purpose") != CONSENT_PURPOSE:
        raise DigitalHumanRequestError("缺少服务端已验证的授权记录，请重新确认授权", "consent_required", 403)
    authoritative = {
        "digital_human_pipeline": CONSENT_PURPOSE,
        "digital_human_stage": "compose",
        "digital_human_consent_id": str(consent_record.get("id") or ""),
        "digital_human_run_id": str(consent_record.get("run_id") or ""),
        "digital_human_plan_digest": str(consent_record.get("plan_digest") or "").lower(),
    }
    if (str(consent_record.get("username") or "") != str(username or "")
            or authoritative["digital_human_plan_digest"] != frozen["plan_digest"]
            or any(str(payload.get(key) or "") != value for key, value in authoritative.items())):
        raise DigitalHumanRequestError(
            "成片授权与本次制作流程不匹配，请重新确认授权",
            "consent_binding_mismatch", 403,
        )
    video_ids, video_files = _owned_completed_files(
        username, payload.get("video_job_ids"), "video", "talking", consent_record,
        frozen["segment_count"],
    )
    material_ids, material_asset_ids, material_files, material_types = _owned_material_files(
        username, payload.get("material_job_ids"), payload.get("material_asset_ids"),
        consent_record, frozen["material_count"],
    )
    prepared = dict(frozen)
    prepared.update(authoritative)
    prepared.update({
        "pipeline": PIPELINE, "mode": PIPELINE,
        "video_job_ids": video_ids, "material_job_ids": material_ids,
        "material_asset_ids": material_asset_ids,
        "video_files": video_files, "material_files": material_files,
        "material_types": material_types,
        "material_generate_count": 0,
    })
    return prepared


def _visual_items(windows, slots):
    items = ([{"kind": "presenter", "start": start, "end": end}
              for start, end in windows] +
             [dict(slot, kind="material") for slot in slots])
    return sorted(items, key=lambda item: (float(item["start"]), 0 if item["kind"] == "presenter" else 1))


def compose(payload, persist_state=None):
    from . import video as video_domain

    job_id = int(payload.get("_job_id") or 0)
    if not job_id:
        raise RuntimeError("数字人成片缺少任务编号")
    videos = [(OUT_DIR / rel).resolve() for rel in payload.get("video_files") or []]
    materials = [(OUT_DIR / rel).resolve() for rel in payload.get("material_files") or []]
    material_types = list(payload.get("material_types") or [])
    if (len(videos) != int(payload.get("segment_count") or 0)
            or len(materials) != int(payload.get("material_count") or 0)
            or len(material_types) != len(materials)
            or any(value not in {"image", "video"} for value in material_types)):
        raise RuntimeError("数字人成片子任务数量不完整")
    for path in videos + materials:
        path.relative_to(OUT_DIR.resolve())
        if not path.is_file() or path.stat().st_size <= 0:
            raise RuntimeError("数字人成片子任务文件不可用")
    if persist_state:
        persist_state("composing")
    out_dir = video_domain.VIDEO_OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    normalized = []
    durations = []
    for index, source in enumerate(videos):
        target = out_dir / ("digital_human_v2_%d_part_%d.mp4" % (job_id, index + 1))
        legacy._run([
            "ffmpeg", "-y", "-i", str(source), "-vf",
            "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1,fps=30",
            "-c:v", "libx264", "-preset", "fast", "-crf", "21", "-c:a", "aac",
            "-ar", "48000", "-ac", "2", "-movflags", "+faststart", str(target),
        ])
        normalized.append(target)
        relative = target.resolve().relative_to(OUT_DIR.resolve()).as_posix()
        durations.append(video_domain._probe_video_duration(relative))
    if any(duration <= 0 for duration in durations):
        raise RuntimeError("数字人口播子片段时长无效")
    joined = out_dir / ("digital_human_v2_%d_joined.mp4" % job_id)
    concat_file = out_dir / ("digital_human_v2_%d_concat.txt" % job_id)
    concat_file.write_text("".join(
        "file '%s'\n" % str(path).replace("'", "'\\''") for path in normalized
    ), encoding="utf-8")
    legacy._run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file), "-c", "copy", str(joined)])
    duration = sum(durations)
    precision_mode = str(payload.get("narration_mode") or "").strip().lower() == "precision"
    windows = ([[0.0, round(duration, 3)]] if precision_mode
               else timeline.presenter_windows(durations, duration))
    slots = ([] if precision_mode
             else timeline.material_slots(windows, duration, len(materials)))
    items = _visual_items(windows, slots)
    command = ["ffmpeg", "-y", "-i", str(joined)]
    for material, media_type, slot in zip(materials, material_types, slots):
        if media_type == "image":
            command.extend(["-loop", "1", "-t", "%.3f" % slot["duration"], "-i", str(material)])
        else:
            command.extend(["-stream_loop", "-1", "-i", str(material)])
    filters = []
    labels = []
    material_input_by_index = {slot["index"]: index + 1 for index, slot in enumerate(slots)}
    for index, item in enumerate(items):
        label = "clip%d" % index
        labels.append("[%s]" % label)
        start, end = float(item["start"]), float(item["end"])
        if item["kind"] == "presenter":
            filters.append(
                "[0:v]trim=start=%.3f:end=%.3f,setpts=PTS-STARTPTS,"
                "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1,fps=30,"
                "tpad=stop_mode=clone:stop_duration=1,trim=duration=%.3f,setpts=PTS-STARTPTS[%s]"
                % (start, end, end - start, label)
            )
        else:
            source_index = material_input_by_index[item["index"]]
            if material_types[source_index - 1] == "image":
                filters.append(
                    "[%d:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
                    "zoompan=z='min(zoom+0.00035,1.055)':x='iw/2-(iw/zoom/2)':"
                    "y='ih/2-(ih/zoom/2)':d=1:s=1080x1920:fps=30,"
                    "setsar=1,format=yuv420p,"
                    "tpad=stop_mode=clone:stop_duration=1,trim=duration=%.3f,"
                    "setpts=PTS-STARTPTS[%s]" % (source_index, end - start, label)
                )
            else:
                filters.append(
                    "[%d:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
                    "setsar=1,fps=30,tpad=stop_mode=clone:stop_duration=1,"
                    "trim=duration=%.3f,setpts=PTS-STARTPTS[%s]" %
                    (source_index, end - start, label)
                )
    filters.append("%sconcat=n=%d:v=1:a=0[joinedv]" % ("".join(labels), len(labels)))
    filters.append(
        "[joinedv]tpad=stop_mode=clone:stop_duration=1,trim=duration=%.3f,"
        "setpts=PTS-STARTPTS[outv]" % duration
    )
    composed = out_dir / ("digital_human_v2_%d_composed.mp4" % job_id)
    command.extend([
        "-filter_complex", ";".join(filters), "-map", "[outv]", "-map", "0:a:0",
        "-af", "apad=pad_dur=1",
        "-t", "%.3f" % duration, "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(composed),
    ])
    legacy._run(command)
    rel = composed.resolve().relative_to(OUT_DIR.resolve()).as_posix()
    if persist_state:
        persist_state("subtitle_processing", plain_video_file=rel)
    subtitle_error = ""
    try:
        final_rel = video_domain.burn_subtitle(
            rel, known_text=payload.get("copy"), style_key="white",
            job_id=job_id, position="bottom",
        )
    except Exception as exc:
        final_rel = rel
        subtitle_error = str(exc)[:220] or "字幕处理失败"
    _path, width, height, final_duration = legacy._verify_final_video(
        video_domain, final_rel, duration,
    )
    result = {
        "pipeline": PIPELINE, "video_file": final_rel,
        "url": "/api/gen/file/" + final_rel, "duration": round(final_duration, 3),
        "width": width, "height": height,
        "gesture_count": int(payload.get("gesture_count") or 0),
        "video_count": len(videos), "material_count": len(materials),
        "presenter_windows": windows,
        "child_jobs": {"videos": payload.get("video_job_ids"), "materials": payload.get("material_job_ids")},
        "verification": {
            "resolution": "1080x1920", "frame_rate": 30,
            "subtitle": "whisper" if not subtitle_error else "unavailable",
            "audio_source": "continuous_presenter_narration", "audio_stream": True,
            "duration_sync": True, "black_frame_check": True,
            "presenter_interval_seconds": ("full_video" if precision_mode else "20-30"),
            "lipsync": ("heygen_precision" if precision_mode else "heygen_avatar"),
            "mouth_motion_review": ("required" if precision_mode else "sampled"),
            "visible_source_labels": False,
        },
        "subtitle_retryable": bool(subtitle_error),
    }
    if subtitle_error:
        result["subtitle_error"] = subtitle_error
    if persist_state:
        persist_state("completed", plain_video_file=rel, subtitle_video_file=final_rel,
                      subtitle_error=subtitle_error)
    return result
