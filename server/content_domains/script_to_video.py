# -*- coding: utf-8 -*-
"""一键成片：现有数字人口播 + 用户图片资产/按需生图 + FFmpeg 自动穿插。"""
import hashlib
import hmac
import io
import json
import math
import os
import pathlib
import random
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid

from .core import OUT_DIR, SMART_MONTAGE_MAX_RUNTIME, adb, closing, jdb

MAX_MATERIAL_SCENES = 8
MAX_SMART_MATERIAL_SCENES = 20
PHOTO_MOTIONS = ("zoom_in", "zoom_out", "pan_left", "pan_right", "pan_up", "pan_down")
MATERIAL_IMAGE_RETRY_CODES = {520}
MATERIAL_IMAGE_RETRY_DELAY = 2
SMART_MONTAGE_PIPELINE = "smart_montage"
SMART_MONTAGE_PLAN_PATH = "/api/gen/script_to_video/plan"
SMART_MONTAGE_MATERIAL_UPLOAD_PATH = "/api/gen/script_to_video/material-upload"
SMART_MONTAGE_TEMPLATE = "smart-montage-v1"
SMART_MONTAGE_MATERIAL_CONTRACT_VERSION = 1
SMART_MONTAGE_UPLOAD_LEASE_SECONDS = 4 * 60 * 60
SMART_MONTAGE_DUPLICATE_RETRIES = 2
SMART_MONTAGE_AUDIO_LEAD_SECONDS = 0.32
SMART_MONTAGE_MAX_NARRATION_SPEED = 1.25
SMART_MONTAGE_AUDIO_TAIL_SECONDS = 0.28
SMART_MONTAGE_FINAL_AUDIO_TAIL_SECONDS = 0.50
SMART_MONTAGE_MAX_DURATION_SECONDS = 90.0
SMART_MONTAGE_MIN_DURATION_SECONDS = 3.0
SMART_MONTAGE_MIN_TRAILING_SILENCE_SECONDS = 0.20
SMART_MONTAGE_MAX_TRAILING_SILENCE_SECONDS = 1.0
SMART_MONTAGE_SUBMISSION_FIELDS = {
    "pipeline", "mode", "copy", "script", "text", "style", "ratio",
    "voice", "plan_digest", "material_upload_ids",
}
_PLAN_DIGEST_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_MATERIAL_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
_material_job_locks = {}
_material_job_locks_guard = threading.Lock()


class ScriptToVideoRecoveryRequired(RuntimeError):
    """The paid provider may have accepted the job; retry from persisted state."""


class ScriptToVideoRecoveryStateUnavailable(RuntimeError):
    """Durable provider state cannot be read, so terminal handling must stop."""


class ScriptToVideoMediaInputError(ValueError):
    """Sanitized pre-provider failure for a frozen interstitial material."""

    def __init__(self, code, message):
        super().__init__(message)
        self.category = "material"
        self.code = str(code)
        self.stage = "media_preflight"

    def audit_summary(self):
        return {
            "stage": self.stage,
            "category": self.category,
            "code": self.code,
        }


_UPLOAD_ID_RE = re.compile(r"^img_[0-9a-f]{32}$")


def _smart_material_root():
    return (OUT_DIR / "_smart_materials").resolve()


class SmartMontageRequestError(ValueError):
    """A typed, user-correctable smart-montage submission error."""

    def __init__(self, message, code, status=400):
        super().__init__(message)
        self.code = str(code)
        self.status = int(status)


def is_smart_montage_payload(payload):
    if not isinstance(payload, dict):
        return False
    return str(payload.get("pipeline") or "").strip().lower() == SMART_MONTAGE_PIPELINE


def normalize_smart_montage_submission(payload):
    """Canonicalize only client-controlled fields for the idempotency hash.

    The derived scene plan is deliberately absent.  Replaying the same client
    request therefore remains stable if a later deployment changes the
    deterministic planner.
    """
    if not isinstance(payload, dict):
        raise SmartMontageRequestError("请求体必须是 JSON 对象", "invalid_request")
    unknown = sorted(set(payload) - SMART_MONTAGE_SUBMISSION_FIELDS)
    if unknown:
        raise SmartMontageRequestError(
            "智能成片提交包含未支持字段: %s" % ", ".join(unknown),
            "invalid_request",
        )
    pipeline = str(payload.get("pipeline") or "").strip().lower()
    mode = str(payload.get("mode") or pipeline).strip().lower()
    if pipeline != SMART_MONTAGE_PIPELINE or mode != SMART_MONTAGE_PIPELINE:
        raise SmartMontageRequestError("智能成片 pipeline 格式无效", "invalid_request")

    copy_field = next(
        (field for field in ("copy", "script", "text") if field in payload),
        None,
    )
    if copy_field is None:
        raise SmartMontageRequestError("请输入成片文案", "invalid_request")
    digest = str(payload.get("plan_digest") or "").strip()
    if not digest:
        raise SmartMontageRequestError(
            "缺少已确认的成片方案摘要，请重新智能拆分",
            "plan_digest_required",
        )
    if not _PLAN_DIGEST_RE.fullmatch(digest):
        raise SmartMontageRequestError("成片方案摘要格式无效", "plan_digest_invalid")

    canonical = {
        "pipeline": SMART_MONTAGE_PIPELINE,
        "copy": payload.get(copy_field),
        "style": payload.get("style"),
        "ratio": payload.get("ratio", "16:9"),
        "plan_digest": digest.lower(),
    }
    if "voice" in payload:
        canonical["voice"] = payload.get("voice")
    if "material_upload_ids" in payload:
        raw_ids = payload.get("material_upload_ids")
        if not isinstance(raw_ids, list) or not 1 <= len(raw_ids) <= MAX_SMART_MATERIAL_SCENES:
            raise SmartMontageRequestError(
                "material_upload_ids 必须是与分镜对应的素材槽位列表",
                "invalid_material_uploads",
            )
        normalized_ids = []
        used_ids = set()
        for item in raw_ids:
            if item is None:
                normalized_ids.append(None)
                continue
            if not isinstance(item, str):
                raise SmartMontageRequestError(
                    "用户素材槽位只能填写 upload_id 或留空",
                    "invalid_material_uploads",
                )
            upload_id = item.strip().lower()
            if not _UPLOAD_ID_RE.fullmatch(upload_id):
                raise SmartMontageRequestError(
                    "用户素材 upload_id 格式无效",
                    "invalid_material_uploads",
                )
            if upload_id in used_ids:
                raise SmartMontageRequestError(
                    "同一张用户素材不能重复绑定多个分镜",
                    "duplicate_material_upload",
                )
            used_ids.add(upload_id)
            normalized_ids.append(upload_id)
        if not used_ids:
            raise SmartMontageRequestError(
                "没有用户素材时请省略 material_upload_ids",
                "invalid_material_uploads",
            )
        canonical["material_upload_ids"] = normalized_ids
    return canonical


def smart_montage_plan_response(payload):
    """Build a preview response with an independently bound digest per style."""
    from .script_video_montage import plan_digest, plan_script_video

    planner_payload = {
        field: payload[field]
        for field in ("copy", "script", "text", "styles", "style", "ratio")
        if field in payload
    }
    plan = plan_script_video(planner_payload)
    response_digest = plan_digest(plan)
    digests = {}
    if isinstance(plan.get("styles"), list):
        for style_plan in plan["styles"]:
            style = style_plan["style"]
            frozen = plan_script_video({
                "copy": plan["copy"], "style": style, "ratio": plan["ratio"],
            })
            digest = plan_digest(frozen)
            style_plan["plan_digest"] = digest
            digests[style] = digest
    else:
        digest = plan_digest(plan)
        plan["plan_digest"] = digest
        digests[plan["style"]] = digest
    return {
        "plan": plan,
        "plan_digest": response_digest,
        "plan_digests": digests,
    }


def _scene_prompt(scene):
    return re.sub(r"\s+", " ", str((scene or {}).get("scene") or "")).strip()[:800]


def _bigrams(text):
    compact = re.sub(r"[\W_]+", "", (text or "").lower(), flags=re.UNICODE)
    return {compact[i:i + 2] for i in range(max(0, len(compact) - 1))}


def _similarity(left, right):
    a, b = _bigrams(left), _bigrams(right)
    if not a or not b:
        return 0.0
    return len(a & b) / float(max(1, min(len(a), len(b))))


def _result_candidates(result):
    if not isinstance(result, dict):
        return []
    candidates = []
    if result.get("file"):
        candidates.append((result.get("prompt") or "", result["file"]))
    for item in result.get("materials") or []:
        if isinstance(item, dict) and item.get("file"):
            candidates.append((item.get("prompt") or "", item["file"]))
    return candidates


def _safe_existing_image(rel):
    try:
        path = (OUT_DIR / str(rel)).resolve()
        path.relative_to(OUT_DIR.resolve())
        return _readable_image_path(path)
    except Exception:
        return False


def _readable_image_path(path):
    path = pathlib.Path(path)
    if not path.is_file() or path.suffix.lower() not in _MATERIAL_EXTENSIONS:
        return False
    try:
        if path.stat().st_size <= 0:
            return False
        raw = path.read_bytes()
        from PIL import Image
        with Image.open(io.BytesIO(raw)) as image:
            detected = str(image.format or "").upper()
            image.verify()
        with Image.open(io.BytesIO(raw)) as image:
            image.load()
        # Do not trust the suffix.  A valid JPEG received as .png (or the
        # reverse) is safe to freeze because downstream tools inspect bytes;
        # corrupt/truncated pixel data is still rejected by verify()+load().
        return detected in {"JPEG", "PNG", "WEBP"}
    except Exception:
        return False


def _material_job_lock(job_id):
    key = int(job_id)
    with _material_job_locks_guard:
        return _material_job_locks.setdefault(key, threading.RLock())


def _load_job_payload(job_id, username=None):
    with closing(jdb()) as conn:
        row = conn.execute(
            "SELECT kind,username,status,payload FROM jobs WHERE id=?", (int(job_id),)
        ).fetchone()
    if not row or row["kind"] != "script_to_video":
        raise RuntimeError("文案成片任务不存在")
    if username is not None and row["username"] != username:
        raise PermissionError("素材不属于当前用户")
    try:
        payload = json.loads(row["payload"] or "{}")
    except Exception as exc:
        raise RuntimeError("文案成片任务数据损坏") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("文案成片任务数据损坏")
    return payload, str(row["status"] or "")


def _state_from_payload(payload):
    state = payload.get("_script_to_video_state") or {}
    return dict(state) if isinstance(state, dict) else {}


