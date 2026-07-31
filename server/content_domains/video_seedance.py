# -*- coding: utf-8 -*-
"""Official Volcengine Ark Seedance video adapter."""

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request


ARK_API_KEY = os.environ.get("ARK_API_KEY", "").strip()
ARK_BASE = os.environ.get(
    "ARK_BASE", "https://ark.cn-beijing.volces.com/api/v3"
).rstrip("/")
SEEDANCE_MODEL = os.environ.get(
    "ARK_SEEDANCE_MODEL", "doubao-seedance-2-0-260128"
).strip()
SEEDANCE_TIMEOUT = int(os.environ.get("ARK_SEEDANCE_TIMEOUT", "1200") or 1200)
SEEDANCE_POLL_INTERVAL = int(
    os.environ.get("ARK_SEEDANCE_POLL_INTERVAL", "10") or 10
)
RATIOS = {"21:9", "16:9", "4:3", "1:1", "3:4", "9:16", "adaptive"}
RESOLUTIONS = {"480p", "720p", "1080p"}
TRANSIENT_HTTP_CODES = {408, 429} | set(range(500, 600))
TRANSIENT_BACKOFF = (5, 10, 20, 30)


class CreateOutcomeUnknown(RuntimeError):
    """The paid create request may have succeeded and must not be repeated."""


class SeedanceRejected(RuntimeError):
    """The provider rejected the create request before returning a task id."""


class SeedanceProviderFailed(RuntimeError):
    """The provider returned a terminal failure for a known task id."""


class TransientSeedanceError(RuntimeError):
    """A known task may be queried again safely."""


def available():
    return bool(ARK_API_KEY)


def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _safe_text(value, limit=500):
    text = str(value or "")
    if ARK_API_KEY:
        text = re.sub(re.escape(ARK_API_KEY), "***", text, flags=re.IGNORECASE)
    text = re.sub(
        r"(?i)\b(authorization\s*:\s*bearer|bearer|api[_-]?key|access[_-]?token|token|secret)"
        r"\s*[:=]?\s*[^\s,;\"']+",
        lambda match: match.group(1) + " ***",
        text,
    )
    return text[:limit]


def _payload_detail(payload):
    if not isinstance(payload, dict):
        return _safe_text(payload)
    detail = payload.get("error") or payload.get("message") or payload.get("detail")
    if isinstance(detail, dict):
        detail = (
            detail.get("message")
            or detail.get("detail")
            or detail.get("code")
            or detail
        )
    return _safe_text(detail or payload)


def _error_detail(exc):
    try:
        raw = exc.read().decode("utf-8", "replace")[:2000]
        return _payload_detail(json.loads(raw or "{}"))
    except Exception:
        return _safe_text(exc)


def _request_json(opener, method, path, body=None, timeout=90):
    if not ARK_API_KEY:
        raise ValueError("Seedance 官方视频未配置（ARK_API_KEY）")
    url = ARK_BASE + "/" + str(path or "").lstrip("/")
    headers = {
        "Authorization": "Bearer " + ARK_API_KEY,
        "Accept": "application/json",
        "User-Agent": "huangque-content/1.0",
    }
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers=headers, method=method
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = _error_detail(exc)
        message = "Seedance 官方接口失败: HTTP %s %s" % (exc.code, detail)
        if method == "POST" and exc.code in TRANSIENT_HTTP_CODES - {429}:
            raise CreateOutcomeUnknown(
                "Seedance 提交结果未知，请勿重复提交: " + message
            ) from exc
        if method == "GET" and exc.code in TRANSIENT_HTTP_CODES:
            raise TransientSeedanceError(message) from exc
        if method == "POST":
            raise SeedanceRejected(message) from exc
        raise RuntimeError(message) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        message = "Seedance 官方视频网络异常: " + _safe_text(exc, 300)
        if method == "POST":
            raise CreateOutcomeUnknown(
                "Seedance 提交结果未知，请勿重复提交: " + message
            ) from exc
        raise TransientSeedanceError(message) from exc
    try:
        payload = json.loads(raw.decode("utf-8", "replace") or "{}")
    except (UnicodeError, ValueError) as exc:
        if method == "POST":
            raise CreateOutcomeUnknown(
                "Seedance 提交结果未知，请勿重复提交：响应无法解析"
            ) from exc
        raise TransientSeedanceError("Seedance 查询返回了无效 JSON") from exc
    if not isinstance(payload, dict):
        if method == "POST":
            raise CreateOutcomeUnknown("Seedance 提交结果未知：响应格式异常")
        raise TransientSeedanceError("Seedance 查询响应格式异常")
    return payload


def _reference_item(url):
    url = str(url or "").strip()
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https", "asset"}:
        raise ValueError("Seedance 参考图必须是公网 URL 或已授权 asset:// 素材")
    return {
        "type": "image_url",
        "image_url": {"url": url},
        "role": "reference_image",
    }


