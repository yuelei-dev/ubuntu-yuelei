# -*- coding: utf-8 -*-
"""Digital-human one-click planning and local final composition.

Paid image/video generation stays in the existing durable child jobs.  This
module only plans the child jobs and combines their completed, owned outputs.
"""
import hashlib
import hmac
import json
import base64
import os
import pathlib
import re
import sqlite3
import subprocess
import time

from .core import OUT_DIR, closing, jdb

PIPELINE = "digital_human_oneclick_compose"
PLAN_PATH = "/api/gen/digital-human-oneclick/plan"
CONSENT_PATH = "/api/gen/digital-human-oneclick/consent"
HEYGEN_PREFLIGHT_PATH = "/api/gen/digital-human-oneclick/heygen-preflight"
GESTURE_RECOVERY_PATH = "/api/gen/digital-human-oneclick/gesture-recovery"
MATERIAL_RECOVERY_PATH = "/api/gen/digital-human-oneclick/material-recovery"
VIDEO_RECOVERY_PATH = "/api/gen/digital-human-oneclick/video-recovery"
VIDEO_COUNT = 3
MATERIAL_COUNT = 6
MAX_SCRIPT_CHARS = 6000
CONSENT_VERSION = "digital-human-oneclick-v1"
CONSENT_PURPOSE = "digital_human_oneclick"
CONSENT_TTL_SECONDS = 30 * 24 * 60 * 60
CONSENT_DB = pathlib.Path(os.environ.get(
    "DIGITAL_HUMAN_ONECLICK_DB",
    str(pathlib.Path(__file__).resolve().parents[1] / "digital_human_oneclick.db"),
))
_HEX_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
_CONSENT_TOKEN_RE = re.compile(r"^dhc_[0-9a-f]{32}\.[0-9a-f]{64}$")
_CONSENT_METADATA_FIELDS = {
    "digital_human_pipeline", "digital_human_stage", "digital_human_run_id",
    "digital_human_plan_digest", "digital_human_consent_token",
    "digital_human_script", "digital_human_item_index",
}
_STAGE_KINDS = {
    "gesture": "image",
    "material": "image",
    "talking": "video",
    "compose": "script_to_video",
}


class DigitalHumanRequestError(ValueError):
    def __init__(self, message, code="invalid_request", status=400, invalid_job_ids=None):
        super().__init__(message)
        self.code = str(code)
        self.status = int(status)
        self.invalid_job_ids = [int(item) for item in (invalid_job_ids or [])]


def _ensure_consent_table(connection):
    connection.execute(
        """CREATE TABLE IF NOT EXISTS digital_human_consents(
            id TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            run_id TEXT NOT NULL,
            consent_version TEXT NOT NULL,
            purpose TEXT NOT NULL,
            plan_digest TEXT NOT NULL,
            photo_sha256 TEXT NOT NULL,
            voice_mode TEXT NOT NULL,
            voice_ref TEXT NOT NULL,
            voice_sha256 TEXT NOT NULL DEFAULT '',
            token_hash TEXT NOT NULL UNIQUE,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            last_used_at INTEGER NOT NULL,
            UNIQUE(username, run_id)
        )"""
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_digital_human_consents_owner "
        "ON digital_human_consents(username, created_at DESC)"
    )


def cdb():
    CONSENT_DB.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(CONSENT_DB), timeout=10)
    try:
        os.chmod(CONSENT_DB, 0o600)
    except OSError:
        pass
    connection.row_factory = sqlite3.Row
    return connection


def init_db(db_factory=None):
    db_factory = db_factory or cdb
    with closing(db_factory()) as connection:
        _ensure_consent_table(connection)
        connection.commit()


def _required_sha256(value, label):
    value = str(value or "").strip().lower()
    if not _HEX_SHA256_RE.fullmatch(value):
        raise DigitalHumanRequestError("%s校验值无效，请重新选择素材" % label)
    return value


def _consent_signature(record, signing_secret):
    secret = str(signing_secret or "").strip()
    if not secret:
        raise DigitalHumanRequestError(
            "授权存证服务尚未配置，请联系管理员",
            "consent_service_unavailable", 503,
        )
    canonical = "|".join(str(record[key]) for key in (
        "id", "username", "run_id", "consent_version", "purpose",
        "plan_digest", "photo_sha256", "voice_mode", "voice_ref",
        "voice_sha256", "created_at", "expires_at",
    ))
    return hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def _public_consent(record, token):
    return {
        "consent_id": record["id"],
        "consent_token": token,
        "run_id": record["run_id"],
        "consent_version": record["consent_version"],
        "purpose": record["purpose"],
        "created_at": int(record["created_at"]),
        "expires_at": int(record["expires_at"]),
    }


def create_consent(payload, username, signing_secret, now=None, db_factory=None):
    """Persist an auditable one-click consent without storing raw user media."""
    if not isinstance(payload, dict):
        raise DigitalHumanRequestError("请求体必须是 JSON 对象")
    allowed = {
        "confirmed", "consent_version", "purpose", "run_id", "plan_digest", "script",
        "photo_sha256", "voice_mode", "voice_ref", "voice_sha256",
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
    run_id = str(payload.get("run_id") or "").strip()
    if not _RUN_ID_RE.fullmatch(run_id):
        raise DigitalHumanRequestError("本次制作流程编号无效，请重新开始")
    authoritative_plan = plan(payload.get("script"))
    plan_digest = _required_sha256(payload.get("plan_digest"), "制作方案")
    if not hmac.compare_digest(plan_digest, authoritative_plan["plan_digest"]):
        raise DigitalHumanRequestError(
            "制作方案与服务端文案拆分结果不一致，请重新分析方案",
            "consent_plan_mismatch", 409,
        )
    photo_sha256 = _required_sha256(payload.get("photo_sha256"), "人物照片")
    voice_mode = str(payload.get("voice_mode") or "").strip().lower()
    if voice_mode not in {"existing", "clone"}:
        raise DigitalHumanRequestError("声音授权类型无效")
    voice_ref = str(payload.get("voice_ref") or "").strip()
    if not voice_ref or len(voice_ref) > 180:
        raise DigitalHumanRequestError("声音资产标识无效")
    voice_sha256 = str(payload.get("voice_sha256") or "").strip().lower()
    if voice_mode == "clone":
        voice_sha256 = _required_sha256(voice_sha256, "声音样本")
    elif voice_sha256:
        raise DigitalHumanRequestError("复用已有声音时不应上传样音校验值")
    username = str(username or "").strip()
    if not username:
        raise DigitalHumanRequestError("未登录或登录已过期", "unauthorized", 401)
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
    _consent_signature(candidate, signing_secret)
    db_factory = db_factory or cdb
    init_db(db_factory)
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
        token = candidate["id"] + "." + _consent_signature(candidate, signing_secret)
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
                    candidate["voice_sha256"], token_hash,
                    candidate["created_at"], candidate["expires_at"], now,
                ),
            )
            connection.commit()
    return _public_consent(candidate, token)