def _persist_job_state(job_id, username, phase, **fields):
    """Atomically persist the server-owned recovery state and heartbeat."""
    now = int(time.time())
    with closing(jdb()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT kind,username,status,payload FROM jobs WHERE id=?", (int(job_id),)
        ).fetchone()
        if not row or row["kind"] != "script_to_video" or row["username"] != username:
            conn.rollback()
            raise RuntimeError("文案成片任务状态不匹配")
        if row["status"] not in {"pending", "running"}:
            conn.rollback()
            raise RuntimeError("文案成片任务已结束")
        payload = json.loads(row["payload"] or "{}")
        if not isinstance(payload, dict):
            conn.rollback()
            raise RuntimeError("文案成片任务数据损坏")
        state = _state_from_payload(payload)
        state.update(fields)
        state.update({"version": 1, "phase": str(phase), "updated_at": now})
        payload["_script_to_video_state"] = state
        cur = conn.execute(
            "UPDATE jobs SET payload=?,updated_at=? WHERE id=? AND status IN ('pending','running')",
            (json.dumps(payload, ensure_ascii=False), now, int(job_id)),
        )
        if cur.rowcount != 1:
            conn.rollback()
            raise RuntimeError("文案成片任务状态保存失败")
        conn.commit()
    try:
        from . import video as video_domain
        video_domain.update_video_asset_phase(job_id, phase, strict=False)
    except Exception:
        pass
    return state


def _owned_source_asset(username, rel):
    rel = str(rel or "").strip()
    if not rel or not _safe_existing_image(rel):
        return False
    with closing(jdb()) as conn:
        rows = conn.execute(
            "SELECT result FROM jobs WHERE username=? AND status='done' "
            "AND kind IN ('image','script_to_video') ORDER BY id DESC LIMIT 500",
            (username,),
        ).fetchall()
    for row in rows:
        try:
            result = json.loads(row["result"] or "{}")
        except Exception:
            continue
        if any(str(candidate) == rel for _, candidate in _result_candidates(result)):
            return True
    return False


def _new_material_root(job_id):
    return "script_materials/%s-%s" % (int(job_id), uuid.uuid4().hex)


def _frozen_material_path(job_id, root, rel):
    expected = (OUT_DIR / str(root)).resolve()
    path = (OUT_DIR / str(rel)).resolve()
    expected.relative_to(OUT_DIR.resolve())
    path.relative_to(expected)
    if not pathlib.PurePosixPath(str(rel).replace("\\", "/")).parts[0] == "script_materials":
        raise ValueError("冻结素材路径无效")
    if not pathlib.Path(root).name.startswith("%s-" % int(job_id)):
        raise ValueError("冻结素材任务绑定无效")
    return path


def _copy_frozen_material(job_id, root, item, source_path):
    source_path = pathlib.Path(source_path).resolve()
    if not _readable_image_path(source_path):
        raise ScriptToVideoMediaInputError(
            "material_invalid",
            "第 %d 个穿插素材为空、不可读或图片已损坏" % (int(item["scene_index"]) + 1),
        )
    target_dir = (OUT_DIR / root).resolve()
    target_dir.relative_to(OUT_DIR.resolve())
    target_dir.mkdir(parents=True, exist_ok=True)
    suffix = source_path.suffix.lower()
    rel = "%s/scene-%02d%s" % (root, int(item["scene_index"]) + 1, suffix)
    target = _frozen_material_path(job_id, root, rel)
    temp = target.with_name(
        ".%s.%s%s" % (target.stem, uuid.uuid4().hex, suffix)
    )
    try:
        shutil.copyfile(str(source_path), str(temp))
        if not _readable_image_path(temp):
            raise ScriptToVideoMediaInputError(
                "material_copy_invalid", "穿插素材冻结副本校验失败",
            )
        os.replace(str(temp), str(target))
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
    return rel


def _frozen_materials_valid(job_id, state, plan):
    root = str(state.get("material_root") or "")
    items = state.get("materials") or []
    if not root or not isinstance(items, list) or len(items) != len(plan):
        return False
    expected = {int(item["scene_index"]) for item in plan}
    actual = set()
    try:
        for item in items:
            index = int(item["scene_index"])
            path = _frozen_material_path(job_id, root, item.get("file"))
            if not _readable_image_path(path):
                return False
            actual.add(index)
    except Exception:
        return False
    return actual == expected


def freeze_reused_materials_for_job(job_id, username):
    """Freeze historical assets before the request is enqueued.

    Generated images remain a worker pre-provider stage, but an old/history path
    is never left as the worker's only copy after this function succeeds.
    """
    with _material_job_lock(job_id):
        payload, _ = _load_job_payload(job_id, username)
        plan = payload.get("material_plan") or []
        state = _state_from_payload(payload)
        root = str(state.get("material_root") or "") or _new_material_root(job_id)
        frozen = {
            int(item["scene_index"]): dict(item)
            for item in (state.get("materials") or []) if isinstance(item, dict)
        }
        for item in plan:
            if item.get("source") != "asset":
                continue
            index = int(item["scene_index"])
            current = frozen.get(index)
            if current:
                path = _frozen_material_path(job_id, root, current.get("file"))
                if _readable_image_path(path):
                    continue
            rel = str(item.get("file") or "")
            if not _owned_source_asset(username, rel):
                raise PermissionError("分镜 %d 的历史素材不存在或不属于当前用户" % (index + 1))
            frozen[index] = dict(item, file=_copy_frozen_material(
                job_id, root, item, (OUT_DIR / rel).resolve()))
        ordered = [frozen[index] for index in sorted(frozen)]
        return _persist_job_state(
            job_id, username, "preparing_materials",
            material_root=root, materials=ordered,
        )


def _match_image_asset(username, prompt):
    """从本人最近图片/一键成片产物中找最接近的静态素材。"""
    with closing(jdb()) as conn:
        rows = conn.execute(
            "SELECT result FROM jobs WHERE username=? AND status='done'"
            " AND kind IN ('image','script_to_video') ORDER BY id DESC LIMIT 240",
            (username,),
        ).fetchall()
    best = None
    for row in rows:
        try:
            result = json.loads(row["result"] or "{}")
        except Exception:
            continue
        for old_prompt, rel in _result_candidates(result):
            score = _similarity(prompt, old_prompt)
            if score >= 0.34 and _safe_existing_image(rel) and (best is None or score > best[0]):
                best = (score, str(rel))
    return best[1] if best else None


def prepare_script_to_video_payload(payload, username, digital_human_consent=None):
    """提交扣点前冻结素材计划，保证能一次算清总价且不发生生成到一半欠费。"""
    body = dict(payload or {})
    # Runtime recovery state is server-owned.  Never accept a client supplied
    # frozen path, provider id, or lifecycle phase.
    body.pop("_script_to_video_state", None)
    from . import digital_human_oneclick
    if str(body.get("pipeline") or "").strip().lower() == digital_human_oneclick.PIPELINE:
        return digital_human_oneclick.prepare_compose_payload(
            body, username, consent_record=digital_human_consent,
        )
    from . import digital_human_v2
    if str(body.get("pipeline") or "").strip().lower() == digital_human_v2.PIPELINE:
        return digital_human_v2.prepare_compose_payload(
            body, username, consent_record=digital_human_consent,
        )
    if str(body.get("pipeline") or "").strip().lower() == SMART_MONTAGE_PIPELINE:
        from .script_video_montage import plan_digest, plan_script_video

        body = normalize_smart_montage_submission(body)

        # 浏览器里的 plan 只用于用户预览；提交时由服务器重新计算，避免篡改
        # 分镜数量、时长或出图提示后少扣点/执行任意模板内容。
        frozen = plan_script_video({
            "copy": body.get("copy") or body.get("script") or body.get("text"),
            "style": body.get("style"),
            "ratio": body.get("ratio"),
        })
        expected_digest = plan_digest(frozen)
        if not hmac.compare_digest(body["plan_digest"], expected_digest):
            raise SmartMontageRequestError(
                "成片规划规则已更新，请重新智能拆分后确认",
                "plan_digest_mismatch",
                409,
            )
        scenes = [dict(scene) for scene in frozen["scenes"]]
        upload_slots = body.get("material_upload_ids")
        if upload_slots is None:
            upload_slots = [None] * len(scenes)
        elif len(upload_slots) != len(scenes):
            raise SmartMontageRequestError(
                "用户素材槽位数量与当前分镜不一致，请重新智能拆分",
                "material_scene_mismatch",
                409,
            )

        upload_metadata = {}
        content_hashes = set()
        if any(upload_slots):
            from . import cli_uploads

            for index, upload_id in enumerate(upload_slots):
                if upload_id is None:
                    continue
                try:
                    meta = cli_uploads.inspect_image(upload_id, username)
                except ValueError as exc:
                    raise SmartMontageRequestError(
                        str(exc), "material_upload_unavailable", 409,
                    ) from exc
                if meta.get("approved_for") != SMART_MONTAGE_PIPELINE:
                    raise SmartMontageRequestError(
                        "用户素材未通过智能成片上传校验，请重新上传",
                        "material_upload_unapproved",
                        409,
                    )
                digest = str(meta.get("sha256") or "")
                if digest in content_hashes:
                    raise SmartMontageRequestError(
                        "用户素材内容重复，请为每个分镜选择不同图片",
                        "duplicate_material_content",
                    )
                content_hashes.add(digest)
                upload_metadata[index] = {
                    "upload_id": upload_id,
                    "sha256": digest,
                    "mime": str(meta.get("mime") or ""),
                    "extension": str(meta.get("extension") or ""),
                    "width": int(meta.get("width") or 0),
                    "height": int(meta.get("height") or 0),
                }

        material_plan = []
        for index, scene in enumerate(scenes):
            prompt = _scene_prompt({"scene": scene.get("image_prompt")})
            if index in upload_metadata:
                material_plan.append({
                    "scene_index": index,
                    "prompt": prompt,
                    "source": "upload",
                    "file": None,
                    **upload_metadata[index],
                })
            else:
                material_plan.append({
                    "scene_index": index,
                    "prompt": prompt,
                    "source": "generate",
                    "file": None,
                })
        generated_count = sum(
            1 for item in material_plan if item["source"] == "generate"
        )
        body.update({
            "pipeline": SMART_MONTAGE_PIPELINE,
            "mode": SMART_MONTAGE_PIPELINE,
            "copy": frozen["copy"],
            "style": frozen["style"],
            "ratio": frozen["ratio"],
            "duration": frozen["duration_seconds"],
            "scenes": scenes,
            "smart_plan": frozen,
            "material_plan": material_plan,
            "material_generate_count": generated_count,
            "material_reused_count": len(scenes) - generated_count,
            "smart_material_contract_version": SMART_MONTAGE_MATERIAL_CONTRACT_VERSION,
        })
        return body
    scenes = [dict(scene) for scene in (body.get("scenes") or []) if isinstance(scene, dict)]
    if not scenes:
        raise ValueError("没有可生成的分镜")
    if len(scenes) > MAX_MATERIAL_SCENES:
        raise ValueError("一键成片最多支持 %d 个分镜" % MAX_MATERIAL_SCENES)
    body["scenes"] = scenes
    if (body.get("style") or "口播").strip() == "剧情":
        return body

    # Submission validation runs before the shared charge/job transaction.
    # Reject a stale or corrupt avatar before image generation, TTS, or HeyGen.
    _preflight_talking_avatar(username, body.get("avatar_id"))

    plan = []
    for index, scene in enumerate(scenes):
        prompt = _scene_prompt(scene)
        if not prompt:
            continue
        existing = _match_image_asset(username, prompt)
        plan.append({
            "scene_index": index,
            "prompt": prompt,
            "source": "asset" if existing else "generate",
            "file": existing,
        })
    body["material_plan"] = plan
    body["material_generate_count"] = sum(1 for item in plan if item["source"] == "generate")
    return body


