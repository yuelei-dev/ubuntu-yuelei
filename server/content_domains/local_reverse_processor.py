# -*- coding: utf-8 -*-
"""Analyze validated local uploads and return a structured reverse prompt."""
import base64
import pathlib
import shutil
import subprocess

from .core import OUT_DIR

_SECTION_ORDER = (
    ("subject", "主体"), ("scene", "场景"), ("composition", "构图"),
    ("action", "动作"), ("lighting", "光影"), ("style", "风格"),
    ("parameters", "参数"),
)
_PLACEHOLDER_VALUES = {
    "主体细节", "场景细节", "构图与镜头", "动作细节",
    "光影色彩", "视觉风格", "生成参数", "画幅、清晰度",
}
_VIDEO_SECTION_MIN_CHARS = {
    "subject": 50, "scene": 50, "composition": 50, "action": 120,
    "lighting": 40, "style": 40, "parameters": 40,
}
_VIDEO_SECTION_MIN_ITEMS = {
    "subject": 5, "scene": 5, "composition": 5, "action": 8,
    "lighting": 4, "style": 4, "parameters": 6,
}
_VIDEO_TOTAL_MIN_CHARS = 500

def _upload_path(payload):
    root = (pathlib.Path(OUT_DIR) / "reverse_uploads").resolve()
    path = pathlib.Path(str((payload or {}).get("local_media_path") or "")).resolve()
    if path.parent != root or not path.is_file():
        raise ValueError("本地素材已失效，请重新上传")
    return path

def _thumbnails(frames, limit=4):
    result = []
    for path in (frames or [])[:limit]:
        try:
            suffix = pathlib.Path(path).suffix.lower()
            mime = {".png": "image/png", ".webp": "image/webp"}.get(suffix, "image/jpeg")
            with open(path, "rb") as source:
                result.append("data:%s;base64,%s" % (
                    mime, base64.b64encode(source.read()).decode()))
        except OSError:
            pass
    return result

