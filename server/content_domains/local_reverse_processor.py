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

def _structured_prompt(media_type, title, duration, script_text, frames):
    from .breakdown import _chat_multimodal, _parse_breakdown_json
    context = "素材：%s\n类型：%s\n时长：%ss" % (
        title, "本地图片" if media_type == "image" else "本地视频", duration or 0)
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
七个字段合计写 500-800 个中文字符，每个字段都使用具体、可执行的视觉短语，不得用“氛围感强”“动作自然”“画面精美”等笼统表述：
- subject 写 70-100 字，至少 5 项可见细节，包括人物/物体的外观、身份、服装、材质、状态和显著特征；
- scene 写 70-100 字，至少 5 项场景细节，包括环境、道具、前中后景、空间关系和背景变化；
- composition 写 70-100 字，至少 5 项镜头细节，包括景别、视角、主体位置、构图关系、焦段与运镜；
- action 写 150-200 字，至少 8 个连续动作节点，按起始—发展—结束写清表情、视线、手势、姿态、位移、物体互动、镜头跟随和转场时机；
- lighting 写 55-80 字，至少 4 项光影细节，包括光源方向、软硬、色温、明暗层次和环境氛围；
- style 写 55-80 字，至少 4 项风格细节，包括媒介、质感、色调、节奏和成片观感；
- parameters 写 55-80 字，至少 6 项可执行参数，包括画幅、分辨率、帧率、快门/景深、镜头运动、时长或生成限制。
"""
    usermsg = context + """

请根据素材反推出可直接用于同风格原创生成的中文提示词，并严格输出一个 JSON 对象：
{"subject":"主体细节","scene":"场景细节","composition":"构图与镜头","action":"动作细节","lighting":"光影色彩","style":"视觉风格","parameters":"生成参数"}
七个字段都必须是非空字符串。主体写清外观、服装、材质和状态；场景写清前中后景与道具；构图写清景别、视角和镜头关系；
动作写清表情、视线、手势、姿态、位移、物体互动及起始—发展—结束；图片根据可见姿态描述动作状态，不虚构既成事实；
光影写清方向、软硬、色温与氛围；风格写清媒介、质感、色调；参数给出画幅、清晰度、帧率或镜头运动等可执行参数。
只输出 JSON，不要 Markdown，不要解释。""" + visual_rules
    raw = _chat_multimodal(
        "你是黄雀传媒提示词反推专家。忠实识别可见信息，输出结构化、具体、可执行的中文生成提示词。",
        usermsg, frames, temp=0.45, max_tokens=1800, image_detail="high")
    data = _parse_breakdown_json(raw)
    core_subject = subject_evidence = timeline = ""
    if media_type == "video":
        core_subject = str(data.get("core_subject") or "").strip()
        subject_evidence = str(data.get("subject_evidence") or "").strip()
        timeline = str(data.get("timeline") or "").strip()
        if not core_subject or not subject_evidence or not timeline:
            raise ValueError("反推结果缺少核心主体判断，请重试")
    sections = {}
    for key, label in _SECTION_ORDER:
        value = str(data.get(key) or data.get(label) or "").strip()
        if not value:
            raise ValueError("反推结果缺少%s信息，请重试" % label)
        sections[key] = value
    if media_type == "video":
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
        _extract_frames, _format_transcript, _heartbeat, _pair_reverse_frames,
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
            overview = _reverse_overview(frame_dir, frames)
            model_frames = [overview] + _pair_reverse_frames(frame_dir, frames)
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
            except Exception:
                asr_failed = True
        _heartbeat(job_id, "analyzing")
        thumbs = _thumbnails(frames)
        sections, prompt = _structured_prompt(
            media_type, title, duration, script_text, model_frames)
        return {
            "type": "breakdown_reverse", "source_type": media_type,
            "source_url": "", "source_title": title,
            "source_platform": "local_" + media_type,
            "duration": duration, "sections": sections, "prompt": prompt,
            "frame_count": len(frames), "frame_thumbnails": thumbs,
            "asr_failed": asr_failed,
        }
    finally:
        try: path.unlink()
        except OSError: pass
        if frame_dir:
            try: shutil.rmtree(frame_dir)
            except OSError: pass