def _smart_material_input_file(rel):
    """Resolve only frozen smart-montage inputs, never general output paths."""
    try:
        root = _smart_material_root()
        path = (OUT_DIR / str(rel or "")).resolve()
        path.relative_to(root)
    except Exception:
        return None
    if path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
        return None
    return path if path.is_file() else None


def cleanup_smart_montage_uploads(payload_or_plan):
    """Remove task-owned frozen copies without ever touching original uploads."""
    if isinstance(payload_or_plan, dict):
        plan = payload_or_plan.get("material_plan") or []
    elif isinstance(payload_or_plan, list):
        plan = payload_or_plan
    else:
        plan = []
    root = _smart_material_root()
    parents = set()
    for item in plan:
        if not isinstance(item, dict) or item.get("source") != "upload":
            continue
        path = _smart_material_input_file(item.get("file"))
        if path is None:
            continue
        parents.add(path.parent)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        item["file"] = None
    for parent in sorted(parents, key=lambda value: len(value.parts), reverse=True):
        if parent == root:
            continue
        try:
            parent.rmdir()
        except OSError:
            pass


def cleanup_orphaned_smart_montage_roots(active_payloads, now=None, grace=600):
    """Remove old task roots that are not referenced by pending/running jobs."""
    now = float(time.time() if now is None else now)
    root = _smart_material_root()
    protected = set()
    for payload in active_payloads or []:
        if not isinstance(payload, dict):
            continue
        for item in payload.get("material_plan") or []:
            if not isinstance(item, dict) or item.get("source") != "upload":
                continue
            path = _smart_material_input_file(item.get("file"))
            if path is not None:
                protected.add(path.parent.resolve())
    try:
        candidates = list(root.glob("task_*"))
    except OSError:
        return 0
    removed = 0
    for candidate in candidates:
        try:
            if candidate.is_symlink():
                if candidate.lstat().st_mtime < now - max(60, int(grace)):
                    candidate.unlink(missing_ok=True)
                    removed += 1
                continue
            resolved = candidate.resolve()
            resolved.relative_to(root)
            if not resolved.is_dir() or resolved in protected:
                continue
            if resolved.stat().st_mtime >= now - max(60, int(grace)):
                continue
            shutil.rmtree(resolved)
            removed += 1
        except (OSError, ValueError, RuntimeError):
            continue
    return removed


def materialize_smart_montage_uploads(payload, username):
    """Freeze approved uploads into job-owned hard links immediately pre-charge."""
    if not is_smart_montage_payload(payload):
        return []
    material_plan = payload.get("material_plan")
    if not isinstance(material_plan, list):
        raise SmartMontageRequestError(
            "智能成片缺少服务端素材方案", "invalid_material_plan",
        )
    upload_items = [
        item for item in material_plan
        if isinstance(item, dict) and item.get("source") == "upload"
    ]
    if not upload_items:
        return []

    from . import cli_uploads

    root = _smart_material_root()
    task_root = (root / ("task_" + uuid.uuid4().hex)).resolve()
    task_root.relative_to(root)
    created = []
    try:
        task_root.mkdir(parents=True, mode=0o700)
        try:
            os.chmod(root, 0o700)
            os.chmod(task_root, 0o700)
        except OSError:
            pass
        for item in upload_items:
            upload_id = str(item.get("upload_id") or "")
            expected = str(item.get("sha256") or "")
            extension = str(item.get("extension") or "")
            scene_index = int(item.get("scene_index"))
            destination = (task_root / ("scene-%02d%s" % (scene_index, extension))).resolve()
            destination.relative_to(task_root)
            try:
                cli_uploads.freeze_image(
                    upload_id,
                    username,
                    destination,
                    SMART_MONTAGE_PIPELINE,
                    expected,
                )
            except ValueError as exc:
                destination.unlink(missing_ok=True)
                raise SmartMontageRequestError(
                    str(exc), "material_upload_unavailable", 409,
                ) from exc
            rel = destination.relative_to(OUT_DIR.resolve()).as_posix()
            item["file"] = rel
            created.append(rel)
        return created
    except SmartMontageRequestError:
        cleanup_smart_montage_uploads(payload)
        try:
            task_root.rmdir()
        except OSError:
            pass
        raise
    except Exception as exc:
        cleanup_smart_montage_uploads(payload)
        try:
            task_root.rmdir()
        except OSError:
            pass
        raise SmartMontageRequestError(
            "用户素材暂时无法保存，请稍后重试",
            "material_staging_failed",
            503,
        ) from exc


def gen_script_to_video(payload):
    """由 run_job 调用，走标准 job 生命周期。"""
    username = (payload.get("_username") or "").strip()
    from . import digital_human_oneclick
    if str(payload.get("pipeline") or "").strip().lower() == digital_human_oneclick.PIPELINE:
        job_id = int(payload.get("_job_id") or 0)

        def persist(phase, **fields):
            return _persist_job_state(job_id, username, phase, **fields)

        return digital_human_oneclick.compose(payload, persist_state=persist)
    from . import digital_human_v2
    if str(payload.get("pipeline") or "").strip().lower() == digital_human_v2.PIPELINE:
        job_id = int(payload.get("_job_id") or 0)

        def persist_v2(phase, **fields):
            return _persist_job_state(job_id, username, phase, **fields)

        return digital_human_v2.compose(payload, persist_state=persist_v2)
    if str(payload.get("pipeline") or "").strip().lower() == SMART_MONTAGE_PIPELINE:
        material_plan = payload.get("material_plan") or []
        has_uploaded_material = any(
            isinstance(item, dict) and item.get("source") == "upload"
            for item in material_plan
        )
        contract_version = int(payload.get("smart_material_contract_version") or 0)
        if (has_uploaded_material
                and contract_version != SMART_MONTAGE_MATERIAL_CONTRACT_VERSION):
            raise ValueError("智能成片素材协议版本不受支持")
        if contract_version not in {0, SMART_MONTAGE_MATERIAL_CONTRACT_VERSION}:
            raise ValueError("智能成片素材协议版本不受支持")
        return _gen_smart_montage(username, payload)
    scenes = payload.get("scenes") or []
    style = (payload.get("style") or "口播").strip()
    if style == "剧情":
        return _gen_drama(username, scenes, payload)
    return _gen_talking(username, scenes, payload)


