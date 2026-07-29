# -*- coding: utf-8 -*-
"""爆款拆解：竞品视频链接 → 下载 → 抽帧 → ASR → 智谱多模态（GPT 安全回退）→ 分镜脚本"""
import os, json, time, base64, tempfile, subprocess, shutil, math, re
import http.client
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


def gen_breakdown(payload):
    """下载视频 → 抽帧 → ASR → GPT-4o 多模态分析 → 分镜拆解。
    由 run_job 调用，走标准 job 生命周期（扣点/退点/reaper 全自动）。
    支持单个 url 或批量 urls（≤5 条，顺序处理）。"""
    import tikhub
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
    for idx, url in enumerate(urls):
        _heartbeat(job_id, "batch_%d_%d" % (idx + 1, len(urls)))
        try:
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
        if is_reverse:
            frame_dir, frames = _extract_frames(
                tmp_video.name, frame_count, duration,
                scale_width=1024, min_frames=8,
            )
        else:
            frame_dir, frames = _extract_frames(
                tmp_video.name, frame_count, duration, scale_width=512,
            )
        model_frames = _pair_reverse_frames(frame_dir, frames) if is_reverse else frames

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
        frame_thumbnails = _frame_thumbnails(frames)
        if mode == _BREAKDOWN_MODE_REVERSE_PROMPT:
            prompt = _reverse_prompt_from_frames(title, duration, platform, script_text, model_frames)
            return {
                "type": "breakdown_reverse",
                "source_url": url,
                "source_title": title,
                "source_platform": platform,
                "duration": duration,
                "prompt": prompt,
                "frame_count": len(frames or []),
                "frame_thumbnails": frame_thumbnails,
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
            "frame_thumbnails": frame_thumbnails,
        }
    finally:
        if tmp_video:
            try: os.unlink(tmp_video.name)
            except: pass
        if frame_dir:
            try: shutil.rmtree(frame_dir)
            except: pass


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
        "请严格输出 JSON：{\"scenes\":[{\"dur\":\"3s\",\"scene\":\"详细画面描述(60-100字)\",\"line\":\"口播台词\"}],"
        "\"analysis\":\"视频主题、叙事结构、情绪与转化目的综合分析(100-160字)\"}，"
        "只输出 JSON 本身，不要解释、不要 markdown 代码块。"
        "4-6 个分镜，各 dur 之和≈总时长；每个 scene 用 60-100 字描述一个可直接拍摄或生成的完整镜头，"
        "必须结合关键帧可见内容，至少写清以下六类细节中的五类："
        "①主体外观、服装或产品特征；②动作起点、过程、结果及与道具的互动；③表情、视线和身体姿态；"
        "④场景环境、关键道具及前中后景关系；⑤景别、机位、构图和推进/跟随/摇移等运镜；"
        "⑥光线方向、明暗层次、色调和画面氛围。不要只写“人物出现”“展示产品”等笼统结论。"
        "line 是原视频对应的口播内容。"
        "若原视频没有人物口播（纯音乐/歌舞/背景乐），或上方口播文案实为歌词、听写乱码、与画面无关的内容，"
        "所有 line 输出空串\"\"，不要编造台词。"
    )
    sysmsg = (
        "你是黄雀传媒资深短视频编导。分析视频关键帧和口播，拆解为简洁的分镜脚本，同时输出一份视频内容综合分析。"
        "只输出 JSON，不要多余内容。"
    )
    raw = _chat_multimodal(
        sysmsg, usermsg, frames, temp=0.2, max_tokens=3200,
    )
    try:
        return _validate_scene_breakdown(_parse_breakdown_json(raw))
    except ValueError as first_error:
        _log_breakdown_parse_failure("zhipu-primary", raw, first_error)

    compact_msg = (
        context + "\n\n"
        "上一次输出未形成完整 JSON。请重新分析并只返回一个完整、可解析的 JSON 对象，禁止代码围栏、解释和重复内容。"
        "固定输出 4 个分镜，格式为："
        "{\"scenes\":[{\"dur\":\"4s\",\"scene\":\"具体画面\",\"line\":\"对应口播或空串\"}],"
        "\"analysis\":\"80-150字综合分析\"}。"
        "每个 scene 50-80 字，至少写清主体特征、连续动作、场景道具、构图运镜和光影氛围；"
        "不得照抄“具体画面”“对应口播”“画面描述”"
        "“口播台词”等格式示例。无人物口播时所有 line 必须为空串。务必闭合全部引号、数组和大括号。"
    )
    raw = _chat_multimodal(
        sysmsg, compact_msg, frames, temp=0.1, max_tokens=2400,
    )
    try:
        return _validate_scene_breakdown(_parse_breakdown_json(raw))
    except ValueError as retry_error:
        _log_breakdown_parse_failure("zhipu-compact", raw, retry_error)

    raw = _chat_multimodal(
        sysmsg, compact_msg, frames, temp=0.1, max_tokens=2400,
        provider="openai",
    )
    try:
        return _validate_scene_breakdown(_parse_breakdown_json(raw))
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


