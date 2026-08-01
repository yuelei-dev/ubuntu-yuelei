# -*- coding: utf-8 -*-
"""爆款拆解：竞品视频链接 → 下载 → 抽帧 → ASR → 智谱多模态（GPT 安全回退）→ 分镜脚本"""
import os, json, time, base64, tempfile, subprocess, shutil, math, re, urllib.parse, urllib.request
import hashlib
import http.client
import inspect
import unicodedata
import urllib.error
from contextlib import closing
from difflib import SequenceMatcher

from .core import OPENAI_BASE, OPENAI_KEY, jdb
from . import egress

# 不支持的平台（视频号加密流需要 Isaac64 解密，暂不支持）
_UNSUPPORTED_PLATFORMS = {"channels", "weixin", "wechat"}
_BREAKDOWN_MODE_SCENES = "scenes"
_BREAKDOWN_MODE_REVERSE_PROMPT = "reverse_prompt"
_BREAKDOWN_SUPPORTED_MODES = {_BREAKDOWN_MODE_SCENES, _BREAKDOWN_MODE_REVERSE_PROMPT}
BREAKDOWN_DOWNLOAD_BUDGET = max(
    30, int(os.environ.get("BREAKDOWN_DOWNLOAD_BUDGET", "180") or "180")
)
BREAKDOWN_MAX_DOWNLOAD_BYTES = max(
    25 * 1024 * 1024,
    int(os.environ.get("BREAKDOWN_MAX_DOWNLOAD_BYTES", str(200 * 1024 * 1024))
        or str(200 * 1024 * 1024)),
)
# breakdown reaper grace is 600s. Keep one absolute analysis budget below it.
BREAKDOWN_ANALYSIS_BUDGET = max(
    60,
    min(
        540,
        int(os.environ.get("BREAKDOWN_ANALYSIS_BUDGET", "540") or "540"),
    ),
)
_GEMINI_REVERSE_MODEL = "gemini-3.1-pro-preview"
_GEMINI_API_BASE = "https://generativelanguage.googleapis.com"
_GEMINI_INLINE_MAX_BYTES = 14 * 1024 * 1024
_GEMINI_INLINE_MAX_DURATION = 15.0
_GEMINI_MAX_MEDIA_BYTES = 200 * 1024 * 1024
_GEMINI_INLINE_MAX_REQUEST_BYTES = 18_000_000
_GEMINI_MAX_RESPONSE_BYTES = 64 * 1024
_GEMINI_REQUEST_TIMEOUT = max(
    30, min(240, int(os.environ.get("BREAKDOWN_GEMINI_TIMEOUT", "180") or "180"))
)
_GEMINI_UNKNOWN = "unknown"
_GEMINI_NOT_APPLICABLE = "not_applicable"
_REVERSE_MAX_SEGMENT_CHARS = 1200
_REVERSE_MAX_TOTAL_CHARS = 4800
_REVERSE_DUPLICATE_SEQUENCE_THRESHOLD = 0.80
_REVERSE_DUPLICATE_SHINGLE_THRESHOLD = 0.70
_REVERSE_STATIC_SSIM_THRESHOLD = 0.995
# Deliberately conservative: ordinary motion must not be mislabeled as a cut.
# A value below this only means the two sampled frames are visually
# discontinuous enough that the model must represent them as separate shots.
_REVERSE_HARD_CUT_SSIM_THRESHOLD = 0.35
_REVERSE_SCENE_SCORE_THRESHOLD = 0.30
_REVERSE_MIN_SEGMENT_SECONDS = 1.0
_REVERSE_MAX_SEGMENTS = 4
_REVERSE_TRANSITION_TYPES = {
    "none",
    "hard_cut",
    "fade",
    "dissolve",
    "wipe",
    "occlusion",
    "whip_pan",
    "push_pull",
    "unknown",
}
_REVERSE_STATIC_ACTION_MARKERS = (
    "动作无变化",
    "姿态无变化",
    "没有动作变化",
    "没有明显动作变化",
    "没有姿态变化",
    "未观察到明显动作变化",
    "未见明显动作变化",
    "人物保持不动",
    "主体保持不动",
    "人物静止不动",
    "主体静止不动",
    "保持同一姿态",
    "保持原有姿态",
    "画面完全静止",
    "画面没有变化",
    "画面无变化",
    "画面内容保持一致",
    "没有任何变化",
    "主体保持静止，未观察到位置或形态变化",
    "无动作",
    "没有动作",
    "未见动作",
    "未观察到动作",
    "未发生动作",
)
_REVERSE_MOTION_ACTION_MARKERS = (
    "坐起", "起身", "抬起", "举起", "放下", "转身", "回头",
    "伸出", "收回", "弯曲", "迈步", "走动", "移动", "前倾", "后仰",
)
_REVERSE_BACK_FACING_MARKERS = (
    "背对镜头", "背向镜头", "后背朝向镜头",
)
_REVERSE_FACE_CLAIM_MARKERS = (
    "表情", "神情", "微笑", "笑容", "眼神", "目光",
    "闭眼", "睁眼", "皱眉",
)
_REVERSE_UNSUPPORTED_INFERENCE_MARKERS = (
    "似乎", "仿佛", "感受风", "享受微风",
    "阳光明媚", "绿草如茵",
)
_REVERSE_SOFT_OBSERVABLE_REWRITES = {
    "阳光明媚": "明亮日间自然光",
    "绿草如茵": "绿色草地",
}
_REVERSE_SOFT_DROP_CLAUSE_MARKERS = (
    "似乎", "仿佛", "感受风", "享受微风",
)
_GEMINI_SOFT_CORRECTABLE_FACT_FIELDS = {
    "subject_identity", "subject_appearance", "wardrobe", "position_scale",
    "action_start", "action_process", "action_end", "direction_speed",
    "foreground", "midground", "background", "shot_scale", "camera_angle",
    "camera_movement", "composition", "lighting_color", "style_texture",
    "rhythm", "continuity",
}
_REVERSE_INVALID_SOUND_MARKERS = (
    "未观察到声音", "从画面未观察到声音", "画面没有声音",
    "画面无声音",
)
_REVERSE_NO_SPEECH_MARKERS = (
    "未检测到可辨识语音", "未检测到可识别语音",
)
_REVERSE_UNRELIABLE_ORIENTATION_MARKERS = (
    "面向树根", "朝向树根",
)
_REVERSE_INTERPRETIVE_ACTION_MARKERS = (
    "整理", "调整", "检查", "寻找", "准备", "感受",
)
# A two-frame reverse request cannot independently establish these ambiguous
# garment/accessory labels.  Treat them as unknown/generic unless a future
# verifier supplies evidence independent from the model response itself.
_REVERSE_AMBIGUOUS_ACCESSORY_MARKERS = (
    "围巾", "披肩", "飘带",
)
_REVERSE_ATTRIBUTE_NEGATION_MARKERS = (
    "未佩戴", "没有佩戴", "未穿", "没有穿", "未见", "没有", "不",
)
_REVERSE_ATTRIBUTE_TOKEN_GROUPS = (
    (
        "黑色", "白色", "灰色", "粉色", "红色", "橙色", "黄色",
        "绿色", "蓝色", "紫色", "棕色", "金色", "银色",
    ),
    ("冷色", "暖色", "中性色"),
    ("左侧", "右侧", "中央", "中间", "上方", "下方"),
    ("俯视", "仰视", "平视"),
    ("特写", "近景", "中景", "全景", "远景", "大远景"),
    ("围巾", "披肩", "飘带", "卫衣", "连帽服", "长衣", "外套", "裙"),
)
_REVERSE_GLOBAL_FACT_FIELDS = (
    ("subject_identity", "主体身份与外观"),
    ("wardrobe", "服装与随身物"),
    ("recurring_scene_objects", "重复场景与关键物"),
    ("scene_style", "场景风格"),
    ("camera_style", "镜头风格"),
    ("lighting_style", "光线与色调"),
)
_REVERSE_FRAME_EVIDENCE_FIELDS = (
    "subject", "scene", "action", "camera", "lighting",
)
_REVERSE_EMPTY_PLACEHOLDER_VALUES = {
    "主体", "主体信息", "主体细节", "场景", "场景信息", "场景细节",
    "动作", "动作信息", "动作细节", "动作自然", "无法确认", "不确定",
    "无可确认信息", "未识别", "未知",
}
_REVERSE_FIXED_CONTINUITY_MARKERS = (
    "与上一段保持一致", "与前一段保持一致",
    "保持与上一段一致", "保持与前一段一致",
    "承接上一段", "承接前一段", "延续上一段", "延续前一段",
    "与上一段一致", "与前一段一致", "同上一段", "同前一段",
)
_REVERSE_MAX_GLOBAL_CHARS = 220
_REVERSE_SLOT_STATUSES = {"observed", "unknown", "not_applicable"}
_REVERSE_GENERATION_SLOT_GROUPS = {
    "subject": (
        "identity", "appearance", "wardrobe", "position_scale",
    ),
    "action": (
        "motion_type", "start", "process", "end",
        "direction_speed", "associated_object",
    ),
    "scene": (
        "foreground", "midground", "background", "spatial_relationship",
    ),
    "camera": (
        "shot_size", "camera_position", "viewing_angle",
        "composition", "movement",
    ),
    "lighting": ("direction_brightness", "color_tone"),
    "style": ("visual_style", "texture"),
    "rhythm": ("pacing",),
    "continuity": ("retained", "changed"),
}
_REVERSE_ALWAYS_APPLICABLE_SLOTS = {
    "subject.identity",
    "subject.appearance",
    "subject.position_scale",
    "action.motion_type",
    "action.start",
    "action.end",
    "scene.background",
    "scene.spatial_relationship",
    "camera.shot_size",
    "camera.camera_position",
    "camera.viewing_angle",
    "camera.composition",
    "camera.movement",
    "lighting.direction_brightness",
    "lighting.color_tone",
    "style.visual_style",
    "style.texture",
    "rhythm.pacing",
}
_REVERSE_GENERATION_SLOT_LABELS = {
    "subject.identity": "主体身份类别",
    "subject.appearance": "主体外观",
    "subject.wardrobe": "服装与随身物",
    "subject.position_scale": "主体位置与画面占比",
    "action.motion_type": "动作类型",
    "action.start": "动作起点",
    "action.process": "动作过程",
    "action.end": "动作终点",
    "action.direction_speed": "动作方向与可见速度",
    "action.associated_object": "动作关联物",
    "scene.foreground": "前景",
    "scene.midground": "中景环境",
    "scene.background": "背景",
    "scene.spatial_relationship": "空间关系",
    "camera.shot_size": "景别",
    "camera.camera_position": "机位",
    "camera.viewing_angle": "视角",
    "camera.composition": "构图",
    "camera.movement": "可见运镜",
    "lighting.direction_brightness": "光线方向与明暗",
    "lighting.color_tone": "光线色调",
    "style.visual_style": "视觉风格",
    "style.texture": "画面材质",
    "rhythm.pacing": "镜头节奏",
    "continuity.retained": "连续性保留项",
    "continuity.changed": "连续性变化项",
}
_REVERSE_VISUAL_SEMANTIC_CONTRACT = {
    "definition": "visual_semantic_not_pixel",
    "score_scope": "reverse_prompt_source_fidelity_and_generation_readiness",
    "target_score": 80,
    "components": {
        "source_evidence_coverage": {
            "target": 100,
            "definition": "observed_slots_with_valid_source_frame_evidence",
        },
        "generation_readiness": {
            "target": 80,
            "definition": "observed_applicable_slots_over_all_applicable_slots",
        },
        "factual_consistency": {
            "target": 100,
            "definition": "no_hard_cut_merge_or_cross_field_evidence_conflict",
        },
    },
    "critical_failures": (
        "hard_cut_merged_as_action",
        "unsupported_fact",
        "subject_scene_action_error",
    ),
    "unknown_semantics": "unknown_is_not_ready_but_is_safer_than_invention",
    "suggested_parameters_scope": "recommendation_not_observed_source_fact",
    "requires_reference_guidance": True,
    "generated_video_similarity_claim": False,
}