def dispatch_http(handler, method, verify_token, must_change_password):
    """Authenticated smart-montage planning and private material upload."""
    path = handler.path.split("?", 1)[0]
    from . import digital_human_oneclick, digital_human_v2
    if path not in {SMART_MONTAGE_PLAN_PATH, SMART_MONTAGE_MATERIAL_UPLOAD_PATH,
                     digital_human_oneclick.PLAN_PATH,
                     digital_human_oneclick.CONSENT_PATH,
                     digital_human_oneclick.GESTURE_RECOVERY_PATH,
                     digital_human_oneclick.MATERIAL_RECOVERY_PATH,
                     digital_human_oneclick.VIDEO_RECOVERY_PATH,
                     digital_human_oneclick.HEYGEN_PREFLIGHT_PATH,
                     digital_human_v2.PLAN_PATH,
                     digital_human_v2.CONSENT_PATH,
                     digital_human_v2.AUDIO_UPLOAD_PATH,
                     digital_human_v2.VIDEO_UPLOAD_PATH,
                     digital_human_v2.MATERIAL_RESOLVE_PATH}:
        return False
    user = verify_token(handler._token())
    if not user:
        handler._send(401, {"detail": "未登录或登录已过期"})
        return True
    if must_change_password(user):
        handler._send(403, {"detail": "请先修改初始密码"})
        return True
    if path == digital_human_oneclick.PLAN_PATH:
        if method != "POST":
            handler._method_not_allowed()
            return True
        try:
            handler._send(200, digital_human_oneclick.plan_response(handler._json_body_strict()))
        except digital_human_oneclick.DigitalHumanRequestError as exc:
            handler._send(exc.status, {"detail": str(exc)[:220], "code": exc.code,
                                      "invalid_job_ids": exc.invalid_job_ids})
        except ValueError as exc:
            handler._send(400, {"detail": str(exc)[:220]})
        return True
    if path == digital_human_v2.PLAN_PATH:
        if method != "POST":
            handler._method_not_allowed()
            return True
        try:
            handler._send(200, digital_human_v2.plan_response(
                handler._json_body_strict(), user["username"],
            ))
        except digital_human_oneclick.DigitalHumanRequestError as exc:
            handler._send(exc.status, {
                "detail": str(exc)[:220], "code": exc.code,
            })
        except ValueError as exc:
            handler._send(400, {"detail": str(exc)[:220]})
        return True
    if path == digital_human_v2.AUDIO_UPLOAD_PATH:
        if method != "POST":
            handler._method_not_allowed()
            return True
        try:
            if handler.headers.get("Transfer-Encoding"):
                raise digital_human_oneclick.DigitalHumanRequestError(
                    "录音上传必须提供 Content-Length", "audio_upload_length_required",
                )
            try:
                length = int(handler.headers.get("Content-Length") or 0)
            except (TypeError, ValueError) as exc:
                raise digital_human_oneclick.DigitalHumanRequestError(
                    "录音上传长度无效", "audio_upload_length_required",
                ) from exc
            handler._send(200, digital_human_v2.audio_upload_response(
                handler.rfile, length, user["username"],
                handler.headers.get("X-HQ-Run-ID"),
                handler.headers.get("Content-Type"),
                handler.headers.get("X-HQ-Audio-SHA256"),
            ))
        except digital_human_oneclick.DigitalHumanRequestError as exc:
            handler._send(exc.status, {
                "detail": str(exc)[:220], "code": exc.code,
                **({"retry_after_ms": 5000} if exc.status == 503 else {}),
            })
        except ValueError as exc:
            handler._send(400, {"detail": str(exc)[:220]})
        return True
    if path == digital_human_v2.VIDEO_UPLOAD_PATH:
        if method != "POST":
            handler._method_not_allowed()
            return True
        try:
            if handler.headers.get("Transfer-Encoding"):
                raise digital_human_oneclick.DigitalHumanRequestError(
                    "真人视频上传必须提供 Content-Length", "video_upload_length_required",
                )
            try:
                length = int(handler.headers.get("Content-Length") or 0)
            except (TypeError, ValueError) as exc:
                raise digital_human_oneclick.DigitalHumanRequestError(
                    "真人视频上传长度无效", "video_upload_length_required",
                ) from exc
            handler._send(200, digital_human_v2.video_upload_response(
                handler.rfile, length, user["username"],
                handler.headers.get("X-HQ-Run-ID"),
                handler.headers.get("Content-Type"),
                handler.headers.get("X-HQ-Video-SHA256"),
            ))
        except digital_human_oneclick.DigitalHumanRequestError as exc:
            handler._send(exc.status, {
                "detail": str(exc)[:220], "code": exc.code,
                **({"retry_after_ms": 5000} if exc.status == 503 else {}),
            })
        except ValueError as exc:
            handler._send(400, {"detail": str(exc)[:220]})
        return True
    if path == digital_human_v2.MATERIAL_RESOLVE_PATH:
        if method != "POST":
            handler._method_not_allowed()
            return True
        try:
            handler._send(200, digital_human_v2.resolve_material_response(
                handler._json_body_strict(), user["username"],
            ))
        except digital_human_oneclick.DigitalHumanRequestError as exc:
            handler._send(exc.status, {
                "detail": str(exc)[:220], "code": exc.code,
                **({"retry_after_ms": 5000} if exc.status == 503 else {}),
            })
        except ValueError as exc:
            handler._send(400, {"detail": str(exc)[:220]})
        return True
    if path == digital_human_oneclick.CONSENT_PATH:
        if method != "POST":
            handler._method_not_allowed()
            return True
        try:
            body = handler._json_body_strict()
            if str(body.get("voice_mode") or "").strip().lower() == "existing":
                from . import audio as audio_domain
                audio_domain.resolve_audio_provider_voice(
                    user["username"], str(body.get("voice_ref") or "").strip(),
                )
            handler._send(200, digital_human_oneclick.consent_response(
                body, user["username"],
                os.environ.get("HQ_INTERNAL_TOKEN", ""),
            ))
        except digital_human_oneclick.DigitalHumanRequestError as exc:
            handler._send(exc.status, {
                "detail": str(exc)[:220], "code": exc.code,
                **({"retry_after_ms": 5000} if exc.status == 503 else {}),
            })
        except ValueError as exc:
            handler._send(400, {"detail": str(exc)[:220]})
        return True
    if path == digital_human_v2.CONSENT_PATH:
        if method != "POST":
            handler._method_not_allowed()
            return True
        try:
            body = handler._json_body_strict()
            if str(body.get("voice_mode") or "").strip().lower() == "existing":
                from . import audio as audio_domain
                audio_domain.resolve_audio_provider_voice(
                    user["username"], str(body.get("voice_ref") or "").strip(),
                )
            handler._send(200, digital_human_v2.consent_response(
                body, user["username"], os.environ.get("HQ_INTERNAL_TOKEN", ""),
            ))
        except digital_human_oneclick.DigitalHumanRequestError as exc:
            handler._send(exc.status, {
                "detail": str(exc)[:220], "code": exc.code,
                **({"retry_after_ms": 5000} if exc.status == 503 else {}),
            })
        except ValueError as exc:
            handler._send(400, {"detail": str(exc)[:220]})
        return True
    if path == digital_human_oneclick.GESTURE_RECOVERY_PATH:
        if method != "POST":
            handler._method_not_allowed()
            return True
        try:
            handler._send(200, digital_human_oneclick.validate_gesture_recovery(
                handler._json_body_strict(), user["username"],
            ))
        except digital_human_oneclick.DigitalHumanRequestError as exc:
            handler._send(exc.status, {"detail": str(exc)[:220], "code": exc.code,
                                      "invalid_job_ids": exc.invalid_job_ids})
        except ValueError as exc:
            handler._send(400, {"detail": str(exc)[:220]})
        return True
    if path == digital_human_oneclick.MATERIAL_RECOVERY_PATH:
        if method != "POST":
            handler._method_not_allowed()
            return True
        try:
            handler._send(200, digital_human_oneclick.validate_material_recovery(
                handler._json_body_strict(), user["username"],
            ))
        except digital_human_oneclick.DigitalHumanRequestError as exc:
            handler._send(exc.status, {"detail": str(exc)[:220], "code": exc.code,
                                      "invalid_job_ids": exc.invalid_job_ids})
        except ValueError as exc:
            handler._send(400, {"detail": str(exc)[:220]})
        return True
    if path == digital_human_oneclick.VIDEO_RECOVERY_PATH:
        if method != "POST":
            handler._method_not_allowed()
            return True
        try:
            handler._send(200, digital_human_oneclick.validate_video_recovery(
                handler._json_body_strict(), user["username"],
            ))
        except digital_human_oneclick.DigitalHumanRequestError as exc:
            handler._send(exc.status, {"detail": str(exc)[:220], "code": exc.code,
                                      "invalid_job_ids": exc.invalid_job_ids})
        except ValueError as exc:
            handler._send(400, {"detail": str(exc)[:220]})
        return True
    if path == digital_human_oneclick.HEYGEN_PREFLIGHT_PATH:
        if method != "POST":
            handler._method_not_allowed()
            return True
        try:
            from . import video as video_domain
            subtitle = video_domain.subtitle_runtime_preflight()
            result = dict(video_domain.heygen_upload_preflight())
            result["subtitle"] = subtitle
            handler._send(200, result)
        except Exception as exc:
            handler._send(int(getattr(exc, "status", 503) or 503), {
                "detail": str(exc)[:220],
                "code": str(getattr(exc, "code", "heygen_upload_unavailable")),
                "no_charge": True,
            })
        return True
    if path == SMART_MONTAGE_MATERIAL_UPLOAD_PATH:
        from . import cli_uploads, miniprogram_security

        if method == "DELETE":
            try:
                body = handler._json_body_strict()
                if (not isinstance(body, dict) or set(body) != {"upload_id"}
                        or not _UPLOAD_ID_RE.fullmatch(
                            str(body.get("upload_id") or "").strip().lower())):
                    raise ValueError("请求必须提供有效的 upload_id")
                # Return a uniform acknowledgement for missing and foreign IDs
                # so this endpoint cannot be used as an ownership oracle.
                cli_uploads.discard_image(body["upload_id"], user["username"])
                handler._send(200, {"ok": True})
            except ValueError as exc:
                handler._send(400, {
                    "detail": str(exc)[:220], "code": "invalid_image_discard",
                })
            return True
        if method != "POST":
            handler._method_not_allowed()
            return True

        uploaded = None
        try:
            if handler.headers.get("Transfer-Encoding"):
                raise ValueError("图片上传必须提供 Content-Length")
            length = int(handler.headers.get("Content-Length") or 0)
            content_type = (
                handler.headers.get("Content-Type") or ""
            ).split(";", 1)[0].strip().lower()
            uploaded = cli_uploads.store_image(
                handler.rfile,
                length,
                user["username"],
                content_type,
                handler.headers.get("X-HQ-Image-SHA256"),
            )
            data, meta = cli_uploads.read_image_bytes(
                uploaded["upload_id"], user["username"],
            )
            if miniprogram_security.configured():
                miniprogram_security.check_image(
                    data,
                    "smart-material%s" % meta["extension"],
                    meta["mime"],
                )
            approved = cli_uploads.approve_image(
                uploaded["upload_id"], user["username"], SMART_MONTAGE_PIPELINE,
                lease_seconds=SMART_MONTAGE_UPLOAD_LEASE_SECONDS,
            )
            handler._send(200, {
                **uploaded,
                "expires_at": int(approved.get("expires_at") or 0),
                "expires_in": max(
                    0, int(approved.get("expires_at") or 0) - int(time.time()),
                ),
                "width": int(approved.get("width") or 0),
                "height": int(approved.get("height") or 0),
            })
        except miniprogram_security.ContentRejected as exc:
            if uploaded:
                cli_uploads.discard_image(uploaded.get("upload_id"), user["username"])
            handler._send(400, {
                "detail": str(exc)[:220], "code": "content_rejected",
            })
        except miniprogram_security.SecurityUnavailable as exc:
            if uploaded:
                cli_uploads.discard_image(uploaded.get("upload_id"), user["username"])
            handler._send(503, {
                "detail": str(exc)[:220],
                "code": exc.code,
                "retry_after_ms": 5000,
            })
        except (TypeError, ValueError) as exc:
            if uploaded:
                cli_uploads.discard_image(uploaded.get("upload_id"), user["username"])
            handler._send(400, {
                "detail": str(exc)[:220], "code": "invalid_image_upload",
            })
        except OSError:
            if uploaded:
                cli_uploads.discard_image(uploaded.get("upload_id"), user["username"])
            handler._send(500, {
                "detail": "图片暂时无法保存", "code": "image_upload_failed",
            })
        return True
    if method != "POST":
        handler._method_not_allowed()
        return True
    try:
        body = handler._json_body_strict()
        if not isinstance(body, dict):
            raise ValueError("请求体必须是 JSON 对象")
        allowed = {"copy", "script", "text", "styles", "style", "ratio"}
        if set(body) - allowed:
            raise ValueError("请求包含未支持字段")
        handler._send(200, smart_montage_plan_response(body))
    except ValueError as exc:
        handler._send(400, {"detail": str(exc)[:220]})
    return True


def _smart_phase(job_id, phase, strict=False, **fields):
    try:
        from . import video as video_domain
        updated = video_domain.update_video_asset_phase(
            job_id, phase, strict=strict, **fields
        )
        if strict and updated is False:
            raise RuntimeError("智能成片作品保存失败")
        return updated
    except Exception:
        if strict:
            raise
        # 阶段信息仅用于进度展示，不能让一次已付费生成因状态写入失败而中断。
        return False


def _smart_voiceover_text(plan):
    """Narrate the complete user-confirmed copy; never silently summarize it."""
    from .script_video_montage import MAX_COPY_CHARACTERS

    copy = re.sub(r"\s+", " ", str(plan.get("copy") or "")).strip()
    if not copy or len(copy) > MAX_COPY_CHARACTERS:
        raise ValueError("智能成片文案必须在 1-%d 个字符内" % MAX_COPY_CHARACTERS)
    return copy


def _strip_voiceover_markup(value):
    value = re.sub(r"(?<!\\)(?:\*\*|__|~~)", "", str(value or ""))
    value = re.sub(r"(?<!\\)[*`]", "", value)
    return re.sub(r"\s+", " ", value).strip()


def _spoken_signature(value):
    return re.sub(r"[\W_]+", "", str(value or ""), flags=re.UNICODE).casefold()


def _smart_scene_voiceover_texts(plan):
    """Return complete, ordered narration segments for the frozen scenes."""
    scenes = plan.get("scenes") or []
    if not 3 <= len(scenes) <= 20:
        raise ValueError("智能成片分镜数量无效")
    texts = []
    for index, scene in enumerate(scenes):
        text = _strip_voiceover_markup(
            scene.get("narration_text") or scene.get("supporting_copy")
        )
        if not text or not _spoken_signature(text):
            raise ValueError("第%d幕旁白不能为空" % (index + 1))
        if not re.search(r"[，,。！？!?；;：:]$", text):
            text += "。" if index == len(scenes) - 1 else "，"
        texts.append(text)
    expected = _spoken_signature(_smart_voiceover_text(plan))
    actual = _spoken_signature("".join(texts))
    if not expected or actual != expected:
        raise ValueError("智能成片分镜未完整覆盖原文案")
    return texts


def _probe_media_duration(path):
    try:
        completed = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(path),
            ],
            check=True, timeout=30, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
        )
        duration = float(completed.stdout.strip())
        return duration if duration > 0 else 0.0
    except (OSError, subprocess.SubprocessError, TypeError, ValueError):
        return 0.0


def _atempo_filter(speed_factor):
    factor = max(1.0, float(speed_factor or 1.0))
    values = []
    while factor > 2.0:
        values.append(2.0)
        factor /= 2.0
    values.append(factor)
    return ",".join("atempo=%.6f" % value for value in values)