def _build_payload(
    model,
    prompt,
    duration,
    ratio,
    resolution,
    generate_audio,
    reference_images=None,
):
    model = str(model or "").strip()
    if model != SEEDANCE_MODEL:
        raise ValueError("不支持的 Seedance 官方模型")
    prompt = str(prompt or "").strip()
    if not prompt:
        raise ValueError("请输入 Seedance 视频提示词")
    try:
        duration = int(duration)
    except (TypeError, ValueError):
        raise ValueError("Seedance 视频时长必须为 4～15 秒") from None
    if duration < 4 or duration > 15:
        raise ValueError("Seedance 视频时长必须为 4～15 秒")
    ratio = str(ratio or "").strip()
    if ratio not in RATIOS:
        raise ValueError("Seedance 不支持该画面比例")
    resolution = str(resolution or "").strip().lower()
    if resolution not in RESOLUTIONS:
        raise ValueError("Seedance 不支持该分辨率")
    if not isinstance(generate_audio, bool):
        raise ValueError("Seedance 声音选项必须为布尔值")
    refs = list(reference_images or [])
    if len(refs) > 9:
        raise ValueError("Seedance 最多支持 9 张参考图")
    content = [{"type": "text", "text": prompt}]
    content.extend(_reference_item(url) for url in refs)
    return {
        "model": model,
        "content": content,
        "resolution": resolution,
        "ratio": ratio,
        "duration": duration,
        "generate_audio": generate_audio,
        "watermark": False,
    }


def _poll(
    opener,
    task_id,
    model,
    duration,
    ratio,
    resolution,
    generate_audio,
    job_id=None,
    heartbeat=None,
    now=None,
    sleep=None,
):
    now = now or time.time
    sleep = sleep or time.sleep
    deadline = now() + SEEDANCE_TIMEOUT
    transient_attempt = 0
    last_transient = None
    while now() < deadline:
        try:
            payload = _request_json(
                opener,
                "GET",
                "/contents/generations/tasks/"
                + urllib.parse.quote(str(task_id), safe=""),
                timeout=60,
            )
            transient_attempt = 0
            last_transient = None
        except TransientSeedanceError as exc:
            last_transient = exc
            if heartbeat:
                heartbeat(
                    job_id,
                    "seedance_retrying",
                    provider_video_id=task_id,
                    model=model,
                    error=_safe_text(exc, 300),
                )
            delay = TRANSIENT_BACKOFF[
                min(transient_attempt, len(TRANSIENT_BACKOFF) - 1)
            ]
            transient_attempt += 1
            if now() + delay >= deadline:
                break
            sleep(delay)
            continue
        status = str(payload.get("status") or "").strip().lower()
        if heartbeat:
            heartbeat(
                job_id,
                "seedance_" + (status or "unknown"),
                provider_video_id=task_id,
                model=str(payload.get("model") or model),
                error="",
            )
        if status == "succeeded":
            content = payload.get("content") or {}
            video_url = (
                str(content.get("video_url") or "").strip()
                if isinstance(content, dict)
                else ""
            )
            if not video_url:
                raise RuntimeError("Seedance 视频已完成但未返回成片 URL")
            return {
                "request_id": task_id,
                "model": str(payload.get("model") or model),
                "source_video_url": video_url,
                "duration": payload.get("duration") or duration,
                "ratio": str(payload.get("ratio") or ratio),
                "resolution": str(payload.get("resolution") or resolution),
                "generate_audio": (
                    payload.get("generate_audio")
                    if isinstance(payload.get("generate_audio"), bool)
                    else generate_audio
                ),
            }
        if status in {"failed", "expired", "cancelled", "canceled"}:
            raise SeedanceProviderFailed(
                "Seedance 视频生成失败: " + _payload_detail(payload)
            )
        if status not in {"queued", "running"}:
            raise SeedanceProviderFailed(
                "Seedance 官方视频返回未知状态: " + (status or "空")
            )
        sleep(SEEDANCE_POLL_INTERVAL)
    if last_transient:
        raise TimeoutError(
            "Seedance 视频查询超时: " + _safe_text(last_transient, 240)
        )
    raise TimeoutError("Seedance 视频生成超时")


def generate(
    model=SEEDANCE_MODEL,
    prompt="",
    duration=5,
    ratio="9:16",
    resolution="720p",
    generate_audio=True,
    reference_images=None,
    job_id=None,
    heartbeat=None,
    now=None,
    sleep=None,
):
    """Create one paid task, then poll only the returned task id."""
    payload = _build_payload(
        model,
        prompt,
        duration,
        ratio,
        resolution,
        generate_audio,
        reference_images,
    )
    opener = _opener()
    created = _request_json(
        opener,
        "POST",
        "/contents/generations/tasks",
        payload,
        timeout=120,
    )
    task_id = str(created.get("id") or "").strip()
    if not task_id:
        raise CreateOutcomeUnknown(
            "Seedance 提交结果未知，请勿重复提交：未返回 task id"
        )
    if heartbeat:
        heartbeat(
            job_id,
            "seedance_" + str(created.get("status") or "queued").lower(),
            provider_video_id=task_id,
            model=model,
            error="",
        )
    return _poll(
        opener,
        task_id,
        model,
        payload["duration"],
        payload["ratio"],
        payload["resolution"],
        payload["generate_audio"],
        job_id=job_id,
        heartbeat=heartbeat,
        now=now,
        sleep=sleep,
    )