def consent_response(payload, username, signing_secret, db_factory=None):
    return {"ok": True, "consent": create_consent(
        payload, username, signing_secret, db_factory=db_factory,
    )}


def _decode_b64_bytes(value, label):
    import base64
    text = str(value or "").strip()
    if "," in text and text.lower().startswith("data:"):
        text = text.split(",", 1)[1]
    try:
        raw = base64.b64decode(text, validate=True)
    except Exception as exc:
        raise DigitalHumanRequestError("%s无法校验，请重新选择" % label) from exc
    if not raw:
        raise DigitalHumanRequestError("%s不能为空" % label)
    return raw


def _expected_cloned_voice(slot_id):
    return "vip_" + re.sub(r"[^a-zA-Z0-9_-]", "_", str(slot_id or ""))


def _load_consent(username, token, now=None, db_factory=None):
    token = str(token or "").strip()
    if not _CONSENT_TOKEN_RE.fullmatch(token):
        raise DigitalHumanRequestError(
            "缺少有效的照片与声音授权，请重新确认", "consent_required", 403,
        )
    now = int(time.time() if now is None else now)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    db_factory = db_factory or cdb
    init_db(db_factory)
    with closing(db_factory()) as connection:
        row = connection.execute(
            "SELECT * FROM digital_human_consents WHERE token_hash=? AND username=?",
            (token_hash, str(username or "").strip()),
        ).fetchone()
        if not row:
            raise DigitalHumanRequestError(
                "照片与声音授权不存在或不属于当前账号", "consent_invalid", 403,
            )
        record = dict(row)
        if int(record["expires_at"]) <= now:
            raise DigitalHumanRequestError(
                "本次授权已过期，请重新开始并授权", "consent_expired", 409,
            )
        connection.execute(
            "UPDATE digital_human_consents SET last_used_at=? WHERE id=?",
            (now, record["id"]),
        )
        connection.commit()
    return record


def _verify_common_binding(body, username):
    token = body.get("digital_human_consent_token")
    record = _load_consent(username, token)
    if str(body.get("digital_human_run_id") or "") != record["run_id"]:
        raise DigitalHumanRequestError("授权与本次制作流程不匹配", "consent_binding_mismatch", 403)
    digest = str(body.get("digital_human_plan_digest") or "").strip().lower()
    if digest != record["plan_digest"]:
        raise DigitalHumanRequestError("授权与制作方案不匹配", "consent_binding_mismatch", 403)
    cleaned = dict(body)
    cleaned.pop("digital_human_consent_token", None)
    cleaned["digital_human_consent_id"] = record["id"]
    return cleaned, record