def _probe_voiceover_bounds(path, duration, fail_closed=False):
    """Return conservative speech bounds while preserving internal pauses."""
    duration_ms = max(1, int(round(float(duration or 0) * 1000)))
    start_ms, end_ms = 0, duration_ms
    try:
        from . import video_compose_media as media

        ranges = media.detect_silence_ranges(
            path, noise_db=-45, minimum_ms=80, timeout=90,
        )
        if (
            ranges
            and int(ranges[0]["start_ms"]) <= 50
            and int(ranges[-1]["end_ms"]) >= duration_ms - 80
            and sum(
                max(0, int(item["end_ms"]) - int(item["start_ms"]))
                for item in ranges
            ) >= duration_ms - 130
        ):
            return 0.0, 0.0
        if ranges and int(ranges[0]["start_ms"]) <= 50:
            # Keep a small safety lip so a quiet initial consonant is not cut.
            start_ms = max(0, int(ranges[0]["end_ms"]) - 60)
        if ranges and int(ranges[-1]["end_ms"]) >= duration_ms - 80:
            # Retain a short natural release; the deterministic scene tail is
            # added later by the master-track builder.
            end_ms = min(duration_ms, int(ranges[-1]["start_ms"]) + 120)
    except Exception as exc:
        if fail_closed:
            raise RuntimeError("智能成片主旁白静音检测失败") from exc
        # Silence probing is a quality enhancement.  Keeping the complete TTS
        # segment is safer than failing a paid job or clipping real speech.
        start_ms, end_ms = 0, duration_ms
    if end_ms - start_ms < 200:
        start_ms, end_ms = 0, duration_ms
    return round(start_ms / 1000.0, 3), round(end_ms / 1000.0, 3)


def _generate_smart_voiceover_segments(
        audio_domain, username, voice, plan, workspace, deadline):
    texts = _smart_scene_voiceover_texts(plan)
    workspace = pathlib.Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    segments, owned_rels = [], set()
    try:
        for index, text in enumerate(texts, 1):
            _smart_deadline_remaining(deadline, "生成第%d幕旁白前" % index)
            result = audio_domain.gen_audio({
                "_username": username,
                "text": text,
                "voice": voice,
                "speed": "normal",
                "pitch": 0,
                "volume": 0,
            }, publish=False)
            rel = result.get("file") if isinstance(result, dict) else None
            if rel:
                owned_rels.add(str(rel))
            source = _out_file(
                rel, {".aac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav"},
            )
            if not source:
                raise RuntimeError("第%d幕旁白生成结果不可用" % index)
            duration = float((result or {}).get("duration_ms") or 0) / 1000.0
            if duration <= 0:
                duration = _probe_media_duration(source)
            if duration <= 0:
                raise RuntimeError("第%d幕旁白时长无效" % index)
            target = workspace / ("scene-%02d%s" % (index, source.suffix.lower()))
            shutil.move(str(source), str(target))
            owned_rels.discard(str(rel))
            speech_start, speech_end = _probe_voiceover_bounds(target, duration)
            speech_duration = round(speech_end - speech_start, 3)
            if speech_duration < 0.2:
                raise RuntimeError("第%d幕旁白有效语音过短" % index)
            segments.append({
                "index": index,
                "text": text,
                "path": target,
                "source_duration_seconds": round(duration, 3),
                "speech_start_seconds": speech_start,
                "speech_end_seconds": speech_end,
                "speech_duration_seconds": speech_duration,
            })
            _smart_deadline_remaining(deadline, "生成第%d幕旁白后" % index)
        return segments
    except Exception:
        for rel in owned_rels:
            _remove_out_file(rel)
        raise


def _allocate_exact_milliseconds(total_ms, weights):
    if total_ms <= 0:
        return [0] * len(weights)
    clean = [max(1.0, float(value or 0)) for value in weights]
    weight_sum = sum(clean)
    raw = [total_ms * value / weight_sum for value in clean]
    units = [int(math.floor(value)) for value in raw]
    remainder = total_ms - sum(units)
    order = sorted(
        range(len(units)), key=lambda index: (-(raw[index] - units[index]), index)
    )
    for index in order[:remainder]:
        units[index] += 1
    return units


def _retime_smart_plan(plan, voiceover_segments):
    """Build one sample-aligned visual slot for every real narration segment."""
    frozen = json.loads(json.dumps(plan, ensure_ascii=False))
    scenes = frozen.get("scenes") or []
    segments = list(voiceover_segments or [])
    if not 3 <= len(scenes) <= 20 or len(segments) != len(scenes):
        raise ValueError("智能成片分镜数量无效")

    lead_ms = int(round(SMART_MONTAGE_AUDIO_LEAD_SECONDS * 1000))
    tails_ms = [
        int(round((
            SMART_MONTAGE_FINAL_AUDIO_TAIL_SECONDS
            if index == len(scenes) - 1 else SMART_MONTAGE_AUDIO_TAIL_SECONDS
        ) * 1000))
        for index in range(len(scenes))
    ]
    speech_ms = [
        max(200, int(round(float(item["speech_duration_seconds"]) * 1000)))
        for item in segments
    ]
    hold_ms = lead_ms * len(scenes) + sum(tails_ms)
    # Leave half a second of global safety when the 90-second ceiling requires
    # a uniform atempo adjustment.  No user copy is truncated.
    available_speech_ms = max(
        1000,
        int(SMART_MONTAGE_MAX_DURATION_SECONDS * 1000) - hold_ms - 500,
    )
    def fitted_at(speed):
        return [max(200, int(math.ceil(value / speed))) for value in speech_ms]

    speed_factor = 1.0
    fitted_speech_ms = fitted_at(speed_factor)
    if sum(fitted_speech_ms) > available_speech_ms:
        fastest = fitted_at(SMART_MONTAGE_MAX_NARRATION_SPEED)
        if sum(fastest) > available_speech_ms:
            raise ValueError("旁白实测时长超过90秒，请缩短文案后重试")
        low, high = 1.0, SMART_MONTAGE_MAX_NARRATION_SPEED
        # Include the per-scene 200 ms floor in the solve.  A direct ratio can
        # otherwise reject a timeline that only needs a tiny additional speed
        # adjustment after short clips hit that floor.
        for _ in range(40):
            candidate = (low + high) / 2.0
            if sum(fitted_at(candidate)) <= available_speech_ms:
                high = candidate
            else:
                low = candidate
        speed_factor = high
        fitted_speech_ms = fitted_at(speed_factor)
    base_scene_ms = [
        lead_ms + speech + tail
        for speech, tail in zip(fitted_speech_ms, tails_ms)
    ]
    base_total_ms = sum(base_scene_ms)
    target_total_ms = max(
        int(SMART_MONTAGE_MIN_DURATION_SECONDS * 1000), base_total_ms,
    )
    if target_total_ms > int(SMART_MONTAGE_MAX_DURATION_SECONDS * 1000):
        raise ValueError("旁白超过90秒且无法安全校准")
    slack_ms = target_total_ms - base_total_ms
    # Any small minimum-duration remainder is visual hold time, not a cue to
    # delay speech. Keep every narration cue locked to the same scene lead and
    # put the remainder after speech in non-final scenes. The three-second
    # product floor avoids turning very short copy into long silent pauses.
    extras_ms = _allocate_exact_milliseconds(
        slack_ms, [1] * (len(scenes) - 1),
    ) + [0]

    cursor = 0
    for scene, segment, speech, tail, base_ms, extra_ms in zip(
            scenes, segments, fitted_speech_ms, tails_ms, base_scene_ms, extras_ms):
        scene_ms = base_ms + extra_ms
        voice_start_ms = cursor + lead_ms
        scene["start_seconds"] = round(cursor / 1000.0, 3)
        scene["duration_seconds"] = round(scene_ms / 1000.0, 3)
        scene["voiceover_start_seconds"] = round(voice_start_ms / 1000.0, 3)
        scene["voiceover_duration_seconds"] = round(speech / 1000.0, 3)
        scene["voiceover_end_seconds"] = round((voice_start_ms + speech) / 1000.0, 3)
        scene["narration_text"] = segment["text"]
        scene["headline"] = _strip_voiceover_markup(scene.get("headline"))
        scene["supporting_copy"] = _strip_voiceover_markup(
            scene.get("supporting_copy")
        )
        cursor += scene_ms
    frozen["estimated_duration_seconds"] = round(
        float(frozen.get("duration_seconds") or SMART_MONTAGE_MIN_DURATION_SECONDS), 3,
    )
    frozen["duration_seconds"] = round(target_total_ms / 1000.0, 3)
    frozen["scene_count"] = len(scenes)
    frozen["narration_duration_seconds"] = round(sum(fitted_speech_ms) / 1000.0, 3)
    frozen["narration_speed_factor"] = round(speed_factor, 6)
    return frozen