def _fixed_reverse_ranges(duration, max_segments=4):
    """Build a gap-free timeline in code; the model never invents timestamps."""
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
        "[%s-%s]" % (
            _format_timeline_second(boundaries[index]),
            _format_timeline_second(boundaries[index + 1]),
        )
        for index in range(segment_count)
    ]


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
            missing = [
                key for key, _label in _REVERSE_SEGMENT_FIELDS
                if not str(value.get(key) or "").strip()
            ]
            if missing:
                raise ValueError(
                    "反推结果第%d段缺少字段：%s，请重试"
                    % (index, ", ".join(missing))
                )
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


def _validate_reverse_prompt_lengths(segments):
    """Enforce length and non-repetition contracts after local assembly."""
    if not segments:
        raise ValueError("反推结果为空，请重试")
    minimum = int(math.ceil(500.0 / len(segments)))
    maximum = int(math.ceil(800.0 / len(segments)))
    lengths = [len(re.sub(r"\s+", "", segment or "")) for segment in segments]
    for index, length in enumerate(lengths, 1):
        if length < minimum:
            raise ValueError(
                "反推结果第%d段过于简略：至少%d字，实际%d字，请重试"
                % (index, minimum, length)
            )
        if length > maximum:
            raise ValueError(
                "反推结果第%d段过长：最多%d字，实际%d字，请重试"
                % (index, maximum, length)
            )
    compact_segments = [
        re.sub(r"[\W_]+", "", segment or "").lower()
        for segment in segments
    ]
    for index, compact in enumerate(compact_segments):
        for previous_index, previous in enumerate(compact_segments[:index]):
            similarity = SequenceMatcher(
                None, previous, compact, autojunk=False,
            ).ratio()
            if compact == previous or (
                min(len(previous), len(compact)) >= 40 and similarity >= 0.92
            ):
                raise ValueError(
                    "反推结果第%d段与第%d段内容重复，请重试"
                    % (index + 1, previous_index + 1)
                )
    total = sum(lengths)
    if total < 500 or total > 800:
        raise ValueError(
            "反推结果总长度需为500-800字，实际%d字，请重试" % total
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


def _reverse_prompt_from_frames(title, duration, platform, script_text, frames):
    timeline_ranges = _fixed_reverse_ranges(duration)
    segment_count = len(timeline_ranges)
    segment_min_chars = int(math.ceil(500.0 / segment_count))
    segment_max_chars = max(segment_min_chars, int(720.0 / segment_count))
    usermsg = (
        _breakdown_source_context(title, duration, platform, script_text) + "\n\n"
        "请基于关键帧和口播，反推出一条可直接用于视频模型生成同款视频的中文执行提示词。"
        "目标是让生成视频在镜头结构和动作节奏上尽可能接近参考视频：严格依据关键帧还原镜头出现顺序、"
        "景别转换、主体动作节点、构图、场景布局、道具位置、光线色调和节奏变化，不要泛化成另一条“同风格原创”视频。"
        "人物具体身份、面部和不可确认的品牌文字属于可替换元素，不得臆造；其余能从关键帧确认的视觉关系应尽量保持。"
        "输入的每张图是两个连续时间点组成的双帧图，左侧早于右侧，图片顺序代表时间推进。"
        "时间轴由程序根据真实视频时长生成，你不要输出、计算或修改任何时间。"
        "请严格按关键帧时间顺序输出 %d 段画面描述，每段说明画面主体、动作起止、"
        "景别、机位、运镜方向、构图变化及转场衔接，前后动作必须连续。"
        "提示词还要具体写清六个层次：①主体至少 5 项（人物/产品的外观、身份、服装、状态和显著特征）"
        "②场景至少 5 项（环境、关键道具、前中后景和空间关系）"
        "③动作与时序至少 8 项（按起始—发展—结束描述人物的表情、视线、手势、肢体姿态、走位及与道具的互动，"
        "同时写清镜头跟随、推进、拉远、摇移或转场的时机，形成可执行的连续过程，避免‘自然地动起来’等笼统表达）"
        "④镜头至少 5 项（景别、视角、构图和整体运镜风格）"
        "⑤光线与色调至少 4 项（照明方向、氛围、材质和色彩质感）"
        "⑥节奏与情绪钩子至少 3 项（节奏变化、情绪推进和观看钩子）。"
        "关键帧无法证明的动作不要写成原视频事实；仅可补充连接相邻关键帧所必需的过渡动作，并明确保持人物、"
        "场景、道具位置和镜头方向连续。提示词结尾增加约束：以随请求附带的参考关键帧为视觉依据，"
        "保持镜头顺序、动作节点和场景布局，不新增人物、道具、镜头或无关情节。"
        "全部描述合计 500-800 字，每段目标 %d-%d 个中文字符。严格只输出一个 JSON 对象，不要标题、解释或 markdown；"
        "对象只能有 segments 一个字段，其值是对象数组。"
        "segments 必须恰好包含 %d 个对象，不要在对象中写时间。每个对象必须包含 subject、scene、action、camera、"
        "lighting、sound、continuity 七个字符串字段，分别填写主体、场景、连续动作、镜头、光影、声音和前后衔接。"
        "每个字段必须直接填写基于关键帧观察到的真实内容；看不清或听不清时写“无可确认信息”，不得编造，不得使用模板占位内容。"
    ) % (segment_count, segment_min_chars, segment_max_chars, segment_count)
    sysmsg = (
        "你是黄雀传媒资深短视频复刻编导。你擅长从连续关键帧中恢复镜头时间轴、动作节点与空间连续性，"
        "并写成视频生成模型可执行的中文提示词。"
        "严格只输出用户指定结构的 JSON 对象，不要任何多余内容。"
    )
    raw = _chat_multimodal(
        sysmsg, usermsg, frames, temp=0.1,
        max_tokens=2400, image_detail=None,
    )
    segments = _validate_reverse_prompt_lengths(
        _parse_reverse_segments(raw, segment_count)
    )
    return "\n".join(
        timeline_range + " " + segment
        for timeline_range, segment in zip(timeline_ranges, segments)
    )


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


def _validate_scene_breakdown(result):
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
        if not scene_text:
            continue
        if any(marker in scene_text or marker in line_text for marker in placeholders):
            raise ValueError("拆解结果包含模板占位内容，请重试")
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


def _extract_frames(video_path, count=6, duration=30, scale_width=512,
                    min_frames=None):
    """ffmpeg 抽帧：场景检测 + 均匀采样兜底。返回 (outdir, [paths])"""
    count = max(2, min(count, 12))  # 限制 2-12 帧，防止异常参数
    scale_width = max(256, min(int(scale_width or 512), 2048))
    outdir = tempfile.mkdtemp()
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-i", video_path,
             "-vf", "select='gt(scene,0.15)',scale=%d:-1" % scale_width,
             "-vsync", "vfr", "-vframes", str(count),
             "%s/frame_%%d.jpg" % outdir],
            check=True, timeout=60,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
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
            subprocess.run(
                ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                 "-i", video_path,
                 "-vf", "fps=%.6f,scale=%d:-1" % (fps, scale_width),
                 "-vframes", str(count),
                 "%s/frame_%%d.jpg" % outdir],
                check=True, timeout=60,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except subprocess.CalledProcessError:
            pass  # 均匀采样也失败 → 返回已有帧（可能 0 张，GPT-4o 仍可纯文本分析）
        frames = sorted([os.path.join(outdir, f) for f in os.listdir(outdir)
                         if f.endswith(".jpg")],
                        key=lambda p: int(os.path.splitext(os.path.basename(p))[0].split("_")[-1]))
    return outdir, frames


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