_SUPPORTED_LINK_HOSTS = (
    "douyin.com", "iesdouyin.com", "xiaohongshu.com", "xhslink.com",
)
_SHARE_URL_RE = re.compile(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+")
_UPLOAD_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")


def _ensure_upload_table(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS breakdown_uploads(
        token TEXT PRIMARY KEY,
        username TEXT NOT NULL,
        suffix TEXT NOT NULL,
        job_id INTEGER NOT NULL UNIQUE,
        created_at INTEGER NOT NULL
    )""")


def _upload_root():
    from . import core
    root = (core.OUT_DIR / "_breakdown_uploads").resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _normalize_supported_link(value):
    match = _SHARE_URL_RE.search(str(value or ""))
    if not match:
        raise ValueError("请粘贴抖音或小红书的完整 http(s) 分享链接")
    url = match.group(0)
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in {"http", "https"} or not any(
            host == suffix or host.endswith("." + suffix)
            for suffix in _SUPPORTED_LINK_HOSTS):
        raise ValueError("仅支持抖音或小红书公开视频链接")
    return url


def _resolved_link(url):
    """Resolve a supported share URL before charging and validate its work ID."""
    import tikhub

    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    path = parsed.path or "/"
    expected_platform = "xhs" if (
        host == "xhslink.com" or host.endswith(".xhslink.com")
        or host == "xiaohongshu.com" or host.endswith(".xiaohongshu.com")
    ) else "douyin"

    if expected_platform == "douyin":
        direct = re.search(r"/video/(\d{15,21})(?:/|$)", path)
        if direct:
            info = {
                "platform": "douyin",
                "id": direct.group(1),
                "note_type": "video",
            }
        elif not (
                host == "v.douyin.com" or host.endswith(".v.douyin.com")):
            raise ValueError("抖音链接缺少具体作品 ID")
        else:
            try:
                info = tikhub.parse_link(url)
            except Exception as exc:
                raise ValueError("抖音短链无法解析，请确认链接公开且未失效") from exc
    else:
        direct = re.search(
            r"/(?:explore|discovery/item|item)/([0-9a-fA-F]{16,64})(?:/|$)",
            path,
        )
        if direct:
            info = {
                "platform": "xhs",
                "id": direct.group(1),
                "note_type": None,
            }
        elif not (host == "xhslink.com" or host.endswith(".xhslink.com")):
            raise ValueError("小红书链接缺少具体笔记 ID")
        else:
            try:
                info = tikhub.parse_link(url)
            except Exception as exc:
                raise ValueError("小红书短链无法解析，请确认链接公开且未失效") from exc

    if not isinstance(info, dict):
        raise ValueError("无法解析该视频链接，请确认链接公开且未失效")
    platform = str(info.get("platform") or "").strip().lower()
    work_id = str(info.get("id") or "").strip()
    valid_id = (
        platform == "douyin" and bool(re.fullmatch(r"\d{15,21}", work_id))
    ) or (
        platform == "xhs" and bool(re.fullmatch(r"[0-9a-fA-F]{16,64}", work_id))
    )
    if platform != expected_platform or not valid_id:
        raise ValueError("无法解析该视频链接，请确认链接公开且未失效")
    return {
        "url": url,
        "platform": platform,
        "id": work_id,
        "note_type": info.get("note_type"),
    }


def validate_breakdown_payload(payload):
    """Validate and resolve public links before the job is charged or enqueued."""
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 JSON 对象")
    if payload.get("local_path") or payload.get("local_media_path") or payload.get("upload_token"):
        raise ValueError("本地素材只能通过专用上传接口提交")
    body = dict(payload)
    body.pop("_resolved_link", None)
    body.pop("_resolved_links", None)
    mode = str(body.get("mode") or _BREAKDOWN_MODE_SCENES).strip().lower()
    if mode not in _BREAKDOWN_SUPPORTED_MODES:
        raise ValueError("不支持的拆解模式")
    raw_urls = body.get("urls")
    if isinstance(raw_urls, list):
        if not raw_urls:
            raise ValueError("请至少提供一个视频链接")
        if len(raw_urls) > 5:
            raise ValueError("一次最多提交 5 条链接")
        urls = [_normalize_supported_link(item) for item in raw_urls]
        if mode == _BREAKDOWN_MODE_REVERSE_PROMPT and len(urls) != 1:
            raise ValueError("提示词反推暂仅支持单条视频链接")
        body.pop("url", None)
        body["urls"] = urls
        body["_resolved_links"] = [_resolved_link(url) for url in urls]
    else:
        body["url"] = _normalize_supported_link(body.get("url"))
        body.pop("urls", None)
        body["_resolved_link"] = _resolved_link(body["url"])
    body["mode"] = mode
    return body


def handle_local_upload_request(handler):
    """Authenticate the raw upload route before entering the trusted token flow."""
    from . import core
    user = core.verify(handler._token())
    if not user:
        return handler._send(401, {"detail": "\u672a\u767b\u5f55"})
    if core._must_change_password(user):
        return handler._send(403, {"detail": "\u8bf7\u5148\u4fee\u6539\u521d\u59cb\u5bc6\u7801"})
    return handle_local_upload(handler, user)


def handle_local_upload(handler, user):
    """Validate a local upload, charge once, persist its token, and enqueue it."""
    from . import core
    _, points_domain, _ = core._domains()
    try:
        core.feature_flags.require_enabled("breakdown")
    except core.feature_flags.FeatureDisabled as exc:
        return handler._send(503, {"detail": str(exc)})
    if core.is_shutting_down():
        return handler._send(503, {
            "detail": "服务正在更新，请稍后重试", "code": "shutting_down",
            "retry_after_ms": 5000,
        })

    query = core.urllib.parse.parse_qs(core.urllib.parse.urlparse(handler.path).query)
    media_type = str((query.get("media_type") or [""])[0]).strip().lower()
    allowed = {
        "image": {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"},
        "video": {"video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm"},
    }
    content_type = str(handler.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    if media_type not in allowed or content_type not in allowed[media_type]:
        return handler._send(415, {"detail": "仅支持 JPG/PNG/WEBP 图片或 MP4/MOV/WEBM 视频"})
    try:
        content_length = int(handler.headers.get("Content-Length") or 0)
    except (TypeError, ValueError):
        content_length = 0
    maximum = 20 * 1024 * 1024 if media_type == "image" else 200 * 1024 * 1024
    if content_length <= 0 or content_length > maximum:
        return handler._send(413, {"detail": "图片最大 20MB，视频最大 200MB"})
    cost = points_domain.cost_of("breakdown", {
        "media_type": media_type, "mode": _BREAKDOWN_MODE_REVERSE_PROMPT,
    })
    points = int(points_domain.get_points(user["username"]) or 0)
    if points < cost:
        return handler._send(402, {
            "detail": "点数不足", "need": cost, "points": points,
        })
    active_jobs = core._user_active_job_count(user["username"])
    if active_jobs >= core.MAX_USER_ACTIVE_JOBS:
        return handler._send(429, {
            "detail": "当前生成任务较多，请完成后再提交", "code": "active_job_cap",
            "active_jobs": active_jobs, "max_active_jobs": core.MAX_USER_ACTIVE_JOBS,
            "retry_after_ms": 4000,
        })

    temp_path = ""
    upload_token = __import__("uuid").uuid4().hex
    suffix = allowed[media_type][content_type]
    try:
        root = _upload_root()
        temp_path = str(root / (upload_token + suffix))
        with open(temp_path, "xb") as uploaded:
            remaining = content_length
            while remaining:
                chunk = handler.rfile.read(min(65536, remaining))
                if not chunk:
                    raise ValueError("上传文件读取不完整")
                uploaded.write(chunk)
                remaining -= len(chunk)
        with open(temp_path, "rb") as uploaded:
            signature = uploaded.read(16)
        valid_signature = {
            "image/jpeg": signature.startswith(b"\xff\xd8\xff"),
            "image/png": signature.startswith(b"\x89PNG\r\n\x1a\n"),
            "image/webp": signature.startswith(b"RIFF") and signature[8:12] == b"WEBP",
            "video/mp4": len(signature) >= 12 and signature[4:8] == b"ftyp",
            "video/quicktime": len(signature) >= 12 and signature[4:8] == b"ftyp",
            "video/webm": signature.startswith(b"\x1a\x45\xdf\xa3"),
        }[content_type]
        if not valid_signature:
            raise ValueError("文件内容与声明格式不一致")
        if media_type == "video":
            duration = _probe_duration(temp_path)
            if duration <= 0:
                raise ValueError("无法读取视频时长")
            if duration > 120.05:
                raise ValueError("视频最长支持 2 分钟")
        body = {
            "upload_token": upload_token,
            "media_type": media_type,
            "mode": _BREAKDOWN_MODE_REVERSE_PROMPT,
        }
        with core._submission_lock:
            points_left = points_domain.deduct_points(
                user["username"], cost, "job:breakdown"
            )
            try:
                now = int(time.time())
                with closing(core.jdb()) as connection:
                    _ensure_upload_table(connection)
                    cursor = connection.execute(
                        "INSERT INTO jobs(kind,username,cost,payload,created_at,updated_at,owner)"
                        " VALUES(?,?,?,?,?,?,?)",
                        ("breakdown", user["username"], cost,
                         json.dumps(body, ensure_ascii=False), now, now,
                         core.SERVICE_OWNER),
                    )
                    job_id = int(cursor.lastrowid)
                    connection.execute(
                        "INSERT INTO breakdown_uploads(token,username,suffix,job_id,created_at)"
                        " VALUES(?,?,?,?,?)",
                        (upload_token, user["username"], suffix, job_id, now),
                    )
                    connection.commit()
            except Exception:
                points_domain.safe_refund_points(
                    user["username"], cost, "local breakdown create rollback"
                )
                raise
            if not core.enqueue_job(job_id, "breakdown", _BREAKDOWN_MODE_REVERSE_PROMPT):
                rejected = core._reject_pending_job(
                    job_id, user["username"], cost, "任务队列已满，请稍后再试"
                )
                if rejected:
                    _remove_trusted_upload(
                        upload_token, user["username"], job_id, temp_path
                    )
                    return handler._send(429, {
                        "detail": "任务队列已满，请稍后再试", "code": "queue_full",
                        "retry_after_ms": 4000,
                    })
        success_response = {
            "job_id": job_id, "cost": cost, "points_left": points_left,
        }
    except points_domain.AuthPointsError as exc:
        _remove_upload(temp_path)
        return handler._send(
            exc.status if exc.status in (402, 403) else 502,
            {"detail": exc.detail, "need": cost},
        )
    except ValueError as exc:
        _remove_upload(temp_path)
        return handler._send(400, {"detail": str(exc)[:180]})
    except Exception as exc:
        _remove_upload(temp_path)
        return handler._send(500, {"detail": "上传任务创建失败，请重试"})
    # The paid job and its upload binding are already durable and queued.
    # Keep response I/O outside the pre-commit cleanup scope: a disconnected
    # client must not delete the source file that the worker still owns.
    return handler._send(200, success_response)


def _remove_upload(path):
    if path:
        try:
            os.unlink(path)
        except Exception:
            pass


def gen_breakdown(payload):
    """下载视频 → 抽帧 → ASR → GPT-4o 多模态分析 → 分镜拆解。
    由 run_job 调用，走标准 job 生命周期（扣点/退点/reaper 全自动）。
    支持单个 url 或批量 urls（≤5 条，顺序处理）。"""
    import tikhub
    upload_token = str((payload or {}).get("upload_token") or "").strip().lower()
    if upload_token:
        return _do_local_reverse(payload, upload_token)
    if (payload or {}).get("local_path"):
        raise ValueError("禁止提交服务器本地路径")
    if (payload or {}).get("local_media_path"):
        from .local_reverse_processor import gen_local_reverse
        return gen_local_reverse(payload)

    mode = _normalize_mode(payload)
    urls = payload.get("urls")
    if urls and isinstance(urls, list):
        urls = [u.strip() for u in urls if isinstance(u, str) and u.strip()]
        if not urls:
            raise ValueError("请粘贴抖音/小红书/视频号链接")
        if len(urls) > 5:
            raise ValueError("批量拆解最多 5 条链接")
        return _do_batch_breakdown(payload, urls)

    url = (payload.get("url") or "").strip()
    if not url:
        raise ValueError("请粘贴抖音/小红书/视频号链接")

    # ① 解析链接
    resolved = payload.get("_resolved_link")
    if isinstance(resolved, dict) and resolved.get("url") == url:
        info = {
            "platform": resolved.get("platform"),
            "id": resolved.get("id"),
            "note_type": resolved.get("note_type"),
        }
    else:
        info = tikhub.parse_link(url)
    platform = (info.get("platform") or "").lower()
    if platform in _UNSUPPORTED_PLATFORMS:
        raise ValueError("视频号暂不支持拆解，请粘贴抖音/小红书链接")

    return _do_breakdown(payload, info, url, mode)


def _normalize_mode(payload):
    mode = str((payload or {}).get("mode") or _BREAKDOWN_MODE_SCENES).strip().lower()
    if not mode:
        mode = _BREAKDOWN_MODE_SCENES
    if mode not in _BREAKDOWN_SUPPORTED_MODES:
        raise ValueError("mode 仅支持 scenes / reverse_prompt")
    return mode


def _do_batch_breakdown(payload, urls):
    """批量拆解：逐个处理，收拢结果。"""
    import tikhub

    job_id = payload.get("_job_id")
    results = []
    errors = []
    resolved_links = payload.get("_resolved_links")
    for idx, url in enumerate(urls):
        _heartbeat(job_id, "batch_%d_%d" % (idx + 1, len(urls)))
        try:
            if (
                isinstance(resolved_links, list)
                and len(resolved_links) == len(urls)
                and isinstance(resolved_links[idx], dict)
                and resolved_links[idx].get("url") == url
            ):
                info = {
                    "platform": resolved_links[idx].get("platform"),
                    "id": resolved_links[idx].get("id"),
                    "note_type": resolved_links[idx].get("note_type"),
                }
            else:
                info = tikhub.parse_link(url)
            platform = (info.get("platform") or "").lower()
            if platform in _UNSUPPORTED_PLATFORMS:
                errors.append({"url": url, "error": "视频号暂不支持"})
                continue
            r = _do_breakdown(payload, info, url)
            results.append(r)
        except ValueError as e:
            errors.append({"url": url, "error": str(e)})
        except Exception as e:
            errors.append({"url": url, "error": "拆解失败：" + str(e)[:200]})

    return {
        "type": "breakdown_batch",
        "results": results,
        "errors": errors,
        "total": len(urls),
    }


def _do_breakdown(payload, info, url, mode=None):
    import tikhub

    mode = mode or _normalize_mode(payload)
    det = tikhub.detail(info["platform"], info["id"], info.get("note_type"))

    job_id = payload.get("_job_id")
    _heartbeat(job_id, "downloading")
    tmp_video = None
    frame_dir = None
    tmp_video = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
    try:
        det = _download_breakdown_video(tikhub, info, det, tmp_video.name)
        duration = _normalize_duration_seconds(det.get("duration"))
        title = det.get("title") or det.get("desc") or ""

        _heartbeat(job_id, "extracting_frames")
        is_reverse = mode == _BREAKDOWN_MODE_REVERSE_PROMPT
        frame_count = 8 if is_reverse else max(4, min(10, int(duration / 5)))
        frame_pts = None
        if is_reverse:
            frame_dir, frames, frame_pts = _split_extracted_frames(
                _extract_frames(
                    tmp_video.name, frame_count, duration,
                    scale_width=1024, min_frames=8, uniform=True,
                    return_pts=True,
                )
            )
        else:
            frame_dir, frames = _extract_frames(
                tmp_video.name, frame_count, duration, scale_width=512,
            )
        model_frames = frames

        script_text = ""
        asr_failed = False
        try:
            _heartbeat(job_id, "transcribing")
            segs = tikhub.transcript(det, video_path=tmp_video.name)
            script_text = _format_transcript(segs)
            if _speech_chars(script_text) < 8:
                script_text = ""  # 热修(20260717)：实际口播字数过短≈无人声（纯音乐/歌舞），按无口播处理
            elif is_reverse and _reverse_transcript_is_abnormal(script_text, duration):
                script_text = ""
        except Exception:
            asr_failed = True

        _heartbeat(job_id, "analyzing")
        platform = info.get("platform", "")
        if mode == _BREAKDOWN_MODE_REVERSE_PROMPT:
            analysis_deadline = time.monotonic() + BREAKDOWN_ANALYSIS_BUDGET
            analysis_heartbeat = lambda: _heartbeat(job_id, "analyzing")
            prompt_result = _gemini_reverse_prompt_from_media(
                tmp_video.name,
                "video/mp4",
                title,
                duration,
                platform,
                script_text,
                deadline=analysis_deadline,
                heartbeat=analysis_heartbeat,
            )
            frames, frame_pts = _fill_reverse_window_frames(
                tmp_video.name,
                frame_dir,
                frames,
                frame_pts,
                prompt_result["windows"],
            )
            _validate_gemini_reverse_entries(
                prompt_result, frames, script_text, frame_pts=frame_pts
            )
            frame_bundle = _reverse_frame_bundle(
                frames, prompt_result["windows"], frame_pts=frame_pts
            )
            global_continuity = _reverse_global_facts_from_segments(
                prompt_result["entries"],
                frame_bundle["segment_model_source_indices"],
                frame_count=len(frames),
            )
            prompt_result["prompt"] = _assemble_reverse_prompt(
                prompt_result["entries"],
                prompt_result["windows"],
                global_continuity,
                enforce_length_limits=False,
            )
            quality_score = _score_reverse_generation_coverage(
                prompt_result["entries"],
                global_continuity,
                prompt_result["windows"],
            )
            target_score = _REVERSE_VISUAL_SEMANTIC_CONTRACT["target_score"]
            if quality_score["total"] < target_score:
                raise ValueError(
                    "反推结果生成要素覆盖度不足：%d分，至少需要%d分，请重试"
                    % (quality_score["total"], target_score)
                )
            frame_thumbnails = _frame_thumbnails(
                frame_bundle["frames"], limit=len(frame_bundle["frames"])
            )
            if len(frame_thumbnails) != len(frame_bundle["frames"]):
                raise ValueError("反推审计证据帧序列化失败，请重试")
            segment_evidence = _reverse_segment_evidence_manifest(
                prompt_result["entries"],
                prompt_result["windows"],
                frame_bundle["segment_source_indices"],
                frame_bundle["segment_model_source_indices"],
            )
            call_budget = _reverse_analysis_call_budget(
                len(prompt_result["windows"])
            )
            audit_sections = {
                "frame_manifest": frame_bundle["manifest"],
                "reference_thumbnail_indices": frame_bundle[
                    "reference_thumbnail_indices"
                ],
                "audit_thumbnail_indices": frame_bundle[
                    "audit_thumbnail_indices"
                ],
                "global_continuity": global_continuity,
                "segment_evidence": segment_evidence,
                "analysis_call_budget": call_budget,
                "timeline_audit": prompt_result.get("timeline_audit") or {},
                "attempt_audit": prompt_result.get("attempt_audit") or [],
                "quality_dimensions": _gemini_quality_dimensions(prompt_result),
                "model_provider": prompt_result.get("provider"),
                "model_id": prompt_result.get("model"),
                "model_attempts": prompt_result.get("attempts"),
                "quality_contract": _reverse_quality_contract(),
                "quality_score": quality_score,
            }
            return {
                "type": "breakdown_reverse",
                "source_url": url,
                "source_title": title,
                "source_platform": platform,
                "duration": duration,
                "prompt": prompt_result["prompt"],
                "frame_count": len(frames or []),
                # Consumers must use reference_thumbnail_indices. Short videos
                # have fewer than four segments, so array position is not a
                # safe implicit reference-image contract.
                "frame_thumbnails": frame_thumbnails,
                "reference_frame_strategy": "explicit_indices_one_per_segment",
                "reference_thumbnail_indices": frame_bundle[
                    "reference_thumbnail_indices"
                ],
                "audit_thumbnail_indices": frame_bundle[
                    "audit_thumbnail_indices"
                ],
                "frame_manifest": frame_bundle["manifest"],
                "global_continuity": global_continuity,
                "segment_evidence": segment_evidence,
                "analysis_call_budget": call_budget,
                "timeline_audit": prompt_result.get("timeline_audit") or {},
                "quality_contract": _reverse_quality_contract(),
                "quality_score": quality_score,
                # assets_store already persists sections and frame_thumbnails;
                # keeping the audit manifest here prevents cleanup from
                # leaving evidence numbers without their encoded source frame.
                "sections": {"reverse_audit": audit_sections},
                "asr_failed": asr_failed,
            }

        result = _breakdown_scenes_from_frames(title, duration, platform, script_text, frames)

        return {
            "type": "breakdown",
            "source_url": url,
            "source_title": title,
            "source_platform": platform,
            "duration": duration,
            "scenes": result.get("scenes", []),
            "analysis": result.get("analysis", ""),
            "asr_failed": asr_failed,
            "frame_thumbnails": _frame_thumbnails(frames),
        }
    finally:
        if tmp_video:
            try: os.unlink(tmp_video.name)
            except: pass
        if frame_dir:
            try: shutil.rmtree(frame_dir)
            except: pass


def _do_local_reverse(payload, upload_token):
    """Consume a trusted upload token through the same #139 reverse engine."""
    media_type = str(payload.get("media_type") or "").strip().lower()
    job_id = payload.get("_job_id")
    username = str(payload.get("_username") or "").strip()
    if media_type not in {"image", "video"}:
        raise ValueError("不支持的本地素材类型")
    if not _UPLOAD_TOKEN_RE.fullmatch(upload_token) or not username or not job_id:
        raise ValueError("无效的上传凭证")
    from . import core
    with closing(core.jdb()) as connection:
        _ensure_upload_table(connection)
        row = connection.execute(
            "SELECT suffix FROM breakdown_uploads"
            " WHERE token=? AND username=? AND job_id=?",
            (upload_token, username, int(job_id)),
        ).fetchone()
        connection.commit()
    if not row:
        raise ValueError("上传凭证不存在或不属于当前任务")
    root = _upload_root()
    candidate = (root / (upload_token + str(row["suffix"]))).resolve()
    if candidate.parent != root or not candidate.is_file():
        raise ValueError("上传文件不存在或已过期")
    path = str(candidate)
    frame_dir = None
    try:
        _heartbeat(job_id, "extracting_frames")
        if media_type == "image":
            # The reverse engine owns one auditable eight-frame bundle. For a
            # still image those entries intentionally point to the same source.
            frames = [path] * 8
            frame_pts = [0.0] * 8
            duration = 0.0
        else:
            duration = _probe_duration(path)
            if duration > 120.05:
                raise ValueError("视频最长支持 2 分钟")
            frame_dir, frames, frame_pts = _split_extracted_frames(
                _extract_frames(
                    path, 8, duration or 30,
                    scale_width=1024, min_frames=8, uniform=True,
                    return_pts=True,
                )
            )
        _heartbeat(job_id, "analyzing")
        suffix = str(row["suffix"] or "").lower()
        media_mime = {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".png": "image/png", ".webp": "image/webp",
            ".mp4": "video/mp4", ".mov": "video/quicktime",
            ".webm": "video/webm",
        }.get(suffix, "video/mp4" if media_type == "video" else "image/jpeg")
        return _reverse_result_from_frames(
            payload,
            frames,
            source_url="",
            title=os.path.basename(path),
            platform="local",
            duration=duration,
            media_path=path,
            media_mime=media_mime,
            frame_pts=frame_pts,
            frame_dir=frame_dir,
        )
    finally:
        if frame_dir:
            try:
                shutil.rmtree(frame_dir)
            except Exception:
                pass
        _remove_trusted_upload(upload_token, username, job_id, path)


def _probe_duration(path):
    try:
        process = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", path,
            ],
            check=True,
            timeout=20,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return max(0.0, float((process.stdout or "0").strip() or 0))
    except Exception as exc:
        raise ValueError("无法读取视频时长，请上传有效的视频文件") from exc


def _remove_trusted_upload(token, username, job_id, path):
    from . import core
    try:
        with closing(core.jdb()) as connection:
            _ensure_upload_table(connection)
            connection.execute(
                "DELETE FROM breakdown_uploads"
                " WHERE token=? AND username=? AND job_id=?",
                (token, username, int(job_id)),
            )
            connection.commit()
    finally:
        root = _upload_root()
        candidate = __import__("pathlib").Path(path).resolve()
        if candidate.parent == root:
            try:
                candidate.unlink()
            except Exception:
                pass


def _reverse_result_from_frames(
    payload, frames, source_url="", title="", platform="", duration=0,
    script_text="", asr_failed=False, media_path=None, media_mime="video/mp4",
    frame_pts=None, frame_dir=None,
):
    """Run the audited reverse engine for a validated local upload."""
    job_id = (payload or {}).get("_job_id")
    analysis_deadline = time.monotonic() + BREAKDOWN_ANALYSIS_BUDGET
    analysis_heartbeat = lambda: _heartbeat(job_id, "analyzing")
    if not media_path:
        raise ValueError("Gemini reverse requires the original media file")
    prompt_result = _gemini_reverse_prompt_from_media(
        media_path,
        media_mime,
        title,
        duration,
        platform,
        script_text,
        deadline=analysis_deadline,
        heartbeat=analysis_heartbeat,
    )
    frames, frame_pts = _fill_reverse_window_frames(
        media_path,
        frame_dir,
        frames,
        frame_pts,
        prompt_result["windows"],
    )
    _validate_gemini_reverse_entries(
        prompt_result, frames, script_text, frame_pts=frame_pts
    )
    frame_bundle = _reverse_frame_bundle(
        frames, prompt_result["windows"], frame_pts=frame_pts
    )
    global_continuity = _reverse_global_facts_from_segments(
        prompt_result["entries"],
        frame_bundle["segment_model_source_indices"],
        frame_count=len(frames),
    )
    prompt_result["prompt"] = _assemble_reverse_prompt(
        prompt_result["entries"],
        prompt_result["windows"],
        global_continuity,
        enforce_length_limits=False,
    )
    quality_score = _score_reverse_generation_coverage(
        prompt_result["entries"],
        global_continuity,
        prompt_result["windows"],
    )
    target_score = _REVERSE_VISUAL_SEMANTIC_CONTRACT["target_score"]
    if quality_score["total"] < target_score:
        raise ValueError(
            "反推结果生成要素覆盖度不足：%d分，至少需要%d分，请重试"
            % (quality_score["total"], target_score)
        )
    frame_thumbnails = _frame_thumbnails(
        frame_bundle["frames"], limit=len(frame_bundle["frames"])
    )
    if len(frame_thumbnails) != len(frame_bundle["frames"]):
        raise ValueError("反推审计证据帧序列化失败，请重试")
    segment_evidence = _reverse_segment_evidence_manifest(
        prompt_result["entries"],
        prompt_result["windows"],
        frame_bundle["segment_source_indices"],
        frame_bundle["segment_model_source_indices"],
    )
    call_budget = _reverse_analysis_call_budget(
        len(prompt_result["windows"])
    )
    audit_sections = {
        "frame_manifest": frame_bundle["manifest"],
        "reference_thumbnail_indices": frame_bundle[
            "reference_thumbnail_indices"
        ],
        "audit_thumbnail_indices": frame_bundle[
            "audit_thumbnail_indices"
        ],
        "global_continuity": global_continuity,
        "segment_evidence": segment_evidence,
        "analysis_call_budget": call_budget,
        "timeline_audit": prompt_result.get("timeline_audit") or {},
        "attempt_audit": prompt_result.get("attempt_audit") or [],
        "quality_dimensions": _gemini_quality_dimensions(prompt_result),
        "model_provider": prompt_result.get("provider"),
        "model_id": prompt_result.get("model"),
        "model_attempts": prompt_result.get("attempts"),
        "quality_contract": _reverse_quality_contract(),
        "quality_score": quality_score,
    }
    return {
        "type": "breakdown_reverse",
        "source_url": source_url,
        "source_title": title,
        "source_platform": platform,
        "duration": duration,
        "prompt": prompt_result["prompt"],
        "frame_count": len(frames or []),
        "frame_thumbnails": frame_thumbnails,
        "reference_frame_strategy": "explicit_indices_one_per_segment",
        "reference_thumbnail_indices": frame_bundle[
            "reference_thumbnail_indices"
        ],
        "audit_thumbnail_indices": frame_bundle[
            "audit_thumbnail_indices"
        ],
        "frame_manifest": frame_bundle["manifest"],
        "global_continuity": global_continuity,
        "segment_evidence": segment_evidence,
        "analysis_call_budget": call_budget,
        "timeline_audit": prompt_result.get("timeline_audit") or {},
        "quality_contract": _reverse_quality_contract(),
        "quality_score": quality_score,
        "sections": {"reverse_audit": audit_sections},
        "asr_failed": asr_failed,
    }


def _download_breakdown_video(tikhub, info, detail, destination):
    """Try alternate CDN URLs, then refresh video details once."""
    current = detail
    deadline = time.time() + BREAKDOWN_DOWNLOAD_BUDGET
    retryable = (
        TimeoutError,
        ConnectionError,
        urllib.error.URLError,
        http.client.IncompleteRead,
    )
    last_error = None
    budget_exhausted = False
    for refresh_attempt in range(2):
        if time.time() >= deadline:
            last_error = TimeoutError("video download budget exhausted")
            break
        alternate_urls = current.get("play_urls")
        if not isinstance(alternate_urls, (list, tuple)):
            alternate_urls = []
        play_urls = list(dict.fromkeys(
            [candidate for candidate in alternate_urls if candidate]
            + ([current.get("play_url")] if current.get("play_url") else [])
        ))[:4]
        if not play_urls:
            if current.get("images"):
                raise ValueError("该链接是图文笔记，不是视频，暂不支持拆解")
            if refresh_attempt:
                raise ValueError("未找到视频下载地址，可能是私密或已删除")
            current = tikhub.detail(
                info["platform"], info["id"], info.get("note_type"), fresh=True
            )
            continue
        for play_index, play_url in enumerate(play_urls, 1):
            if time.time() >= deadline:
                last_error = TimeoutError("video download budget exhausted")
                budget_exhausted = True
                break
            try:
                downloaded_bytes = tikhub.download_to_file(
                    play_url, deadline, destination,
                    max_bytes=BREAKDOWN_MAX_DOWNLOAD_BYTES,
                )
                if not isinstance(downloaded_bytes, int) or downloaded_bytes <= 0:
                    raise ConnectionError(
                        "video download returned no complete bytes"
                    )
                current["play_url"] = play_url
                return current
            except ValueError as error:
                last_error = error
                if play_index >= len(play_urls):
                    raise
                print(
                    "[breakdown] video URL %d/%d exceeded limit; trying alternate: %s"
                    % (play_index, len(play_urls), str(error)[:160]),
                    flush=True,
                )
            except retryable as error:
                last_error = error
                print(
                    "[breakdown] video URL %d/%d failed: %s"
                    % (play_index, len(play_urls), str(error)[:160]),
                    flush=True,
                )
        if budget_exhausted:
            break
        if time.time() >= deadline:
            last_error = TimeoutError("video download budget exhausted")
            break
        if refresh_attempt == 0:
            current = tikhub.detail(
                info["platform"], info["id"], info.get("note_type"), fresh=True
            )
    if isinstance(last_error, ValueError):
        raise last_error
    if last_error is not None:
        raise TimeoutError(
            "video download failed after alternate URLs and one detail refresh"
        ) from last_error
    raise RuntimeError("video download retry state is invalid")


# ============ 辅助函数 ============


def _frame_thumbnails(frames, limit=4):
    thumbs = []
    for fp in (frames or [])[:max(0, int(limit or 0))]:
        try:
            with open(fp, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            thumbs.append("data:image/jpeg;base64," + b64)
        except Exception:
            pass
    return thumbs


def _breakdown_source_context(title, duration, platform, script_text):
    return (
        "视频标题：" + str(title) + "\n"
        "时长：" + str(duration) + "s\n"
        "平台：" + str(platform) + "\n\n"
        "口播文案（带时间轴）：\n" + (str(script_text) if script_text else "（无人物口播或转写不可用，请根据画面帧判断内容）")
    )


def _breakdown_scenes_from_frames(title, duration, platform, script_text, frames):
    context = _breakdown_source_context(title, duration, platform, script_text)
    usermsg = (
        context + "\n\n"
        "以下图片按请求顺序编号为1到%d。请严格输出 JSON："
        "{\"scenes\":[{\"dur\":\"3s\",\"detail_facts\":{"
        "\"subject\":{\"status\":\"observed\",\"identity\":\"主体身份或类型\",\"appearance\":\"外观服装颜色材质\","
        "\"position_scale\":\"位置和画面占比\",\"evidence_frames\":[1]},"
        "\"action\":{\"status\":\"observed\",\"start\":\"起始状态\",\"process\":\"动作过程\",\"end\":\"结束状态\","
        "\"direction_speed\":\"方向和速度\",\"motion\":\"gesture\",\"evidence_frames\":[1,2]},"
        "\"setting\":{\"status\":\"observed\",\"location\":\"地点\",\"foreground\":\"前景\",\"midground\":\"中景\","
        "\"background\":\"背景\",\"evidence_frames\":[1]},"
        "\"camera\":{\"status\":\"observed\",\"shot_size\":\"medium\",\"angle\":\"eye_level\","
        "\"composition\":\"centered\",\"movement\":\"static\",\"evidence_frames\":[1]},"
        "\"lighting\":{\"status\":\"observed\",\"source_direction\":\"光源和方向\",\"quality\":\"soft\","
        "\"contrast\":\"medium\",\"color_tone\":\"neutral\",\"evidence_frames\":[1]}},"
        "\"line\":\"口播台词\"}],"
        "\"analysis\":\"视频主题、叙事结构、情绪与转化目的综合分析(150-240字)\"}，"
        "只输出 JSON 本身，不要解释、不要 markdown 代码块。"
        "4-6 个分镜，各 dur 之和≈总时长；不要输出 scene，服务端会根据 detail_facts 组装画面文字。"
        "主体写清外观、服装或产品的颜色、材质、位置和画面占比；"
        "动作按实际先后写起点、过程、结果、方向以及与道具的互动，静止画面要明确写静止；"
        "场景写清地点、关键道具以及前景、中景、背景的空间关系；"
        "镜头写清景别、机位高低、视角、构图和可见的推进、跟随、摇移或固定机位；"
        "光影写清光源方向、软硬、明暗层次、色温和主色调。"
        "每个栏目只能使用 status=observed 或 status=unknown。observed 时该栏所有字段必须填写具体值，"
        "并提供至少一个1到%d之间、确实支持该栏事实的 evidence_frames；"
        "unknown 时该栏所有文字字段必须为空串且 evidence_frames 必须为空数组。"
        "motion 只能取 static/gesture/posture_change/translation/rotation/interaction/mixed；"
        "shot_size 只能取 extreme_closeup/closeup/medium/full/wide/extreme_wide；"
        "angle 只能取 eye_level/high/low/overhead/dutch；composition 只能取 "
        "centered/rule_of_thirds/symmetrical/leading_lines/layered/mixed；movement 只能取 "
        "static/pan/tilt/dolly_in/dolly_out/tracking/handheld/orbit/mixed；"
        "quality 只能取 soft/hard/mixed，contrast 只能取 low/medium/high，"
        "color_tone 只能取 warm/cool/neutral/mixed。"
        "五个栏目中至少三个必须为 observed。不得把“未提供、无法判断、字段为空”等空信息标成 observed。"
        "不得为了详细而编造动作、道具、文字或氛围。"
        "line 是原视频对应的口播内容。"
        "若原视频没有人物口播（纯音乐/歌舞/背景乐），或上方口播文案实为歌词、听写乱码、与画面无关的内容，"
        "所有 line 输出空串\"\"，不要编造台词。"
    ) % (len(frames), len(frames))
    sysmsg = (
        "你是黄雀传媒资深短视频编导。分析视频关键帧和口播，拆解为简洁的分镜脚本，同时输出一份视频内容综合分析。"
        "只输出 JSON，不要多余内容。"
    )
    raw = _chat_multimodal(
        sysmsg, usermsg, frames, temp=0.2, max_tokens=3200,
    )
    try:
        return _validate_scene_breakdown(
            _parse_breakdown_json(raw), require_detail=True,
            frame_count=len(frames),
        )
    except ValueError as first_error:
        _log_breakdown_parse_failure("zhipu-primary", raw, first_error)

    compact_msg = (
        context + "\n\n"
        "上一次输出未形成完整 JSON。请重新分析并只返回一个完整、可解析的 JSON 对象，禁止代码围栏、解释和重复内容。"
        "固定输出 4 个分镜，格式为：{\"scenes\":[{\"dur\":\"4s\",\"detail_facts\":{"
        "\"subject\":{\"status\":\"observed\",\"identity\":\"\",\"appearance\":\"\","
        "\"position_scale\":\"\",\"evidence_frames\":[]},"
        "\"action\":{\"status\":\"observed\",\"start\":\"\",\"process\":\"\",\"end\":\"\","
        "\"direction_speed\":\"\",\"motion\":\"\",\"evidence_frames\":[]},"
        "\"setting\":{\"status\":\"observed\",\"location\":\"\",\"foreground\":\"\","
        "\"midground\":\"\",\"background\":\"\",\"evidence_frames\":[]},"
        "\"camera\":{\"status\":\"observed\",\"shot_size\":\"\",\"angle\":\"\","
        "\"composition\":\"\",\"movement\":\"\",\"evidence_frames\":[]},"
        "\"lighting\":{\"status\":\"observed\",\"source_direction\":\"\",\"quality\":\"\","
        "\"contrast\":\"\",\"color_tone\":\"\",\"evidence_frames\":[]}},\"line\":\"对应口播或空串\"}],"
        "\"analysis\":\"100-180字综合分析\"}。"
        "不要输出 scene；服务端根据结构化槽位组装。每栏只能是 observed 或 unknown；"
        "observed 必须填写该栏全部字段并引用1到%d之间的原始帧，unknown 必须全部留空且无证据帧；"
        "motion取static/gesture/posture_change/translation/rotation/interaction/mixed；"
        "shot_size取extreme_closeup/closeup/medium/full/wide/extreme_wide；"
        "angle取eye_level/high/low/overhead/dutch；composition取centered/rule_of_thirds/"
        "symmetrical/leading_lines/layered/mixed；movement取static/pan/tilt/dolly_in/dolly_out/"
        "tracking/handheld/orbit/mixed；quality取soft/hard/mixed；contrast取low/medium/high；"
        "color_tone取warm/cool/neutral/mixed，不得翻译、组合或自造；"
        "至少三个栏目为 observed。不得为补细节编造关键帧中不存在的内容；"
        "不得照抄“具体画面”“对应口播”“画面描述”"
        "“口播台词”等格式示例。无人物口播时所有 line 必须为空串。务必闭合全部引号、数组和大括号。"
    ) % len(frames)
    raw = _chat_multimodal(
        sysmsg, compact_msg, frames, temp=0.1, max_tokens=2400,
    )
    try:
        return _validate_scene_breakdown(
            _parse_breakdown_json(raw), require_detail=True,
            frame_count=len(frames),
        )
    except ValueError as retry_error:
        _log_breakdown_parse_failure("zhipu-compact", raw, retry_error)

    raw = _chat_multimodal(
        sysmsg, compact_msg, frames, temp=0.1, max_tokens=2400,
        provider="openai",
    )
    try:
        return _validate_scene_breakdown(
            _parse_breakdown_json(raw), require_detail=True,
            frame_count=len(frames),
        )
    except ValueError as fallback_error:
        _log_breakdown_parse_failure("openai-fallback", raw, fallback_error)
        raise


def _log_breakdown_parse_failure(attempt, raw, error):
    print(
        "[breakdown] %s invalid output: %s raw(%d)=%s"
        % (
            attempt,
            str(error),
            len(raw or ""),
            str(raw)[:400].replace("\n", " "),
        ),
        flush=True,
    )


def _normalize_duration_seconds(raw_duration):
    """Normalize TikHub seconds/milliseconds without discarding sub-second precision."""
    try:
        duration = float(raw_duration or 30)
    except (TypeError, ValueError):
        duration = 30.0
    if duration > 1000:
        duration /= 1000.0
    return max(0.001, round(duration, 3))


def _format_timeline_second(seconds):
    total_tenths = int(round(max(0.0, float(seconds or 0)) * 10))
    minutes, remainder_tenths = divmod(total_tenths, 600)
    return "%02d:%04.1f" % (minutes, remainder_tenths / 10.0)


def _reverse_segment_windows(duration, max_segments=4):
    """Build gap-free numeric windows; model prompts and output share these bounds."""
    duration = max(0.001, float(duration or 0))
    segment_count = min(
        max(1, int(max_segments or 1)),
        max(1, int(math.ceil(duration / 3.0))),
    )
    boundaries = [
        index * duration / segment_count
        for index in range(segment_count + 1)
    ]
    return [
        (
            boundaries[index],
            boundaries[index + 1],
            "[%s-%s]" % (
                _format_timeline_second(boundaries[index]),
                _format_timeline_second(boundaries[index + 1]),
            ),
        )
        for index in range(segment_count)
    ]


def _fixed_reverse_ranges(duration, max_segments=4):
    """Build a gap-free timeline in code; the model never invents timestamps."""
    return [
        label
        for _start, _end, label in _reverse_segment_windows(
            duration, max_segments=max_segments
        )
    ]


def _round_tenth(value):
    return math.floor(max(0.0, float(value or 0)) * 10.0 + 0.5) / 10.0


def _round_whole_second(value):
    return int(math.floor(max(0.0, float(value or 0)) + 0.5))


def _reverse_display_range(start, end):
    return "%d-%d秒" % (
        _round_whole_second(start),
        _round_whole_second(end),
    )


def _detect_reverse_transition_candidates(path, duration):
    """Return evidence-only FFmpeg scene candidates; never infer a cut in Python."""
    duration = _round_tenth(duration)
    command = [
        "ffmpeg", "-hide_banner", "-nostdin", "-v", "info", "-i", path,
        "-vf",
        "select='gt(scene,%.2f)',metadata=print"
        % _REVERSE_SCENE_SCORE_THRESHOLD,
        "-an", "-f", "null", "-",
    ]
    try:
        process = subprocess.run(
            command,
            check=False,
            timeout=max(20, min(60, int(math.ceil(duration * 2.0)))),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except Exception as error:
        return [], {
            "detector": "ffmpeg_scene_score",
            "threshold": _REVERSE_SCENE_SCORE_THRESHOLD,
            "status": "unavailable",
            "error_type": type(error).__name__,
        }
    output = "%s\n%s" % (process.stdout or "", process.stderr or "")
    pattern = re.compile(
        r"frame:\s*\d+\s+pts:[^\r\n]*?pts_time:([0-9]+(?:\.[0-9]+)?)"
        r"[\s\S]{0,240}?lavfi\.scene_score=([0-9]+(?:\.[0-9]+)?)"
    )
    candidates = []
    for match in pattern.finditer(output):
        at_seconds = _round_tenth(match.group(1))
        score = round(float(match.group(2)), 6)
        if 0.0 < at_seconds < duration:
            candidates.append({
                "at_seconds": at_seconds,
                "score": score,
                "detector": "ffmpeg_scene_score",
            })
    return candidates, {
        "detector": "ffmpeg_scene_score",
        "threshold": _REVERSE_SCENE_SCORE_THRESHOLD,
        "status": "ok" if process.returncode == 0 else "partial",
        "candidate_count": len(candidates),
        "ffmpeg_returncode": int(process.returncode),
    }


def _build_authoritative_reverse_timeline(duration, candidates=None):
    """Choose at most three evidence-backed cuts and build one gap-free timeline."""
    duration = max(0.1, _round_tenth(duration))
    normalized = []
    for raw in candidates or []:
        try:
            at_seconds = _round_tenth(raw.get("at_seconds"))
            score = float(raw.get("score") or 0.0)
        except (AttributeError, TypeError, ValueError):
            continue
        if (
            at_seconds < _REVERSE_MIN_SEGMENT_SECONDS
            or duration - at_seconds < _REVERSE_MIN_SEGMENT_SECONDS
        ):
            continue
        normalized.append({
            "at_seconds": at_seconds,
            "score": round(score, 6),
            "detector": str(raw.get("detector") or "ffmpeg_scene_score"),
        })
    strongest = sorted(
        normalized,
        key=lambda item: (-item["score"], item["at_seconds"]),
    )
    selected = []
    for candidate in strongest:
        at_seconds = candidate["at_seconds"]
        if any(
            abs(at_seconds - previous["at_seconds"])
            < _REVERSE_MIN_SEGMENT_SECONDS
            for previous in selected
        ):
            continue
        selected.append(candidate)
        if len(selected) >= _REVERSE_MAX_SEGMENTS - 1:
            break
    selected.sort(key=lambda item: item["at_seconds"])
    boundaries = [0.0] + [
        item["at_seconds"] for item in selected
    ] + [duration]
    windows = [
        (
            boundaries[index],
            boundaries[index + 1],
            _reverse_display_range(
                boundaries[index], boundaries[index + 1],
            ),
        )
        for index in range(len(boundaries) - 1)
    ]
    transitions = [
        {
            "boundary_id": index,
            "at_seconds": candidate["at_seconds"],
            "display_at_second": _round_whole_second(
                candidate["at_seconds"]
            ),
            "score": candidate["score"],
            "detector": candidate["detector"],
        }
        for index, candidate in enumerate(selected, 1)
    ]
    return {
        "windows": windows,
        "transitions": transitions,
        "duration_seconds": duration,
        "display_duration_seconds": _round_whole_second(duration),
        "max_segments": _REVERSE_MAX_SEGMENTS,
        "min_segment_seconds": _REVERSE_MIN_SEGMENT_SECONDS,
        "source": (
            "ffmpeg_scene_candidates"
            if selected else "single_full_media_segment"
        ),
    }


def _authoritative_reverse_timeline(path, duration):
    candidates, detector_audit = _detect_reverse_transition_candidates(
        path, duration,
    )
    result = _build_authoritative_reverse_timeline(duration, candidates)
    result["detector_audit"] = detector_audit
    return result


def _reverse_frame_time(frame_index, frame_count, duration):
    """Map a chronological audit-frame index onto the source timeline."""
    frame_index = int(frame_index or 0)
    frame_count = int(frame_count or 0)
    duration = max(0.0, float(duration or 0))
    if frame_count <= 1:
        return 0.0
    return duration * max(0, frame_index - 1) / float(frame_count - 1)


def _group_reverse_frame_indices(frame_count, segments, frame_pts=None):
    """Return the single authoritative source-frame ownership mapping.

    Integer callers retain the legacy equal grouping contract. Production
    Gemini callers pass the FFmpeg-owned windows so unequal shots receive only
    the audit frames whose chronological source positions fall inside them.
    frame_pts 提供每张帧的真实 FFmpeg PTS（秒）时按真实时间归属；无 PTS 的
    均匀映射仅为测试遗留调用保留。
    """
    frame_count = max(0, int(frame_count or 0))
    if isinstance(segments, int):
        segment_count = max(1, segments)
        return [
            list(range(
                int(round(index * frame_count / float(segment_count))) + 1,
                int(round((index + 1) * frame_count / float(segment_count))) + 1,
            ))
            for index in range(segment_count)
        ]

    windows = list(segments or [])
    if not windows:
        return []
    duration = float(windows[-1][1])
    pts_seconds = None
    if frame_pts is not None:
        try:
            candidate = [float(value) for value in list(frame_pts)]
        except (TypeError, ValueError):
            candidate = []
        if len(candidate) == frame_count:
            pts_seconds = candidate
    groups = [[] for _window in windows]
    for frame_index in range(1, frame_count + 1):
        if pts_seconds is not None:
            at_seconds = pts_seconds[frame_index - 1]
        else:
            at_seconds = _reverse_frame_time(
                frame_index, frame_count, duration,
            )
        assigned = False
        for window_index, (start, end, _label) in enumerate(windows):
            if (
                float(start) <= at_seconds < float(end)
                or (
                    window_index == len(windows) - 1
                    and at_seconds <= float(end)
                )
            ):
                groups[window_index].append(frame_index)
                assigned = True
                break
        if not assigned:
            # PTS 越界（如封装 start_time 偏移）时归入最近的端点窗口，
            # 证据帧绝不丢弃、也绝不跨窗口重映射。
            if at_seconds < float(windows[0][0]):
                groups[0].append(frame_index)
            else:
                groups[-1].append(frame_index)
    return groups


def _group_reverse_frames(frames, segments, frame_pts=None):
    """Partition all audit frames by the single authoritative segment mapping."""
    ordered = list(frames or [])
    groups = _group_reverse_frame_indices(
        len(ordered), segments, frame_pts=frame_pts
    )
    if not groups:
        raise ValueError("反推时间段为空，无法绑定原始帧证据")
    if isinstance(segments, int) and any(len(group) < 2 for group in groups):
        raise ValueError(
            "反推关键帧不足：%d个时间段至少需要%d张原始帧"
            % (max(1, segments), max(1, segments) * 2)
        )
    if any(not group for group in groups):
        raise ValueError(
            "反推关键帧不足：至少一个权威时间段没有对应原始帧证据"
        )
    return [
        [ordered[source_index - 1] for source_index in source_indices]
        for source_indices in groups
    ]


def _reverse_model_frame_groups(frames, segments, frame_pts=None):
    """Select only the first/last frame in each segment for the VLM request."""
    result = []
    for group in _group_reverse_frames(frames, segments, frame_pts=frame_pts):
        result.append(
            [group[0], group[-1]] if len(group) > 1 else [group[0]]
        )
    return result


def _reverse_reference_frames(frames, segments, frame_pts=None):
    """Return one chronological source frame per segment for downstream generation."""
    return [
        group[-1]
        for group in _group_reverse_frames(frames, segments, frame_pts=frame_pts)
    ]


def _reverse_frame_bundle(frames, segments, frame_pts=None):
    """Keep explicit downstream indexes separate from the audit-frame set."""
    ordered = list(frames or [])
    segment_source_indices = _group_reverse_frame_indices(
        len(ordered), segments, frame_pts=frame_pts
    )
    if (
        isinstance(segments, int)
        and any(len(indices) < 2 for indices in segment_source_indices)
    ):
        raise ValueError(
            "反推关键帧不足：%d个时间段至少需要%d张原始帧"
            % (max(1, segments), max(1, segments) * 2)
        )
    if not segment_source_indices or any(
        not indices for indices in segment_source_indices
    ):
        raise ValueError(
            "反推关键帧不足：权威时间段与原始帧无法完整对应"
        )
    segment_model_source_indices = [
        (
            [indices[0], indices[-1]]
            if len(indices) > 1 else [indices[0]]
        )
        for indices in segment_source_indices
    ]
    reference_source_indices = [
        indices[-1] for indices in segment_source_indices if indices
    ]
    remaining_source_indices = [
        index for index in range(1, len(ordered) + 1)
        if index not in reference_source_indices
    ]
    source_order = reference_source_indices + remaining_source_indices
    location = {}
    for segment_index, indices in enumerate(segment_source_indices, 1):
        for local_index, source_index in enumerate(indices, 1):
            location[source_index] = (segment_index, local_index)
    manifest = []
    for thumbnail_index, source_index in enumerate(source_order, 1):
        segment_index, local_index = location[source_index]
        manifest.append({
            "thumbnail_index": thumbnail_index,
            "source_frame_index": source_index,
            "segment_index": segment_index,
            "segment_local_index": local_index,
            "downstream_reference": (
                source_index in reference_source_indices
            ),
        })
    return {
        "frames": [ordered[index - 1] for index in source_order],
        "manifest": manifest,
        "reference_thumbnail_indices": list(
            range(1, len(reference_source_indices) + 1)
        ),
        "audit_thumbnail_indices": list(
            range(len(reference_source_indices) + 1, len(source_order) + 1)
        ),
        "segment_source_indices": segment_source_indices,
        "segment_model_source_indices": segment_model_source_indices,
    }


def _reverse_segment_evidence_manifest(
    entries, windows, segment_source_indices, segment_model_source_indices
):
    result = []
    for entry, (_start, _end, timeline), source_indices, model_source_indices in zip(
        entries or [],
        windows or [],
        segment_source_indices or [],
        segment_model_source_indices or [],
    ):
        mapped = {}
        for key, local_indices in (
            entry.get("evidence_frames") or {}
        ).items():
            mapped[key] = [
                model_source_indices[index - 1]
                for index in local_indices
                if 1 <= index <= len(model_source_indices)
            ]
        slot_source_evidence = {}
        for path in _reverse_generation_slot_paths():
            local_indices = _reverse_generation_slot(
                entry, path
            ).get("evidence_frames") or []
            slot_source_evidence[path] = [
                model_source_indices[index - 1]
                for index in local_indices
                if 1 <= index <= len(model_source_indices)
            ]
        result.append({
            "timeline": timeline,
            "source_parameters": {
                "scope": "measured_source_fact",
                "timeline": timeline,
                "duration_seconds": round(float(_end) - float(_start), 1),
            },
            "segment_source_frames": list(source_indices),
            "local_to_source": {
                str(index): source_index
                for index, source_index in enumerate(model_source_indices, 1)
            },
            "local_evidence_frames": entry.get("evidence_frames") or {},
            "source_evidence_frames": mapped,
            "generation_structure": json.loads(json.dumps(
                entry.get("generation") or {}, ensure_ascii=False
            )),
            "shot_boundary": json.loads(json.dumps(
                entry.get("shot_boundary") or {}, ensure_ascii=False
            )),
            "shot_states": json.loads(json.dumps(
                entry.get("shots") or [], ensure_ascii=False
            )),
            "generation_slot_source_evidence": slot_source_evidence,
            "generation_suggestions": json.loads(json.dumps(
                entry.get("generation_suggestions") or {},
                ensure_ascii=False,
            )),
            "attempt_audit": json.loads(json.dumps(
                entry.get("attempt_audit") or [], ensure_ascii=False
            )),
            "validation_summary": json.loads(json.dumps(
                entry.get("validation_summary") or {}, ensure_ascii=False
            )),
            "omitted_unsupported_fields": json.loads(json.dumps(
                entry.get("omitted_unsupported_fields") or [],
                ensure_ascii=False,
            )),
            "evidence_seconds": entry.get("evidence_seconds") or {},
            "generation_advice": entry.get("generation_advice") or {},
            "cut_from_previous": bool(entry.get("cut_from_previous")),
            "transition_from_previous": json.loads(json.dumps(
                entry.get("transition_from_previous") or {},
                ensure_ascii=False,
            )),
            "generation_readiness": entry.get("readiness") or {},
            "continuity_source_frames": entry.get(
                "continuity_evidence_frames", []
            ),
        })
    return result


def _reverse_source_frame_segment(frame_index, frame_count, segment_count):
    frame_count = int(frame_count or 0)
    segment_count = int(segment_count or 0)
    try:
        frame_index = int(frame_index)
    except (TypeError, ValueError):
        return 0
    if frame_count <= 0 or segment_count <= 0:
        return 0
    for index, source_indices in enumerate(
        _group_reverse_frame_indices(frame_count, segment_count), 1
    ):
        if frame_index in source_indices:
            return index
    return 0


def _clock_to_seconds(value):
    text = str(value or "").strip().replace(",", ".")
    parts = text.split(":")
    try:
        if len(parts) == 3:
            return (
                float(parts[0]) * 3600
                + float(parts[1]) * 60
                + float(parts[2])
            )
        return float(text.rstrip("sS"))
    except (TypeError, ValueError):
        return None


def _segment_transcript(script_text, start, end):
    """Keep only ASR lines whose timestamps overlap the current visual segment."""
    text = str(script_text or "").replace("\r", "").strip()
    if not text:
        return ""
    matches = []
    bracket_pattern = re.compile(
        r"^\s*\[\s*([0-9:.]+)\s*[sS]?\s*[-–—至到]\s*"
        r"([0-9:.]+)\s*[sS]?\s*\]\s*(.*?)\s*$"
    )
    srt_pattern = re.compile(
        r"^\s*([0-9:,\.]+)\s*-->\s*([0-9:,\.]+)\s*$"
    )
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        bracket = bracket_pattern.match(line)
        if bracket:
            item_start = _clock_to_seconds(bracket.group(1))
            item_end = _clock_to_seconds(bracket.group(2))
            spoken = bracket.group(3).strip()
        else:
            srt = srt_pattern.match(line)
            if not srt:
                index += 1
                continue
            item_start = _clock_to_seconds(srt.group(1))
            item_end = _clock_to_seconds(srt.group(2))
            spoken_lines = []
            index += 1
            while index < len(lines) and lines[index].strip():
                spoken_lines.append(lines[index].strip())
                index += 1
            spoken = " ".join(spoken_lines).strip()
        if (
            item_start is not None
            and item_end is not None
            and spoken
            and item_end > float(start)
            and item_start < float(end)
        ):
            matches.append(spoken)
        index += 1
    return "\n".join(dict.fromkeys(matches))


def _parse_reverse_segments(raw, expected_count):
    values = None
    parsed_json = False
    try:
        result = _parse_breakdown_json(raw)
        parsed_json = True
        values = result.get("segments") if isinstance(result, dict) else None
    except ValueError:
        pass
    if parsed_json and not isinstance(values, list):
        raise ValueError("反推结果缺少 segments 数组，请重试")
    if parsed_json and len(values) != expected_count:
        raise ValueError(
            "反推结果段数错误：需要%d段，实际%d段，请重试"
            % (expected_count, len(values))
        )
    if not parsed_json:
        return _split_reverse_text(raw, expected_count)

    segments = []
    placeholders = {
        "第一段画面描述", "第二段画面描述",
        "第三段画面描述", "第四段画面描述",
    }
    for index, value in enumerate(values, 1):
        if isinstance(value, dict):
            value = _compose_reverse_segment(value)
        text = " ".join(str(value or "").replace("\r", "").split()).strip()
        if not text:
            raise ValueError("反推结果第%d段为空，请重试" % index)
        if text in placeholders:
            raise ValueError("反推结果第%d段内容不完整，请重试" % index)
        segments.append(text)
    return segments


_REVERSE_SEGMENT_FIELDS = (
    ("subject", "主体"),
    ("scene", "场景"),
    ("action", "动作"),
    ("camera", "镜头"),
    ("lighting", "光影"),
    ("sound", "声音"),
    ("continuity", "衔接"),
)


def _reverse_generation_slot_paths():
    return [
        "%s.%s" % (group, key)
        for group, keys in _REVERSE_GENERATION_SLOT_GROUPS.items()
        for key in keys
    ]


def _reverse_normalize_indices(values, error_message):
    if not isinstance(values, list):
        raise ValueError(error_message)
    result = []
    for raw_index in values:
        try:
            frame_index = int(raw_index)
        except (TypeError, ValueError):
            raise ValueError(error_message)
        if frame_index not in result:
            result.append(frame_index)
    return result


def _reverse_parse_generation_structure(value):
    raw_generation = value.get("generation")
    if raw_generation in (None, ""):
        return {}
    if not isinstance(raw_generation, dict):
        raise ValueError("反推结果 generation 必须是结构化对象，请重试")
    generation = {}
    for group, keys in _REVERSE_GENERATION_SLOT_GROUPS.items():
        raw_group = raw_generation.get(group)
        if not isinstance(raw_group, dict):
            raise ValueError("反推结果 generation.%s 缺少结构化槽位，请重试" % group)
        generation[group] = {}
        for key in keys:
            path = "%s.%s" % (group, key)
            raw_slot = raw_group.get(key)
            if not isinstance(raw_slot, dict):
                raise ValueError("反推结果槽位 %s 格式错误，请重试" % path)
            status = str(raw_slot.get("status") or "").strip().lower()
            if status not in _REVERSE_SLOT_STATUSES:
                raise ValueError(
                    "反推结果槽位 %s 状态必须是 observed/unknown/not_applicable"
                    % path
                )
            text = " ".join(
                str(raw_slot.get("value") or "").replace("\r", "").split()
            ).strip()
            evidence = _reverse_normalize_indices(
                raw_slot.get("evidence_frames") or [],
                "反推结果槽位 %s 帧证据格式错误，请重试" % path,
            )
            if status == "observed" and not _compact_reverse_text(text):
                raise ValueError("反推结果槽位 %s 标记 observed 但没有事实值" % path)
            if status == "unknown":
                text = "unknown"
                evidence = []
            if status == "not_applicable":
                text = ""
                evidence = []
            generation[group][key] = {
                "status": status,
                "value": text,
                "evidence_frames": evidence,
            }
    return generation


def _reverse_parse_shot_structure(value):
    raw_boundary = value.get("shot_boundary")
    if raw_boundary in (None, ""):
        boundary = {}
    elif not isinstance(raw_boundary, dict):
        raise ValueError("反推结果 shot_boundary 格式错误，请重试")
    else:
        boundary_type = str(raw_boundary.get("type") or "").strip().lower()
        if boundary_type not in {"continuous", "hard_cut", "unknown"}:
            raise ValueError(
                "反推结果 shot_boundary.type 必须是 continuous/hard_cut/unknown"
            )
        boundary = {
            "type": boundary_type,
            "evidence_frames": _reverse_normalize_indices(
                raw_boundary.get("evidence_frames") or [],
                "反推结果镜头切换帧证据格式错误，请重试",
            ),
        }
    raw_shots = value.get("shots")
    if raw_shots in (None, ""):
        return boundary, []
    if not isinstance(raw_shots, list):
        raise ValueError("反推结果 shots 必须是数组，请重试")
    shots = []
    for raw_shot in raw_shots:
        if not isinstance(raw_shot, dict):
            raise ValueError("反推结果镜头状态必须是结构化对象，请重试")
        try:
            frame = int(raw_shot.get("frame"))
        except (TypeError, ValueError):
            raise ValueError("反推结果镜头状态缺少局部帧编号，请重试")
        shot = {"frame": frame}
        for key in ("subject", "scene", "camera", "lighting", "style"):
            shot[key] = " ".join(
                str(raw_shot.get(key) or "").replace("\r", "").split()
            ).strip()
        shots.append(shot)
    return boundary, shots


def _reverse_generation_slot(entry, path):
    group, key = path.split(".", 1)
    return (
        (entry.get("generation") or {}).get(group, {}).get(key)
        or {"status": "unknown", "value": "unknown", "evidence_frames": []}
    )


def _reverse_generation_group_text(entry, group):
    values = []
    for key in _REVERSE_GENERATION_SLOT_GROUPS.get(group, ()):
        path = "%s.%s" % (group, key)
        slot = _reverse_generation_slot(entry, path)
        if slot.get("status") == "observed":
            text = str(slot.get("value") or "").strip()
            if path == "action.motion_type":
                text = {"dynamic": "动态", "static": "静止"}.get(text, text)
            values.append(
                "%s：%s" % (_REVERSE_GENERATION_SLOT_LABELS[path], text)
            )
    return "，".join(value for value in values if value)


def _compose_reverse_generation_segment(entry, suggestion=None):
    generation = entry.get("generation") or {}
    if not generation:
        return _compose_reverse_segment(entry.get("fields") or {})
    boundary = (entry.get("shot_boundary") or {}).get("type")
    parts = []
    if boundary == "hard_cut":
        shot_parts = []
        for number, shot in enumerate(entry.get("shots") or [], 1):
            details = []
            for key, label in (
                ("subject", "主体"),
                ("scene", "场景"),
                ("camera", "构图镜头"),
                ("lighting", "光影色彩"),
                ("style", "风格材质"),
            ):
                text = str(shot.get(key) or "").strip()
                if text and text != "unknown":
                    details.append("%s：%s" % (label, text.rstrip("；;。")))
            shot_parts.append(
                "镜头%s（局部帧%d）：%s"
                % (
                    "A" if number == 1 else "B",
                    int(shot.get("frame") or number),
                    "；".join(details),
                )
            )
        parts.append("硬切；" + "；硬切至".join(shot_parts))
    else:
        group_labels = (
            ("subject", "主体"),
            ("action", "动作"),
            ("scene", "场景"),
            ("camera", "构图与镜头"),
            ("lighting", "光影色彩"),
            ("style", "风格材质"),
            ("rhythm", "节奏"),
        )
        for group, label in group_labels:
            text = _reverse_generation_group_text(entry, group)
            if text:
                parts.append("%s：%s" % (label, text.rstrip("；;。")))
    unknown_labels = [
        _REVERSE_GENERATION_SLOT_LABELS[path]
        for path in _reverse_generation_slot_paths()
        if _reverse_generation_slot(entry, path).get("status") == "unknown"
    ]
    if unknown_labels:
        parts.append("证据不足（unknown）：%s" % "、".join(unknown_labels))
    if suggestion:
        parts.append(
            "生成建议（非源画面事实）：时长%.1f秒，保持源画幅，"
            "按本段参考帧顺序，高提示词遵循度"
            % float(suggestion.get("duration_seconds") or 0)
        )
    return "；".join(parts) + "。"


def _compose_reverse_segment(value):
    """Turn a structured model segment into one executable Chinese prompt."""
    parts = []
    for key, label in _REVERSE_SEGMENT_FIELDS:
        text = " ".join(str(value.get(key) or "").replace("\r", "").split()).strip()
        if text:
            parts.append("%s：%s" % (label, text.rstrip("；;。")))
    if parts:
        return "；".join(parts) + "。"
    return value.get("description") or value.get("prompt") or ""


def _reverse_quality_contract():
    """Return an isolated, JSON-safe copy of the auditable 100-point rubric."""
    return json.loads(json.dumps(
        _REVERSE_VISUAL_SEMANTIC_CONTRACT, ensure_ascii=False
    ))


def _compose_reverse_global_facts(global_continuity, include_evidence=False):
    facts = (global_continuity or {}).get("facts") or {}
    evidence = (global_continuity or {}).get("evidence_frames") or {}
    parts = []
    for key, label in _REVERSE_GLOBAL_FACT_FIELDS:
        text = " ".join(str(facts.get(key) or "").replace("\r", "").split()).strip()
        if text:
            part = "%s：%s" % (label, text.rstrip("；;。"))
            if include_evidence:
                indices = evidence.get(key) or []
                part += "（证据原始帧：%s）" % "、".join(
                    str(index) for index in indices
                )
            parts.append(part)
    return "；".join(parts)


def _parse_reverse_global_facts(raw, frame_count, segment_count):
    parsed = _parse_breakdown_json(raw)
    facts = parsed.get("global_facts") if isinstance(parsed, dict) else None
    evidence = parsed.get("evidence_frames") if isinstance(parsed, dict) else None
    if not isinstance(facts, dict) or not isinstance(evidence, dict):
        raise ValueError("反推全局连续性缺少事实或帧证据，请重试")

    normalized_facts = {}
    normalized_evidence = {}
    for key, _label in _REVERSE_GLOBAL_FACT_FIELDS:
        text = " ".join(str(facts.get(key) or "").replace("\r", "").split()).strip()
        normalized_facts[key] = text
        if _compact_reverse_text(text) in {
            _compact_reverse_text(value)
            for value in _REVERSE_EMPTY_PLACEHOLDER_VALUES
        }:
            raise ValueError("反推全局连续性包含空洞占位内容，请重试")
        raw_indices = evidence.get(key) or []
        if not isinstance(raw_indices, list):
            raise ValueError("反推全局连续性帧证据格式错误，请重试")
        indices = []
        for value in raw_indices:
            try:
                index = int(value)
            except (TypeError, ValueError):
                raise ValueError("反推全局连续性帧证据格式错误，请重试")
            if index < 1 or index > int(frame_count or 0):
                raise ValueError("反推全局连续性帧证据超出关键帧范围，请重试")
            if index not in indices:
                indices.append(index)
        if text and not indices:
            raise ValueError("反推全局连续性事实缺少原始帧编号，请重试")
        if text:
            evidence_segments = {
                _reverse_source_frame_segment(
                    index,
                    int(frame_count or 0),
                    segment_count=max(1, int(segment_count or 1)),
                )
                for index in indices
            }
            evidence_segments.discard(0)
            if len(evidence_segments) < 2:
                raise ValueError(
                    "反推全局连续性事实必须覆盖至少两个不同时间段，请重试"
                )
        normalized_evidence[key] = indices

    all_facts = " ".join(normalized_facts.values())
    compact = _compact_reverse_text(all_facts)
    if not compact:
        raise ValueError("反推全局连续性事实为空，请重试")
    if len(compact) > _REVERSE_MAX_GLOBAL_CHARS:
        raise ValueError(
            "反推全局连续性事实过长：最多%d字，实际%d字，请重试"
            % (_REVERSE_MAX_GLOBAL_CHARS, len(compact))
        )
    unsupported = next(
        (
            marker for marker in _REVERSE_UNSUPPORTED_INFERENCE_MARKERS
            if marker in all_facts
        ),
        None,
    )
    if unsupported:
        raise ValueError(
            "反推全局连续性包含无证据主观推断“%s”，请重试" % unsupported
        )
    return {
        "facts": normalized_facts,
        "evidence_frames": normalized_evidence,
        "frame_count": int(frame_count or 0),
        "segment_count": max(1, int(segment_count or 1)),
    }


def _reverse_global_facts_from_segments(
    entries, segment_model_source_indices, frame_count
):
    """Deterministically aggregate only facts already validated per segment."""
    entries = list(entries or [])
    source_groups = list(segment_model_source_indices or [])
    segment_count = len(entries)
    empty = {
        "facts": {
            key: "" for key, _label in _REVERSE_GLOBAL_FACT_FIELDS
        },
        "evidence_frames": {
            key: [] for key, _label in _REVERSE_GLOBAL_FACT_FIELDS
        },
        "changes": {},
        "frame_count": int(frame_count or 0),
        "segment_count": segment_count,
        "aggregation": "deterministic_validated_segment_intersection",
        "model_calls": 0,
        "image_count": 0,
    }
    if segment_count <= 1:
        return empty
    if len(source_groups) != segment_count:
        raise ValueError("反推全局连续性缺少分段原始帧映射，请重试")

    if all(entry.get("generation") for entry in entries):
        global_slot_map = {
            "subject_identity": (
                "subject.identity", "subject.appearance",
            ),
            "wardrobe": ("subject.wardrobe",),
            "recurring_scene_objects": (
                "scene.foreground", "scene.midground", "scene.background",
            ),
            "scene_style": ("style.visual_style", "style.texture"),
            "camera_style": (
                "camera.shot_size", "camera.viewing_angle",
                "camera.composition",
            ),
            "lighting_style": (
                "lighting.direction_brightness", "lighting.color_tone",
            ),
        }
        changes = {}
        for global_key, paths in global_slot_map.items():
            retained_parts = []
            retained_evidence = []
            for path in paths:
                slots = [
                    _reverse_generation_slot(entry, path)
                    for entry in entries
                ]
                if (
                    all(slot.get("status") == "observed" for slot in slots)
                    and all(
                        _reverse_continuity_attribute_equivalent(
                            path,
                            slots[0].get("value"),
                            slot.get("value"),
                        )
                        for slot in slots[1:]
                    )
                ):
                    retained_parts.append(
                        "%s：%s"
                        % (
                            _REVERSE_GENERATION_SLOT_LABELS[path],
                            slots[0].get("value"),
                        )
                    )
                    for slot, source_indices in zip(slots, source_groups):
                        for local_index in slot.get("evidence_frames") or []:
                            if 1 <= local_index <= len(source_indices):
                                source_index = source_indices[local_index - 1]
                                if source_index not in retained_evidence:
                                    retained_evidence.append(source_index)
                else:
                    per_segment = []
                    for segment_index, (slot, source_indices) in enumerate(
                        zip(slots, source_groups), 1
                    ):
                        if slot.get("status") != "observed":
                            continue
                        evidence_frames = [
                            source_indices[local_index - 1]
                            for local_index in slot.get("evidence_frames") or []
                            if 1 <= local_index <= len(source_indices)
                        ]
                        per_segment.append({
                            "segment_index": segment_index,
                            "attribute": path,
                            "text": slot.get("value"),
                            "evidence_frames": evidence_frames,
                        })
                    if per_segment:
                        changes.setdefault(global_key, []).extend(per_segment)
            if retained_parts and len({
                _reverse_source_frame_segment(
                    frame, int(frame_count or 0), segment_count
                )
                for frame in retained_evidence
            }) >= 2:
                empty["facts"][global_key] = "，".join(retained_parts)
                empty["evidence_frames"][global_key] = retained_evidence
        empty["changes"] = changes
        empty["aggregation"] = (
            "deterministic_normalized_validated_attribute_intersection"
        )
        return empty

    field_map = {
        "subject_identity": "subject",
        "recurring_scene_objects": "scene",
        "camera_style": "camera",
        "lighting_style": "lighting",
    }
    field_values = {}
    field_evidence = {}
    for segment_key in ("subject", "scene", "action", "camera", "lighting"):
        field_values[segment_key] = [
            str(entry.get("fields", {}).get(segment_key) or "").strip()
            for entry in entries
        ]
        field_evidence[segment_key] = []
        for entry, source_indices in zip(entries, source_groups):
            field_evidence[segment_key].append([
                source_indices[index - 1]
                for index in (
                    entry.get("evidence_frames", {}).get(segment_key) or []
                )
                if 1 <= index <= len(source_indices)
            ])

    for global_key, segment_key in field_map.items():
        values = field_values[segment_key]
        compact_values = [_compact_reverse_text(value) for value in values]
        evidence_by_segment = field_evidence[segment_key]
        if (
            all(compact_values)
            and len(set(compact_values)) == 1
            and all(evidence_by_segment)
        ):
            empty["facts"][global_key] = values[0]
            empty["evidence_frames"][global_key] = list(dict.fromkeys(
                frame
                for segment_frames in evidence_by_segment
                for frame in segment_frames
            ))
    changes = {}
    for segment_key in ("subject", "scene", "action", "camera", "lighting"):
        values = field_values[segment_key]
        compact_values = [_compact_reverse_text(value) for value in values]
        if all(compact_values) and len(set(compact_values)) == 1:
            continue
        changes[segment_key] = [
            {
                "segment_index": index,
                "text": value,
                "evidence_frames": evidence_frames,
            }
            for index, (value, evidence_frames) in enumerate(
                zip(values, field_evidence[segment_key]), 1
            )
            if _compact_reverse_text(value) and evidence_frames
        ]
    empty["changes"] = {
        key: values for key, values in changes.items() if values
    }
    return empty


def _compact_reverse_text(value):
    return re.sub(r"[\W_]+", "", str(value or "")).lower()


def _reverse_text_similarity(left, right):
    left_compact = _compact_reverse_text(left)
    right_compact = _compact_reverse_text(right)
    if not left_compact or not right_compact:
        return 0.0
    return SequenceMatcher(
        None, left_compact, right_compact, autojunk=False,
    ).ratio()


def _reverse_shingle_jaccard(left, right, size=8):
    left_compact = _compact_reverse_text(left)
    right_compact = _compact_reverse_text(right)
    if min(len(left_compact), len(right_compact)) < size:
        return 0.0
    left_set = {
        left_compact[index:index + size]
        for index in range(len(left_compact) - size + 1)
    }
    right_set = {
        right_compact[index:index + size]
        for index in range(len(right_compact) - size + 1)
    }
    union = left_set | right_set
    return len(left_set & right_set) / float(len(union)) if union else 0.0


def _reverse_segments_are_duplicate(current, previous):
    if current.get("generation") and previous.get("generation"):
        discriminators = (
            "subject.identity",
            "subject.position_scale",
            "action.start",
            "action.process",
            "action.end",
            "scene.background",
            "camera.shot_size",
            "camera.movement",
        )
        for path in discriminators:
            current_slot = _reverse_generation_slot(current, path)
            previous_slot = _reverse_generation_slot(previous, path)
            if (
                current_slot.get("status") == "observed"
                and previous_slot.get("status") == "observed"
                and not _reverse_attribute_equivalent(
                    current_slot.get("value"), previous_slot.get("value")
                )
            ):
                return False
    semantic_keys = ("subject", "action", "scene", "camera", "lighting")
    current_fields = current.get("fields") or {}
    previous_fields = previous.get("fields") or {}
    structured_comparison = (
        sum(bool(current_fields.get(key)) for key in semantic_keys) >= 3
        and sum(bool(previous_fields.get(key)) for key in semantic_keys) >= 3
    )
    if structured_comparison:
        # Compare observable values, not repeated rendering labels or
        # generation-advice scaffolding. An identical action alone must not
        # make a different subject/scene a duplicate.
        current_text = _reverse_segment_field_text(current, *semantic_keys)
        previous_text = _reverse_segment_field_text(previous, *semantic_keys)
    else:
        current_text = current.get("text", "")
        previous_text = previous.get("text", "")
    current_compact = _compact_reverse_text(current_text)
    previous_compact = _compact_reverse_text(previous_text)
    if not current_compact or not previous_compact:
        return False
    if current_compact == previous_compact:
        return True
    if min(len(current_compact), len(previous_compact)) >= 40:
        if (
            _reverse_text_similarity(current_text, previous_text)
            >= _REVERSE_DUPLICATE_SEQUENCE_THRESHOLD
        ):
            return True
        if (
            _reverse_shingle_jaccard(current_text, previous_text)
            >= _REVERSE_DUPLICATE_SHINGLE_THRESHOLD
        ):
            return True

    if structured_comparison:
        return False

    current_action = current.get("fields", {}).get("action", "")
    previous_action = previous.get("fields", {}).get("action", "")
    if min(
        len(_compact_reverse_text(current_action)),
        len(_compact_reverse_text(previous_action)),
    ) >= 12 and _reverse_text_similarity(
        current_action, previous_action
    ) >= 0.85:
        return True
    return False


def _parse_reverse_segment_evidence(raw):
    """Parse one segment while preserving fields used by evidence validation."""
    fields = {}
    parsed = _parse_breakdown_json(raw)
    values = parsed.get("segments") if isinstance(parsed, dict) else None
    if not isinstance(values, list):
        raise ValueError("反推结果缺少 segments 数组，请重试")
    if len(values) != 1:
        raise ValueError(
            "单段反推结果段数错误：需要1段，实际%d段，请重试" % len(values)
        )
    value = values[0]
    if not isinstance(value, dict):
        raise ValueError("反推结果本段必须是结构化对象，请重试")
    generation = _reverse_parse_generation_structure(value)
    shot_boundary, shots = _reverse_parse_shot_structure(value)
    fields = {
        key: " ".join(
            str(value.get(key) or "").replace("\r", "").split()
        ).strip()
        for key, _label in _REVERSE_SEGMENT_FIELDS
    }
    if generation:
        fields.update({
            "subject": _reverse_generation_group_text(
                {"generation": generation}, "subject"
            ),
            "scene": _reverse_generation_group_text(
                {"generation": generation}, "scene"
            ),
            "action": _reverse_generation_group_text(
                {"generation": generation}, "action"
            ),
            "camera": _reverse_generation_group_text(
                {"generation": generation}, "camera"
            ),
            "lighting": _reverse_generation_group_text(
                {"generation": generation}, "lighting"
            ),
            "continuity": "",
        })
    raw_evidence = value.get("evidence_frames") or {}
    if raw_evidence and not isinstance(raw_evidence, dict):
        raise ValueError("反推结果本段帧证据格式错误，请重试")
    evidence_frames = {}
    for key in _REVERSE_FRAME_EVIDENCE_FIELDS:
        indices = raw_evidence.get(key) or []
        if not isinstance(indices, list):
            raise ValueError("反推结果本段帧证据格式错误，请重试")
        normalized = []
        for raw_index in indices:
            try:
                frame_index = int(raw_index)
            except (TypeError, ValueError):
                raise ValueError("反推结果本段帧证据格式错误，请重试")
            if frame_index not in normalized:
                normalized.append(frame_index)
        evidence_frames[key] = normalized
    if generation:
        for key in _REVERSE_FRAME_EVIDENCE_FIELDS:
            group = "lighting" if key == "lighting" else key
            if group not in generation:
                continue
            derived = []
            for slot in generation[group].values():
                if slot.get("status") != "observed":
                    continue
                for frame_index in slot.get("evidence_frames") or []:
                    if frame_index not in derived:
                        derived.append(frame_index)
            evidence_frames[key] = derived
    continuity_evidence_frames = value.get("continuity_evidence_frames") or []
    if not isinstance(continuity_evidence_frames, list):
        raise ValueError("反推结果本段跨段衔接证据格式错误，请重试")
    normalized_continuity_evidence = []
    for raw_index in continuity_evidence_frames:
        try:
            frame_index = int(raw_index)
        except (TypeError, ValueError):
            raise ValueError("反推结果本段跨段衔接证据格式错误，请重试")
        if frame_index not in normalized_continuity_evidence:
            normalized_continuity_evidence.append(frame_index)
    parsed_entry = {
        "fields": fields,
        "generation": generation,
        "shot_boundary": shot_boundary,
        "shots": shots,
    }
    text = (
        _compose_reverse_generation_segment(parsed_entry)
        if generation else _compose_reverse_segment(fields)
    )
    text = str(text or "").strip()
    if not text:
        raise ValueError(
            "反推结果本段为空，缺少 subject、scene、action，请重试"
        )
    return {
        "text": text,
        "fields": fields,
        "generation": generation,
        "shot_boundary": shot_boundary,
        "shots": shots,
        "evidence_frames": evidence_frames,
        "continuity_evidence_frames": normalized_continuity_evidence,
    }


def _reverse_frame_pair_ssim(left, right):
    """Return an auditable pair similarity, or None when ffmpeg cannot prove it."""
    try:
        completed = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "info",
                "-i", left, "-i", right,
                "-lavfi", "[0:v][1:v]ssim",
                "-f", "null", "-",
            ],
            check=True,
            timeout=20,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    detail = completed.stderr.decode("utf-8", "replace")
    matches = re.findall(r"\bAll:([0-9.]+)", detail)
    return float(matches[-1]) if matches else None


def _frames_are_effectively_static(frame_paths):
    """Return True only when every adjacent source frame is visually near-identical."""
    ordered = list(frame_paths or [])
    if len(ordered) < 2:
        return False
    return all(
        similarity is not None
        and similarity >= _REVERSE_STATIC_SSIM_THRESHOLD
        for similarity in (
            _reverse_frame_pair_ssim(left, right)
            for left, right in zip(ordered, ordered[1:])
        )
    )


def _reverse_action_has_static_clause(action):
    action = str(action or "").strip()
    if not action:
        return False
    boundary = r"[，,；;。.!！?？\n]"
    clause_prefix = r"(?:(?:但|却|而|同时|仍然|仍|并且|并)\s*)*"
    return any(
        re.search(
            r"(?:^|%s)\s*%s%s\s*(?=$|%s)"
            % (boundary, clause_prefix, re.escape(marker), boundary),
            action,
        )
        for marker in _REVERSE_STATIC_ACTION_MARKERS
    )


def _reverse_segment_claims_static(entry):
    action = str(entry.get("fields", {}).get("action") or "")
    return (
        _reverse_action_has_static_clause(action)
        and not any(marker in action for marker in _REVERSE_MOTION_ACTION_MARKERS)
    )


def _reverse_segment_field_text(entry, *keys):
    fields = entry.get("fields", {})
    selected = keys or tuple(key for key, _label in _REVERSE_SEGMENT_FIELDS)
    return " ".join(str(fields.get(key) or "") for key in selected).strip()


def _reverse_sound_matches_transcript(sound, transcript):
    sound_text = _compact_reverse_text(sound)
    transcript_text = _compact_reverse_text(transcript)
    if not sound_text or not transcript_text:
        return False
    for marker in (
        "人物口播", "人物说出", "人物说", "口播内容", "语音内容",
        "台词内容", "口播", "语音", "台词", "说出",
        "女声说", "男声说", "有人说", "人物", "女声", "男声", "说",
    ):
        sound_text = sound_text.replace(_compact_reverse_text(marker), "")
    if len(sound_text) < 2:
        return False
    return sound_text in transcript_text


def _reverse_attribute_equivalent(left, right):
    left_compact = _compact_reverse_text(left)
    right_compact = _compact_reverse_text(right)
    if not left_compact or not right_compact:
        return False
    left_negated = any(
        marker in left_compact for marker in _REVERSE_ATTRIBUTE_NEGATION_MARKERS
    )
    right_negated = any(
        marker in right_compact for marker in _REVERSE_ATTRIBUTE_NEGATION_MARKERS
    )
    if left_negated != right_negated:
        return False
    if left_compact in right_compact or right_compact in left_compact:
        return True
    return SequenceMatcher(None, left_compact, right_compact).ratio() >= 0.72


def _reverse_normalize_attribute_value(value):
    compact = _compact_reverse_text(value)
    replacements = (
        ("连帽上衣", "连帽服"),
        ("连帽衫", "连帽服"),
        ("位于画面中间", "位于画面中央"),
        ("画面正中", "画面中央"),
        ("一名", ""),
        ("一位", ""),
        ("的", ""),
    )
    for source, target in replacements:
        compact = compact.replace(source, target)
    return compact


def _reverse_continuity_attribute_equivalent(path, left, right):
    """Compare normalized observable attributes, never sentence similarity alone."""
    left_normalized = _reverse_normalize_attribute_value(left)
    right_normalized = _reverse_normalize_attribute_value(right)
    if not left_normalized or not right_normalized:
        return False

    left_negated = any(
        marker in left_normalized
        for marker in _REVERSE_ATTRIBUTE_NEGATION_MARKERS
    )
    right_negated = any(
        marker in right_normalized
        for marker in _REVERSE_ATTRIBUTE_NEGATION_MARKERS
    )
    if left_negated != right_negated:
        return False

    for group in _REVERSE_ATTRIBUTE_TOKEN_GROUPS:
        left_tokens = {token for token in group if token in left_normalized}
        right_tokens = {token for token in group if token in right_normalized}
        if left_tokens or right_tokens:
            if left_tokens != right_tokens:
                return False

    if left_normalized == right_normalized:
        return True
    # The remaining fuzzy allowance is only for wording around already-equal
    # critical tokens; it cannot override negation, color, garment, position,
    # shot-size or viewing-angle conflicts above.
    return (
        left_normalized in right_normalized
        or right_normalized in left_normalized
        or SequenceMatcher(
            None, left_normalized, right_normalized
        ).ratio() >= 0.82
    )


def _reverse_validate_generation_structure(
    entry, frame_paths, index, pair_ssim=None
):
    generation = entry.get("generation") or {}
    if not generation:
        raise ValueError(
            "反推结果第%d段缺少可生成的 generation 槽位结构，请重试" % index
        )
    frame_count = len(frame_paths or [])
    boundary = entry.get("shot_boundary") or {}
    boundary_type = boundary.get("type")
    if boundary_type == "unknown":
        raise ValueError("反推结果第%d段无法确认是否存在镜头切换，请重试" % index)
    if boundary_type not in {"continuous", "hard_cut"}:
        raise ValueError("反推结果第%d段缺少镜头连续/硬切判定，请重试" % index)
    if set(boundary.get("evidence_frames") or []) != {1, frame_count}:
        raise ValueError(
            "反推结果第%d段镜头切换判定必须引用本段首尾帧，请重试" % index
        )
    if (
        pair_ssim is not None
        and pair_ssim <= _REVERSE_HARD_CUT_SSIM_THRESHOLD
        and boundary_type != "hard_cut"
    ):
        raise ValueError(
            "反推结果第%d段首尾帧存在硬切，不能合并为同一动作，请重试" % index
        )
    if (
        pair_ssim is not None
        and pair_ssim >= _REVERSE_STATIC_SSIM_THRESHOLD
        and boundary_type == "hard_cut"
    ):
        raise ValueError("反推结果第%d段静止双帧被误判为硬切，请重试" % index)

    shots = entry.get("shots") or []
    if len(shots) != 2 or [shot.get("frame") for shot in shots] != [1, frame_count]:
        raise ValueError(
            "反推结果第%d段必须分别保存首尾帧的可生成镜头状态，请重试" % index
        )
    for shot_number, shot in enumerate(shots, 1):
        for key in ("subject", "scene", "camera", "lighting", "style"):
            if not str(shot.get(key) or "").strip():
                raise ValueError(
                    "反推结果第%d段镜头%d缺少%s状态，请重试"
                    % (index, shot_number, key)
                )

    applicable = []
    ready = []
    observed = []
    evidenced = []
    for path in _reverse_generation_slot_paths():
        slot = _reverse_generation_slot(entry, path)
        status = slot.get("status")
        if (
            path in _REVERSE_ALWAYS_APPLICABLE_SLOTS
            and boundary_type != "hard_cut"
            and status == "not_applicable"
        ):
            raise ValueError(
                "反推结果第%d段必需槽位%s不能标记not_applicable"
                % (index, path)
            )
        if boundary_type == "hard_cut" and path.startswith("action."):
            if status != "not_applicable":
                raise ValueError(
                    "反推结果第%d段硬切两侧不能编造成同一连续动作，请重试" % index
                )
            continue
        if status == "not_applicable":
            continue
        applicable.append(path)
        if status == "observed":
            ready.append(path)
            observed.append(path)
            evidence = slot.get("evidence_frames") or []
            if (
                evidence
                and all(1 <= frame <= frame_count for frame in evidence)
            ):
                evidenced.append(path)
            else:
                raise ValueError(
                    "反推结果第%d段槽位%s缺少有效帧证据，请重试"
                    % (index, path)
                )

    readiness = int(round(100.0 * len(ready) / max(1, len(applicable))))
    readiness_target = int(
        _REVERSE_VISUAL_SEMANTIC_CONTRACT["components"]
        ["generation_readiness"]["target"]
    )
    if readiness < readiness_target:
        raise ValueError(
            "反推结果第%d段生成就绪槽位仅%d%%，至少需要%d%%；"
            "证据不足应写unknown而非编造，但本段仍不足以生成同款"
            % (index, readiness, readiness_target)
        )

    motion_type = _reverse_generation_slot(
        entry, "action.motion_type"
    ).get("value", "")
    if boundary_type == "continuous":
        if motion_type not in {"dynamic", "static"}:
            raise ValueError(
                "反推结果第%d段 action.motion_type 必须明确为dynamic或static"
                % index
            )
        start = _reverse_generation_slot(entry, "action.start")
        end = _reverse_generation_slot(entry, "action.end")
        motion = _reverse_generation_slot(entry, "action.motion_type")
        if (
            1 not in (start.get("evidence_frames") or [])
            or frame_count not in (end.get("evidence_frames") or [])
            or not {1, frame_count}.issubset(
                set(motion.get("evidence_frames") or [])
            )
        ):
            raise ValueError(
                "反推结果第%d段动作起点必须引用首帧、终点必须引用尾帧，"
                "动作类型必须同时引用首尾帧"
                % index
            )
        if motion_type == "dynamic":
            for path in ("action.process", "action.direction_speed"):
                if _reverse_generation_slot(
                    entry, path
                ).get("status") == "not_applicable":
                    raise ValueError(
                        "反推结果第%d段动态动作槽位%s不能标记not_applicable"
                        % (index, path)
                    )
            if (
                start.get("status") != "observed"
                or end.get("status") != "observed"
                or _reverse_attribute_equivalent(
                    start.get("value"), end.get("value")
                )
            ):
                raise ValueError(
                    "反推结果第%d段动态动作必须给出可区分的首尾状态，请重试"
                    % index
                )
        elif not _frames_are_effectively_static(frame_paths):
            raise ValueError("反推结果第%d段无静止画面证据，请重试" % index)

    identity = _reverse_generation_slot(
        entry, "subject.identity"
    ).get("value", "")
    if (
        any(marker in identity for marker in ("人物", "女性", "男性", "男孩", "女孩"))
        and _reverse_generation_slot(
            entry, "subject.wardrobe"
        ).get("status") == "not_applicable"
    ):
        raise ValueError(
            "反推结果第%d段人物服装槽位不能标记not_applicable；"
            "无法确认时必须写unknown"
            % index
        )

    observed_generation_text = " ".join(
        str(_reverse_generation_slot(entry, path).get("value") or "")
        for path in _reverse_generation_slot_paths()
        if _reverse_generation_slot(entry, path).get("status") == "observed"
    )
    shot_state_text = " ".join(
        str(shot.get(key) or "")
        for shot in shots
        for key in ("subject", "scene", "camera", "lighting", "style")
    )
    ambiguous_accessory = next(
        (
            marker for marker in _REVERSE_AMBIGUOUS_ACCESSORY_MARKERS
            if marker in observed_generation_text or marker in shot_state_text
        ),
        None,
    )
    if ambiguous_accessory:
        raise ValueError(
            "反推结果第%d段把双帧无法独立核验的织物写成“%s”；"
            "同一次模型响应中的重复描述不能自证事实，必须改写为unknown或可见中性形状"
            % (index, ambiguous_accessory)
        )

    associated = _reverse_generation_slot(
        entry, "action.associated_object"
    )
    process_value = _reverse_generation_slot(
        entry, "action.process"
    ).get("value", "")
    interpretive_action = next(
        (
            marker for marker in _REVERSE_INTERPRETIVE_ACTION_MARKERS
            if marker in process_value
        ),
        None,
    )
    if interpretive_action:
        raise ValueError(
            "反推结果第%d段动作包含双帧不能证明的意图词“%s”；"
            "不得把手部变化臆写为整理卫衣等动作，只能写首尾可见位移"
            % (index, interpretive_action)
        )
    if associated.get("status") == "observed":
        supporting_text = " ".join([
            _reverse_generation_slot(
                entry, "subject.wardrobe"
            ).get("value", ""),
            _reverse_generation_slot(
                entry, "subject.appearance"
            ).get("value", ""),
        ] + [str(shot.get("subject") or "") for shot in shots])
        if not _reverse_attribute_equivalent(
            associated.get("value"), supporting_text
        ):
            raise ValueError(
                "反推结果第%d段动作关联物无法由主体外观/服装和首尾镜头共同证明，"
                "不得把织物臆认为围巾等具体物件"
                % index
            )

    factual_checks = (
        "shot_boundary_matches_pair_evidence",
        "shot_states_have_local_frame_evidence",
        "observed_slots_have_local_frame_evidence",
        "action_start_end_match_first_last_frames",
        "static_claim_requires_ssim",
        "no_ambiguous_accessory_self_corroboration",
        "no_interpretive_action_from_sparse_frames",
    )
    passed_factual_checks = list(factual_checks)
    factual_consistency = int(round(
        100.0 * len(passed_factual_checks) / max(1, len(factual_checks))
    ))
    return {
        "applicable_slots": len(applicable),
        "ready_slots": len(ready),
        "observed_slots": len(observed),
        "evidenced_slots": len(evidenced),
        "generation_readiness": readiness,
        "source_evidence_coverage": int(round(
            100.0 * len(evidenced) / max(1, len(observed))
        )),
        "factual_consistency": factual_consistency,
        "factual_consistency_checks": passed_factual_checks,
        "shot_boundary": boundary_type,
        "pair_ssim": pair_ssim,
    }


def _validate_reverse_segment_evidence(
    entry, previous_entries, frame_paths, index, transcript="",
    require_frame_evidence=False, global_continuity=None,
    require_generation_readiness=False, pair_ssim=None,
    enforce_length_limit=True,
):
    text = entry.get("text", "")
    compact = _compact_reverse_text(text)
    if not compact:
        raise ValueError("反推结果第%d段为空，请重试" % index)
    if enforce_length_limit and len(compact) > _REVERSE_MAX_SEGMENT_CHARS:
        raise ValueError(
            "反推结果第%d段过长：最多%d字，实际%d字，请重试"
            % (index, _REVERSE_MAX_SEGMENT_CHARS, len(compact))
        )

    fields = entry.get("fields", {})
    missing_critical_fields = []
    for key, label in (("subject", "主体"), ("scene", "场景"), ("action", "动作")):
        if (
            key == "action"
            and (entry.get("shot_boundary") or {}).get("type") == "hard_cut"
        ):
            continue
        compact_field = _compact_reverse_text(fields.get(key, ""))
        if not compact_field:
            missing_critical_fields.append("%s（%s）" % (key, label))
            continue
        if compact_field in {
            _compact_reverse_text(value)
            for value in _REVERSE_EMPTY_PLACEHOLDER_VALUES
        }:
            raise ValueError(
                "反推结果第%d段关键%s是空洞占位内容，请重试" % (index, label)
            )
    if missing_critical_fields:
        raise ValueError(
            "反推结果第%d段缺少可生成的关键字段：%s，请重试"
            % (index, "、".join(missing_critical_fields))
        )
    if require_frame_evidence:
        evidence_frames = entry.get("evidence_frames") or {}
        frame_count = len(frame_paths or [])
        for key in _REVERSE_FRAME_EVIDENCE_FIELDS:
            if not _compact_reverse_text(fields.get(key, "")):
                continue
            indices = evidence_frames.get(key) or []
            if not indices:
                raise ValueError(
                    "反推结果第%d段%s缺少本段原始帧证据，请重试"
                    % (index, dict(_REVERSE_SEGMENT_FIELDS)[key])
                )
            if any(frame < 1 or frame > frame_count for frame in indices):
                raise ValueError(
                    "反推结果第%d段帧证据超出本段原始帧范围，请重试" % index
                )
        action_evidence = set(evidence_frames.get("action") or [])
        if (
            (entry.get("shot_boundary") or {}).get("type") != "hard_cut"
            and frame_count >= 2
            and not {1, frame_count}.issubset(action_evidence)
        ):
            raise ValueError(
                "反推结果第%d段动作时序必须同时引用本段首尾原始帧，请重试"
                % index
            )

    subject = fields.get("subject", "")
    action = fields.get("action", "")
    sound = fields.get("sound", "")
    continuity = fields.get("continuity", "")
    all_fields = _reverse_segment_field_text(entry)
    visual_fields = _reverse_segment_field_text(
        entry,
        "subject", "scene", "action", "camera", "lighting", "continuity",
    )

    if (
        _reverse_action_has_static_clause(action)
        and any(marker in action for marker in _REVERSE_MOTION_ACTION_MARKERS)
    ):
        raise ValueError("反推结果第%d段动作与“无变化”自相矛盾，请重试" % index)

    facing_text = _reverse_segment_field_text(entry, "subject", "action")
    if (
        any(marker in facing_text for marker in _REVERSE_BACK_FACING_MARKERS)
        and any(marker in facing_text for marker in _REVERSE_FACE_CLAIM_MARKERS)
    ):
        raise ValueError("反推结果第%d段描述了不可见的背面表情，请重试" % index)

    unsupported = next(
        (
            marker for marker in _REVERSE_UNSUPPORTED_INFERENCE_MARKERS
            if marker in visual_fields
        ),
        None,
    )
    if unsupported:
        raise ValueError(
            "反推结果第%d段包含无证据主观推断“%s”，请重试"
            % (index, unsupported)
        )

    unreliable_orientation = next(
        (
            marker for marker in _REVERSE_UNRELIABLE_ORIENTATION_MARKERS
            if marker in visual_fields
        ),
        None,
    )
    if unreliable_orientation:
        raise ValueError(
            "反推结果第%d段包含无可靠证据方位“%s”，请重试"
            % (index, unreliable_orientation)
        )

    invalid_sound = next(
        (
            marker for marker in _REVERSE_INVALID_SOUND_MARKERS
            if marker in sound
        ),
        None,
    )
    if invalid_sound:
        raise ValueError("反推结果第%d段从画面推断声音，请重试" % index)
    if _compact_reverse_text(sound) and not str(transcript or "").strip():
        raise ValueError("反推结果第%d段声音缺少本段ASR证据，请重试" % index)
    if (
        _compact_reverse_text(sound)
        and not _reverse_sound_matches_transcript(sound, transcript)
    ):
        raise ValueError("反推结果第%d段声音与本段ASR内容不匹配，请重试" % index)
    if (
        str(transcript or "").strip()
        and any(marker in sound for marker in _REVERSE_NO_SPEECH_MARKERS)
    ):
        raise ValueError("反推结果第%d段声音与本段ASR自相矛盾，请重试" % index)

    fixed_continuity = next(
        (
            marker for marker in _REVERSE_FIXED_CONTINUITY_MARKERS
            if marker in continuity
        ),
        None,
    )
    if fixed_continuity:
        raise ValueError(
            "反推结果第%d段使用固定衔接文字“%s”，请重试"
            % (index, fixed_continuity)
        )
    if not global_continuity and (
        _compact_reverse_text(continuity)
        or entry.get("continuity_evidence_frames")
    ):
        raise ValueError(
            "反推结果第%d段不能在隔离分段分析中编造跨段衔接，请重试"
            % index
        )
    if require_frame_evidence and _compact_reverse_text(continuity):
        continuity_indices = entry.get("continuity_evidence_frames") or []
        total_frames = int((global_continuity or {}).get("frame_count") or 8)
        segment_count = int(
            (global_continuity or {}).get("segment_count") or 0
        )
        if segment_count < 1:
            raise ValueError(
                "反推结果第%d段衔接缺少实际分段数量，请重试" % index
            )
        if segment_count == 1:
            raise ValueError(
                "反推结果第%d段是单段视频，不能编造跨段衔接，请重试" % index
            )
        allowed_global_indices = {
            frame
            for indices in (
                (global_continuity or {}).get("evidence_frames") or {}
            ).values()
            for frame in indices
        }
        if (
            not continuity_indices
            or any(
                frame < 1 or frame > total_frames
                for frame in continuity_indices
            )
            or not set(continuity_indices).issubset(allowed_global_indices)
        ):
            raise ValueError(
                "反推结果第%d段衔接缺少可核验的全局原始帧证据，请重试"
                % index
            )
        continuity_segments = {
            _reverse_source_frame_segment(
                frame, total_frames, segment_count
            )
            for frame in continuity_indices
        }
        if (
            index not in continuity_segments
            or len(continuity_segments) < 2
            or not any(
                abs(segment - index) == 1
                for segment in continuity_segments
            )
        ):
            raise ValueError(
                "反推结果第%d段衔接必须覆盖本段和相邻时间段原始帧，请重试"
                % index
            )

    if "字幕" in all_fields and not re.search(
        r"字幕[^“”\"]*[“\"]\s*[^“”\"]{1,80}\s*[”\"]",
        all_fields,
    ):
        raise ValueError("反推结果第%d段字幕缺少可核验逐字内容，请重试" % index)

    if min(
        len(_compact_reverse_text(subject)),
        len(_compact_reverse_text(action)),
    ) >= 12 and _reverse_text_similarity(subject, action) >= 0.80:
        raise ValueError("反推结果第%d段主体与动作机械重复，请重试" % index)

    if (
        _reverse_segment_claims_static(entry)
        and not _frames_are_effectively_static(frame_paths)
    ):
        raise ValueError("反推结果第%d段无静止画面证据，请重试" % index)

    if require_generation_readiness:
        entry["validation_summary"] = _reverse_validate_generation_structure(
            entry, frame_paths, index, pair_ssim=pair_ssim
        )

    for previous_index, previous in enumerate(previous_entries, 1):
        if _reverse_segments_are_duplicate(entry, previous):
            raise ValueError(
                "反推结果第%d段与第%d段内容重复，请重试"
                % (index, previous_index)
            )
    return entry


def _validate_reverse_prompt_lengths(
    segments, check_duplicates=True, enforce_length_limits=True,
    enforce_total_length_limit=True,
):
    """Keep a generous output cap and strict duplicate guard; no minimum length."""
    if not segments:
        raise ValueError("反推结果为空，请重试")
    lengths = [len(re.sub(r"\s+", "", segment or "")) for segment in segments]
    for index, length in enumerate(lengths, 1):
        if enforce_length_limits and length > _REVERSE_MAX_SEGMENT_CHARS:
            raise ValueError(
                "反推结果第%d段过长：最多%d字，实际%d字，请重试"
                % (index, _REVERSE_MAX_SEGMENT_CHARS, length)
            )
    if check_duplicates:
        entries = [{"text": segment, "fields": {}} for segment in segments]
        for index, entry in enumerate(entries):
            for previous_index, previous in enumerate(entries[:index]):
                if _reverse_segments_are_duplicate(entry, previous):
                    raise ValueError(
                        "反推结果第%d段与第%d段内容重复，请重试"
                        % (index + 1, previous_index + 1)
                    )
    total = sum(lengths)
    if enforce_total_length_limit and total > _REVERSE_MAX_TOTAL_CHARS:
        raise ValueError(
            "反推结果总长度最多%d字，实际%d字，请重试"
            % (_REVERSE_MAX_TOTAL_CHARS, total)
        )
    return segments


def _split_reverse_text(text, expected_count):
    """Deterministically recover one model response without another AI call."""
    cleaned = _strip_json_code_fence(text)
    cleaned = re.sub(r"^\s*(?:提示词|反推结果)\s*[:：]\s*", "", cleaned)
    cleaned = re.sub(r"^\s*[\[{]\s*\"?segments\"?\s*[:：]\s*", "", cleaned)
    cleaned = cleaned.strip().strip("`\"'[]{} \n")
    if len(cleaned) < expected_count * 8:
        raise ValueError("反推结果解析失败，请重试")

    sentences = [
        item.strip(" \t\r\n,，\"'")
        for item in re.split(r"(?<=[。！？；])\s*|\n+", cleaned)
        if item.strip(" \t\r\n,，\"'")
    ]
    if len(sentences) >= expected_count:
        groups = []
        start = 0
        for index in range(expected_count):
            remaining_groups = expected_count - index
            remaining_items = len(sentences) - start
            take = max(1, int(math.ceil(remaining_items / float(remaining_groups))))
            groups.append("".join(sentences[start:start + take]))
            start += take
    else:
        groups = []
        for index in range(expected_count):
            begin = round(index * len(cleaned) / float(expected_count))
            end = round((index + 1) * len(cleaned) / float(expected_count))
            groups.append(cleaned[begin:end].strip())

    result = []
    for index, group in enumerate(groups, 1):
        group = re.sub(
            r"^\s*(?:[-*]\s*|\d+[.)、]\s*)?"
            r"(?:\[[^\]]+\]|\d+(?:\.\d+)?\s*[-至到]\s*\d+(?:\.\d+)?\s*秒)\s*[:：]?\s*",
            "",
            group,
        ).strip()
        if not group:
            raise ValueError("反推结果第%d段为空，请重试" % index)
        result.append(group)
    return result


def _reverse_generation_schema_template():
    slot = {"status": "", "value": "", "evidence_frames": []}
    return {
        group: {key: dict(slot) for key in keys}
        for group, keys in _REVERSE_GENERATION_SLOT_GROUPS.items()
    }


def _reverse_segment_messages(
    title, duration, platform, transcript, index, segment_count, timeline_range,
    retry=False, retry_error=None, pair_ssim=None,
):
    retry_note = ""
    if retry:
        retry_note = (
            "这是本时间段基于原始帧的重新分析。不要沿用任何历史草稿、模板句或其他时间段文字，"
            "必须重新逐帧观察后作答。上一轮校验错误：%s。"
            "请针对错误中列出的缺失 subject、scene 或 action 重新检查当前原始双帧；"
            "非人物主体同样必须填写，禁止返回全空JSON。"
            % str(retry_error or "输出未通过结构与证据校验")
        )
    cut_hint = ""
    if pair_ssim is not None and pair_ssim <= _REVERSE_HARD_CUT_SSIM_THRESHOLD:
        cut_hint = (
            "代码像素预检发现首尾帧强不连续（SSIM=%.3f），本段必须标记hard_cut，"
            "分别描述镜头A和镜头B，所有action槽位标记not_applicable；"
            "绝不能把两侧主体编成同一连续动作。\n"
            % pair_ssim
        )
    schema = {
        "segments": [{
            "shot_boundary": {
                "type": "continuous|hard_cut|unknown",
                "evidence_frames": [1, 2],
            },
            "shots": [
                {
                    "frame": 1,
                    "subject": "",
                    "scene": "",
                    "camera": "",
                    "lighting": "",
                    "style": "",
                },
                {
                    "frame": 2,
                    "subject": "",
                    "scene": "",
                    "camera": "",
                    "lighting": "",
                    "style": "",
                },
            ],
            "generation": _reverse_generation_schema_template(),
            "sound": "",
            "continuity_evidence_frames": [],
        }],
    }
    usermsg = (
        "视频标题（仅作背景，不能代替画面证据）：%s\n"
        "视频总时长：%ss\n"
        "平台：%s\n"
        "当前时间段：第%d/%d段 %s\n"
        "当前时间段ASR证据：%s\n\n"
        "%s"
        "本次先做隔离分段取证；跨段连续性将在全部分段通过校验后由代码确定性归纳。"
        "不得引用其他时间段、历史草稿或臆造跨段事实。\n\n"
        "%s"
        "随请求附带的图片只属于当前时间段，并按时间先后排列；至少包含两个原始时间点。"
        "只分析这些图片，不得借用其他时间段画面。逐帧比较主体位置、手臂手腕、身体姿态、视线、"
        "场景道具、遮挡、构图和光线的真实变化。只有相邻帧确实近乎一致时，action 才能写画面或姿态无变化；"
        "存在坐起、抬手、转身等动作时绝不能同时写“未观察到明显动作变化”，细微动作也必须明确区分。"
        "主体背对镜头或面部被遮挡时，不得描述表情、眼神或微笑。"
        "不能从图片直接确认的身份、品牌、地点、情绪、运镜、光源、方位或情节一律省略；"
        "不要写“似乎在感受风”“阳光明媚”“绿草如茵”等主观修辞，要改成可观察的姿态、光照或颜色事实。"
        "不得写“面向树根”等无法由本段帧可靠证明的朝向。"
        "sound 只能逐字引用或紧贴复述上方当前时间段ASR能够证明的口播或可辨识语音；"
        "不得从ASR推断音乐、音效、情绪或环境声；没有证据就留空，无法与ASR文字直接匹配也留空，"
        "不得根据画面写“未观察到声音”。字幕或屏幕文字只有在本段图片清晰可读时才可逐字引用，"
        "并用中文引号标出实际文字；看不清就省略。"
        "不要补写过渡动作，不要为了篇幅扩写，不设最低字数，宁可短也不能编造。"
        "这是可直接交给视频生成模型的分段提示词，不是普通画面说明。"
        "主体不一定是人物；产品、普通物体、几何图形、色块、文字、动物或风景中的主要可见对象也必须作为 subject，"
        "按证据写清颜色、形状、材质和画面位置，不得因画面无人而把主体留空。"
        "如果没有独立实体，subject 应写可由双帧证明的“抽象画面”或“纯色背景”及其主色、范围和结构。"
        "scene 只写主体周围环境、背景和空间关系，不能把主要实体只塞进 scene 而让 subject 为空。"
        "静态非人物画面也必须形成可生成提示词：若首尾两帧中的主体位置、形状和画面确实一致，"
        "action 应准确写“主体保持静止，未观察到位置或形态变化”，并引用局部帧1、2；"
        "不得为填充 action 编造移动、变形或运镜。"
        "先判定shot_boundary：continuous表示同一镜头连续变化，hard_cut表示两个不同镜头；"
        "必须引用局部帧1、2。shots必须分别保存首帧和尾帧的主体、场景、景别机位构图、光影色彩、"
        "风格材质；无法确认的项明确写unknown，不得把一侧事实复制到另一侧。\n"
        "generation每个槽位都是{status,value,evidence_frames}："
        "status只能是observed、unknown、not_applicable。observed必须有可直接证明该值的局部帧；"
        "unknown表示证据不足，value写unknown且不带帧；not_applicable只用于画面确实不适用的槽位。"
        "unknown不会冒充生成就绪，不能为了达到80%%把猜测标为observed。"
        "主体身份类别不等于真人姓名；只写人物/动物/物体等可见类别。"
        "服装、关联物件必须用可见中性名称；本链路没有独立物件检测器，围巾、披肩、飘带等易混淆配饰"
        "一律写unknown或可观察的中性形状（如“长条织物”），不得让同一次回答中的多个字段互相自证。"
        "整理、调整、检查、寻找、准备、感受等意图词无法由两个端点帧证明，一律改写为首尾可见位置或姿态变化。"
        "动作必须拆成起点、过程、终点、方向与可见速度；两帧不能证明过程或精确速度时写unknown。"
        "dynamic必须给出不同的首尾状态；static只有双帧近乎一致时可用。"
        "hard_cut时两侧不是同一动作，所有action槽位必须not_applicable，分别依靠shots描述。"
        "scene必须区分前景、中景、背景和空间关系；camera必须区分景别、机位、视角、构图和可见运镜；"
        "lighting/style/rhythm分别记录光线色调、风格材质和镜头节奏。"
        "continuity槽位在隔离分析阶段标记not_applicable，代码稍后只从规范化已验证属性聚合。"
        "不要输出生成建议参数，代码会将建议与源事实分开生成。"
        "sound仍只能与本段ASR逐字匹配；无证据留空。"
        "只输出以下结构的一个JSON对象，不要时间码、解释或markdown：%s"
    ) % (
        str(title or ""),
        str(duration),
        str(platform or ""),
        index,
        segment_count,
        timeline_range,
        transcript or "（无可确认的本段口播或声音）",
        cut_hint,
        retry_note,
        json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
    )
    sysmsg = (
        "你是短视频画面证据分析员。准确性高于完整性和篇幅；只写当前时间段原始帧或ASR能证明的事实。"
        "严格输出指定JSON，不得复制其他时间段，不得使用固定连续性扩写。"
    )
    return sysmsg, usermsg


def _score_reverse_generation_coverage(entries, global_continuity, windows):
    """Keep source evidence, generation readiness and consistency independent."""
    entries = list(entries or [])
    if entries and all(entry.get("structured_generation") for entry in entries):
        segment_scores = []
        for index, entry in enumerate(entries, 1):
            summary = _gemini_validation_summary(entry)
            entry["validation_summary"] = summary
            segment_scores.append({
                "segment_index": index,
                **summary,
            })
        components = {
            key: min(score[key] for score in segment_scores)
            for key in (
                "source_evidence_coverage",
                "generation_readiness",
                "factual_consistency",
            )
        }
        return {
            "total": min(components.values()),
            "components": components,
            "segment_scores": segment_scores,
            "legacy_unstructured": False,
            "generated_video_similarity_claim": False,
        }
    if entries and all(entry.get("generation") for entry in entries):
        segment_scores = []
        for index, entry in enumerate(entries, 1):
            summary = dict(entry.get("validation_summary") or {})
            if not summary:
                raise ValueError(
                    "反推结果第%d段缺少生成就绪校验摘要，请重试" % index
                )
            segment_scores.append({
                "segment_index": index,
                **summary,
            })
        source_score = min(
            score["source_evidence_coverage"] for score in segment_scores
        )
        readiness_score = min(
            score["generation_readiness"] for score in segment_scores
        )
        consistency_score = min(
            score["factual_consistency"] for score in segment_scores
        )
        components = {
            "source_evidence_coverage": source_score,
            "generation_readiness": readiness_score,
            "factual_consistency": consistency_score,
        }
        return {
            "total": min(components.values()),
            "components": components,
            "segment_scores": segment_scores,
            "generated_video_similarity_claim": False,
        }

    # Compatibility for direct helper callers. Production reverse requests
    # require the structured path before this scorer is reached.
    fields = [entry.get("fields", {}) for entry in (entries or [])]
    expected = len(windows or [])
    complete = lambda key: (
        bool(fields)
        and len(fields) == expected
        and all(_compact_reverse_text(item.get(key, "")) for item in fields)
    )
    evidence_complete = lambda key: (
        complete(key)
        and all(
            bool(entry.get("evidence_frames", {}).get(key))
            for entry in (entries or [])
        )
    )
    parts = {
        "subject": 30 if evidence_complete("subject") else 0,
        "action_timing": (
            20 if evidence_complete("action") else 0
        ) + (5 if expected else 0),
        "scene_composition": 20 if evidence_complete("scene") else 0,
        "camera_duration": (
            10 if evidence_complete("camera") else 0
        ) + (5 if expected else 0),
        "lighting_style": 10 if evidence_complete("lighting") else 0,
    }
    total = sum(parts.values())
    return {
        "total": total,
        "parts": parts,
        "components": {
            "source_evidence_coverage": total,
            "generation_readiness": 0,
            "factual_consistency": 0,
        },
        "legacy_unstructured": True,
        "generated_video_similarity_claim": False,
    }


def _assemble_reverse_prompt(
    entries, windows, global_continuity=None, enforce_length_limits=True,
    enforce_total_length_limit=True,
):
    raw_segments = []
    for entry, (start, end, _timeline_range) in zip(entries, windows):
        if entry.get("generation"):
            suggestion = {
                "scope": "recommendation_not_observed_source_fact",
                "duration_seconds": round(float(end) - float(start), 1),
                "reference_local_frames": [1, 2],
            }
            entry["generation_suggestions"] = suggestion
            entry["text"] = _compose_reverse_generation_segment(
                entry, suggestion=suggestion
            )
        raw_segments.append(entry["text"])
    segments = _validate_reverse_prompt_lengths(
        raw_segments,
        check_duplicates=not all(entry.get("generation") for entry in entries),
        enforce_length_limits=enforce_length_limits,
        enforce_total_length_limit=enforce_total_length_limit,
    )
    global_facts = _compose_reverse_global_facts(global_continuity)
    lines = []
    for index, ((_start, _end, timeline_range), segment) in enumerate(
        zip(windows, segments)
    ):
        if index == 0 and global_facts:
            segment = "全局连续性事实：" + global_facts + "；本段：" + segment
        lines.append(timeline_range + " " + segment)
    return "\n".join(lines)


_GEMINI_FACT_FIELDS = (
    "subject_identity", "subject_appearance", "wardrobe", "position_scale",
    "action_start", "action_process", "action_end", "direction_speed",
    "foreground", "midground", "background", "shot_scale", "camera_angle",
    "camera_movement", "composition", "lighting_color", "style_texture",
    "rhythm", "sound", "subtitles", "continuity",
)
_GEMINI_OPTIONAL_FACT_FIELDS = {"wardrobe", "sound", "subtitles", "continuity"}


def _gemini_reverse_schema():
    fact_row = {
        "type": "object", "additionalProperties": False,
        "required": ["key", "value", "evidence_seconds"],
        "properties": {
            "key": {"type": "string", "enum": list(_GEMINI_FACT_FIELDS)},
            "value": {"type": "string"},
            "evidence_seconds": {
                "type": "array",
                "items": {"type": "number", "minimum": 0},
                "maxItems": 3,
            },
        },
    }
    transition = {
        "type": "object", "additionalProperties": False,
        "required": ["type", "description", "evidence_seconds"],
        "properties": {
            "type": {
                "type": "string",
                "enum": sorted(_REVERSE_TRANSITION_TYPES),
            },
            "description": {"type": "string"},
            "evidence_seconds": {
                "type": "array",
                "items": {"type": "number", "minimum": 0},
                "maxItems": 3,
            },
        },
    }
    return {
        "type": "object", "additionalProperties": False, "required": ["shots"],
        "properties": {"shots": {
            "type": "array", "minItems": 1, "maxItems": 4,
            "items": {
                "type": "object", "additionalProperties": False,
                "required": [
                    "segment_id", "transition_from_previous", "facts",
                    "generation_advice",
                ],
                "properties": {
                    "segment_id": {"type": "integer", "minimum": 1},
                    "transition_from_previous": transition,
                    "facts": {
                        "type": "array",
                        "minItems": len(_GEMINI_FACT_FIELDS),
                        "maxItems": len(_GEMINI_FACT_FIELDS),
                        "items": fact_row,
                    },
                    "generation_advice": {
                        "type": "object", "additionalProperties": False,
                        "required": ["aspect_ratio", "fps", "camera_control", "negative_prompt"],
                        "properties": {
                            "aspect_ratio": {"type": "string"},
                            "fps": {"type": "string"},
                            "camera_control": {"type": "string"},
                            "negative_prompt": {"type": "string"},
                        },
                    },
                },
            },
        }},
    }


def _gemini_reverse_provider_schema():
    """Drop only live-API-incompatible array bounds; parser retains them."""
    def compatible(value):
        if isinstance(value, dict):
            return {
                key: compatible(item)
                for key, item in value.items()
                if key not in {"minItems", "maxItems"}
            }
        if isinstance(value, list):
            return [compatible(item) for item in value]
        return value

    return compatible(_gemini_reverse_schema())


def _redact_sensitive_text(value, limit=240):
    text = str(value or "")
    text = re.sub(r"https?://\S+", "[redacted-url]", text)
    text = re.sub(
        r"(?i)\b(authorization)\b\s*[:=]\s*(?:bearer\s+)?[\"']?[^,\s;\"'}]+",
        r"\1: [redacted-credential]",
        text,
    )
    text = re.sub(
        r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{6,}",
        "Bearer [redacted-credential]",
        text,
    )
    text = re.sub(
        r"(?i)\b(api[_ -]?key|access[_ -]?token|token|secret)\b"
        r"\s*[:=]\s*[\"']?[^,\s;\"'}]+",
        r"\1=[redacted-credential]",
        text,
    )
    text = re.sub(
        r"\b(?:AIza|AQ\.)[A-Za-z0-9._-]{8,}\b",
        "[redacted-credential]",
        text,
    )
    return text[:max(0, int(limit))]


def _gemini_http_error_summary(error):
    code = int(getattr(error, "code", 0) or 0)
    status = ""
    message = ""
    try:
        raw = error.read(8193)
        if len(raw) <= 8192:
            payload = json.loads(raw.decode("utf-8"))
            detail = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(detail, dict):
                code = int(detail.get("code") or code)
                status = str(detail.get("status") or "")
                message = _gemini_fact_text(detail.get("message"))
    except Exception:
        pass
    status = status if re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", status) else ""
    message = _redact_sensitive_text(message)
    parts = ["Gemini HTTP %d" % code]
    if status:
        parts.append(status)
    if message:
        parts.append(message)
    return ": ".join(parts)




def _gemini_timeout(deadline):
    remaining = _analysis_remaining(deadline)
    if remaining is None:
        return _GEMINI_REQUEST_TIMEOUT
    return max(1, min(_GEMINI_REQUEST_TIMEOUT, int(remaining - 1)))


def _gemini_open(request, deadline=None, heartbeat=None, retry_transient=True):
    attempts = 2 if retry_transient else 1
    last_error = None
    for attempt in range(attempts):
        _analysis_remaining(deadline)
        if heartbeat:
            heartbeat()
        try:
            return urllib.request.urlopen(request, timeout=_gemini_timeout(deadline))
        except urllib.error.HTTPError as error:
            last_error = error
            code = int(getattr(error, "code", 0) or 0)
            if attempt + 1 < attempts and (code == 429 or 500 <= code < 600):
                continue
            raise RuntimeError(_gemini_http_error_summary(error)) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last_error = error
            if attempt + 1 < attempts:
                continue
            raise RuntimeError("Gemini request failed: %s" % type(error).__name__) from error
    raise RuntimeError("Gemini request failed") from last_error


def _gemini_json_request(url, body, api_key, deadline=None, heartbeat=None):
    request = urllib.request.Request(
        url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key}, method="POST",
    )
    with _gemini_open(request, deadline=deadline, heartbeat=heartbeat) as response:
        raw = response.read()
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as error:
        raise ValueError("Gemini returned invalid JSON transport response") from error


def _gemini_upload_file(path, mime_type, api_key, deadline=None, heartbeat=None):
    size = os.path.getsize(path)
    if size <= 0 or size > _GEMINI_MAX_MEDIA_BYTES:
        raise ValueError("Gemini reverse media size is outside the allowed range")
    start = urllib.request.Request(
        _GEMINI_API_BASE + "/upload/v1beta/files",
        data=json.dumps({"file": {"display_name": "breakdown-reverse-input"}}).encode("utf-8"),
        headers={
            "Content-Type": "application/json", "x-goog-api-key": api_key,
            "X-Goog-Upload-Protocol": "resumable", "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(size),
            "X-Goog-Upload-Header-Content-Type": mime_type,
        }, method="POST",
    )
    with _gemini_open(start, deadline=deadline, heartbeat=heartbeat) as response:
        upload_url = response.headers.get("X-Goog-Upload-URL")
    if not upload_url or not str(upload_url).startswith(_GEMINI_API_BASE + "/"):
        raise RuntimeError("Gemini Files API did not return a trusted upload URL")
    with open(path, "rb") as source:
        payload = source.read(_GEMINI_MAX_MEDIA_BYTES + 1)
    if len(payload) != size:
        raise ValueError("Gemini reverse media changed during upload")
    finalize = urllib.request.Request(
        upload_url, data=payload,
        headers={"Content-Type": mime_type, "x-goog-api-key": api_key,
                 "X-Goog-Upload-Offset": "0", "X-Goog-Upload-Command": "upload, finalize"},
        method="POST",
    )
    with _gemini_open(finalize, deadline=deadline, heartbeat=heartbeat) as response:
        result = json.loads(response.read().decode("utf-8"))
    file_info = result.get("file") or result
    name = str(file_info.get("name") or "")
    uri = str(file_info.get("uri") or "")
    if not re.fullmatch(r"files/[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?", name) or not uri.startswith("https://"):
        raise RuntimeError("Gemini Files API returned an invalid file reference")
    return {"name": name, "uri": uri, "mime_type": mime_type}


def _gemini_wait_for_file_active(file_info, api_key, deadline=None, heartbeat=None):
    request = urllib.request.Request(
        _GEMINI_API_BASE + "/v1beta/" + file_info["name"],
        headers={"x-goog-api-key": api_key},
        method="GET",
    )
    for _attempt in range(30):
        with _gemini_open(
            request, deadline=deadline, heartbeat=heartbeat, retry_transient=True,
        ) as response:
            result = json.loads(response.read().decode("utf-8"))
        state = str(result.get("state") or "").upper()
        if state == "ACTIVE":
            active = dict(file_info)
            active["uri"] = str(result.get("uri") or active["uri"])
            return active
        if state in {"FAILED", "ERROR"}:
            raise RuntimeError("Gemini Files API could not process the media")
        _analysis_remaining(deadline)
        time.sleep(1)
    raise RuntimeError("Gemini Files API media processing did not complete")


def _gemini_delete_file(file_info, api_key, deadline=None, heartbeat=None):
    if not file_info:
        return
    request = urllib.request.Request(
        _GEMINI_API_BASE + "/v1beta/" + file_info["name"],
        headers={"x-goog-api-key": api_key}, method="DELETE",
    )
    try:
        with _gemini_open(request, deadline=deadline, heartbeat=heartbeat, retry_transient=False) as response:
            response.read()
    except Exception:
        print("[breakdown] Gemini temporary file cleanup failed", flush=True)


def _gemini_media_part(path, mime_type, duration, api_key, deadline=None, heartbeat=None,
                       inline_payload_bytes=None, title="", platform="", transcript="",
                       windows=None):
    size = os.path.getsize(path)
    if size <= 0 or size > _GEMINI_MAX_MEDIA_BYTES:
        raise ValueError("Gemini reverse media size is outside the allowed range")
    if inline_payload_bytes is None:
        inline_payload_bytes = _gemini_inline_payload_bytes(
            path, mime_type, title, duration, platform, transcript,
            windows=windows,
        )
    projected = int(inline_payload_bytes)
    if (size <= _GEMINI_INLINE_MAX_BYTES
            and float(duration or 0) <= _GEMINI_INLINE_MAX_DURATION
            and projected <= _GEMINI_INLINE_MAX_REQUEST_BYTES):
        with open(path, "rb") as source:
            payload = source.read(_GEMINI_INLINE_MAX_BYTES + 1)
        if len(payload) != size:
            raise ValueError("Gemini reverse media changed during read")
        return {"inline_data": {"mime_type": mime_type, "data": base64.b64encode(payload).decode("ascii")}}, None
    uploaded = _gemini_upload_file(path, mime_type, api_key, deadline=deadline, heartbeat=heartbeat)
    return {"file_data": {"mime_type": mime_type, "file_uri": uploaded["uri"]}}, uploaded


def _gemini_reverse_instruction(
    title, duration, platform, transcript, windows=None, retry_error="",
):
    windows = list(
        windows
        or _build_authoritative_reverse_timeline(duration)["windows"]
    )
    authoritative_segments = [
        {
            "segment_id": index,
            "start_seconds": start,
            "end_seconds": end,
            "display_range": label,
        }
        for index, (start, end, label) in enumerate(windows, 1)
    ]
    retry = ""
    if retry_error:
        retry = (
            "The previous response failed validation: %s. Re-analyze the original media; "
            "do not reuse the rejected draft. If unresolved slots are named, fill them only "
            "when visible evidence supports them; otherwise keep unknown and allow strict failure. "
            % str(retry_error)[:900]
        )
    output_contract = (
        'Return only one complete minified JSON object with exactly the root key "shots"; '
        'no markdown or wrapper; no indentation or line breaks. Keep every value concise and evidence-bound; '
        'do not repeat the same description in multiple fact values. '
        'Each shots item must have exactly segment_id, transition_from_previous, facts, '
        'and generation_advice. Never return start_seconds or end_seconds. '
        'facts must be an array with exactly one row for every key, in this order: %s. '
        'Each fact row must have exactly key, value, and evidence_seconds. evidence_seconds must be an array '
        'of 1-3 timestamps inside the current shot for an observed value, or [] for unknown/not_applicable. '
        'transition_from_previous must have exactly type, description, and evidence_seconds. '
        'For segment 1 use type none, description not_applicable, and []. For later segments classify only '
        'the server-provided boundary as hard_cut, fade, dissolve, wipe, occlusion, whip_pan, push_pull, '
        'or unknown; do not create or move a boundary. Use [] for unknown. '
        'generation_advice must have exactly '
        'aspect_ratio, fps, camera_control, and negative_prompt. '
    ) % ", ".join(_GEMINI_FACT_FIELDS)
    return (
        "Analyze the complete original video using exactly the authoritative server segments below. "
        "Return exactly one shots item for each segment_id, in ascending order. The server owns all "
        "timeline boundaries; never add, remove, merge, split, or move a segment. Facts and generation advice "
        "must remain separate. Every visible fact must cite evidence_seconds inside its shot. "
        "Return facts as exactly one row per required key; each row contains key, value, and "
        "evidence_seconds. Never repeat or omit a fact key. "
        "Use exactly 'unknown' when evidence is insufficient and 'not_applicable' only for wardrobe, "
        "sound, subtitles, or continuity. For those four optional facts use not_applicable when the "
        "feature is absent: wardrobe for a non-person/no visible clothing; sound when Verified ASR is "
        "(none) or has no text in this shot; subtitles when no text is visibly readable; continuity "
        "for the first shot or when no evidence-backed relation exists. Their evidence_seconds must "
        "then be []. Do not infer identity, brand, emotion, place, sound, text, "
        "or intent. Do not use unknown for an observable absence or static state: a visible non-person "
        "object or geometric shape is the subject; describe an unchanged subject at action start, "
        "process, and end as static with endpoint evidence; describe each depth layer using the "
        "shot-specific visible object, color, position, or empty image region. If a layer is absent, "
        "state its observable absence together with that scene-specific region instead of a generic "
        "'no distinct layer' template. "
        "Never invent a person, wardrobe, object, depth layer, or motion to replace an observed absence. "
        "Describe subject appearance and clothing; action start, process, end, direction "
        "and speed; foreground, midground and background; shot scale, angle, movement and composition; "
        "lighting, color, style, texture and rhythm. Quote only visible subtitles or verified ASR. "
        "Do not merge different shots, repeat template prose, or prioritize length over accuracy. "
        "Before returning, compare every shot pair: do not copy the same subject/action/scene/camera/"
        "lighting sentence across shots. If a scene returns later, describe only evidence-backed "
        "temporal differences; every non-sentinel value must contain shot-specific visible evidence, "
        "and never invent differences merely to avoid duplication. "
        "Write fact values and generation advice in Chinese, except the two exact sentinel values. %s"
        "Authoritative segments: %s. Title: %s. Platform: %s. Verified ASR: %s. %s"
    ) % (
        output_contract,
        json.dumps(
            authoritative_segments,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        str(title or "")[:200],
        str(platform or "")[:40],
        str(transcript or "(none)")[:3000],
        retry,
    )


def _gemini_request_body(
    media_part, title, duration, platform, transcript, windows=None,
    validation_error="",
):
    return {
        "systemInstruction": {
            "parts": [{"text": "You are an evidence-bound video prompt reverse director."}],
        },
        "contents": [{
            "role": "user",
            "parts": [
                media_part,
                {"text": _gemini_reverse_instruction(
                    title, duration, platform, transcript, windows,
                    validation_error,
                )},
            ],
        }],
        "generationConfig": {
            "temperature": 0.1,
            # Gemini 3.1 Pro defaults to high thinking. Reserve enough of the
            # model's 65,536-token output capacity for the complete JSON while
            # retaining balanced reasoning for evidence-bound video analysis.
            "maxOutputTokens": 32768,
            "thinkingConfig": {"thinkingLevel": "medium"},
            # The live endpoint rejects minItems/maxItems in this nested
            # schema. The provider schema omits only those two constraints;
            # the parser still enforces 1-4 shots and exactly 21 fact rows.
            "responseMimeType": "application/json",
            "responseJsonSchema": _gemini_reverse_provider_schema(),
        },
    }


def _gemini_inline_payload_bytes(
    path, mime_type, title, duration, platform, transcript, windows=None,
):
    size = os.path.getsize(path)
    encoded_size = 4 * ((size + 2) // 3)
    placeholder = {"inline_data": {"mime_type": mime_type, "data": ""}}
    body = _gemini_request_body(
        placeholder, title, duration, platform, transcript,
        windows=windows,
        validation_error="x" * 300,
    )
    return len(json.dumps(body, ensure_ascii=False).encode("utf-8")) + encoded_size


def _gemini_candidate_text(response):
    try:
        candidate = response["candidates"][0]
        text = candidate["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError) as error:
        raise RuntimeError("Gemini returned no structured candidate") from error
    if str(candidate.get("finishReason") or "").upper() == "MAX_TOKENS":
        raise ValueError("Gemini structured candidate was truncated at the output token limit")
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError("Gemini returned an empty structured candidate")
    if len(text.encode("utf-8")) > _GEMINI_MAX_RESPONSE_BYTES:
        raise ValueError("Gemini structured candidate exceeds the total output limit")
    return text.strip()


def _gemini_fact_text(value):
    return " ".join(str(value or "").replace("\r", " ").split()).strip()


def _soften_gemini_subjective_fact(value):
    """Rewrite safe visual prose and drop only unsupported subjective clauses."""
    text = _gemini_fact_text(value)
    corrections = []
    for marker, replacement in _REVERSE_SOFT_OBSERVABLE_REWRITES.items():
        if marker not in text:
            continue
        text = text.replace(marker, replacement)
        corrections.append({
            "marker": marker,
            "action": "rewritten_to_observable",
        })

    pieces = re.split(r"([,，;；。!！?？]|(?<!\d)\.(?!\d))", text)
    kept = []
    for offset in range(0, len(pieces), 2):
        clause = pieces[offset].strip()
        separator = pieces[offset + 1] if offset + 1 < len(pieces) else ""
        unsupported = next(
            (
                marker for marker in _REVERSE_SOFT_DROP_CLAUSE_MARKERS
                if marker in clause
            ),
            None,
        )
        if unsupported:
            corrections.append({
                "marker": unsupported,
                "action": "dropped_subjective_clause",
            })
            continue
        if clause:
            kept.append(clause + separator)
    normalized = _gemini_fact_text("".join(kept).strip(" ,，;；。.!！?？"))
    return normalized, corrections


def _gemini_evidence_to_local(times, start, end):
    midpoint = (float(start) + float(end)) / 2.0
    return sorted({1 if float(value) <= midpoint else 2 for value in (times or [])})


def _render_gemini_entry_text(entry):
    fields = entry.get("fields") or {}
    advice = entry.get("generation_advice") or {}
    parts = []
    transition = entry.get("transition_from_previous") or {}
    if transition.get("boundary_id") is not None:
        type_labels = {
            "hard_cut": "直接硬切",
            "fade": "淡入淡出",
            "dissolve": "叠化",
            "wipe": "划像",
            "occlusion": "遮挡转场",
            "whip_pan": "甩镜转场",
            "push_pull": "推拉转场",
            "unknown": "转场类型无法确认",
        }
        transition_text = "%d秒转场: %s" % (
            int(transition.get("display_at_second") or 0),
            type_labels.get(
                str(transition.get("type") or ""),
                str(transition.get("type") or ""),
            ),
        )
        description = str(transition.get("description") or "").strip()
        if description not in {"", _GEMINI_NOT_APPLICABLE}:
            transition_text += "; " + description
        duration = float(transition.get("duration_seconds") or 0.0)
        if duration > 0:
            transition_text += "; duration %.1fs" % duration
        parts.append(transition_text)
    parts.extend([
        "subject: " + str(fields.get("subject") or ""),
        "action: " + str(fields.get("action") or ""),
        "scene: " + str(fields.get("scene") or ""),
        "camera: " + str(fields.get("camera") or ""),
        "lighting/style: " + str(fields.get("lighting") or ""),
    ])
    if fields.get("sound"):
        parts.append("verified sound/ASR: " + str(fields["sound"]))
    if fields.get("subtitles"):
        parts.append("visible subtitles/text: " + str(fields["subtitles"]))
    parts.append(
        "generation advice: aspect ratio %s; fps %s; camera control %s; "
        "negative prompt %s"
        % (
            advice.get("aspect_ratio") or "",
            advice.get("fps") or "",
            advice.get("camera_control") or "",
            advice.get("negative_prompt") or "",
        )
    )
    return "; ".join(parts)


def _omit_unsupported_gemini_sound(entry, reason):
    fields = entry.get("fields") or {}
    sound = _gemini_fact_text(fields.get("sound"))
    if not sound:
        return False
    sound_was_ready = sound not in {
        _GEMINI_UNKNOWN,
        _GEMINI_NOT_APPLICABLE,
    }
    fields["sound"] = ""
    evidence = entry.get("evidence_seconds") or {}
    evidence["sound"] = []
    readiness = entry.get("readiness") or {}
    readiness["applicable"] = max(
        0, int(readiness.get("applicable") or 0) - 1,
    )
    if sound_was_ready:
        readiness["ready"] = max(
            0, int(readiness.get("ready") or 0) - 1,
        )
    omitted = entry.setdefault("omitted_unsupported_fields", [])
    notice = {"field": "sound", "reason": reason}
    if notice not in omitted:
        omitted.append(notice)
    summary = _gemini_validation_summary(entry)
    summary["omitted_unsupported_fields"] = list(omitted)
    entry["validation_summary"] = summary
    entry["text"] = _render_gemini_entry_text(entry)
    return True


def _bind_gemini_sound_evidence(prompt_result, script_text):
    entries = list((prompt_result or {}).get("entries") or [])
    windows = list((prompt_result or {}).get("windows") or [])
    for entry, window in zip(entries, windows):
        sound = str((entry.get("fields") or {}).get("sound") or "").strip()
        if not sound:
            continue
        transcript = _segment_transcript(script_text, window[0], window[1])
        if not transcript.strip():
            _omit_unsupported_gemini_sound(entry, "no_segment_asr")
        elif not _reverse_sound_matches_transcript(sound, transcript):
            _omit_unsupported_gemini_sound(entry, "segment_asr_mismatch")
    return prompt_result


def _gemini_expand_fact_rows(rows):
    if not isinstance(rows, list) or len(rows) != len(_GEMINI_FACT_FIELDS):
        raise ValueError("Gemini shot facts do not match schema")
    facts = {}
    evidence = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"key", "value", "evidence_seconds"}:
            raise ValueError("Gemini shot fact row does not match schema")
        key = row["key"]
        if key not in _GEMINI_FACT_FIELDS or key in facts:
            raise ValueError("Gemini shot fact keys must be unique and complete")
        facts[key] = row["value"]
        evidence[key] = row["evidence_seconds"]
    if set(facts) != set(_GEMINI_FACT_FIELDS):
        raise ValueError("Gemini shot fact keys must be unique and complete")
    return facts, evidence


def _gemini_validation_summary(entry):
    readiness = entry.get("readiness") or {}
    applicable = int(readiness.get("applicable") or 0)
    ready = int(readiness.get("ready") or 0)
    percent = round(100.0 * ready / applicable, 1) if applicable else 0.0
    return {
        "source_evidence_coverage": 100 if applicable else 0,
        "generation_readiness": percent,
        "factual_consistency": 100,
    }


def _validate_authoritative_reverse_windows(windows):
    values = list(windows or [])
    if not 1 <= len(values) <= _REVERSE_MAX_SEGMENTS:
        raise ValueError("Server reverse timeline must contain 1-4 segments")
    validated = []
    previous_end = 0.0
    for index, window in enumerate(values, 1):
        if not isinstance(window, (list, tuple)) or len(window) != 3:
            raise ValueError(
                "Server reverse segment %d has an invalid window" % index
            )
        start, end, label = float(window[0]), float(window[1]), str(window[2])
        if abs(start - previous_end) > 0.01 or end <= start:
            raise ValueError(
                "Server reverse segment %d is not gap-free "
                "(previous_end=%.1f, start=%.1f, end=%.1f)"
                % (index, previous_end, start, end)
            )
        validated.append((start, end, label))
        previous_end = end
    return validated


def _parse_gemini_reverse_result(raw, windows):
    try:
        result = json.loads(str(raw or ""))
    except Exception as error:
        raise ValueError("Gemini reverse output is not complete JSON") from error
    if not isinstance(result, dict) or set(result) != {"shots"}:
        raise ValueError("Gemini reverse output root does not match schema")
    shots = result.get("shots")
    # Numeric duration remains a direct-helper compatibility input only.
    # Production callers always pass the FFmpeg-owned authoritative windows.
    if isinstance(windows, (int, float)) and isinstance(shots, list) and shots:
        helper_duration = max(0.1, _round_tenth(windows))
        helper_boundaries = [
            _round_tenth(index * helper_duration / len(shots))
            for index in range(len(shots) + 1)
        ]
        helper_boundaries[-1] = helper_duration
        windows = [
            (
                helper_boundaries[index],
                helper_boundaries[index + 1],
                _reverse_display_range(
                    helper_boundaries[index],
                    helper_boundaries[index + 1],
                ),
            )
            for index in range(len(shots))
        ]
    windows = _validate_authoritative_reverse_windows(windows)
    if not isinstance(shots, list) or len(shots) != len(windows):
        raise ValueError(
            "Gemini reverse output segment count mismatch: expected %d "
            "segments with ids %s, received %d"
            % (
                len(windows),
                ",".join(str(index) for index in range(1, len(windows) + 1)),
                len(shots) if isinstance(shots, list) else 0,
            )
        )
    entries = []
    for index, (shot, window) in enumerate(zip(shots, windows), 1):
        compact_required = {
            "segment_id", "transition_from_previous", "facts",
            "generation_advice",
        }
        legacy_required = compact_required | {"evidence_seconds"}
        if (not isinstance(shot, dict)
                or (set(shot) != compact_required and set(shot) != legacy_required)):
            raise ValueError("Gemini shot %d does not match schema" % index)
        try:
            segment_id = int(shot["segment_id"])
        except (TypeError, ValueError):
            raise ValueError(
                "Gemini segment %d has an invalid segment_id; expected %d"
                % (index, index)
            )
        if segment_id != index:
            raise ValueError(
                "Gemini segment order mismatch at position %d: expected "
                "segment_id %d, received %d; valid server range is %s"
                % (index, index, segment_id, window[2])
            )
        start, end, timeline = window
        transition = shot["transition_from_previous"]
        if (
            not isinstance(transition, dict)
            or set(transition)
            != {"type", "description", "evidence_seconds"}
        ):
            raise ValueError(
                "Gemini segment %d transition_from_previous does not "
                "match schema" % index
            )
        transition_type = str(transition["type"] or "").strip().lower()
        transition_description = _gemini_fact_text(
            transition["description"]
        )
        transition_evidence = transition["evidence_seconds"]
        if transition_type not in _REVERSE_TRANSITION_TYPES:
            raise ValueError(
                "Gemini segment %d transition type is invalid" % index
            )
        if not isinstance(transition_evidence, list) or len(transition_evidence) > 3:
            raise ValueError(
                "Gemini segment %d transition evidence must contain at "
                "most 3 timestamps" % index
            )
        if index == 1:
            if (
                transition_type != "none"
                or transition_description != _GEMINI_NOT_APPLICABLE
                or transition_evidence
            ):
                raise ValueError(
                    "Gemini segment 1 transition must be none with "
                    "not_applicable and no evidence"
                )
        elif transition_type == "none":
            raise ValueError(
                "Gemini segment %d must classify server boundary %d at "
                "%d seconds" % (
                    index,
                    index - 1,
                    _round_whole_second(start),
                )
            )
        elif transition_type == "unknown":
            if transition_evidence:
                raise ValueError(
                    "Gemini segment %d unknown transition must not cite "
                    "evidence" % index
                )
        else:
            if not transition_description:
                raise ValueError(
                    "Gemini segment %d transition description is empty"
                    % index
                )
            if not transition_evidence:
                raise ValueError(
                    "Gemini segment %d transition lacks evidence near "
                    "server boundary %d at %d seconds"
                    % (
                        index,
                        index - 1,
                        _round_whole_second(start),
                    )
                )
            for point in transition_evidence:
                point = float(point)
                if point < start - 1.01 or point > start + 1.01:
                    raise ValueError(
                        "Gemini segment %d transition evidence %.1f is "
                        "outside boundary %d evidence window %.1f-%.1f"
                        % (
                            index,
                            point,
                            index - 1,
                            max(0.0, start - 1.0),
                            start + 1.0,
                        )
                    )
        if set(shot) == compact_required:
            facts, evidence = _gemini_expand_fact_rows(shot["facts"])
        else:
            facts, evidence = shot["facts"], shot["evidence_seconds"]
        advice = shot["generation_advice"]
        if not isinstance(facts, dict) or set(facts) != set(_GEMINI_FACT_FIELDS):
            raise ValueError("Gemini shot facts do not match schema")
        if not isinstance(evidence, dict) or set(evidence) != set(_GEMINI_FACT_FIELDS):
            raise ValueError("Gemini shot evidence does not match schema")
        if not isinstance(advice, dict) or set(advice) != {"aspect_ratio", "fps", "camera_control", "negative_prompt"}:
            raise ValueError("Gemini generation advice does not match schema")
        for key in ("aspect_ratio", "fps", "camera_control", "negative_prompt"):
            if not isinstance(advice[key], str):
                raise ValueError("Gemini generation advice %s must be a string" % key)
            advice[key] = _gemini_fact_text(advice[key])
        if not re.fullmatch(r"(?:1:1|3:4|4:3|9:16|16:9|21:9|source|original)", advice["aspect_ratio"]):
            raise ValueError("Gemini generation advice aspect_ratio is invalid")
        if not re.fullmatch(r"(?:24|25|30|50|60)(?:\s*fps)?", advice["fps"], re.IGNORECASE):
            raise ValueError("Gemini generation advice fps is invalid")
        if not advice["camera_control"]:
            raise ValueError("Gemini generation advice camera_control is invalid")
        if not advice["negative_prompt"]:
            raise ValueError("Gemini generation advice negative_prompt is invalid")
        applicable = ready = 0
        lightweight_corrections = []
        for key in _GEMINI_FACT_FIELDS:
            value = _gemini_fact_text(facts[key])
            if (
                key in _GEMINI_SOFT_CORRECTABLE_FACT_FIELDS
                and value not in {_GEMINI_UNKNOWN, _GEMINI_NOT_APPLICABLE}
            ):
                value, corrections = _soften_gemini_subjective_fact(value)
                for correction in corrections:
                    lightweight_corrections.append({
                        "field": key,
                        **correction,
                    })
                if not value:
                    value = _GEMINI_UNKNOWN
                    evidence[key] = []
            facts[key] = value
            if not value:
                raise ValueError("Gemini shot %d has an invalid %s value" % (index, key))
            if value == _GEMINI_NOT_APPLICABLE and key not in _GEMINI_OPTIONAL_FACT_FIELDS:
                raise ValueError("Gemini marked a required visual fact not_applicable")
            points = evidence[key]
            if not isinstance(points, list) or len(points) > 3:
                raise ValueError(
                    "Gemini shot %d %s evidence must contain at most 3 timestamps"
                    % (index, key)
                )
            for point in points:
                if float(point) < start - 0.11 or float(point) > end + 0.11:
                    raise ValueError("Gemini evidence time is outside its shot")
            if value != _GEMINI_NOT_APPLICABLE:
                applicable += 1
                if value != _GEMINI_UNKNOWN:
                    if not points:
                        raise ValueError("Gemini visible fact lacks evidence time")
                    ready += 1
        readiness_target = (
            _REVERSE_VISUAL_SEMANTIC_CONTRACT["components"]
            ["generation_readiness"]["target"] / 100.0
        )
        if applicable < 1 or ready / float(applicable) < readiness_target:
            unresolved = [
                key for key in _GEMINI_FACT_FIELDS
                if facts[key] == _GEMINI_UNKNOWN
            ]
            raise ValueError(
                "Gemini shot %d generation readiness is below %d percent "
                "(%d/%d ready; unresolved: %s)"
                % (
                    index,
                    int(round(readiness_target * 100)),
                    ready,
                    applicable,
                    ",".join(unresolved) or "none",
                )
            )
        critical_groups = {
            "subject": ("subject_identity", "subject_appearance", "position_scale"),
            "action": ("action_start", "action_end"),
            "scene": ("foreground", "midground", "background"),
            "camera": ("shot_scale", "camera_angle", "composition"),
        }
        for group, keys in critical_groups.items():
            if all(
                facts[key] in {_GEMINI_UNKNOWN, _GEMINI_NOT_APPLICABLE}
                for key in keys
            ):
                raise ValueError(
                    "Gemini critical %s facts are not ready" % group
                )
        action_start_frames = _gemini_evidence_to_local(evidence["action_start"], start, end)
        action_end_frames = _gemini_evidence_to_local(evidence["action_end"], start, end)
        if 1 not in action_start_frames or 2 not in action_end_frames:
            raise ValueError("Gemini action does not cite both shot endpoints")
        subject = "; ".join(facts[key] for key in ("subject_identity", "subject_appearance", "wardrobe", "position_scale")
                            if facts[key] != _GEMINI_NOT_APPLICABLE)
        action = "; ".join(facts[key] for key in ("action_start", "action_process", "action_end", "direction_speed"))
        scene = "; ".join(facts[key] for key in ("foreground", "midground", "background"))
        camera = "; ".join(facts[key] for key in ("shot_scale", "camera_angle", "camera_movement", "composition"))
        lighting = "; ".join(facts[key] for key in ("lighting_color", "style_texture", "rhythm"))
        sound = facts["sound"] if facts["sound"] != _GEMINI_NOT_APPLICABLE else ""
        subtitles = facts["subtitles"] if facts["subtitles"] != _GEMINI_NOT_APPLICABLE else ""
        local_evidence = {
            "subject": _gemini_evidence_to_local(evidence["subject_identity"] + evidence["subject_appearance"], start, end) or [1],
            "scene": _gemini_evidence_to_local(evidence["foreground"] + evidence["midground"] + evidence["background"], start, end) or [1],
            "action": sorted(set(action_start_frames + action_end_frames)),
            "camera": _gemini_evidence_to_local(evidence["shot_scale"] + evidence["camera_angle"] + evidence["composition"], start, end) or [1],
            "lighting": _gemini_evidence_to_local(evidence["lighting_color"], start, end) or [1],
        }
        transition_duration = 0.0
        if len(transition_evidence) >= 2:
            transition_duration = _round_tenth(
                max(float(value) for value in transition_evidence)
                - min(float(value) for value in transition_evidence)
            )
        transition_result = {
            "boundary_id": index - 1 if index > 1 else None,
            "at_seconds": start if index > 1 else None,
            "display_at_second": (
                _round_whole_second(start) if index > 1 else None
            ),
            "type": transition_type,
            "description": transition_description,
            "duration_seconds": transition_duration,
            "evidence_seconds": list(transition_evidence),
            "time_source": "server_ffmpeg",
            "type_source": "gemini",
        }
        entry = {
            "text": "",
            "fields": {"subject": subject, "scene": scene, "action": action, "camera": camera,
                       "lighting": lighting, "sound": sound, "subtitles": subtitles,
                       "continuity": ""},
            "evidence_frames": local_evidence, "continuity_evidence_frames": [],
            "evidence_seconds": evidence, "generation_advice": advice,
            "segment_id": segment_id,
            "transition_from_previous": transition_result,
            "cut_from_previous": (
                index > 1 and transition_type not in {"none", "unknown"}
            ),
            "readiness": {"applicable": applicable, "ready": ready},
            "structured_generation": True,
        }
        if lightweight_corrections:
            entry["lightweight_corrections"] = lightweight_corrections
        entry["validation_summary"] = _gemini_validation_summary(entry)
        entry["text"] = _render_gemini_entry_text(entry)
        entries.append(entry)
    return {"entries": entries, "windows": windows}


def _validate_gemini_reverse_entries(prompt_result, frames, script_text, frame_pts=None):
    _bind_gemini_sound_evidence(prompt_result, script_text)
    entries = prompt_result.get("entries") or []
    windows = prompt_result.get("windows") or []
    frame_groups = _reverse_model_frame_groups(
        frames, windows, frame_pts=frame_pts
    )
    accepted = []
    for index, (entry, window, frame_group) in enumerate(
        zip(entries, windows, frame_groups), 1
    ):
        if len(frame_group) == 1:
            entry["evidence_frames"] = {
                key: [1] if values else []
                for key, values in (
                    entry.get("evidence_frames") or {}
                ).items()
            }
        transcript = _segment_transcript(script_text, window[0], window[1])
        _validate_reverse_segment_evidence(
            entry, accepted, frame_group, index,
            transcript=transcript, require_frame_evidence=True,
            enforce_length_limit=False,
        )
        accepted.append(entry)
    if len(accepted) != len(entries) or len(entries) != len(windows):
        raise ValueError("Gemini reverse evidence grouping is incomplete")
    return prompt_result


def _gemini_quality_dimensions(prompt_result):
    entries = list((prompt_result or {}).get("entries") or [])
    applicable = sum(int((entry.get("readiness") or {}).get("applicable") or 0) for entry in entries)
    ready = sum(int((entry.get("readiness") or {}).get("ready") or 0) for entry in entries)
    readiness = round(100.0 * ready / applicable, 1) if applicable else 0.0
    cited = sum(
        1 for entry in entries for points in (entry.get("evidence_seconds") or {}).values()
        if points
    )
    return {
        "source_evidence_coverage": {"cited_fact_slots": cited, "validated": bool(entries)},
        "generation_readiness": {"ready": ready, "applicable": applicable, "percent": readiness},
        "factual_consistency": {"validated": bool(entries), "strict_failure_on_error": True},
        "end_to_end_similarity_claimed": False,
    }


def _gemini_validation_retry_error(error, parsed):
    message = str(error or "")
    match = re.search(r"第(\d+)段与第(\d+)段内容重复", message)
    windows = list((parsed or {}).get("windows") or [])
    if not match:
        return message
    current_index = int(match.group(1))
    previous_index = int(match.group(2))
    if not (
        1 <= current_index <= len(windows)
        and 1 <= previous_index <= len(windows)
    ):
        return message
    current = windows[current_index - 1]
    previous = windows[previous_index - 1]
    return (
        "%s. Rewatch the original video intervals for shot %d (%.1f-%.1fs) "
        "and shot %d (%.1f-%.1fs) independently. Do not copy either rejected "
        "shot text. Keep both server-owned segment IDs and intervals unchanged; "
        "never merge, delete, split, move, or renumber either shot. Describe at "
        "least one directly visible "
        "difference in subject, action, scene, camera, or lighting, with "
        "evidence_seconds inside the corresponding interval. Never invent a "
        "difference merely to pass validation. If no difference can be "
        "verified, return only evidence-bound values and accept strict "
        "validation failure rather than changing the authoritative timeline."
    ) % (
        message,
        previous_index, float(previous[0]), float(previous[1]),
        current_index, float(current[0]), float(current[1]),
    )


def _gemini_reverse_prompt_from_media(path, mime_type, title, duration, platform, transcript,
                                      deadline=None, heartbeat=None, timeline=None):
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not configured")
    timeline = timeline or _authoritative_reverse_timeline(path, duration)
    windows = _validate_authoritative_reverse_windows(
        timeline.get("windows") or [],
    )
    uploaded = None
    try:
        media_part, uploaded = _gemini_media_part(
            path, mime_type, duration, api_key,
            deadline=deadline, heartbeat=heartbeat,
            title=title, platform=platform, transcript=transcript,
            windows=windows,
        )
        if uploaded:
            uploaded = _gemini_wait_for_file_active(
                uploaded, api_key, deadline=deadline, heartbeat=heartbeat,
            )
            media_part = {"file_data": {"mime_type": mime_type, "file_uri": uploaded["uri"]}}
        validation_error = ""
        attempt_audit = []
        for attempt in range(2):
            body = _gemini_request_body(
                media_part, title, duration, platform, transcript, windows,
                validation_error,
            )
            started_at = time.monotonic()
            response = _gemini_json_request(
                _GEMINI_API_BASE + "/v1beta/models/" + _GEMINI_REVERSE_MODEL + ":generateContent",
                body, api_key, deadline=deadline, heartbeat=heartbeat)
            parsed = None
            raw_text = ""
            try:
                raw_text = _gemini_candidate_text(response)
                parsed = _parse_gemini_reverse_result(raw_text, windows)
                _bind_gemini_sound_evidence(parsed, transcript)
                # Run deterministic duplicate/length checks while the original
                # media is still available for the one allowed validation
                # retry. Do not salvage or rewrite provider content.
                _assemble_reverse_prompt(
                    parsed["entries"],
                    parsed["windows"],
                    enforce_length_limits=False,
                )
                attempt_audit.append({
                    "attempt": attempt + 1,
                    "http_status": 200,
                    "response_chars": len(raw_text),
                    "elapsed_ms": int(
                        round((time.monotonic() - started_at) * 1000)
                    ),
                    "validation": "passed",
                })
                parsed.update({
                    "provider": "google",
                    "model": _GEMINI_REVERSE_MODEL,
                    "attempts": attempt + 1,
                    "attempt_audit": attempt_audit,
                    "timeline_audit": json.loads(json.dumps(
                        timeline, ensure_ascii=False,
                    )),
                })
                return parsed
            except ValueError as error:
                validation_error = _gemini_validation_retry_error(
                    error, parsed,
                )
                attempt_audit.append({
                    "attempt": attempt + 1,
                    "http_status": 200,
                    "response_chars": len(raw_text),
                    "elapsed_ms": int(
                        round((time.monotonic() - started_at) * 1000)
                    ),
                    "validation": "failed",
                    "error": str(error)[:500],
                })
                print(
                    "[breakdown] gemini validation attempt=%d failed=%s"
                    % (attempt + 1, str(error)[:500])
                )
                if attempt:
                    raise
        raise ValueError("Gemini reverse validation failed")
    finally:
        # An exhausted analysis deadline must not suppress provider cleanup.
        # Cleanup is best-effort and never masks the primary exception.
        if uploaded:
            _gemini_delete_file(
                uploaded, api_key,
                deadline=time.monotonic() + 15,
                heartbeat=None,
            )


def _reverse_analysis_call_budget(segment_count):
    """Expose the bounded call cost under one shared 540-second deadline."""
    segment_count = max(1, min(4, int(segment_count or 1)))
    return {
        "analysis_deadline_seconds": BREAKDOWN_ANALYSIS_BUDGET,
        "max_images_per_request": 0,
        "max_video_inputs_per_request": 1,
        "global_model_calls": 0,
        "normal_logical_calls": 1,
        "worst_logical_calls": 2,
        "normal_physical_http_attempts": 1,
        "same_provider_physical_attempts_per_logical": 2,
        "worst_physical_http_attempts": 4,
        "provider": "google",
        "model": _GEMINI_REVERSE_MODEL,
        "http_4xx_retry": False,
        "cross_provider_fallback": False,
    }


def _reverse_response_hash(raw):
    return hashlib.sha256(
        str(raw or "").encode("utf-8", "replace")
    ).hexdigest()


def _reverse_validation_audit_summary(error):
    text = " ".join(str(error or "").replace("\r", "").split()).strip()
    text = re.sub(r"https?://\S+", "[redacted-url]", text)
    return text[:240] or "validation_failed"


def _reverse_prompt_from_frames(
    title, duration, platform, script_text, frames,
    return_details=False, deadline=None, heartbeat=None,
):
    windows = _reverse_segment_windows(duration)
    segment_count = len(windows)
    frame_groups = _reverse_model_frame_groups(frames, segment_count)
    entries = []
    strict_generation = bool(return_details)

    for index, ((start, end, timeline_range), frame_group) in enumerate(
        zip(windows, frame_groups), 1
    ):
        transcript = _segment_transcript(script_text, start, end)
        retry_error = None
        attempt_audit = []
        pair_ssim = (
            _reverse_frame_pair_ssim(frame_group[0], frame_group[-1])
            if strict_generation else None
        )
        for attempt in range(2):
            sysmsg, usermsg = _reverse_segment_messages(
                title,
                duration,
                platform,
                transcript,
                index,
                segment_count,
                timeline_range,
                retry=bool(attempt),
                retry_error=retry_error,
                pair_ssim=pair_ssim,
            )
            if deadline is None and heartbeat is None:
                raw = _reverse_chat_multimodal(
                    sysmsg,
                    usermsg,
                    frame_group,
                    temp=0.1,
                    max_tokens=2000 if strict_generation else 900,
                    image_detail=None,
                )
            else:
                raw = _reverse_chat_multimodal(
                    sysmsg,
                    usermsg,
                    frame_group,
                    temp=0.1,
                    max_tokens=2000 if strict_generation else 900,
                    image_detail=None,
                    deadline=deadline,
                    heartbeat=heartbeat,
                )
            try:
                entry = _parse_reverse_segment_evidence(raw)
                _validate_reverse_segment_evidence(
                    entry,
                    entries,
                    frame_group,
                    index,
                    transcript=transcript,
                    require_frame_evidence=True,
                    require_generation_readiness=strict_generation,
                    pair_ssim=pair_ssim,
                )
                attempt_audit.append({
                    "attempt": attempt + 1,
                    "response_sha256": _reverse_response_hash(raw),
                    "validation": "accepted",
                })
                entry["attempt_audit"] = attempt_audit
                break
            except ValueError as error:
                retry_error = error
                attempt_audit.append({
                    "attempt": attempt + 1,
                    "response_sha256": _reverse_response_hash(raw),
                    "validation": "rejected",
                    "summary": _reverse_validation_audit_summary(error),
                })
                _log_breakdown_parse_failure(
                    "reverse-segment-%d-attempt-%d" % (index, attempt + 1),
                    raw,
                    error,
                )
                if attempt:
                    raise
        entries.append(entry)

    prompt = _assemble_reverse_prompt(entries, windows)
    if return_details:
        return {"prompt": prompt, "entries": entries, "windows": windows}
    return prompt


def _clean_reverse_prompt(raw):
    text = str(raw or "").replace("\r", "").strip()
    if text.startswith("```"):
        lines = [line for line in text.splitlines() if not line.strip().startswith("```")]
        text = "\n".join(lines).strip()
    text = " ".join(line.strip() for line in text.splitlines() if line.strip()).strip().strip('"“”')
    if not text:
        raise ValueError("反推结果解析失败，请重试")
    return text


def _strip_json_code_fence(raw):
    text = str(raw or "").strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    first = lines[0].strip().lower()
    if first not in ("```", "```json"):
        return text
    lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _iter_json_objects(raw):
    """扫描文本中所有 JSON 对象。超长输入跳过扫描直接返回空（防 O(n²) 卡死）。"""
    text = str(raw or "")
    n = len(text)
    if n > 50000:   # 超长文本不逐字符扫描，交给外层 json.loads 直接试
        return
    for start in range(n):
        if text[start] != "{":
            continue
        depth = 0
        in_str = False
        escape = False
        for i in range(start, n):
            ch = text[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    yield text[start:i + 1]
                    break


def _parse_breakdown_json(raw):
    candidates = []
    seen = set()
    for candidate in (str(raw or "").strip(), _strip_json_code_fence(raw)):
        if candidate and candidate not in seen:
            candidates.append(candidate)
            seen.add(candidate)
    for candidate in list(candidates):
        for obj in _iter_json_objects(candidate):
            if obj not in seen:
                candidates.append(obj)
                seen.add(obj)
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except Exception:
            pass
    raise ValueError("拆解结果解析失败，请重试")


_SCENE_DETAIL_FACT_SPECS = {
    "subject": {
        "label": "主体",
        "text": ("identity", "appearance", "position_scale"),
        "enums": {},
    },
    "action": {
        "label": "动作",
        "text": ("start", "process", "end", "direction_speed"),
        "enums": {
            "motion": {
                "static", "gesture", "posture_change", "translation",
                "rotation", "interaction", "mixed",
            },
        },
    },
    "setting": {
        "label": "场景",
        "text": ("location", "foreground", "midground", "background"),
        "enums": {},
    },
    "camera": {
        "label": "镜头",
        "text": (),
        "enums": {
            "shot_size": {
                "extreme_closeup", "closeup", "medium", "full",
                "wide", "extreme_wide",
            },
            "angle": {"eye_level", "high", "low", "overhead", "dutch"},
            "composition": {
                "centered", "rule_of_thirds", "symmetrical",
                "leading_lines", "layered", "mixed",
            },
            "movement": {
                "static", "pan", "tilt", "dolly_in", "dolly_out",
                "tracking", "handheld", "orbit", "mixed",
            },
        },
    },
    "lighting": {
        "label": "光影",
        "text": ("source_direction",),
        "enums": {
            "quality": {"soft", "hard", "mixed"},
            "contrast": {"low", "medium", "high"},
            "color_tone": {"warm", "cool", "neutral", "mixed"},
        },
    },
}
_SCENE_DETAIL_UNKNOWN_EXACT = {"unknown", "none", "null", "n/a", "na"}
_SCENE_DETAIL_UNKNOWN_CN_FRAGMENTS = (
    "未知", "不确定", "无法确认", "未提供",
)
_SCENE_DETAIL_BLANK_PLACEHOLDER_RE = re.compile(
    r"(?:字段|栏目|内容|信息|细节|资料|描述|数值|值|项)"
    r"(?:均|都|全部)?(?:为|是|呈|等于)?空白$"
)
_SCENE_DETAIL_ENUM_LABELS = {
    "static": "固定/静止",
    "gesture": "肢体动作",
    "posture_change": "姿态变化",
    "translation": "位置移动",
    "rotation": "旋转",
    "interaction": "交互动作",
    "mixed": "混合",
    "extreme_closeup": "大特写",
    "closeup": "特写",
    "medium": "中景",
    "full": "全身景",
    "wide": "全景",
    "extreme_wide": "远景",
    "eye_level": "平视",
    "high": "高机位",
    "low": "低机位",
    "overhead": "俯拍",
    "dutch": "倾斜机位",
    "centered": "居中构图",
    "rule_of_thirds": "三分构图",
    "symmetrical": "对称构图",
    "leading_lines": "引导线构图",
    "layered": "层次构图",
    "pan": "横摇",
    "tilt": "俯仰摇镜",
    "dolly_in": "推进",
    "dolly_out": "拉远",
    "tracking": "跟随",
    "handheld": "手持",
    "orbit": "环绕",
    "soft": "柔光",
    "hard": "硬光",
    "low": "低反差",
    "high": "高反差",
    "warm": "暖色调",
    "cool": "冷色调",
    "neutral": "中性色调",
}


def _scene_detail_fact_contract_error(detail_facts, frame_count):
    if not isinstance(detail_facts, dict):
        return "缺少结构化事实槽位"
    try:
        frame_count = int(frame_count)
    except (TypeError, ValueError):
        return "缺少可核验的关键帧数量"
    if frame_count < 1:
        return "缺少可核验的关键帧数量"

    observed_count = 0
    for field, spec in _SCENE_DETAIL_FACT_SPECS.items():
        slot = detail_facts.get(field)
        if not isinstance(slot, dict):
            return "缺少%s结构化事实槽位" % spec["label"]
        status = str(slot.get("status") or "").strip().lower()
        if status not in {"observed", "unknown"}:
            return "%s状态必须是observed或unknown" % spec["label"]

        expected_values = {}
        for key in spec["text"]:
            expected_values[key] = str(slot.get(key) or "").strip()
        for key in spec["enums"]:
            expected_values[key] = str(slot.get(key) or "").strip().lower()
        evidence = slot.get("evidence_frames")
        if not isinstance(evidence, list):
            return "%s缺少证据帧数组" % spec["label"]

        if status == "unknown":
            if any(expected_values.values()) or evidence:
                return "%s为unknown时不得携带事实或证据" % spec["label"]
            continue

        observed_count += 1
        for key in spec["text"]:
            value = expected_values[key]
            if not value or len(value) > 160:
                return "%s的%s必须是1到160字的具体事实" % (spec["label"], key)
            compact_value = re.sub(r"\s+", "", value).lower()
            while (
                compact_value
                and unicodedata.category(compact_value[-1]).startswith("P")
            ):
                compact_value = compact_value[:-1]
            if not compact_value:
                return "%s的%s必须是1到160字的具体事实" % (
                    spec["label"], key,
                )
            if (
                compact_value in _SCENE_DETAIL_UNKNOWN_EXACT
                or any(
                    marker in compact_value
                    for marker in _SCENE_DETAIL_UNKNOWN_CN_FRAGMENTS
                )
                or compact_value == "空白"
                or _SCENE_DETAIL_BLANK_PLACEHOLDER_RE.search(compact_value)
            ):
                return "%s的%s不能使用unknown占位" % (spec["label"], key)
        for key, allowed in spec["enums"].items():
            if expected_values[key] not in allowed:
                return "%s的%s枚举无效" % (spec["label"], key)
        if not evidence:
            return "%s缺少原始帧证据" % spec["label"]
        if any(
            isinstance(index, bool)
            or not isinstance(index, int)
            or index < 1
            or index > frame_count
            for index in evidence
        ):
            return "%s证据帧超出1到%d范围" % (spec["label"], frame_count)

    if observed_count < 3:
        return "至少三个栏目需要结构化可观察事实和证据"
    return ""


def _scene_detail_enum_label(value, field=None):
    scoped = {
        ("angle", "low"): "低机位",
        ("angle", "high"): "高机位",
        ("contrast", "low"): "低反差",
        ("contrast", "medium"): "中等反差",
        ("contrast", "high"): "高反差",
    }
    if (field, str(value or "")) in scoped:
        return scoped[(field, str(value or ""))]
    return _SCENE_DETAIL_ENUM_LABELS.get(str(value or ""), str(value or ""))


def _compose_scene_detail(detail_facts):
    parts = []
    for field, spec in _SCENE_DETAIL_FACT_SPECS.items():
        slot = detail_facts[field]
        label = spec["label"]
        if slot["status"] == "unknown":
            parts.append("%s：无法确认" % label)
            continue
        if field == "subject":
            value = "，".join((
                slot["identity"], slot["appearance"], slot["position_scale"],
            ))
        elif field == "action":
            value = "起点%s，过程%s，结果%s，%s，动作类型%s" % (
                slot["start"], slot["process"], slot["end"],
                slot["direction_speed"],
                _scene_detail_enum_label(slot["motion"], "motion"),
            )
        elif field == "setting":
            value = "%s，前景%s，中景%s，背景%s" % (
                slot["location"], slot["foreground"],
                slot["midground"], slot["background"],
            )
        elif field == "camera":
            value = "%s，%s，%s，%s" % (
                _scene_detail_enum_label(slot["shot_size"], "shot_size"),
                _scene_detail_enum_label(slot["angle"], "angle"),
                _scene_detail_enum_label(slot["composition"], "composition"),
                _scene_detail_enum_label(slot["movement"], "movement"),
            )
        else:
            value = "%s，%s，%s，%s" % (
                slot["source_direction"],
                _scene_detail_enum_label(slot["quality"], "quality"),
                _scene_detail_enum_label(slot["contrast"], "contrast"),
                _scene_detail_enum_label(slot["color_tone"], "color_tone"),
            )
        parts.append("%s：%s" % (label, value))
    return "；".join(parts) + "。"


def _validate_scene_breakdown(result, require_detail=False, frame_count=None):
    if not isinstance(result, dict):
        raise ValueError("拆解结果为空，请重试")
    scenes = result.get("scenes")
    if not isinstance(scenes, list):
        raise ValueError("拆解结果为空，请重试")
    placeholders = ("画面描述", "具体画面", "口播台词", "对应口播")
    valid_scenes = []
    for scene in scenes:
        if not isinstance(scene, dict):
            continue
        scene_text = str(scene.get("scene") or "").strip()
        line_text = str(scene.get("line") or "").strip()
        if any(marker in scene_text or marker in line_text for marker in placeholders):
            raise ValueError("拆解结果包含模板占位内容，请重试")
        if require_detail:
            detail_error = _scene_detail_fact_contract_error(
                scene.get("detail_facts"), frame_count,
            )
            if detail_error:
                raise ValueError(
                    "拆解结果第%d段画面细节不足（%s），请重试"
                    % (len(valid_scenes) + 1, detail_error)
                )
            scene["scene"] = _compose_scene_detail(scene["detail_facts"])
        elif not scene_text:
            continue
        valid_scenes.append(scene)
    if not valid_scenes:
        raise ValueError("拆解结果为空，请重试")
    result["scenes"] = valid_scenes
    return result


def _heartbeat(job_id, phase):
    """刷新 updated_at 防止 reaper 误杀 + 写 _hb_phase 供前端展示（用前缀防与用户 payload 字段冲突）"""
    try:
        now = int(time.time())
        with closing(jdb()) as c:
            row = c.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row:
                p = json.loads(row["payload"] or "{}")
                p["_hb_phase"] = phase
                c.execute("UPDATE jobs SET payload=?, updated_at=? WHERE id=?",
                          (json.dumps(p, ensure_ascii=False), now, job_id))
                c.commit()
    except Exception:
        pass


def _speech_chars(transcript_text):
    """量转写里的实际口播字数（剥掉 [0s-3s] 时间轴标记），过短≈无人声"""
    import re as _re
    return len(_re.sub(r"\[[^\]]*\]", "", transcript_text or "").strip())


def _reverse_transcript_is_abnormal(transcript_text, duration):
    """Reject implausibly dense or highly repetitive ASR before visual analysis."""
    text = re.sub(r"\[[^\]]*\]", "", transcript_text or "")
    text = re.sub(r"\s+", "", text)
    if not text:
        return False
    try:
        duration = max(1.0, float(duration or 0))
    except (TypeError, ValueError):
        duration = 1.0
    if len(text) > max(120, int(duration * 12)):
        return True
    if len(text) < 80:
        return False
    shingles = [text[index:index + 8] for index in range(len(text) - 7)]
    return bool(shingles) and len(set(shingles)) / float(len(shingles)) < 0.35


def _format_transcript(segs):
    """兼容 whisper segment 列表和 SRT 字符串"""
    if not segs:
        return ""
    if isinstance(segs, str):
        return segs
    if isinstance(segs, list) and segs:
        if isinstance(segs[0], dict):
            lines = []
            for s in segs:
                start = s.get("start") or s.get("seek") or 0
                end = s.get("end") or 0
                text = s.get("text") or s.get("transcript") or ""
                if str(text).strip():
                    lines.append("[%ss-%ss] %s" % (start, end, str(text).strip()))
            return "\n".join(lines)
    return str(segs)


_SHOWINFO_PTS_PATTERN = re.compile(
    r"pts_time:(-?[0-9]+(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?)"
)


def _parse_showinfo_pts(stderr_text):
    """从 ffmpeg showinfo 的 stderr 逐帧解析真实输出 PTS（秒）。"""
    points = []
    for line in str(stderr_text or "").splitlines():
        if "showinfo" not in line:
            continue
        match = _SHOWINFO_PTS_PATTERN.search(line)
        if match:
            points.append(float(match.group(1)))
    return points


def _showinfo_pts_from_completed(completed):
    stderr = getattr(completed, "stderr", b"") or b""
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", "replace")
    return _parse_showinfo_pts(stderr)


def _extract_frames(video_path, count=6, duration=30, scale_width=512,
                    min_frames=None, uniform=False, return_pts=False):
    """ffmpeg 抽帧：场景检测 + 均匀采样兜底。返回 (outdir, [paths])；
    return_pts=True 时返回 (outdir, [paths], [pts_seconds])，PTS 取自
    ffmpeg showinfo 输出的真实帧时间戳，与帧路径一一绑定。"""
    count = max(2, min(count, 12))  # 限制 2-12 帧，防止异常参数
    scale_width = max(256, min(int(scale_width or 512), 2048))
    outdir = tempfile.mkdtemp()
    pts_seconds = []
    if not uniform:
        try:
            completed = subprocess.run(
                ["ffmpeg", "-y", "-hide_banner", "-loglevel", "info",
                 "-i", video_path,
                 "-vf", "select='gt(scene,0.15)',showinfo,scale=%d:-1" % scale_width,
                 "-vsync", "vfr", "-vframes", str(count),
                 "%s/frame_%%d.jpg" % outdir],
                check=True, timeout=60,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            pts_seconds = _showinfo_pts_from_completed(completed)
        except subprocess.CalledProcessError:
            pass  # 场景检测失败 → 退到均匀采样
    frames = sorted([os.path.join(outdir, f) for f in os.listdir(outdir)
                     if f.endswith(".jpg")],
                    key=lambda p: int(os.path.splitext(os.path.basename(p))[0].split("_")[-1]))
    fallback_threshold = (
        max(2, min(int(min_frames), count))
        if min_frames is not None else max(2, count // 2)
    )
    if len(frames) < fallback_threshold:
        shutil.rmtree(outdir)
        outdir = tempfile.mkdtemp()
        fps = max(float(count) / max(float(duration or 1), 1.0), 0.001)
        try:
            completed = subprocess.run(
                ["ffmpeg", "-y", "-hide_banner", "-loglevel", "info",
                 "-i", video_path,
                 "-vf", "fps=%.6f,showinfo,scale=%d:-1" % (fps, scale_width),
                 "-vframes", str(count),
                 "%s/frame_%%d.jpg" % outdir],
                check=True, timeout=60,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            pts_seconds = _showinfo_pts_from_completed(completed)
        except subprocess.CalledProcessError:
            pass  # 均匀采样也失败 → 返回已有帧（可能 0 张，GPT-4o 仍可纯文本分析）
        frames = sorted([os.path.join(outdir, f) for f in os.listdir(outdir)
                         if f.endswith(".jpg")],
                        key=lambda p: int(os.path.splitext(os.path.basename(p))[0].split("_")[-1]))
    if len(pts_seconds) != len(frames):
        # showinfo 解析失败时按 ffmpeg fps 滤镜的确定性输出时间戳兜底：
        # fps=count/duration 第 i 帧输出 PTS = i*duration/count（起点为 0）。
        pts_seconds = [
            index * float(duration or len(frames)) / max(len(frames), 1)
            for index in range(len(frames))
        ]
    if return_pts:
        return outdir, frames, pts_seconds
    return outdir, frames


def _split_extracted_frames(extracted):
    """兼容返回 (outdir, frames) 的旧测试桩与 (outdir, frames, pts) 新契约。"""
    if len(extracted) == 3:
        return extracted[0], list(extracted[1]), list(extracted[2])
    return extracted[0], list(extracted[1]), None


def _fill_reverse_window_frames(video_path, frame_dir, frames, frame_pts,
                                windows, scale_width=1024):
    """为没有任何采样帧落入的权威窗口在该窗口内补抽一帧。

    只用 ffmpeg -ss/-to 在空窗口内部取帧，并记录其真实 PTS 后按时间序插入
    帧序列；绝不把其他窗口的既有帧重映射进空窗口伪造证据。补抽失败时保留
    空窗口，由下游分组校验抛出“证据不足”错误。
    """
    ordered = list(frames or [])
    if frame_pts is None or not ordered or not windows:
        return ordered, frame_pts
    pts_seconds = [float(value) for value in frame_pts]
    if len(pts_seconds) != len(ordered):
        return ordered, frame_pts
    groups = _group_reverse_frame_indices(
        len(ordered), windows, frame_pts=pts_seconds
    )
    if not groups or all(groups):
        return ordered, pts_seconds
    scale_width = max(256, min(int(scale_width or 1024), 2048))
    directory = frame_dir or os.path.dirname(ordered[0]) or tempfile.mkdtemp()
    for window_index, group in enumerate(groups):
        if group:
            continue
        start = float(windows[window_index][0])
        end = float(windows[window_index][1])
        output = os.path.join(
            directory, "frame_window_%d.jpg" % (window_index + 1)
        )
        try:
            completed = subprocess.run(
                ["ffmpeg", "-y", "-hide_banner", "-loglevel", "info",
                 "-ss", "%.3f" % start, "-to", "%.3f" % end,
                 "-i", video_path,
                 "-vf", "scale=%d:-1,showinfo" % scale_width,
                 "-frames:v", "1", output],
                check=True, timeout=30,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
                OSError):
            continue
        if not os.path.isfile(output):
            continue
        showinfo = _showinfo_pts_from_completed(completed)
        # 输入级 -ss 会把时间戳重置为 0，真实源时间 = 窗口起点 + 帧 PTS。
        at_seconds = start + showinfo[0] if showinfo else (start + end) / 2.0
        if not (start <= at_seconds < end):
            at_seconds = (start + end) / 2.0
        position = 0
        while position < len(pts_seconds) and pts_seconds[position] <= at_seconds:
            position += 1
        ordered.insert(position, output)
        pts_seconds.insert(position, at_seconds)
    return ordered, pts_seconds


def _pair_reverse_frames(frame_dir, frames):
    """将 8 个时间点按先后顺序拼成 4 张左右双帧图。"""
    ordered = list(frames or [])
    if len(ordered) < 8:
        raise ValueError("反推高清帧不足 8 张")
    paired = []
    for index in range(4):
        left, right = ordered[index * 2:index * 2 + 2]
        output = os.path.join(frame_dir, "reverse_pair_%d.jpg" % (index + 1))
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-i", left, "-i", right,
             "-filter_complex", "hstack=inputs=2", "-q:v", "2", output],
            check=True, timeout=30,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        paired.append(output)
    return paired


def _analysis_remaining(deadline):
    if deadline is None:
        return None
    remaining = float(deadline) - time.monotonic()
    if remaining <= 1:
        raise TimeoutError("反推分析已超过总时间预算，请重试")
    return remaining


def _analysis_attempt_log(message, deadline=None, heartbeat=None):
    print("[breakdown] %s" % message, flush=True)
    _analysis_remaining(deadline)
    if heartbeat:
        heartbeat()


def _post_json_idempotent_compat(
    official_base, fallback_base, path, data, headers, *,
    log, max_attempts, timeout,
):
    """Use per-request timeout when the deployed egress ABI supports it."""
    kwargs = {"log": log, "max_attempts": max_attempts}
    try:
        parameters = inspect.signature(
            egress.post_json_idempotent
        ).parameters
    except (TypeError, ValueError):
        parameters = {"timeout": None}
    accepts_kwargs = any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
        if hasattr(parameter, "kind")
    )
    if "timeout" in parameters or accepts_kwargs:
        kwargs["timeout"] = timeout
    else:
        print(
            "[breakdown] egress timeout ABI unavailable; using runtime default",
            flush=True,
        )
    return egress.post_json_idempotent(
        official_base, fallback_base, path, data, headers, **kwargs
    )


def _post_zhipu(body, api_key, deadline=None, heartbeat=None):
    base = os.environ.get(
        "REVERSE_ZHIPU_BASE", "https://open.bigmodel.cn/api/paas/v4"
    ).rstrip("/")
    configured_timeout = int(
        os.environ.get("BREAKDOWN_ZHIPU_TIMEOUT", "210") or 210
    )
    remaining = _analysis_remaining(deadline)
    timeout = configured_timeout
    max_attempts = 2
    if remaining is not None:
        # Two physical attempts share the same absolute budget. A small margin
        # ensures the second timeout cannot run past the analysis deadline.
        max_attempts = 2 if remaining >= 3 else 1
        timeout = min(
            configured_timeout,
            max(1, int((remaining - 1) / float(max_attempts))),
        )
    if heartbeat:
        heartbeat()
    try:
        response = _post_json_idempotent_compat(
            base, base, "/chat/completions",
            json.dumps(body, ensure_ascii=False).encode(),
            {"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
            log=lambda message: _analysis_attempt_log(
                message, deadline=deadline, heartbeat=heartbeat
            ),
            max_attempts=max_attempts,
            timeout=timeout,
        )
        _analysis_remaining(deadline)
        return response
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", "replace")
        except Exception:
            detail = ""
        print(
            "[breakdown] zhipu http error: status=%s body=%s"
            % (getattr(exc, "code", "?"), detail[:500].replace("\n", " ")),
            flush=True,
        )
        try:
            exc.breakdown_response_detail = detail
        except Exception:
            pass
        raise


def _post_openai_fallback(body, deadline=None, heartbeat=None):
    from .image import OPENAI_OFFICIAL_BASE

    if deadline is None:
        return egress.post_json(
            OPENAI_OFFICIAL_BASE, OPENAI_BASE,
            "/v1/chat/completions", json.dumps(body, ensure_ascii=False).encode(),
            {"Authorization": "Bearer " + OPENAI_KEY, "Content-Type": "application/json"},
            log=lambda message: print("[breakdown] %s" % message, flush=True),
        )
    remaining = _analysis_remaining(deadline)
    if heartbeat:
        heartbeat()
    response = _post_json_idempotent_compat(
        OPENAI_OFFICIAL_BASE,
        OPENAI_BASE,
        "/v1/chat/completions",
        json.dumps(body, ensure_ascii=False).encode(),
        {"Authorization": "Bearer " + OPENAI_KEY, "Content-Type": "application/json"},
        log=lambda message: _analysis_attempt_log(
            message, deadline=deadline, heartbeat=heartbeat
        ),
        max_attempts=1,
        timeout=max(1, int(remaining - 1)),
    )
    _analysis_remaining(deadline)
    return response


def _chat_content(response):
    content = (response.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
    if not content:
        raise RuntimeError("multimodal provider returned empty content")
    return content


def _zhipu_rejected_request(exc):
    """HTTP 4xx 表示请求已被上游明确拒绝，没有可等待的生成结果。"""
    return (
        isinstance(exc, urllib.error.HTTPError)
        and 400 <= int(getattr(exc, "code", 0) or 0) < 500
    )


def _zhipu_image_limit_error(exc):
    if not _zhipu_rejected_request(exc):
        return False
    detail = str(getattr(exc, "breakdown_response_detail", "") or "")
    return bool(
        re.search(r'["\']?code["\']?\s*:\s*["\']?1210\b', detail)
        or ("1210" in detail and "图片数量" in detail)
    )


def _reverse_chat_multimodal(
    sysmsg, usermsg, image_paths, temp=0.1, max_tokens=900,
    image_detail=None, deadline=None, heartbeat=None,
):
    image_paths = list(image_paths or [])
    if len(image_paths) > 2:
        raise ValueError("反推单次模型请求最多只能携带2张图片")
    kwargs = {
        "temp": temp,
        "max_tokens": max_tokens,
        "image_detail": image_detail,
        "provider": "zhipu",
        "model": "glm-4v-plus",
        "allow_provider_fallback": False,
    }
    if deadline is not None or heartbeat is not None:
        kwargs.update({"deadline": deadline, "heartbeat": heartbeat})
    return _chat_multimodal(
        sysmsg,
        usermsg,
        image_paths,
        **kwargs
    )


def _chat_multimodal(sysmsg, usermsg, image_paths, temp=0.7,
                     max_tokens=None, image_detail="low", provider="zhipu",
                     deadline=None, heartbeat=None,
                     allow_provider_fallback=True, model=None):
    """智谱多模态优先，仅投递前失败时安全回退 GPT。"""

    content = [{"type": "text", "text": usermsg}]
    for path in image_paths:
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        image_url = {"url": "data:image/jpeg;base64," + b64}
        if image_detail is not None:
            image_url["detail"] = image_detail
        content.append({"type": "image_url", "image_url": image_url})

    use_openai = provider == "openai"
    body = {
        "model": model or os.environ.get(
            "BREAKDOWN_FALLBACK_MODEL" if use_openai else "BREAKDOWN_MODEL",
            "gpt-4o" if use_openai else "glm-4v-plus",
        ),
        "messages": [
            {"role": "system", "content": sysmsg},
            {"role": "user", "content": content}
        ],
        "temperature": temp,
    }
    if max_tokens is not None:
        body["max_tokens"] = int(max_tokens)

    if use_openai:
        if not OPENAI_KEY:
            raise RuntimeError("OPENAI_API_KEY is not configured")
        if deadline is None and heartbeat is None:
            response = _post_openai_fallback(body)
        else:
            response = _post_openai_fallback(
                body, deadline=deadline, heartbeat=heartbeat
            )
        result = _chat_content(response)
        print("[breakdown] openai format fallback success: %s" % body["model"], flush=True)
        return result
    if provider != "zhipu":
        raise ValueError("unsupported multimodal provider: " + str(provider))

    zhipu_key = os.environ.get("REVERSE_ZHIPU_KEY", "").strip()
    if not zhipu_key:
        raise RuntimeError("REVERSE_ZHIPU_KEY is not configured")

    try:
        if deadline is None and heartbeat is None:
            response = _post_zhipu(body, zhipu_key)
        else:
            response = _post_zhipu(
                body,
                zhipu_key,
                deadline=deadline,
                heartbeat=heartbeat,
            )
    except Exception as exc:
        if not allow_provider_fallback:
            if _zhipu_image_limit_error(exc):
                raise ValueError(
                    "智谱输入图片数量超过限制（错误码1210）；"
                    "反推单次请求最多只能携带2张图片"
                ) from exc
            print(
                "[breakdown] zhipu failure, reverse provider fallback disabled: %s"
                % type(exc).__name__,
                flush=True,
            )
            raise
        rejected = _zhipu_rejected_request(exc)
        if not rejected and not egress._pre_delivery_failure(exc):
            print(
                "[breakdown] zhipu ambiguous/delivered failure, no fallback: %s"
                % type(exc).__name__,
                flush=True,
            )
            raise
        print(
            "[breakdown] zhipu %s, fallback to openai: %s"
            % (
                "request rejected" if rejected else "pre-delivery failure",
                type(exc).__name__,
            ),
            flush=True,
        )
        fallback_body = dict(body)
        fallback_body["model"] = os.environ.get("BREAKDOWN_FALLBACK_MODEL", "gpt-4o")
        try:
            if deadline is None and heartbeat is None:
                response = _post_openai_fallback(fallback_body)
            else:
                response = _post_openai_fallback(
                    fallback_body,
                    deadline=deadline,
                    heartbeat=heartbeat,
                )
            return _chat_content(response)
        except Exception as fallback_exc:
            print(
                "[breakdown] openai fallback failure: %s"
                % type(fallback_exc).__name__,
                flush=True,
            )
            raise

    content = _chat_content(response)
    print("[breakdown] zhipu success: %s" % body["model"], flush=True)
    return content


HANDLERS = {"breakdown": gen_breakdown}
