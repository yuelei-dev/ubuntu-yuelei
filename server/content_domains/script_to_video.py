# -*- coding: utf-8 -*-
"""一键成片：现有数字人口播 + 用户图片资产/按需生图 + FFmpeg 自动穿插。"""
import hashlib
import hmac
import json
import random
import re
import subprocess
import time
import uuid

from .core import OUT_DIR, SMART_MONTAGE_MAX_RUNTIME, adb, closing, jdb

MAX_MATERIAL_SCENES = 8
PHOTO_MOTIONS = ("zoom_in", "zoom_out", "pan_left", "pan_right", "pan_up", "pan_down")
MATERIAL_IMAGE_RETRY_CODES = {520}
MATERIAL_IMAGE_RETRY_DELAY = 2
SMART_MONTAGE_PIPELINE = "smart_montage"
SMART_MONTAGE_PLAN_PATH = "/api/gen/script_to_video/plan"
SMART_MONTAGE_TEMPLATE = "smart-montage-v1"
SMART_MONTAGE_DUPLICATE_RETRIES = 2
SMART_MONTAGE_SUBMISSION_FIELDS = {
    "pipeline", "mode", "copy", "script", "text", "style", "ratio",
    "voice", "plan_digest",
}
_PLAN_DIGEST_RE = re.compile(r"^[0-9a-fA-F]{64}$")


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
        return path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
    except Exception:
        return False


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


