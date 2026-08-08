# -*- coding: utf-8 -*-
"""一键成片：现有数字人口播 + 用户图片资产/按需生图 + FFmpeg 自动穿插。"""
import hashlib
import hmac
import json
import math
import os
import pathlib
import random
import re
import shutil
import subprocess
import tempfile
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