def _build_smart_voiceover_master(segments, render_plan, output_path, deadline=None):
    scenes = render_plan.get("scenes") or []
    if len(segments) != len(scenes):
        raise RuntimeError("旁白分段与分镜数量不一致")
    output_path = pathlib.Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = ["ffmpeg", "-y"]
    for segment in segments:
        command.extend(["-i", str(segment["path"])])
    speed_factor = max(1.0, float(render_plan.get("narration_speed_factor") or 1.0))
    filters, labels = [], []
    for index, (segment, scene) in enumerate(zip(segments, scenes)):
        local_start = max(
            0.0,
            float(scene["voiceover_start_seconds"]) - float(scene["start_seconds"]),
        )
        chain = [
            "[%d:a]atrim=start=%.3f:end=%.3f" % (
                index,
                float(segment["speech_start_seconds"]),
                float(segment["speech_end_seconds"]),
            ),
            "asetpts=PTS-STARTPTS",
            "aresample=48000",
            "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo",
        ]
        if speed_factor > 1.000001:
            chain.append(_atempo_filter(speed_factor))
        delay_ms = max(0, int(round(local_start * 1000)))
        chain.extend([
            "adelay=%d|%d" % (delay_ms, delay_ms),
            "apad=whole_dur=%.3f" % float(scene["duration_seconds"]),
            "atrim=start=0:end=%.3f" % float(scene["duration_seconds"]),
            "asetpts=N/SR/TB[a%d]" % index,
        ])
        filters.append(",".join(chain))
        labels.append("[a%d]" % index)
    filters.append(
        "%sconcat=n=%d:v=0:a=1[voice]" % ("".join(labels), len(labels))
    )
    command.extend([
        "-filter_complex", ";".join(filters), "-map", "[voice]",
        "-ar", "48000", "-ac", "2", "-c:a", "libmp3lame", "-q:a", "2",
        str(output_path),
    ])
    timeout = 300
    if deadline is not None:
        timeout = max(1, min(timeout, int(_smart_deadline_remaining(
            deadline, "合成主旁白前",
        ))))
    try:
        subprocess.run(
            command, check=True, timeout=timeout,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        try:
            output_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise RuntimeError("智能成片主旁白合成失败") from exc
    actual_duration = _probe_media_duration(output_path)
    expected_duration = float(render_plan["duration_seconds"])
    if actual_duration <= 0 or abs(actual_duration - expected_duration) > 0.18:
        output_path.unlink(missing_ok=True)
        raise RuntimeError("智能成片主旁白时长校验失败")
    speech_start, speech_end = _probe_voiceover_bounds(
        output_path, actual_duration, fail_closed=True,
    )
    if speech_end - speech_start < 0.2:
        output_path.unlink(missing_ok=True)
        raise RuntimeError("智能成片主旁白没有检测到有效语音")
    trailing = actual_duration - speech_end
    if (
        trailing < SMART_MONTAGE_MIN_TRAILING_SILENCE_SECONDS
        or trailing > SMART_MONTAGE_MAX_TRAILING_SILENCE_SECONDS
    ):
        output_path.unlink(missing_ok=True)
        raise RuntimeError("智能成片主旁白尾部静音异常")
    return actual_duration


def _publish_smart_voiceover_master(source):
    rel = "audio/aud_sync_%s.mp3" % uuid.uuid4().hex
    target = (OUT_DIR / rel).resolve()
    target.relative_to(OUT_DIR.resolve())
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    try:
        shutil.copy2(source, partial)
        os.replace(partial, target)
    except Exception:
        partial.unlink(missing_ok=True)
        target.unlink(missing_ok=True)
        raise
    return rel, target


def _out_file(rel, suffixes=None):
    try:
        path = (OUT_DIR / str(rel)).resolve()
        path.relative_to(OUT_DIR.resolve())
    except Exception:
        return None
    if not path.is_file() or (suffixes and path.suffix.lower() not in suffixes):
        return None
    return path


def _remove_out_file(rel):
    path = _out_file(rel)
    if path:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _smart_deadline_remaining(deadline, stage):
    remaining = float(deadline) - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("智能成片处理超过两小时总时限（%s）" % stage)
    return remaining


def _smart_material_images(plan, material_plan=None, deadline=None):
    from . import image as image_domain

    materials, hashes = [], set()
    ratio = plan.get("ratio") or "16:9"
    scenes = plan.get("scenes") or []
    if material_plan is None:
        # Compatibility for jobs accepted before the upload contract existed.
        material_plan = [
            {"scene_index": index, "source": "generate", "file": None}
            for index in range(len(scenes))
        ]
    if not isinstance(material_plan, list) or len(material_plan) != len(scenes):
        raise ValueError("智能成片素材方案与分镜数量不一致")

    uploaded = {}
    for position, raw_item in enumerate(material_plan):
        if not isinstance(raw_item, dict) or raw_item.get("scene_index") != position:
            raise ValueError("智能成片素材分镜顺序无效")
        source = str(raw_item.get("source") or "")
        if source not in {"generate", "upload"}:
            raise ValueError("智能成片素材来源无效")
        item = dict(raw_item)
        if source != "upload":
            continue
        path = _smart_material_input_file(item.get("file"))
        if path is None:
            raise ValueError("第 %d 幕用户素材已失效" % (position + 1))
        digest = _sha256_file(path)
        expected = str(item.get("sha256") or "")
        if not expected or not hmac.compare_digest(digest, expected):
            raise ValueError("第 %d 幕用户素材校验失败" % (position + 1))
        if digest in hashes:
            raise ValueError("第 %d 幕用户素材与其他分镜重复" % (position + 1))
        hashes.add(digest)
        uploaded[position] = {
            "scene_index": position,
            "prompt": str(item.get("prompt") or ""),
            "source": "upload",
            "file": str(item["file"]),
            "sha256": digest,
            "upload_id": str(item.get("upload_id") or ""),
        }
    try:
        for position, scene in enumerate(scenes):
            if position in uploaded:
                materials.append(uploaded[position])
                continue
            if deadline is not None:
                _smart_deadline_remaining(deadline, "生成第 %d 幕素材前" % (position + 1))
            base_prompt = str(scene.get("image_prompt") or "").strip()
            if not base_prompt:
                raise ValueError("第 %d 幕缺少画面提示" % (position + 1))
            accepted = None
            for attempt in range(SMART_MONTAGE_DUPLICATE_RETRIES + 1):
                if deadline is not None:
                    _smart_deadline_remaining(
                        deadline,
                        "生成第 %d 幕素材第 %d 次尝试前" % (position + 1, attempt + 1),
                    )
                prompt = (
                    "%s 本次成片唯一镜头编号 %02d/%02d，视觉变化版本 %d；"
                    "必须与其他镜头在主体动作、机位和构图上明显不同。"
                ) % (base_prompt, position + 1, len(scenes), attempt + 1)
                try:
                    generated = image_domain.gen_image({
                        "prompt": prompt,
                        "ratio": ratio,
                        "quality": "standard",
                        "provider": "openai",
                        "count": 1,
                    })
                except Exception as exc:
                    if (
                        getattr(exc, "code", None) in MATERIAL_IMAGE_RETRY_CODES
                        and attempt < SMART_MONTAGE_DUPLICATE_RETRIES
                    ):
                        time.sleep(MATERIAL_IMAGE_RETRY_DELAY)
                        continue
                    raise
                if deadline is not None:
                    _smart_deadline_remaining(
                        deadline, "生成第 %d 幕素材后" % (position + 1),
                    )
                rel = generated.get("file") if isinstance(generated, dict) else None
                path = _out_file(rel, {".jpg", ".jpeg", ".png", ".webp", ".avif"})
                if not path:
                    raise RuntimeError("第 %d 幕生图结果不可用" % (position + 1))
                digest = _sha256_file(path)
                if digest in hashes:
                    _remove_out_file(rel)
                    if attempt < SMART_MONTAGE_DUPLICATE_RETRIES:
                        continue
                    raise RuntimeError("第 %d 幕素材与其他分镜重复" % (position + 1))
                accepted = {
                    "scene_index": position,
                    "prompt": base_prompt,
                    "source": "generate",
                    "file": str(rel),
                    "sha256": digest,
                }
                hashes.add(digest)
                break
            if not accepted:
                raise RuntimeError("第 %d 幕素材生成失败" % (position + 1))
            materials.append(accepted)
        if len(materials) != len(scenes) or len(hashes) != len(scenes):
            raise RuntimeError("分镜素材数量或唯一性校验未通过")
        return materials
    except Exception:
        _cleanup_generated_materials(materials)
        raise


def _gen_smart_montage(username, payload):
    from . import audio as audio_domain
    from . import script_video_render as montage_renderer
    from . import video as video_domain

    if not username:
        raise ValueError("智能成片任务缺少用户信息")
    plan = payload.get("smart_plan")
    if not isinstance(plan, dict):
        raise ValueError("智能成片任务缺少服务端方案")
    job_id = payload.get("_job_id")
    deadline = time.monotonic() + SMART_MONTAGE_MAX_RUNTIME
    voice = str(payload.get("voice") or "S_d21F8OR62").strip()
    materials, audio_rel, output_rel = [], None, None
    try:
        _smart_deadline_remaining(deadline, "生成旁白前")
        _smart_phase(job_id, "generating_audio", mode=SMART_MONTAGE_PIPELINE)
        with tempfile.TemporaryDirectory(prefix="hq-smart-voice-") as raw_audio_dir:
            audio_workspace = pathlib.Path(raw_audio_dir)
            voiceover_segments = _generate_smart_voiceover_segments(
                audio_domain, username, voice, plan, audio_workspace, deadline,
            )
            render_plan = _retime_smart_plan(plan, voiceover_segments)
            temporary_master = audio_workspace / "voiceover-master.mp3"
            _build_smart_voiceover_master(
                voiceover_segments, render_plan, temporary_master, deadline=deadline,
            )
            audio_rel, voiceover = _publish_smart_voiceover_master(temporary_master)
        _smart_deadline_remaining(deadline, "处理旁白后")

        _smart_phase(job_id, "generating_assets", audio_file=audio_rel, voice=voice)
        materials = _smart_material_images(
            render_plan, payload.get("material_plan"), deadline=deadline,
        )

        output_rel = "video/script_montage_%s.mp4" % uuid.uuid4().hex
        output_path = (OUT_DIR / output_rel).resolve()
        output_path.relative_to(OUT_DIR.resolve())
        output_path.parent.mkdir(parents=True, exist_ok=True)
        material_paths = [_out_file(item["file"]) for item in materials]
        if any(path is None for path in material_paths):
            raise RuntimeError("智能成片素材在渲染前不可用")

        generated_materials = [
            item for item in materials if item.get("source") == "generate"
        ]
        uploaded_count = len(materials) - len(generated_materials)
        preview_file = generated_materials[0]["file"] if generated_materials else None
        _smart_phase(
            job_id,
            "rendering",
            **({"image_file": preview_file} if preview_file else {}),
        )
        render_timeout = min(
            max(600, int(render_plan["duration_seconds"] * 24)),
            max(1, int(_smart_deadline_remaining(deadline, "开始渲染前"))),
        )
        report = montage_renderer.render(
            render_plan,
            material_paths,
            output_path,
            voiceover=voiceover,
            timeout=render_timeout,
        )
        _smart_deadline_remaining(deadline, "渲染后")
        _smart_phase(job_id, "verifying", video_file=output_rel)
        verified_duration = float(((report.get("output") or {}).get("duration_ms") or 0)) / 1000.0
        if verified_duration <= 0:
            verified_duration = _probe_media_duration(output_path)
        if verified_duration <= 0:
            raise RuntimeError("智能成片输出时长校验失败")
        video_url = video_domain.public_url(output_rel, "video/mp4", private=True)
        public_materials = []
        for item in materials:
            public_item = {
                "scene_index": int(item["scene_index"]),
                "prompt": str(item.get("prompt") or ""),
                "source": str(item.get("source") or ""),
                "sha256": str(item.get("sha256") or ""),
            }
            if item.get("source") == "generate":
                public_item["file"] = str(item.get("file") or "")
            public_materials.append(public_item)
        result = {
            "type": "script_to_video",
            "pipeline": SMART_MONTAGE_PIPELINE,
            "mode": SMART_MONTAGE_PIPELINE,
            "status": "done",
            "phase": "completed",
            "video_file": output_rel,
            "video_url": video_url,
            "audio_file": str(audio_rel),
            "image_file": preview_file,
            "text": plan.get("copy") or "",
            "copy": plan.get("copy") or "",
            "voice": voice,
            "style": render_plan["style"],
            "ratio": render_plan["ratio"],
            "resolution": "%dx%d" % (
                1920 if render_plan["ratio"] == "16:9" else 1080,
                1080 if render_plan["ratio"] == "16:9" else 1920,
            ),
            "motion": "template",
            "duration": round(verified_duration, 3),
            "scene_count": len(render_plan["scenes"]),
            "materials": public_materials,
            "material_generated_count": len(generated_materials),
            "material_reused_count": uploaded_count,
            "smart_plan": render_plan,
            "model": "%s@%s" % (
                report.get("template_id") or SMART_MONTAGE_TEMPLATE,
                report.get("template_version") or "1.0.0",
            ),
        }
        _smart_phase(
            job_id, "completed", status="done", video_file=output_rel,
            video_url=video_url, audio_file=audio_rel, strict=True,
        )
        return result
    except Exception:
        _cleanup_generated_materials(materials)
        if audio_rel:
            _remove_out_file(audio_rel)
        if output_rel:
            _remove_out_file(output_rel)
        _smart_phase(job_id, "failed", status="failed", error="智能成片生成失败")
        raise
    finally:
        cleanup_smart_montage_uploads(payload)


def _material_images(plan):
    from . import image as image_domain

    materials = []
    try:
        for item in plan:
            rel = item.get("file")
            source = item.get("source")
            if source == "generate":
                image_payload = {
                    "prompt": item["prompt"], "ratio": "9:16", "quality": "standard",
                    "provider": "openai", "count": 1,
                }
                try:
                    generated = image_domain.gen_image(image_payload)
                except Exception as exc:
                    # 520 来自出境中转的瞬时异常。此时整段数字人口播已经生成并计费；
                    # 只补偿重试当前图片一次，比重跑整条 HeyGen 成片的成本低得多。
                    # 已生成的前序图片保留在 materials 中，不重复调用。
                    if getattr(exc, "code", None) not in MATERIAL_IMAGE_RETRY_CODES:
                        raise
                    time.sleep(MATERIAL_IMAGE_RETRY_DELAY)
                    generated = image_domain.gen_image(image_payload)
                rel = generated.get("file")
            if not rel or not _safe_existing_image(rel):
                raise RuntimeError("分镜 %d 的素材不可用" % (int(item["scene_index"]) + 1))
            materials.append({
                "scene_index": int(item["scene_index"]),
                "prompt": item["prompt"],
                "source": source,
                "file": str(rel),
            })
        return materials
    except Exception:
        _cleanup_generated_materials(materials)
        raise


def _cleanup_material_root(job_id, state):
    root = str((state or {}).get("material_root") or "")
    if not root:
        return
    try:
        path = _frozen_material_path(job_id, root, root + "/placeholder").parent
        shutil.rmtree(str(path))
    except Exception:
        pass


def cleanup_unsubmitted_materials(job_id):
    try:
        payload, _ = _load_job_payload(job_id)
        from . import digital_human_oneclick, digital_human_v2
        if str(payload.get("pipeline") or "").strip().lower() in {
                digital_human_oneclick.PIPELINE, digital_human_v2.PIPELINE}:
            return
    except Exception:
        pass
    state = get_recovery_state(job_id)
    if str(state.get("phase") or "") in {"preparing_materials", "materials_ready"}:
        _cleanup_material_root(job_id, state)


def _prepare_frozen_materials(job_id, username, plan):
    """Finish every material before the paid video create request."""
    if not job_id:
        raise RuntimeError("文案成片素材冻结缺少任务编号")
    with _material_job_lock(job_id):
        payload, _ = _load_job_payload(job_id, username)
        state = _state_from_payload(payload)
        if _frozen_materials_valid(job_id, state, plan):
            if state.get("phase") != "materials_ready":
                state = _persist_job_state(
                    job_id, username, "materials_ready",
                    material_root=state["material_root"], materials=state["materials"],
                )
            return [dict(item) for item in state["materials"]]

        root = str(state.get("material_root") or "") or _new_material_root(job_id)
        frozen = {}
        for item in (state.get("materials") or []):
            if not isinstance(item, dict):
                continue
            try:
                path = _frozen_material_path(job_id, root, item.get("file"))
                if _readable_image_path(path):
                    frozen[int(item["scene_index"])] = dict(item)
            except Exception:
                continue
        _persist_job_state(
            job_id, username, "preparing_materials",
            material_root=root, materials=[frozen[k] for k in sorted(frozen)],
        )
        generated_originals = []
        try:
            from . import image as image_domain
            for item in plan:
                index = int(item["scene_index"])
                if index in frozen:
                    continue
                source = str(item.get("source") or "")
                if source == "asset":
                    rel = str(item.get("file") or "")
                    if not _owned_source_asset(username, rel):
                        raise PermissionError(
                            "分镜 %d 的历史素材不存在或不属于当前用户" % (index + 1))
                elif source == "generate":
                    image_payload = {
                        "prompt": item["prompt"], "ratio": "9:16",
                        "quality": "standard", "provider": "openai", "count": 1,
                    }
                    try:
                        generated = image_domain.gen_image(image_payload)
                    except Exception as exc:
                        if getattr(exc, "code", None) not in MATERIAL_IMAGE_RETRY_CODES:
                            raise
                        time.sleep(MATERIAL_IMAGE_RETRY_DELAY)
                        generated = image_domain.gen_image(image_payload)
                    rel = str(generated.get("file") or "")
                    generated_originals.append(rel)
                else:
                    raise ValueError("分镜 %d 的素材来源无效" % (index + 1))
                source_path = (OUT_DIR / rel).resolve()
                source_path.relative_to(OUT_DIR.resolve())
                frozen[index] = dict(
                    item,
                    file=_copy_frozen_material(job_id, root, item, source_path),
                )
                _persist_job_state(
                    job_id, username, "preparing_materials", material_root=root,
                    materials=[frozen[k] for k in sorted(frozen)],
                )
            ordered = [frozen[int(item["scene_index"])] for item in plan]
            state = _persist_job_state(
                job_id, username, "materials_ready",
                material_root=root, materials=ordered,
            )
            return [dict(item) for item in state["materials"]]
        except Exception:
            _cleanup_material_root(job_id, {"material_root": root})
            raise
        finally:
            for rel in generated_originals:
                try:
                    source = (OUT_DIR / rel).resolve()
                    source.relative_to(OUT_DIR.resolve())
                    source.unlink(missing_ok=True)
                except Exception:
                    pass


def _cleanup_generated_materials(materials):
    for item in materials:
        if item.get("source") != "generate":
            continue
        try:
            (OUT_DIR / item["file"]).resolve().unlink(missing_ok=True)
        except Exception:
            pass


def _scene_ranges(scenes, duration):
    weights = []
    for scene in scenes:
        line = str(scene.get("line") or "").strip()
        try:
            declared = float(str(scene.get("dur") or "").lower().replace("s", ""))
        except (TypeError, ValueError):
            declared = 0
        weights.append(declared if declared > 0 else max(1, len(line)))
    total = sum(weights) or len(scenes) or 1
    cursor, ranges = 0.0, []
    for weight in weights:
        span = duration * weight / total
        ranges.append((cursor, min(duration, cursor + span)))
        cursor += span
    return ranges


def _photo_motion_filter(width, height):
    """为静态素材随机选择轻微 Ken Burns 动效；只改变剪辑，不调用视频生成 API。"""
    motion = random.choice(PHOTO_MOTIONS)
    center = "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
    if motion == "zoom_in":
        effect = "z='min(zoom+0.0008,1.08)':" + center
    elif motion == "zoom_out":
        effect = "z='if(eq(on,0),1.08,max(1.001,zoom-0.0008))':" + center
    else:
        progress = "min(on/200\\,1)"
        axes = {
            "pan_left":  ("(iw-iw/zoom)*%s" % progress, "(ih-ih/zoom)/2"),
            "pan_right": ("(iw-iw/zoom)*(1-%s)" % progress, "(ih-ih/zoom)/2"),
            "pan_up":    ("(iw-iw/zoom)/2", "(ih-ih/zoom)*%s" % progress),
            "pan_down":  ("(iw-iw/zoom)/2", "(ih-ih/zoom)*(1-%s)" % progress),
        }
        x, y = axes[motion]
        effect = "z=1.06:x='%s':y='%s'" % (x, y)
    return "zoompan=%s:d=1:s=%dx%d:fps=25" % (effect, width, height)


def _compose_materials(video_file, scenes, materials):
    if not materials:
        return video_file
    from . import video as video_domain

    source = video_domain._resolve_out_file(video_file)
    if not source:
        raise RuntimeError("数字人口播成片文件不存在")
    duration = video_domain._probe_video_duration(video_file)
    width, height = video_domain._probe_video_size(source)
    ranges = _scene_ranges(scenes, duration)
    command = ["ffmpeg", "-y", "-i", str(source)]
    for material in materials:
        command.extend(["-loop", "1", "-i", str((OUT_DIR / material["file"]).resolve())])

    filters, previous = [], "[0:v]"
    for pos, material in enumerate(materials):
        index = material["scene_index"]
        start, end = ranges[index]
        # 每个分镜中段穿插静态素材，前后保留数字人，避免整片只剩图片。
        show_start = start + (end - start) * 0.20
        show_end = start + (end - start) * 0.78
        prepared, output = "[mat%d]" % pos, "[mix%d]" % pos
        filters.append(
            "[%d:v]scale=%d:%d:force_original_aspect_ratio=increase,"
            "crop=%d:%d,setsar=1,%s%s" %
            (pos + 1, width, height, width, height,
             _photo_motion_filter(width, height), prepared)
        )
        filters.append(
            "%s%soverlay=0:0:enable='between(t,%.3f,%.3f)'%s" %
            (previous, prepared, show_start, show_end, output)
        )
        previous = output
    output = video_domain.VIDEO_OUT_DIR / ("script_broll_%d.mp4" % int(time.time() * 1000))
    command.extend([
        "-filter_complex", ";".join(filters), "-map", previous, "-map", "0:a?",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-c:a", "copy",
        "-t", "%.3f" % duration, "-shortest", "-movflags", "+faststart", str(output),
    ])
    subprocess.run(command, check=True, timeout=900, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return video_domain._faststart_video_file(output.resolve().relative_to(OUT_DIR.resolve()).as_posix())


def get_recovery_state(job_id):
    try:
        payload, _ = _load_job_payload(job_id)
        return _state_from_payload(payload)
    except Exception as exc:
        raise ScriptToVideoRecoveryStateUnavailable(
            "文案成片恢复状态暂不可读"
        ) from exc


def _provider_base_result(state):
    result = state.get("provider_result") or {}
    return dict(result) if isinstance(result, dict) else {}


def _provider_file_exists(result):
    try:
        from . import video as video_domain
        return bool(video_domain._resolve_out_file(result.get("video_file")))
    except Exception:
        return False


def recover_paid_job_error(job_id, error, requeue):
    """Keep a possibly billed script job recoverable instead of refunding it."""
    from . import video as video_domain

    if isinstance(error, video_domain.HeyGenProviderFailed):
        return False
    payload, _ = _load_job_payload(job_id)
    from . import digital_human_oneclick, digital_human_v2
    if str(payload.get("pipeline") or "").strip().lower() in {
            digital_human_oneclick.PIPELINE, digital_human_v2.PIPELINE}:
        return False
    state = get_recovery_state(job_id)
    phase = str(state.get("phase") or "")
    provider_id = str(state.get("provider_video_id") or "").strip()
    if phase == "provider_submitting" and not provider_id:
        # The create response may have been lost.  Re-POSTing would risk a
        # second charge, while refunding may give away a completed provider job.
        return True
    if phase in {"provider_submitted", "provider_completed", "composing", "done"}:
        if provider_id or _provider_file_exists(_provider_base_result(state)):
            requeue(job_id)
            return True
    return isinstance(error, ScriptToVideoRecoveryRequired)


def reclaim_orphaned_jobs(requeue, logger=print):
    """Requeue durable script jobs and hold ambiguous create requests."""
    try:
        with closing(jdb()) as conn:
            rows = conn.execute(
                "SELECT id,payload FROM jobs WHERE kind='script_to_video' AND status='running'"
            ).fetchall()
    except Exception as exc:
        raise ScriptToVideoRecoveryStateUnavailable(
            "文案成片启动恢复状态暂不可读"
        ) from exc
    handled = 0
    held = set()

    def hold(row, reason):
        job_id = int(row["id"])
        held.add(job_id)
        logger(
            "[script-to-video] recovery state %s; hold job=%s"
            % (reason, job_id), flush=True,
        )

    for row in rows:
        try:
            payload = json.loads(row["payload"] or "")
        except Exception:
            hold(row, "invalid-json")
            continue
        if not isinstance(payload, dict):
            hold(row, "invalid-payload")
            continue
        from . import digital_human_oneclick, digital_human_v2
        if str(payload.get("pipeline") or "").strip().lower() in {
                digital_human_oneclick.PIPELINE, digital_human_v2.PIPELINE}:
            if requeue(row["id"]):
                handled += 1
            continue
        raw_state = payload.get("_script_to_video_state")
        if not isinstance(raw_state, dict):
            hold(row, "missing-or-invalid")
            continue
        state = dict(raw_state)
        phase = str(state.get("phase") or "")
        provider_id = str(state.get("provider_video_id") or "").strip()
        if not phase:
            hold(row, "missing-phase")
            continue
        if phase == "provider_submitting" and not provider_id:
            hold(row, "provider-create-unknown")
            continue
        if phase == "provider_submitted" and not provider_id:
            hold(row, "provider-id-missing")
            continue
        if phase in {"provider_completed", "composing", "done"}:
            provider_result = _provider_base_result(state)
            if not provider_id and not _provider_file_exists(provider_result):
                hold(row, "provider-result-missing")
                continue
        safe = phase in {
            "preparing_materials", "materials_ready", "provider_submitting",
            "provider_submitted", "provider_completed", "composing", "done",
        }
        if not safe:
            hold(row, "unknown-phase")
            continue
        if requeue(row["id"]):
            handled += 1
    return {"handled": handled, "held": held}


def _gen_talking(username, scenes, payload):
    """Freeze materials, submit once, then compose from durable local state."""
    lines = [(scene.get("line") or "").strip() for scene in scenes]
    lines = [line for line in lines if line]
    if not lines:
        raise ValueError("脚本中没有口播文案，请先生成脚本")
    full_text = "\n\n".join(lines)

    from . import video as video_domain

    want_subtitle = payload.get("subtitle", True)
    material_plan = payload.get("material_plan") or []
    job_id = payload.get("_job_id")
    runtime_managed = False
    current_payload = {}
    if job_id:
        try:
            current_payload, _ = _load_job_payload(job_id, username)
            runtime_managed = True
        except PermissionError:
            raise
        except Exception as exc:
            # Direct unit/library callers historically supplied a display-only
            # job id without a jobs database.  Real workers always have a
            # persisted row and therefore never take this compatibility path.
            if "不存在" not in str(exc) and "no such table" not in str(exc).lower():
                raise
    try:
        avatar = _preflight_talking_avatar(username, payload.get("avatar_id"))
    except video_domain.HeyGenMediaInputError as exc:
        if runtime_managed:
            _persist_job_state(
                job_id, username, "preparing_materials",
                input_error=exc.audit_summary(),
            )
        raise
    try:
        materials = (
            _prepare_frozen_materials(job_id, username, material_plan)
            if runtime_managed else _material_images(material_plan)
        )
    except ScriptToVideoMediaInputError as exc:
        if runtime_managed:
            _persist_job_state(
                job_id, username, "preparing_materials",
                input_error=exc.audit_summary(),
            )
        raise
    except PermissionError:
        if runtime_managed:
            _persist_job_state(
                job_id, username, "preparing_materials",
                input_error={
                    "stage": "media_preflight",
                    "category": "material",
                    "code": "material_missing_or_unowned",
                },
            )
        raise
    if runtime_managed:
        current_payload, _ = _load_job_payload(job_id, username)
    state = _state_from_payload(current_payload)
    phase = str(state.get("phase") or "")
    final_result = state.get("final_result") or {}
    if phase == "done" and isinstance(final_result, dict) and _provider_file_exists(final_result):
        return dict(final_result)
    if phase == "provider_submitting" and not state.get("provider_video_id"):
        raise ScriptToVideoRecoveryRequired(
            "供应商提交结果待核对，已停止重复提交"
        )

    result = _provider_base_result(state) if runtime_managed else {}
    if not _provider_file_exists(result):
        def on_prepared(data):
            _persist_job_state(
                job_id, username, "materials_ready",
                audio_file=data.get("audio_file"), image_file=data.get("image_file"),
            )

        def on_submitting(data):
            _persist_job_state(
                job_id, username, "provider_submitting",
                provider=data.get("provider"),
                provider_transport=data.get("provider_transport"),
                image_asset_id=data.get("image_asset_id"),
                audio_asset_id=data.get("audio_asset_id"),
                actual_resolution=data.get("actual_resolution"),
            )

        def on_submitted(data):
            _persist_job_state(
                job_id, username, "provider_submitted",
                provider=data.get("provider"),
                provider_transport=data.get("provider_transport"),
                provider_video_id=data.get("provider_video_id"),
                image_asset_id=data.get("image_asset_id"),
                audio_asset_id=data.get("audio_asset_id"),
                actual_resolution=data.get("actual_resolution"),
            )

        def on_rejected(_data):
            _persist_job_state(
                job_id, username, "materials_ready",
                provider=None, provider_video_id=None,
                provider_transport=None, actual_resolution=None,
                image_asset_id=None, audio_asset_id=None,
            )
            video_domain.update_video_asset_phase(
                job_id, "materials_ready", strict=True,
            )

        def on_completed(data):
            provider_result = {
                key: data.get(key) for key in (
                    "video_id", "video_file", "video_url", "source_video_url",
                    "thumbnail_url", "duration", "provider", "image_file",
                    "image_url", "image_asset_id", "audio_asset_id",
                    "provider_transport", "actual_resolution",
                ) if data.get(key) is not None
            }
            _persist_job_state(
                job_id, username, "provider_completed",
                provider_video_id=data.get("video_id"),
                provider_result=provider_result,
            )
            video_domain.update_video_asset_phase(
                job_id, "provider_completed", strict=True,
                provider_video_id=data.get("video_id"),
                video_file=data.get("video_file"),
                source_video_url=data.get("source_video_url"),
            )

        lifecycle = {
            "state": state,
            "on_prepared": on_prepared,
            "on_submitting": on_submitting,
            "on_rejected": on_rejected,
            "on_submitted": on_submitted,
            "on_completed": on_completed,
        }
        video_payload = {
                "_username": username,
                "_job_id": job_id,
                "mode": "text",
                "text": full_text,
                "avatar_id": str(avatar["id"]),
                "voice": payload.get("voice") or "S_d21F8OR62",
                "resolution": payload.get("resolution") or "720p",
                "ratio": payload.get("ratio") or "9:16",
                "motion": payload.get("motion") or "medium",
                "motion_prompt": payload.get("motion_prompt") or "",
                "subtitle": False if material_plan else want_subtitle,
            }
        try:
            result = (
                video_domain.gen_video(video_payload, provider_lifecycle=lifecycle)
                if runtime_managed else video_domain.gen_video(video_payload)
            )
            if runtime_managed:
                _persist_job_state(
                    job_id, username, "provider_completed",
                    provider_video_id=result.get("provider_video_id"),
                    provider_result=result,
                )
        except video_domain.HeyGenProviderFailed:
            raise
        except BaseException as exc:
            latest = get_recovery_state(job_id) if runtime_managed else {}
            if runtime_managed and isinstance(exc, video_domain.HeyGenMediaInputError):
                _persist_job_state(
                    job_id, username,
                    str(latest.get("phase") or "materials_ready"),
                    input_error=exc.audit_summary(),
                )
            if str(latest.get("phase") or "") in {
                    "provider_submitting", "provider_submitted", "provider_completed"}:
                raise ScriptToVideoRecoveryRequired(str(exc)[:220]) from exc
            raise

    if runtime_managed:
        _persist_job_state(
            job_id, username, "composing",
            provider_video_id=(result.get("provider_video_id") or result.get("video_id")),
            provider_result=result,
        )
    result.setdefault("provider_video_file", result.get("video_file"))
    result.setdefault("provider_video_url", result.get("video_url"))
    try:
        if materials:
            composed = _compose_materials(result.get("video_file"), scenes, materials)
            if want_subtitle:
                composed = video_domain.burn_subtitle(
                    composed, known_text=full_text,
                    style_key=payload.get("subtitle_style") or "white",
                    job_id=payload.get("_job_id"),
                    position=payload.get("subtitle_position") or "bottom",
                )
            result["plain_video_file"] = result.get("video_file")
            result["video_file"] = composed
            result["video_url"] = video_domain.public_url(composed, "video/mp4", private=True)
    except Exception as exc:
        raise ScriptToVideoRecoveryRequired(
            "基础成片已保留，本地合成待恢复: %s" % str(exc)[:180]
        ) from exc
    result.update({
        "type": "script_to_video",
        "scene_count": len(scenes),
        "pipeline": "talking_with_materials" if material_plan else "talking",
        "materials": materials,
        "material_generated_count": sum(1 for item in materials if item["source"] == "generate"),
        "material_reused_count": sum(1 for item in materials if item["source"] == "asset"),
    })
    if runtime_managed:
        _persist_job_state(
            job_id, username, "done",
            provider_video_id=(result.get("provider_video_id") or result.get("video_id")),
            final_result=result,
        )
    return result


def _gen_drama(username, scenes, payload):
    """剧情模式保持现有果肉视频链路。"""
    descs = [(scene.get("scene") or "").strip() for scene in scenes]
    descs = [desc for desc in descs if desc]
    if not descs:
        raise ValueError("脚本中没有画面描述，请先生成脚本")
    from .video import gen_xiaole_video
    result = gen_xiaole_video({
        "_username": username,
        "_job_id": payload.get("_job_id"),
        "channel": "grok",
        "prompt": "、".join(descs) + "。连贯运镜，电影质感，竖屏",
        "ratio": payload.get("ratio") or "9:16",
        "duration": payload.get("duration") or 10,
        "model": payload.get("model") or "grok-imagine-video",
        "resolution": payload.get("resolution") or "720p",
    })
    result.update({"type": "script_to_video", "scene_count": len(scenes), "pipeline": "grok"})
    return result


def _get_first_avatar(username):
    try:
        with closing(adb()) as conn:
            row = conn.execute(
                "SELECT id, name, image_file FROM avatars WHERE username=?"
                " AND status!='deleted' ORDER BY id ASC LIMIT 1",
                (username,),
            ).fetchone()
        return dict(row) if row else None
    except Exception:
        return None


def _preflight_talking_avatar(username, avatar_id=None):
    from . import video as video_domain

    avatar = (
        video_domain.get_video_avatar(username, str(avatar_id))
        if avatar_id else _get_first_avatar(username)
    )
    if not avatar:
        raise ValueError("你还没有创建数字人形象。请先在视频页上传人物照片创建形象。")
    image_file = str(avatar.get("image_file") or "").strip()
    if not image_file:
        raise video_domain.HeyGenMediaInputError(
            "avatar", "avatar_missing", "数字人形象文件不存在",
        )
    video_domain.preflight_heygen_image_file(image_file, "avatar")
    return avatar


HANDLERS = {"script_to_video": gen_script_to_video}