def _post_zhipu(body, api_key):
    base = os.environ.get(
        "REVERSE_ZHIPU_BASE", "https://open.bigmodel.cn/api/paas/v4"
    ).rstrip("/")
    timeout = int(os.environ.get("BREAKDOWN_ZHIPU_TIMEOUT", "210") or 210)
    try:
        return egress.post_json_idempotent(
            base, base, "/chat/completions",
            json.dumps(body, ensure_ascii=False).encode(),
            {"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
            log=lambda message: print("[breakdown] %s" % message, flush=True),
            max_attempts=2,
            timeout=timeout,
        )
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
        raise


def _post_openai_fallback(body):
    from .image import OPENAI_OFFICIAL_BASE

    return egress.post_json(
        OPENAI_OFFICIAL_BASE, OPENAI_BASE,
        "/v1/chat/completions", json.dumps(body, ensure_ascii=False).encode(),
        {"Authorization": "Bearer " + OPENAI_KEY, "Content-Type": "application/json"},
        log=lambda message: print("[breakdown] %s" % message, flush=True),
    )


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


def _chat_multimodal(sysmsg, usermsg, image_paths, temp=0.7,
                     max_tokens=None, image_detail="low", provider="zhipu"):
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
        "model": os.environ.get(
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
        response = _post_openai_fallback(body)
        result = _chat_content(response)
        print("[breakdown] openai format fallback success: %s" % body["model"], flush=True)
        return result
    if provider != "zhipu":
        raise ValueError("unsupported multimodal provider: " + str(provider))

    zhipu_key = os.environ.get("REVERSE_ZHIPU_KEY", "").strip()
    if not zhipu_key:
        raise RuntimeError("REVERSE_ZHIPU_KEY is not configured")

    try:
        response = _post_zhipu(body, zhipu_key)
    except Exception as exc:
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
            response = _post_openai_fallback(fallback_body)
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