def verify_child_submission_with_record(payload, username, kind):
    """Bind a paid child submission and return its authoritative consent."""
    if not isinstance(payload, dict):
        return payload, None
    pipeline = str(payload.get("digital_human_pipeline") or "").strip().lower()
    has_fields = any(str(key).startswith("digital_human_") for key in payload)
    if not pipeline:
        if has_fields:
            raise DigitalHumanRequestError("数字人一键生成授权字段不完整")
        return payload, None
    if pipeline != CONSENT_PURPOSE:
        raise DigitalHumanRequestError("数字人一键生成流程标识无效")
    stage = str(payload.get("digital_human_stage") or "").strip().lower()
    if _STAGE_KINDS.get(stage) != str(kind or ""):
        raise DigitalHumanRequestError("数字人一键生成步骤与任务类型不匹配")
    cleaned, record = _verify_common_binding(payload, username)
    authoritative_plan = plan(payload.get("digital_human_script"))
    if not hmac.compare_digest(
            authoritative_plan["plan_digest"], record["plan_digest"]):
        raise DigitalHumanRequestError(
            "子任务文案与本次授权方案不一致，请重新开始",
            "consent_plan_mismatch", 409,
        )
    raw_item_index = payload.get("digital_human_item_index")
    if isinstance(raw_item_index, bool):
        raw_item_index = None
    if stage == "gesture":
        try:
            segment_index = int(raw_item_index)
            if segment_index < 0:
                raise IndexError
            segment = authoritative_plan["segments"][segment_index]
        except (TypeError, ValueError, IndexError, KeyError):
            raise DigitalHumanRequestError(
                "手势照步骤编号无效", "consent_plan_mismatch", 409,
            )
        cleaned["prompt"] = segment["gesture_prompt"]
        cleaned["digital_human_item_index"] = segment_index
        references = payload.get("reference_images")
        if not isinstance(references, list) or len(references) != 1:
            raise DigitalHumanRequestError(
                "手势照必须且只能使用本次授权的一张人物照片",
                "consent_photo_mismatch", 403,
            )
        actual = hashlib.sha256(_decode_b64_bytes(references[0], "人物照片")).hexdigest()
        if not hmac.compare_digest(actual, record["photo_sha256"]):
            raise DigitalHumanRequestError(
                "人物照片与授权记录不一致，请重新开始并授权",
                "consent_photo_mismatch", 403,
            )
        # Banana accepts trusted references through `images`, while the
        # one-click consent contract deliberately receives `reference_images`
        # so the portrait can be verified before any paid request is created.
        cleaned.pop("reference_images", None)
        cleaned["images"] = [references[0]]
        # The one-click workflow owns its paid image route. Do not let an old
        # page or a forged child request switch providers after consent.
        cleaned["provider"] = "banana"
        cleaned["model"] = "nb2"
        cleaned["quality"] = "std"
    elif stage == "talking":
        try:
            segment_index = int(raw_item_index)
            if segment_index < 0:
                raise IndexError
            segment = authoritative_plan["segments"][segment_index]
        except (TypeError, ValueError, IndexError, KeyError):
            raise DigitalHumanRequestError(
                "口播步骤编号无效", "consent_plan_mismatch", 409,
            )
        profile = segment["speech_profile"]
        cleaned.update({
            "text": segment["text"], "speed": profile["speed"],
            "pitch": profile["pitch"], "volume": profile["volume"],
            "motion": profile["motion"], "delivery": profile["delivery"],
            "digital_human_item_index": segment_index,
        })
        actual_voice = str(payload.get("voice") or "").strip()
        expected_voice = (record["voice_ref"] if record["voice_mode"] == "existing"
                          else _expected_cloned_voice(record["voice_ref"]))
        if actual_voice != expected_voice:
            raise DigitalHumanRequestError(
                "口播声音与授权记录不一致，请重新开始并授权",
                "consent_voice_mismatch", 403,
            )
        gesture_job_id = payload.get("gesture_job_id")
        if isinstance(gesture_job_id, bool):
            gesture_job_id = 0
        try:
            gesture_job_id = int(gesture_job_id)
        except (TypeError, ValueError):
            gesture_job_id = 0
        if gesture_job_id <= 0:
            raise DigitalHumanRequestError(
                "口播缺少本次授权的手势照任务编号",
                "talking_gesture_binding_invalid", 409,
            )
        with closing(jdb()) as connection:
            gesture_job = connection.execute(
                "SELECT id,kind,status,payload,result FROM jobs "
                "WHERE id=? AND username=? AND COALESCE(deleted,0)=0",
                (gesture_job_id, str(username or "").strip()),
            ).fetchone()
        if (not gesture_job or gesture_job["kind"] != "image"
                or gesture_job["status"] != "done"):
            raise DigitalHumanRequestError(
                "口播手势照不存在、未完成或不属于当前账号",
                "talking_gesture_binding_invalid", 409,
            )
        _child_binding(
            gesture_job["payload"], gesture_job_id, "gesture", record,
            segment_index,
        )
        try:
            gesture_result = json.loads(gesture_job["result"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            gesture_result = {}
        relative_file = _result_file(gesture_result, "image")
        try:
            gesture_path = (OUT_DIR / relative_file).resolve()
            gesture_path.relative_to(OUT_DIR.resolve())
        except Exception:
            gesture_path = None
        if (not gesture_path or not gesture_path.is_file()
                or gesture_path.stat().st_size <= 0):
            raise DigitalHumanRequestError(
                "口播手势照文件已不可用，请重新生成手势照",
                "talking_gesture_binding_invalid", 409,
            )
        suffix = gesture_path.suffix.lower()
        mime = {
            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".webp": "image/webp",
        }.get(suffix)
        if not mime:
            raise DigitalHumanRequestError(
                "口播手势照格式不受支持",
                "talking_gesture_binding_invalid", 409,
            )
        cleaned["image_data"] = "data:%s;base64,%s" % (
            mime, base64.b64encode(gesture_path.read_bytes()).decode("ascii"),
        )
        cleaned.pop("gesture_job_id", None)
        # This is still before core creates/charges the video child job.  Load
        # the local subtitle stack now so a missing deployment dependency can
        # never waste a paid HeyGen submission.
        try:
            from . import video as video_domain
            video_domain.subtitle_runtime_preflight()
        except Exception as exc:
            raise DigitalHumanRequestError(
                str(exc)[:220],
                str(getattr(exc, "code", "subtitle_runtime_unavailable")),
                int(getattr(exc, "status", 503) or 503),
            ) from exc
    elif stage == "compose":
        if str(payload.get("plan_digest") or "").strip().lower() != record["plan_digest"]:
            raise DigitalHumanRequestError("成片方案与授权记录不一致", "consent_binding_mismatch", 403)
    elif stage == "material":
        try:
            material_index = int(raw_item_index)
            if material_index < 0:
                raise IndexError
            material = authoritative_plan["materials"][material_index]
        except (TypeError, ValueError, IndexError, KeyError):
            raise DigitalHumanRequestError(
                "主画面步骤编号无效", "consent_plan_mismatch", 409,
            )
        cleaned["prompt"] = material["prompt"]
        cleaned["digital_human_item_index"] = material_index
        # Only references expanded from the established one-click payload are
        # forwarded. A forged client-side Banana `images` field must not bypass
        # the existing upload/reference contract.
        references = cleaned.pop("reference_images", None)
        cleaned.pop("images", None)
        if references is not None:
            cleaned["images"] = references
        cleaned["provider"] = "banana"
        cleaned["model"] = "nb2"
        cleaned["quality"] = "std"
    cleaned.pop("digital_human_script", None)
    return cleaned, record


def verify_child_submission(payload, username, kind):
    """Bind every one-click paid child submission to server-side consent."""
    return verify_child_submission_with_record(payload, username, kind)[0]


def verify_clone_submission(payload, username):
    if not isinstance(payload, dict):
        raise DigitalHumanRequestError("请求体必须是 JSON 对象")
    pipeline = str(payload.get("digital_human_pipeline") or "").strip().lower()
    has_fields = any(key in payload for key in _CONSENT_METADATA_FIELDS)
    if not pipeline:
        if has_fields:
            raise DigitalHumanRequestError("数字人一键生成声音授权字段不完整")
        return payload
    if pipeline != CONSENT_PURPOSE:
        raise DigitalHumanRequestError("数字人一键生成流程标识无效")
    if str(payload.get("digital_human_stage") or "").strip().lower() != "voice_clone":
        raise DigitalHumanRequestError("声音复刻步骤标识无效")
    cleaned, record = _verify_common_binding(payload, username)
    if record["voice_mode"] != "clone":
        raise DigitalHumanRequestError("当前授权未允许重新复刻声音", "consent_voice_mismatch", 403)
    if str(payload.get("slot_id") or "").strip() != record["voice_ref"]:
        raise DigitalHumanRequestError("音色槽位与授权记录不一致", "consent_voice_mismatch", 403)
    actual = hashlib.sha256(_decode_b64_bytes(payload.get("audio"), "声音样本")).hexdigest()
    if not hmac.compare_digest(actual, record["voice_sha256"]):
        raise DigitalHumanRequestError("声音样本与授权记录不一致", "consent_voice_mismatch", 403)
    return cleaned


def _clean_script(value):
    text = re.sub(r"[ \t\r\f\v]+", " ", str(value or ""))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) < 12:
        raise DigitalHumanRequestError("口播文案太短，请至少输入 12 个字")
    if len(text) > MAX_SCRIPT_CHARS:
        raise DigitalHumanRequestError("口播文案最多支持 6000 个字")
    return text


def _sentences(text):
    chunks = [part.strip() for part in re.findall(r"[^。！？!?；;\n]+[。！？!?；;]?", text)]
    return [part for part in chunks if part]


def _split_three(text):
    chunks = _sentences(text)
    if len(chunks) < 3:
        size = max(1, (len(text) + 2) // 3)
        chunks = [text[i:i + size] for i in range(0, len(text), size)]
    if len(chunks) < 3:
        chunks.extend([""] * (3 - len(chunks)))
    total = sum(len(item) for item in chunks)
    cumulative = []
    cursor = 0
    for item in chunks:
        cursor += len(item)
        cumulative.append(cursor)
    cut_one = min(range(1, len(chunks) - 1), key=lambda value: abs(cumulative[value - 1] - total / 3.0))
    cut_two = min(range(cut_one + 1, len(chunks)), key=lambda value: abs(cumulative[value - 1] - total * 2.0 / 3.0))
    groups = (chunks[:cut_one], chunks[cut_one:cut_two], chunks[cut_two:])
    return ["".join(group).strip() for group in groups]


def _digest(plan):
    canonical = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def plan(script):
    copy = _clean_script(script)
    parts = _split_three(copy)
    gestures = (
        "人物保持与参考照片完全一致，竖屏半身口播照，右手自然抬起作开场讲解手势，左手放松；神态亲切有活力，眼神稳定直视镜头，嘴唇自然闭合",
        "人物保持与参考照片完全一致，竖屏半身口播照，双手在胸前自然展开作对比说明手势；神态专注放松，眼神稳定直视镜头，嘴唇自然闭合",
        "人物保持与参考照片完全一致，竖屏半身口播照，一手轻指前方作总结强调手势，另一手自然放松；神态自信亲切，眼神稳定直视镜头，嘴唇自然闭合",
    )
    segments = []
    materials = []
    for index, part in enumerate(parts):
        excerpt = re.sub(r"\s+", " ", part)[:220]
        segment = {
            "index": index,
            "text": part,
            "role": ("hook", "explain", "cta")[index],
            "speech_profile": (
                {"speed": 1.08, "pitch": 1, "volume": 2, "motion": "high", "delivery": "energetic_hook"},
                {"speed": 0.98, "pitch": 0, "volume": 1, "motion": "medium", "delivery": "clear_explain"},
                {"speed": 1.04, "pitch": 1, "volume": 2, "motion": "high", "delivery": "confident_cta"},
            )[index],
            "gesture_prompt": gestures[index] + "。服装、发型、眼镜、面部特征和背景风格一致，双手完整可见，真实摄影，不添加文字。",
        }
        segments.append(segment)
        materials.extend([
            {
                "index": index * 2,
                "segment_index": index,
                "source_policy": "customer_then_feishu_then_ai",
                "material_query": excerpt,
                "prompt": "为竖屏短视频制作真实电影感主画面，无人物口播框、无文字、无水印。画面准确表达：" + excerpt,
            },
            {
                "index": index * 2 + 1,
                "segment_index": index,
                "source_policy": "customer_then_feishu_then_ai",
                "material_query": excerpt,
                "prompt": "为竖屏短视频制作信息补充镜头，真实商业纪录片风格，无文字、无水印。内容紧扣：" + excerpt,
            },
        ])
    core = {"pipeline": PIPELINE, "copy": copy, "ratio": "9:16", "segments": segments, "materials": materials}
    return dict(core, plan_digest=_digest(core))


def plan_response(payload):
    if not isinstance(payload, dict):
        raise DigitalHumanRequestError("请求体必须是 JSON 对象")
    result = plan(payload.get("script") or payload.get("copy") or payload.get("text"))
    return {"ok": True, "plan": result}


def _result_file(result, kind):
    if not isinstance(result, dict):
        return ""
    keys = ("video_file", "file") if kind == "video" else ("file", "image_file")
    for key in keys:
        value = str(result.get(key) or "").strip().replace("\\", "/")
        if value:
            return value
    return ""


def _child_binding(payload_text, job_id, expected_stage, consent_record,
                   expected_index=None):
    try:
        payload = json.loads(payload_text or "")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DigitalHumanRequestError(
            "子任务 #%d 的授权记录损坏，请重新生成" % job_id,
            "child_consent_binding_invalid", 409,
        ) from exc
    if not isinstance(payload, dict):
        raise DigitalHumanRequestError(
            "子任务 #%d 缺少有效授权记录，请重新生成" % job_id,
            "child_consent_binding_invalid", 409,
        )
    expected = {
        "digital_human_pipeline": CONSENT_PURPOSE,
        "digital_human_stage": expected_stage,
        "digital_human_consent_id": consent_record["id"],
        "digital_human_run_id": consent_record["run_id"],
        "digital_human_plan_digest": consent_record["plan_digest"],
    }
    if any(str(payload.get(key) or "") != str(value) for key, value in expected.items()):
        raise DigitalHumanRequestError(
            "子任务 #%d 不属于本次授权制作流程，请重新生成" % job_id,
            "child_consent_binding_mismatch", 409,
        )
    if expected_index is not None:
        raw_index = payload.get("digital_human_item_index")
        if isinstance(raw_index, bool):
            raw_index = None
        try:
            actual_index = int(raw_index)
        except (TypeError, ValueError):
            actual_index = -1
        if actual_index != int(expected_index):
            raise DigitalHumanRequestError(
                "子任务 #%d 不属于本次方案的第 %d 个位置，请重新生成" %
                (job_id, int(expected_index) + 1),
                "child_consent_binding_mismatch", 409,
            )


def _recoverable_done_file(result_text, kind):
    try:
        result = json.loads(result_text or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    relative_file = _result_file(result, kind)
    if not relative_file:
        return False
    try:
        path = (OUT_DIR / relative_file).resolve()
        path.relative_to(OUT_DIR.resolve())
    except Exception:
        return False
    return path.is_file() and path.stat().st_size > 0


def _recovery_entries(payload, field, limit, label, error_code):
    """Parse sparse recovery entries without collapsing their plan indexes."""
    raw_entries = payload.get(field)
    if not isinstance(raw_entries, list) or not 1 <= len(raw_entries) <= limit:
        raise DigitalHumanRequestError(
            "需要 1 至 %d 个有效的%s任务" % (limit, label), error_code, 409,
        )
    entries = []
    seen_indexes = set()
    seen_job_ids = set()
    for raw in raw_entries:
        if not isinstance(raw, dict) or set(raw) != {"index", "job_id"}:
            raise DigitalHumanRequestError(
                "%s恢复项必须包含 index 和 job_id" % label,
                error_code, 409,
            )
        raw_index = raw.get("index")
        raw_job_id = raw.get("job_id")
        if (not isinstance(raw_index, int) or isinstance(raw_index, bool)
                or not isinstance(raw_job_id, int) or isinstance(raw_job_id, bool)):
            raise DigitalHumanRequestError(
                "%s恢复项格式无效" % label, error_code, 409,
            )
        index = raw_index
        job_id = raw_job_id
        if (index < 0 or index >= limit or job_id <= 0
                or index in seen_indexes or job_id in seen_job_ids):
            raise DigitalHumanRequestError(
                "%s恢复项的 index 和 job_id 必须有效且互不重复" % label,
                error_code, 409, [job_id] if job_id > 0 else [],
            )
        seen_indexes.add(index)
        seen_job_ids.add(job_id)
        entries.append({"index": index, "job_id": job_id})
    return entries


def validate_gesture_recovery(payload, username):
    """Authorize portrait-free resume only for this consent's recoverable jobs."""
    if not isinstance(payload, dict):
        raise DigitalHumanRequestError("请求体必须是 JSON 对象")
    allowed = set(_CONSENT_METADATA_FIELDS) | {"gesture_job_ids"}
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise DigitalHumanRequestError("手势照恢复提交包含不支持字段：" + ", ".join(unknown))
    if str(payload.get("digital_human_pipeline") or "").strip().lower() != CONSENT_PURPOSE:
        raise DigitalHumanRequestError("数字人一键生成流程标识无效")
    if str(payload.get("digital_human_stage") or "").strip().lower() != "gesture_recovery":
        raise DigitalHumanRequestError("手势照恢复步骤标识无效")
    _cleaned, consent_record = _verify_common_binding(payload, username)
    entries = _recovery_entries(
        payload, "gesture_job_ids", 3, "手势照", "gesture_recovery_invalid",
    )
    job_ids = [entry["job_id"] for entry in entries]
    placeholders = ",".join("?" for _ in job_ids)
    with closing(jdb()) as connection:
        rows = connection.execute(
            "SELECT id,kind,username,status,payload,result FROM jobs "
            "WHERE username=? AND COALESCE(deleted,0)=0 AND id IN (%s)" % placeholders,
            [str(username or "").strip()] + job_ids,
        ).fetchall()
    by_id = {int(row["id"]): row for row in rows}
    statuses = []
    for entry in entries:
        expected_index = entry["index"]
        job_id = entry["job_id"]
        row = by_id.get(job_id)
        if (not row or row["kind"] != "image"
                or str(row["status"] or "").strip().lower()
                not in {"pending", "running", "done"}):
            raise DigitalHumanRequestError(
                "手势照任务 #%d 不存在、类型不符或已不可恢复，请重新附加原人物照片" % job_id,
                "gesture_recovery_invalid", 409, [job_id],
            )
        try:
            _child_binding(
                row["payload"], job_id, "gesture", consent_record,
                expected_index,
            )
        except DigitalHumanRequestError as exc:
            raise DigitalHumanRequestError(
                "手势照任务 #%d 不属于本次照片授权，请重新附加原人物照片" % job_id,
                "gesture_recovery_invalid", 409, [job_id],
            ) from exc
        if (str(row["status"] or "").strip().lower() == "done"
                and not _recoverable_done_file(row["result"], "image")):
            raise DigitalHumanRequestError(
                "手势照任务 #%d 的图片文件已不可用，请重新附加原人物照片" % job_id,
                "gesture_recovery_invalid", 409, [job_id],
            )
        statuses.append({
            "index": expected_index, "job_id": job_id,
            "status": str(row["status"]).lower(),
        })
    return {"ok": True, "gesture_jobs": statuses}


def validate_material_recovery(payload, username):
    """Authorize upload-free resume only when all six material jobs are durable."""
    if not isinstance(payload, dict):
        raise DigitalHumanRequestError("请求体必须是 JSON 对象")
    allowed = set(_CONSENT_METADATA_FIELDS) | {"material_job_ids"}
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise DigitalHumanRequestError("主画面恢复提交包含不支持字段：" + ", ".join(unknown))
    if str(payload.get("digital_human_pipeline") or "").strip().lower() != CONSENT_PURPOSE:
        raise DigitalHumanRequestError("数字人一键生成流程标识无效")
    if str(payload.get("digital_human_stage") or "").strip().lower() != "material_recovery":
        raise DigitalHumanRequestError("主画面恢复步骤标识无效")
    _cleaned, consent_record = _verify_common_binding(payload, username)
    entries = _recovery_entries(
        payload, "material_job_ids", MATERIAL_COUNT,
        "主画面", "material_recovery_invalid",
    )
    job_ids = [entry["job_id"] for entry in entries]
    placeholders = ",".join("?" for _ in job_ids)
    with closing(jdb()) as connection:
        rows = connection.execute(
            "SELECT id,kind,username,status,payload,result FROM jobs "
            "WHERE username=? AND COALESCE(deleted,0)=0 AND id IN (%s)" % placeholders,
            [str(username or "").strip()] + job_ids,
        ).fetchall()
    by_id = {int(row["id"]): row for row in rows}
    statuses = []
    for entry in entries:
        expected_index = entry["index"]
        job_id = entry["job_id"]
        row = by_id.get(job_id)
        status = str(row["status"] or "").strip().lower() if row else ""
        if not row or row["kind"] != "image" or status not in {"pending", "running", "done"}:
            raise DigitalHumanRequestError(
                "主画面任务 #%d 不存在、类型不符或已不可恢复；原客户素材已失效，请放弃旧任务并重新设置" % job_id,
                "material_recovery_invalid", 409, [job_id],
            )
        try:
            _child_binding(
                row["payload"], job_id, "material", consent_record,
                expected_index,
            )
        except DigitalHumanRequestError as exc:
            raise DigitalHumanRequestError(
                "主画面任务 #%d 不属于本次授权制作；原客户素材已失效，请放弃旧任务并重新设置" % job_id,
                "material_recovery_invalid", 409, [job_id],
            ) from exc
        if status == "done" and not _recoverable_done_file(row["result"], "image"):
            raise DigitalHumanRequestError(
                "主画面任务 #%d 的图片文件已不可用；原客户素材已失效，请放弃旧任务并重新设置" % job_id,
                "material_recovery_invalid", 409, [job_id],
            )
        statuses.append({
            "index": expected_index, "job_id": job_id, "status": status,
        })
    return {"ok": True, "material_jobs": statuses}


def validate_video_recovery(payload, username):
    """Authorize reuse of the three talking jobs before local composition."""
    if not isinstance(payload, dict):
        raise DigitalHumanRequestError("请求体必须是 JSON 对象")
    allowed = set(_CONSENT_METADATA_FIELDS) | {"video_job_ids"}
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise DigitalHumanRequestError("口播恢复提交包含不支持字段：" + ", ".join(unknown))
    if str(payload.get("digital_human_pipeline") or "").strip().lower() != CONSENT_PURPOSE:
        raise DigitalHumanRequestError("数字人一键生成流程标识无效")
    if str(payload.get("digital_human_stage") or "").strip().lower() != "video_recovery":
        raise DigitalHumanRequestError("口播恢复步骤标识无效")
    _cleaned, consent_record = _verify_common_binding(payload, username)
    entries = _recovery_entries(
        payload, "video_job_ids", VIDEO_COUNT,
        "口播", "video_recovery_invalid",
    )
    job_ids = [entry["job_id"] for entry in entries]
    placeholders = ",".join("?" for _ in job_ids)
    with closing(jdb()) as connection:
        rows = connection.execute(
            "SELECT id,kind,username,status,payload,result FROM jobs "
            "WHERE username=? AND COALESCE(deleted,0)=0 AND id IN (%s)" % placeholders,
            [str(username or "").strip()] + job_ids,
        ).fetchall()
    by_id = {int(row["id"]): row for row in rows}
    statuses = []
    for entry in entries:
        expected_index = entry["index"]
        job_id = entry["job_id"]
        row = by_id.get(job_id)
        status = str(row["status"] or "").strip().lower() if row else ""
        if not row or row["kind"] != "video" or status not in {"pending", "running", "done"}:
            raise DigitalHumanRequestError(
                "口播任务 #%d 不存在、类型不符或已不可恢复，请重新生成口播" % job_id,
                "video_recovery_invalid", 409, [job_id],
            )
        try:
            _child_binding(
                row["payload"], job_id, "talking", consent_record,
                expected_index,
            )
        except DigitalHumanRequestError as exc:
            raise DigitalHumanRequestError(
                "口播任务 #%d 不属于本次授权制作，请重新生成口播" % job_id,
                "video_recovery_invalid", 409, [job_id],
            ) from exc
        if status == "done" and not _recoverable_done_file(row["result"], "video"):
            raise DigitalHumanRequestError(
                "口播任务 #%d 的视频文件已不可用，请重新生成口播" % job_id,
                "video_recovery_invalid", 409, [job_id],
            )
        statuses.append({
            "index": expected_index, "job_id": job_id, "status": status,
        })
    return {"ok": True, "video_jobs": statuses}


def _owned_completed_files(username, ids, kind, expected_stage, consent_record):
    if not isinstance(ids, list) or any(isinstance(item, bool) for item in ids):
        raise DigitalHumanRequestError("子任务编号格式无效")
    try:
        normalized = [int(item) for item in ids]
    except (TypeError, ValueError):
        raise DigitalHumanRequestError("子任务编号格式无效")
    expected = VIDEO_COUNT if kind == "video" else MATERIAL_COUNT
    if len(normalized) != expected or len(set(normalized)) != expected:
        raise DigitalHumanRequestError("需要 %d 个互不重复的%s任务" % (expected, "口播视频" if kind == "video" else "主画面"))
    placeholders = ",".join("?" for _ in normalized)
    with closing(jdb()) as connection:
        rows = connection.execute(
            "SELECT id,kind,status,payload,result FROM jobs WHERE username=? "
            "AND COALESCE(deleted,0)=0 AND id IN (%s)" % placeholders,
            [username] + normalized,
        ).fetchall()
    by_id = {int(row["id"]): row for row in rows}
    files = []
    for expected_index, job_id in enumerate(normalized):
        row = by_id.get(job_id)
        if not row or row["kind"] != kind or row["status"] != "done":
            raise DigitalHumanRequestError("子任务 #%d 不存在、未完成或不属于当前账号" % job_id, "child_job_unavailable", 409)
        _child_binding(
            row["payload"], job_id, expected_stage, consent_record,
            expected_index,
        )
        try:
            result = json.loads(row["result"] or "{}")
        except Exception:
            result = {}
        rel = _result_file(result, kind)
        try:
            path = (OUT_DIR / rel).resolve()
            path.relative_to(OUT_DIR.resolve())
        except Exception:
            path = None
        if not path or not path.is_file() or path.stat().st_size <= 0:
            raise DigitalHumanRequestError("子任务 #%d 的本地成片已不可用" % job_id, "child_file_unavailable", 409)
        files.append(path.relative_to(OUT_DIR.resolve()).as_posix())
    return normalized, files


def prepare_compose_payload(payload, username, consent_record=None):
    if not isinstance(payload, dict):
        raise DigitalHumanRequestError("请求体必须是 JSON 对象")
    allowed = {
        "pipeline", "mode", "script", "copy", "text", "plan_digest",
        "video_job_ids", "material_job_ids", "digital_human_pipeline",
        "digital_human_stage", "digital_human_run_id",
        "digital_human_plan_digest", "digital_human_consent_id",
        "digital_human_script", "digital_human_item_index",
    }
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise DigitalHumanRequestError("提交包含不支持字段：" + ", ".join(unknown))
    if str(payload.get("pipeline") or "").strip().lower() != PIPELINE:
        raise DigitalHumanRequestError("pipeline 无效")
    frozen = plan(payload.get("script") or payload.get("copy") or payload.get("text"))
    if str(payload.get("plan_digest") or "").strip().lower() != frozen["plan_digest"]:
        raise DigitalHumanRequestError("制作方案已变化，请重新开始生成", "plan_digest_mismatch", 409)
    if not isinstance(consent_record, dict):
        raise DigitalHumanRequestError(
            "缺少服务端已验证的授权记录，请重新确认授权",
            "consent_required", 403,
        )
    authoritative = {
        "digital_human_pipeline": CONSENT_PURPOSE,
        "digital_human_stage": "compose",
        "digital_human_consent_id": str(consent_record.get("id") or ""),
        "digital_human_run_id": str(consent_record.get("run_id") or ""),
        "digital_human_plan_digest": str(consent_record.get("plan_digest") or "").lower(),
    }
    if (str(consent_record.get("username") or "") != str(username or "")
            or str(consent_record.get("purpose") or "") != CONSENT_PURPOSE
            or not authoritative["digital_human_consent_id"]
            or authoritative["digital_human_plan_digest"] != frozen["plan_digest"]
            or any(str(payload.get(key) or "") != value
                   for key, value in authoritative.items())):
        raise DigitalHumanRequestError(
            "成片授权与本次制作流程不匹配，请重新确认授权",
            "consent_binding_mismatch", 403,
        )
    video_ids, video_files = _owned_completed_files(
        username, payload.get("video_job_ids"), "video", "talking", consent_record,
    )
    material_ids, material_files = _owned_completed_files(
        username, payload.get("material_job_ids"), "image", "material", consent_record,
    )
    prepared = {
        "pipeline": PIPELINE,
        "mode": PIPELINE,
        "copy": frozen["copy"],
        "ratio": "9:16",
        "plan_digest": frozen["plan_digest"],
        "segments": frozen["segments"],
        "materials": frozen["materials"],
        "video_job_ids": video_ids,
        "material_job_ids": material_ids,
        "video_files": video_files,
        "material_files": material_files,
        "material_generate_count": 0,
    }
    prepared.update(authoritative)
    return prepared


def _run(command, timeout=1200):
    try:
        subprocess.run(command, check=True, timeout=timeout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode("utf-8", "replace")[-800:]
        raise RuntimeError("本地视频合成失败：" + detail) from exc


def _verify_final_video(video_domain, rel, expected_duration):
    path = video_domain._resolve_out_file(rel)
    if not path:
        raise RuntimeError("最终成片文件不存在")
    width, height = video_domain._probe_video_size(path)
    duration = video_domain._probe_video_duration(rel)
    if (width, height) != (1080, 1920) or duration <= 0:
        raise RuntimeError("最终成片分辨率或时长校验未通过")
    if abs(duration - float(expected_duration)) > max(0.75, float(expected_duration) * 0.03):
        raise RuntimeError("最终成片音画时长不一致")
    audio = subprocess.run([
        "ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
        "stream=codec_type", "-of", "default=nw=1:nk=1", str(path),
    ], check=False, timeout=60, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if audio.returncode != 0 or b"audio" not in (audio.stdout or b""):
        raise RuntimeError("最终成片缺少音轨")
    black = subprocess.run([
        "ffmpeg", "-hide_banner", "-i", str(path), "-vf",
        "blackdetect=d=0.5:pix_th=0.02", "-an", "-f", "null", "-",
    ], check=False, timeout=300, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    black_text = (black.stderr or b"").decode("utf-8", "replace")
    black_durations = [float(value) for value in re.findall(r"black_duration:([0-9.]+)", black_text)]
    if black_durations and max(black_durations) >= max(1.0, duration * 0.9):
        raise RuntimeError("最终成片检测到持续黑帧")
    return path, width, height, duration


def compose(payload, persist_state=None):
    from . import video as video_domain

    job_id = int(payload.get("_job_id") or 0)
    if not job_id:
        raise RuntimeError("数字人一键生成缺少任务编号")
    videos = [(OUT_DIR / rel).resolve() for rel in payload.get("video_files") or []]
    images = [(OUT_DIR / rel).resolve() for rel in payload.get("material_files") or []]
    if len(videos) != VIDEO_COUNT or len(images) != MATERIAL_COUNT:
        raise RuntimeError("数字人一键生成子任务数量不完整")
    for path in videos + images:
        path.relative_to(OUT_DIR.resolve())
        if not path.is_file() or path.stat().st_size <= 0:
            raise RuntimeError("数字人一键生成子任务文件不可用")
    if persist_state:
        persist_state("composing")

    out_dir = video_domain.VIDEO_OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    normalized = []
    for index, source in enumerate(videos):
        target = out_dir / ("digital_human_%d_part_%d.mp4" % (job_id, index + 1))
        _run([
            "ffmpeg", "-y", "-i", str(source), "-vf",
            "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1,fps=30",
            "-c:v", "libx264", "-preset", "fast", "-crf", "21", "-c:a", "aac", "-ar", "48000",
            "-ac", "2", "-movflags", "+faststart", str(target),
        ])
        normalized.append(target)
    joined = out_dir / ("digital_human_%d_joined.mp4" % job_id)
    concat_file = out_dir / ("digital_human_%d_concat.txt" % job_id)
    concat_file.write_text("".join("file '%s'\n" % str(path).replace("'", "'\\''") for path in normalized), encoding="utf-8")
    _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file), "-c", "copy", str(joined)])
    duration = video_domain._probe_video_duration(joined.resolve().relative_to(OUT_DIR.resolve()).as_posix())
    if duration <= 0:
        raise RuntimeError("口播子片段合并后时长无效")

    background = out_dir / ("digital_human_%d_background.mp4" % job_id)
    scene_duration = duration / float(MATERIAL_COUNT)
    command = ["ffmpeg", "-y"]
    for image in images:
        command.extend(["-loop", "1", "-t", "%.3f" % scene_duration, "-i", str(image)])
    filters = []
    labels = []
    for index in range(MATERIAL_COUNT):
        label = "bg%d" % index
        labels.append("[%s]" % label)
        filters.append(
            "[%d:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
            "zoompan=z='min(zoom+0.00035,1.055)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
            "d=1:s=1080x1920:fps=30,trim=duration=%.3f,setpts=PTS-STARTPTS[%s]" % (index, scene_duration, label)
        )
    filters.append("%sconcat=n=%d:v=1:a=0[joinedbg]" % ("".join(labels), MATERIAL_COUNT))
    filters.append(
        "[joinedbg]drawbox=x=44:y=44:w=356:h=58:color=black@0.68:t=fill,"
        "drawtext=text='CONCEPT / AI FILL':fontcolor=white:fontsize=26:x=62:y=60[outv]"
    )
    command.extend(["-filter_complex", ";".join(filters), "-map", "[outv]", "-t", "%.3f" % duration,
                    "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p", str(background)])
    _run(command)

    composed = out_dir / ("digital_human_%d_composed.mp4" % job_id)
    pip_filter = (
        "color=c=#B86B2B:s=456x456:r=30,format=rgba,"
        "geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='if(lte((X-228)*(X-228)+(Y-228)*(Y-228),50176),255,0)'[ring];"
        "[1:v]scale=440:440:force_original_aspect_ratio=decrease,"
        "pad=440:440:(ow-iw)/2:(oh-ih)/2:color=black@0,format=rgba,"
        "geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='if(lte((X-220)*(X-220)+(Y-220)*(Y-220),47524),255,0)'[pip];"
        "[0:v][ring]overlay=40:1112:format=auto[framed];"
        "[framed][pip]overlay=48:1120:format=auto,format=yuv420p[outv]"
    )
    _run([
        "ffmpeg", "-y", "-i", str(background), "-i", str(joined), "-filter_complex", pip_filter,
        "-map", "[outv]", "-map", "1:a:0", "-t", "%.3f" % duration, "-c:v", "libx264",
        "-preset", "fast", "-crf", "20", "-c:a", "aac", "-b:a", "192k", "-shortest",
        "-movflags", "+faststart", str(composed),
    ])
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
        # HeyGen has already been paid and the local composition is complete.
        # Preserve and deliver that video; a local subtitle retry must never
        # create another provider task or discard the paid output.
        final_rel = rel
        subtitle_error = str(exc)[:220] or "字幕处理失败"
    final_path, width, height, final_duration = _verify_final_video(
        video_domain, final_rel, duration,
    )
    result = {
        "pipeline": PIPELINE,
        "video_file": final_rel,
        "url": "/api/gen/file/" + final_rel,
        "duration": round(final_duration, 3),
        "width": width,
        "height": height,
        "segments": payload.get("segments") or [],
        "child_jobs": {"videos": payload.get("video_job_ids"), "materials": payload.get("material_job_ids")},
        "verification": {
            "resolution": "1080x1920", "frame_rate": 30,
            "subtitle": "whisper" if not subtitle_error else "unavailable",
            "audio_source": "joined_presenter", "audio_stream": True,
            "duration_sync": True, "black_frame_check": True,
            "material_provenance": "CONCEPT / AI FILL",
        },
        "subtitle_retryable": bool(subtitle_error),
    }
    if subtitle_error:
        result["subtitle_error"] = subtitle_error
    if persist_state:
        persist_state("done", final_result=result)
    for path in normalized + [concat_file, joined, background, composed]:
        try:
            if final_path and path.resolve() == final_path.resolve():
                continue
            path.unlink(missing_ok=True)
        except OSError:
            pass
    return result