def _reverse_overview(frame_dir, frames):
    """Build one 4x2 contact sheet so the model sees the whole story first."""
    ordered = list(frames or [])[:8]
    if len(ordered) < 8:
        raise ValueError("视频总览帧不足 8 张")
    output = str(pathlib.Path(frame_dir) / "reverse_overview.jpg")
    command = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    for frame in ordered:
        command.extend(["-i", frame])
    scaled = ";".join("[%d:v]scale=256:-2[s%d]" % (i, i) for i in range(8))
    inputs = "".join("[s%d]" % i for i in range(8))
    layout = (
        "0_0|w0_0|w0+w1_0|w0+w1+w2_0|"
        "0_h0|w0_h0|w0+w1_h0|w0+w1+w2_h0"
    )
    command.extend([
        "-filter_complex",
        scaled + ";" + inputs + "xstack=inputs=8:layout=" + layout + ":fill=black[v]",
        "-map", "[v]", "-frames:v", "1", "-q:v", "2", output,
    ])
    subprocess.run(
        command, check=True, timeout=30,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return output

def _model_text(value):
    """Normalize GLM's occasional list/dict values into readable Chinese."""
    if isinstance(value, (list, tuple)):
        return "；".join(filter(None, (_model_text(item) for item in value)))
    if isinstance(value, dict):
        return "；".join(filter(None, (_model_text(item) for item in value.values())))
    return str(value or "").strip()

def _detail_items(value):
    text = _model_text(value)
    for separator in ("\n", "。", "！", "？", "，", ",", "；", ";", "→", "->"):
        text = text.replace(separator, "|")
    return [item.strip(" -—0123456789.、：:") for item in text.split("|")
            if item.strip(" -—0123456789.、：:")]

def _contains_placeholder(value):
    normalized = _model_text(value).replace(" ", "")
    return any(placeholder.replace(" ", "") in normalized
               for placeholder in _PLACEHOLDER_VALUES)

def _duration_tenth(value):
    try:
        return round(max(0.0, float(value or 0)), 1)
    except (TypeError, ValueError):
        return 0.0

def _transcript_is_abnormal(transcript_text, duration):
    """Ignore implausibly dense/repetitive ASR so it cannot override the frames."""
    import re
    text = re.sub(r"\[[^\]]*\]", "", transcript_text or "")
    text = re.sub(r"\s+", "", text)
    if not text:
        return False
    duration = max(1.0, float(duration or 0))
    if len(text) > max(120, int(duration * 12)):
        return True
    if len(text) < 80:
        return False
    shingles = [text[index:index + 8] for index in range(len(text) - 7)]
    return bool(shingles) and len(set(shingles)) / float(len(shingles)) < 0.35

def _validate_video_result(data, sections):
    core_subject = _model_text(data.get("core_subject"))
    evidence = _model_text(data.get("subject_evidence"))
    timeline = _model_text(data.get("timeline"))
    if not core_subject or len(_detail_items(evidence)) < 3 or len(_detail_items(timeline)) < 6:
        raise ValueError("反推结果缺少核心主体判断，请重试")
    for key, minimum in _VIDEO_SECTION_MIN_CHARS.items():
        value = sections.get(key, "")
        if _contains_placeholder(value) or len(value) < minimum:
            raise ValueError("反推结果%s过于简略，请重试" % dict(_SECTION_ORDER)[key])
        if len(_detail_items(value)) < _VIDEO_SECTION_MIN_ITEMS[key]:
            raise ValueError("反推结果%s细节不足，请重试" % dict(_SECTION_ORDER)[key])
    if sum(len(sections[key]) for key in _VIDEO_SECTION_MIN_CHARS) < _VIDEO_TOTAL_MIN_CHARS:
        raise ValueError("反推结果未达到详细度要求，请重试")
    return core_subject, evidence, timeline

def _structured_prompt(media_type, title, duration, script_text, frames):
    from .breakdown import _chat_multimodal, _parse_breakdown_json
    context = "素材：%s\n类型：%s\n时长：%ss" % (
        title, "本地图片" if media_type == "image" else "本地视频",
        _duration_tenth(duration))
    if script_text:
        context += (
            "\n音频语义参考（低权重，仅用于判断主题；不得复述原句或输出口播稿）：\n"
            + script_text[:600]
        )
    visual_rules = ""
    if media_type == "video":
        visual_rules = """
这是视频生成提示词反推，不是口播文案、解说词、字幕或脚本创作。以关键帧中的可见画面、人物动作、场景变化和镜头运动为第一依据；
音频语义仅辅助判断主题，不得把转写原句、营销话术、旁白或台词写入任何字段。音频与画面冲突时以画面为准。
动作字段必须按时间顺序描述起始—发展—结束，并写清人物/物体运动、镜头运动和转场；构图字段必须体现视频镜头的景别变化与运镜关系。
第一张图片是 8 个时间点组成的 4×2 总览图，阅读顺序为从左到右、从上到下；后四张图片分别补充相邻时间点。
先在内部完成“贯穿多数时间点的核心主体”判断，再填写七个展示字段。产品在多个特写、操作或使用画面中反复出现时，产品必须是 core_subject 和 subject 的第一主体；
人物仅作为展示者或使用者，不得用后段出现的办公、咖啡馆等生活场景替代贯穿视频的产品主线。
JSON 还必须包含三个非空内部字段："core_subject" 写唯一核心主体，"subject_evidence" 写至少 3 个不同时间点的可见证据，
"timeline" 按总览图顺序写至少 6 个连续节点。subject 必须同时覆盖核心产品和关键人物，action 必须与 timeline 一致。
timeline 如需标注时间，时间值只保留 1 位小数（0.1 秒精度），不得输出百分之一秒或千分之一秒。
subject_evidence 和 timeline 可以输出 JSON 字符串数组，其余字段必须是字符串。所有值必须来自实际画面，禁止复制字段名或字段说明。
七个字段合计写 500-800 个中文字符，每个字段都使用具体、可执行的视觉短语，不得用“氛围感强”“动作自然”“画面精美”等笼统表述：
- subject 写 70-100 字，至少 5 项可见细节，包括人物/物体的外观、身份、服装、材质、状态和显著特征；
- scene 写 70-100 字，至少 5 项场景细节，包括环境、道具、前中后景、空间关系和背景变化；
- composition 写 70-100 字，至少 5 项镜头细节，包括景别、视角、主体位置、构图关系、焦段与运镜；
- action 写 150-200 字，至少 8 个连续动作节点，按起始—发展—结束写清表情、视线、手势、姿态、位移、物体互动、镜头跟随和转场时机；
- lighting 写 55-80 字，至少 4 项光影细节，包括光源方向、软硬、色温、明暗层次和环境氛围；
- style 写 55-80 字，至少 4 项风格细节，包括媒介、质感、色调、节奏和成片观感；
- parameters 写 55-80 字，至少 6 项可执行参数，包括画幅、分辨率、帧率、快门/景深、镜头运动、时长或生成限制。
"""
    required_keys = (
        "core_subject, subject_evidence, timeline, "
        if media_type == "video" else ""
    ) + "subject, scene, composition, action, lighting, style, parameters"
    usermsg = context + """

请根据素材反推出可直接用于同风格原创生成的中文提示词，并严格输出一个 JSON 对象。
必需字段名：%s。
不要输出示例、字段说明或占位词；每个字段值都必须替换为从当前素材实际识别出的具体内容。
主体写清外观、服装、材质和状态；场景写清前中后景与道具；构图写清景别、视角和镜头关系；
动作写清表情、视线、手势、姿态、位移、物体互动及起始—发展—结束；图片根据可见姿态描述动作状态，不虚构既成事实；
光影写清方向、软硬、色温与氛围；风格写清媒介、质感、色调；参数给出画幅、清晰度、帧率或镜头运动等可执行参数。
只输出 JSON，不要 Markdown，不要解释。""" % required_keys + visual_rules
    raw = _chat_multimodal(
        "你是黄雀传媒提示词反推专家。忠实识别当前图片中的可见信息；禁止复制字段说明，所有字段必须填写具体识别结果。",
        usermsg, frames, temp=0.1 if media_type == "video" else 0.45,
        max_tokens=2400 if media_type == "video" else 1800, image_detail="high")
    data = _parse_breakdown_json(raw)
    sections = {}
    for key, label in _SECTION_ORDER:
        value = _model_text(data.get(key) or data.get(label))
        if not value:
            raise ValueError("反推结果缺少%s信息，请重试" % label)
        sections[key] = value
    if media_type == "video":
        core_subject, subject_evidence, timeline = _validate_video_result(
            data, sections)
        sections["subject"] = "核心主体：%s；识别依据：%s；%s" % (
            core_subject, subject_evidence, sections["subject"])
        sections["action"] = "完整时间线：%s；%s" % (
            timeline, sections["action"])
    prompt = "\n".join("%s：%s" % (label, sections[key])
                       for key, label in _SECTION_ORDER)
    return sections, prompt

def gen_local_reverse(payload):
    import tikhub
    from .breakdown import (
        _extract_frames, _format_transcript, _heartbeat,
        _reverse_result_from_frames, _reverse_transcript_is_abnormal,
        _speech_chars,
    )
    path = _upload_path(payload)
    media_type = str(payload.get("local_media_type") or "").lower()
    if media_type not in {"image", "video"}:
        raise ValueError("本地素材类型无效")
    title = str(payload.get("source_title") or path.name)[:120]
    duration = float(payload.get("duration") or 0)
    job_id = payload.get("_job_id")
    frame_dir = None
    try:
        _heartbeat(job_id, "extracting_frames")
        if media_type == "image":
            frames = [str(path)]
            model_frames = frames
        else:
            frame_dir, frames = _extract_frames(
                str(path), 8, duration or 1, scale_width=1024, min_frames=8)
            if not frames:
                raise ValueError("视频关键帧读取失败，请确认文件完整")
        script_text = ""
        asr_failed = False
        if media_type == "video":
            try:
                _heartbeat(job_id, "transcribing")
                transcript = tikhub.transcript({"title": title}, video_path=str(path))
                if isinstance(transcript, dict): transcript = transcript.get("text") or ""
                script_text = _format_transcript(transcript)
                if _speech_chars(script_text) < 8:
                    script_text = ""
                elif _reverse_transcript_is_abnormal(script_text, duration):
                    script_text = ""
            except Exception:
                asr_failed = True
        _heartbeat(job_id, "analyzing")
        if media_type == "video":
            media_mime = {
                ".mp4": "video/mp4",
                ".mov": "video/quicktime",
                ".webm": "video/webm",
            }.get(path.suffix.lower(), "video/mp4")
            return _reverse_result_from_frames(
                payload,
                frames,
                source_url="",
                title=title,
                platform="local",
                duration=duration,
                script_text=script_text,
                asr_failed=asr_failed,
                media_path=str(path),
                media_mime=media_mime,
            )
        thumbs = _thumbnails(frames)
        sections, prompt = _structured_prompt(
            media_type, title, duration, script_text, model_frames)
        return {
            "type": "breakdown_reverse", "source_type": media_type,
            "source_url": "", "source_title": title,
            "source_platform": "local_" + media_type,
            "duration": _duration_tenth(duration),
            "sections": sections, "prompt": prompt,
            "frame_count": len(frames), "frame_thumbnails": thumbs,
            "asr_failed": asr_failed,
        }
    finally:
        try: path.unlink()
        except OSError: pass
        if frame_dir:
            try: shutil.rmtree(frame_dir)
            except OSError: pass