def prepare_script_to_video_payload(payload, username):
    """提交扣点前冻结素材计划，保证能一次算清总价且不发生生成到一半欠费。"""
    body = dict(payload or {})
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
        body.update({
            "pipeline": SMART_MONTAGE_PIPELINE,
            "mode": SMART_MONTAGE_PIPELINE,
            "copy": frozen["copy"],
            "style": frozen["style"],
            "ratio": frozen["ratio"],
            "duration": frozen["duration_seconds"],
            "scenes": scenes,
            "smart_plan": frozen,
            "material_plan": [
                {
                    "scene_index": index,
                    "prompt": _scene_prompt({"scene": scene.get("image_prompt")}),
                    "source": "generate",
                    "file": None,
                }
                for index, scene in enumerate(scenes)
            ],
            "material_generate_count": len(scenes),
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


def gen_script_to_video(payload):
    """由 run_job 调用，走标准 job 生命周期。"""
    username = (payload.get("_username") or "").strip()
    if str(payload.get("pipeline") or "").strip().lower() == SMART_MONTAGE_PIPELINE:
        return _gen_smart_montage(username, payload)
    scenes = payload.get("scenes") or []
    style = (payload.get("style") or "口播").strip()
    if style == "剧情":
        return _gen_drama(username, scenes, payload)
    return _gen_talking(username, scenes, payload)


def dispatch_http(handler, method, verify_token, must_change_password):
    """Authenticated, read-only smart-montage planning endpoint."""
    path = handler.path.split("?", 1)[0]
    if path != SMART_MONTAGE_PLAN_PATH:
        return False
    if method != "POST":
        handler._method_not_allowed()
        return True
    user = verify_token(handler._token())
    if not user:
        handler._send(401, {"detail": "未登录或登录已过期"})
        return True
    if must_change_password(user):
        handler._send(403, {"detail": "请先修改初始密码"})
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


def _smart_phase(job_id, phase, **fields):
    try:
        from . import video as video_domain
        video_domain.update_video_asset_phase(job_id, phase, **fields)
    except Exception:
        # 阶段信息仅用于进度展示，不能让一次已付费生成因状态写入失败而中断。
        pass


def _smart_voiceover_text(plan):
    """Narrate the complete user-confirmed copy; never silently summarize it."""
    from .script_video_montage import MAX_COPY_CHARACTERS

    copy = re.sub(r"\s+", " ", str(plan.get("copy") or "")).strip()
    if not copy or len(copy) > MAX_COPY_CHARACTERS:
        raise ValueError("智能成片文案必须在 1-%d 个字符内" % MAX_COPY_CHARACTERS)
    return copy


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


def _fit_voiceover_to_plan(audio_rel, voiceover, duration, planned_duration):
    """Speed up an overlong narration so the user-confirmed plan stays frozen."""
    planned_duration = float(planned_duration or 0)
    duration = float(duration or 0)
    if duration <= 0 or duration <= planned_duration - 0.35:
        return audio_rel, voiceover, duration
    target = max(1.0, planned_duration - 0.6)
    speed_factor = duration / target
    fitted_rel = "audio/aud_fit_%s.mp3" % uuid.uuid4().hex
    fitted = (OUT_DIR / fitted_rel).resolve()
    fitted.relative_to(OUT_DIR.resolve())
    fitted.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", str(voiceover), "-vn",
                "-filter:a", _atempo_filter(speed_factor),
                "-c:a", "libmp3lame", "-q:a", "3", str(fitted),
            ],
            check=True, timeout=180, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        fitted_duration = _probe_media_duration(fitted)
        if fitted_duration <= 0 or fitted_duration > planned_duration - 0.15:
            raise RuntimeError("旁白时长校准结果无效")
    except Exception as exc:
        try:
            fitted.unlink(missing_ok=True)
        except OSError:
            pass
        raise RuntimeError("旁白时长校准失败") from exc
    _remove_out_file(audio_rel)
    return fitted_rel, fitted, fitted_duration


def _retime_smart_plan(plan, voiceover_duration=0):
    frozen = json.loads(json.dumps(plan, ensure_ascii=False))
    planned = float(frozen.get("duration_seconds") or 10)
    voiceover_duration = max(0.0, float(voiceover_duration or 0))
    if voiceover_duration > planned - 0.1:
        raise ValueError("旁白时长超过已确认方案")
    duration = round(max(10.0, min(90.0, planned)), 3)
    scenes = frozen.get("scenes") or []
    if not 3 <= len(scenes) <= 20:
        raise ValueError("智能成片分镜数量无效")
    total_ms = int(round(duration * 1000))
    weights = [max(250, int(round(float(scene.get("duration_seconds") or 0) * 1000))) for scene in scenes]
    weight_sum = sum(weights)
    raw = [total_ms * weight / float(weight_sum) for weight in weights]
    units = [int(value) for value in raw]
    remainder = total_ms - sum(units)
    order = sorted(range(len(units)), key=lambda index: (-(raw[index] - units[index]), index))
    for index in order[:remainder]:
        units[index] += 1
    cursor = 0
    for scene, scene_ms in zip(scenes, units):
        scene["start_seconds"] = round(cursor / 1000.0, 3)
        scene["duration_seconds"] = round(scene_ms / 1000.0, 3)
        cursor += scene_ms
    frozen["duration_seconds"] = duration
    frozen["scene_count"] = len(scenes)
    return frozen


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


def _smart_material_images(plan, deadline=None):
    from . import image as image_domain

    materials, hashes = [], set()
    ratio = plan.get("ratio") or "16:9"
    scenes = plan.get("scenes") or []
    try:
        for position, scene in enumerate(scenes):
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
        narration = _smart_voiceover_text(plan)
        audio_result = audio_domain.gen_audio({
            "_username": username,
            "text": narration,
            "voice": voice,
            "speed": "normal",
            "pitch": 0,
            "volume": 0,
        })
        _smart_deadline_remaining(deadline, "生成旁白后")
        audio_rel = audio_result.get("file") if isinstance(audio_result, dict) else None
        voiceover = _out_file(audio_rel, {".aac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav"})
        if not voiceover:
            raise RuntimeError("旁白生成结果不可用")
        duration_ms = audio_result.get("duration_ms") if isinstance(audio_result, dict) else None
        voiceover_duration = float(duration_ms or 0) / 1000.0
        if voiceover_duration <= 0:
            voiceover_duration = _probe_media_duration(voiceover)
        audio_rel, voiceover, voiceover_duration = _fit_voiceover_to_plan(
            audio_rel, voiceover, voiceover_duration,
            float(plan.get("duration_seconds") or 10),
        )
        _smart_deadline_remaining(deadline, "处理旁白后")
        render_plan = _retime_smart_plan(plan, voiceover_duration)

        _smart_phase(job_id, "generating_assets", audio_file=audio_rel, voice=voice)
        materials = _smart_material_images(render_plan, deadline=deadline)

        output_rel = "video/script_montage_%s.mp4" % uuid.uuid4().hex
        output_path = (OUT_DIR / output_rel).resolve()
        output_path.relative_to(OUT_DIR.resolve())
        output_path.parent.mkdir(parents=True, exist_ok=True)
        material_paths = [_out_file(item["file"]) for item in materials]
        if any(path is None for path in material_paths):
            raise RuntimeError("智能成片素材在渲染前不可用")

        _smart_phase(job_id, "rendering", image_file=materials[0]["file"])
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
        result = {
            "type": "script_to_video",
            "pipeline": SMART_MONTAGE_PIPELINE,
            "mode": SMART_MONTAGE_PIPELINE,
            "status": "done",
            "phase": "completed",
            "video_file": output_rel,
            "video_url": video_url,
            "audio_file": str(audio_rel),
            "image_file": materials[0]["file"],
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
            "materials": materials,
            "material_generated_count": len(materials),
            "material_reused_count": 0,
            "smart_plan": render_plan,
            "model": "%s@%s" % (
                report.get("template_id") or SMART_MONTAGE_TEMPLATE,
                report.get("template_version") or "1.0.0",
            ),
        }
        _smart_phase(
            job_id, "completed", status="done", video_file=output_rel,
            video_url=video_url, audio_file=audio_rel,
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


def _gen_talking(username, scenes, payload):
    """先生成完整数字人口播，再按分镜在中段穿插用户资产或新生成静态图。"""
    lines = [(scene.get("line") or "").strip() for scene in scenes]
    lines = [line for line in lines if line]
    if not lines:
        raise ValueError("脚本中没有口播文案，请先生成脚本")
    full_text = "\n\n".join(lines)

    avatar_id = payload.get("avatar_id")
    if avatar_id:
        from .video import get_video_avatar
        avatar = get_video_avatar(username, str(avatar_id))
    else:
        avatar = _get_first_avatar(username)
    if not avatar:
        raise ValueError("你还没有创建数字人形象。请先在视频页上传人物照片创建形象。")

    from . import video as video_domain

    want_subtitle = payload.get("subtitle", True)
    material_plan = payload.get("material_plan") or []
    result = video_domain.gen_video({
        "_username": username,
        "_job_id": payload.get("_job_id"),
        "mode": "text",
        "text": full_text,
        "avatar_id": str(avatar["id"]),
        "voice": payload.get("voice") or "S_d21F8OR62",
        "resolution": payload.get("resolution") or "720p",
        "ratio": payload.get("ratio") or "9:16",
        "motion": payload.get("motion") or "medium",
        "motion_prompt": payload.get("motion_prompt") or "",
        "subtitle": False if material_plan else want_subtitle,
    })
    materials = _material_images(material_plan)
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
    except Exception:
        _cleanup_generated_materials(materials)
        raise
    result.update({
        "type": "script_to_video",
        "scene_count": len(scenes),
        "pipeline": "talking_with_materials" if material_plan else "talking",
        "materials": materials,
        "material_generated_count": sum(1 for item in materials if item["source"] == "generate"),
        "material_reused_count": sum(1 for item in materials if item["source"] == "asset"),
    })
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


HANDLERS = {"script_to_video": gen_script_to_video}
