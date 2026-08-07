# -*- coding: utf-8 -*-
"""Safe HyperFrames preparation and rendering for script-driven montages."""

import hashlib
import html
import math
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import wave

from . import video_compose_media as media


BASE = pathlib.Path(__file__).resolve().parents[1]
_LOCAL_TEMPLATE_ROOT = BASE.parent / "site" / "assets" / "one-click" / "templates"
_DEPLOYED_TEMPLATE_ROOT = pathlib.Path(
    "/var/www/huangquechuanmei/assets/one-click/templates"
)
TEMPLATE_ROOT = pathlib.Path(os.environ.get(
    "SCRIPT_VIDEO_TEMPLATE_ROOT",
    str(_LOCAL_TEMPLATE_ROOT if _LOCAL_TEMPLATE_ROOT.is_dir() else _DEPLOYED_TEMPLATE_ROOT),
))
TEMPLATE_ID = "smart-montage-v1"
TEMPLATE_VERSION = "1.0.0"
HYPERFRAMES_VERSION = "0.7.96"
_DEFAULT_NPX = (
    "/home/ubuntu/.local/hq-node/bin/npx"
    if pathlib.Path("/home/ubuntu/.local/hq-node/bin/npx").is_file()
    else "npx"
)
HYPERFRAMES_COMMAND = (_DEFAULT_NPX, "--yes", "hyperframes@" + HYPERFRAMES_VERSION)

_RATIOS = {
    "16:9": (1920, 1080, "landscape"),
    "9:16": (1080, 1920, "portrait"),
}
_STYLE_LABELS = {
    "luxe": "LUXE EDIT",
    "pop": "POP CUT",
    "clinic": "CLINIC NOTE",
}
_IMAGE_SUFFIXES = {".avif", ".jpeg", ".jpg", ".png", ".webp"}
_AUDIO_SUFFIXES = {".aac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav"}
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_URL_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.I)
_MARKER_RE = re.compile(r"__[A-Z0-9_]+__")


class RenderError(ValueError):
    """A public-safe montage validation or rendering error."""


def _number(value, field):
    if isinstance(value, bool):
        raise RenderError(field + "格式无效")
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise RenderError(field + "格式无效")
    if not math.isfinite(result):
        raise RenderError(field + "格式无效")
    return result


def _text(value, limit, field):
    value = _CONTROL_RE.sub("", str(value or ""))
    value = " ".join(value.split()).strip()
    if not value:
        raise RenderError(field + "不能为空")
    if len(value) > limit:
        value = value[:limit].rstrip()
    return value


def _material_value(item, index):
    if isinstance(item, (str, os.PathLike)):
        return item
    if not isinstance(item, dict):
        raise RenderError("第%d幕素材格式无效" % index)
    if "scene_index" in item:
        try:
            scene_index = int(item.get("scene_index"))
        except (TypeError, ValueError):
            raise RenderError("第%d幕素材序号无效" % index)
        if scene_index != index - 1:
            raise RenderError("素材与分镜顺序不一致")
    for key in ("file", "path", "image_file", "local_path"):
        if item.get(key):
            return item[key]
    raise RenderError("第%d幕素材缺少本地文件" % index)


def _local_file(value, suffixes, public_name):
    raw = str(value or "").strip()
    if not raw or _URL_RE.match(raw) or raw.startswith(("//", "\\\\")):
        raise RenderError(public_name + "必须是本地文件")
    try:
        path = pathlib.Path(raw).expanduser().resolve()
        valid = path.suffix.lower() in suffixes and path.is_file()
    except (OSError, RuntimeError, ValueError) as error:
        raise RenderError(public_name + "不是有效的本地文件") from error
    if not valid:
        raise RenderError(public_name + "不是有效的本地文件")
    return path


def _file_digest(path, public_name):
    digest = hashlib.sha256()
    try:
        with pathlib.Path(path).open("rb") as source:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError as error:
        raise RenderError(public_name + "无法读取") from error
    return digest.hexdigest()


def _optional_audio(explicit, plan, keys, public_name):
    value = explicit
    if value is None:
        for key in keys:
            if plan.get(key):
                value = plan[key]
                break
    if value in (None, ""):
        return None
    if isinstance(value, dict):
        value = value.get("file") or value.get("path") or value.get("local_path")
    return _local_file(value, _AUDIO_SUFFIXES, public_name)


def normalize_plan(plan, materials=None, voiceover=None, bgm=None):
    """Validate the planner/render boundary without accepting executable content."""
    if not isinstance(plan, dict):
        raise RenderError("成片方案格式无效")
    style = str(plan.get("style") or "").strip().lower()
    if style not in _STYLE_LABELS:
        raise RenderError("成片风格无效")
    ratio = str(plan.get("ratio") or "").strip()
    if ratio not in _RATIOS:
        raise RenderError("成片比例无效")
    duration = _number(plan.get("duration_seconds"), "成片时长")
    if duration < 10 or duration > 90:
        raise RenderError("成片时长超出 10-90 秒范围")
    duration = round(duration, 3)

    raw_scenes = plan.get("scenes")
    if not isinstance(raw_scenes, list) or not 3 <= len(raw_scenes) <= 20:
        raise RenderError("分镜数量必须在 3-20 幕之间")
    scenes = []
    cursor = 0.0
    for index, item in enumerate(raw_scenes, 1):
        if not isinstance(item, dict):
            raise RenderError("第%d幕分镜格式无效" % index)
        start = _number(item.get("start_seconds"), "第%d幕开始时间" % index)
        scene_duration = _number(item.get("duration_seconds"), "第%d幕时长" % index)
        if start < 0 or scene_duration < 0.25:
            raise RenderError("第%d幕时间范围无效" % index)
        if abs(start - cursor) > 0.05:
            raise RenderError("分镜时间必须连续且不能重叠")
        scenes.append({
            "start_seconds": round(cursor, 3),
            "duration_seconds": round(scene_duration, 3),
            "headline": _text(item.get("headline"), 64, "第%d幕标题" % index),
            "supporting_copy": _text(
                item.get("supporting_copy"), 160, "第%d幕说明" % index
            ),
        })
        cursor = round(cursor + scene_duration, 3)
    if abs(cursor - duration) > 0.08:
        raise RenderError("分镜总时长与成片时长不一致")
    scenes[-1]["duration_seconds"] = round(
        scenes[-1]["duration_seconds"] + duration - cursor, 3
    )
    if scenes[-1]["duration_seconds"] < 0.25:
        raise RenderError("最后一幕时长无效")

    if materials is None:
        materials = plan.get("materials")
    if not isinstance(materials, (list, tuple)) or len(materials) != len(scenes):
        raise RenderError("素材数量必须与分镜数量一致")
    image_paths = []
    seen_paths = set()
    seen_hashes = set()
    for index, item in enumerate(materials, 1):
        path = _local_file(
            _material_value(item, index), _IMAGE_SUFFIXES, "第%d幕素材" % index
        )
        identity = os.path.normcase(str(path))
        digest = _file_digest(path, "第%d幕素材" % index)
        if identity in seen_paths or digest in seen_hashes:
            raise RenderError("每幕必须使用不同的本地图片")
        seen_paths.add(identity)
        seen_hashes.add(digest)
        image_paths.append(path)

    width, height, ratio_class = _RATIOS[ratio]
    return {
        "style": style,
        "ratio": ratio,
        "ratio_class": ratio_class,
        "width": width,
        "height": height,
        "duration_seconds": duration,
        "scenes": scenes,
        "materials": image_paths,
        "voiceover": _optional_audio(
            voiceover, plan, ("voiceover", "voiceover_file"), "旁白"
        ),
        "bgm": _optional_audio(bgm, plan, ("bgm", "bgm_file"), "背景音乐"),
    }


def _seconds(value):
    return "%.3f" % float(value)


def _scene_markup(data, material_names):
    total = len(data["scenes"])
    rows = []
    for index, (scene, material_name) in enumerate(
            zip(data["scenes"], material_names), 1):
        prefix = "scene-%02d" % index
        progress = int(round(index * 100.0 / total))
        # HyperFrames samples the exact composition end after clip teardown on some
        # browser builds. Keep the final visual clip alive for one extra frame so
        # the delivery frame holds the resolved scene instead of the bare page.
        clip_duration = scene["duration_seconds"] + (1.0 / 30.0 if index == total else 0)
        rows.append(
            '    <section id="{p}" class="clip scene" data-start="{start}" '
            'data-duration="{duration}" data-track-index="1">\n'
            '      <div id="{p}-fill" class="scene-fill"></div>\n'
            '      <div id="{p}-atmosphere" class="atmosphere" data-layout-ignore></div>\n'
            '      <div id="{p}-orb" class="accent-orb" data-layout-ignore></div>\n'
            '      <div id="{p}-ordinal" class="ordinal" data-layout-ignore>{ordinal}</div>\n'
            '      <div id="{p}-photo-stage" class="photo-stage">\n'
            '        <div id="{p}-photo-shell" class="photo-shell" '
            'data-layout-allow-overflow>\n'
            '          <img id="{p}-image" src="{src}" alt="" />\n'
            '          <div id="{p}-photo-tint" class="photo-tint"></div>\n'
            '        </div>\n'
            '      </div>\n'
            '      <div id="{p}-content" class="content">\n'
            '        <div id="{p}-eyebrow" class="eyebrow"><i '
            'id="{p}-eyebrow-rule"></i><span>BEAUTY STORY {ordinal}</span></div>\n'
            '        <h1 id="{p}-headline">{headline}</h1>\n'
            '        <p id="{p}-support" class="support">{support}</p>\n'
            '      </div>\n'
            '      <div id="{p}-badge" class="badge">{label}</div>\n'
            '      <div id="{p}-decor-line" class="decor-line"></div>\n'
            '      <div id="{p}-metric" class="metric"><b>{progress:02d}</b>'
            '<small>FLOW</small></div>\n'
            '      <div id="{p}-clinical-bars" class="clinical-bars"><i></i>'
            '<i></i><i></i></div>\n'
            '      <div id="{p}-transition" class="transition-layer"></div>\n'
            '    </section>'.format(
                p=prefix,
                start=_seconds(scene["start_seconds"]),
                duration=_seconds(clip_duration),
                ordinal="%02d" % index,
                src=html.escape(material_name, quote=True),
                headline=html.escape(scene["headline"]),
                support=html.escape(scene["supporting_copy"]),
                label=_STYLE_LABELS[data["style"]],
                progress=progress,
            )
        )
    return "\n".join(rows)


def _silent_wav(path, duration_seconds):
    sample_rate = 48000
    remaining = int(math.ceil(duration_seconds * sample_rate))
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        silence = b"\x00\x00" * 48000
        while remaining:
            frames = min(remaining, 48000)
            output.writeframesraw(silence[:frames * 2])
            remaining -= frames
        output.writeframes(b"")


def _copy_audio(data, workspace):
    audio_dir = workspace / "assets" / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    duration = _seconds(data["duration_seconds"])
    rows = []
    if data["voiceover"]:
        name = "voiceover" + data["voiceover"].suffix.lower()
        shutil.copy2(data["voiceover"], audio_dir / name)
        rows.append(
            '    <audio id="voiceover" src="assets/audio/{name}" data-start="0" '
            'data-duration="{duration}" data-track-index="40" data-volume="1"></audio>'.format(
                name=name, duration=duration
            )
        )
    if data["bgm"]:
        name = "bgm" + data["bgm"].suffix.lower()
        shutil.copy2(data["bgm"], audio_dir / name)
        rows.append(
            '    <audio id="bgm" src="assets/audio/{name}" data-start="0" '
            'data-duration="{duration}" data-track-index="41" data-volume="0.18"></audio>'.format(
                name=name, duration=duration
            )
        )
    if not rows:
        name = "program-silence.wav"
        _silent_wav(audio_dir / name, data["duration_seconds"])
        rows.append(
            '    <audio id="program-silence" src="assets/audio/{name}" data-start="0" '
            'data-duration="{duration}" data-track-index="40" data-volume="1"></audio>'.format(
                name=name, duration=duration
            )
        )
    return "\n".join(rows)


def _set_then_to(selector, before, after, set_at, tween_at=None):
    before_text = ",".join("%s:%s" % item for item in before)
    after_text = ",".join("%s:%s" % item for item in after)
    if tween_at is None:
        tween_at = set_at
    return '    tl.set("%s",{%s},%s);\n    tl.to("%s",{%s},%s);' % (
        selector, before_text, _seconds(set_at),
        selector, after_text, _seconds(tween_at),
    )


def _timeline_script(data):
    style = data["style"]
    total_duration = data["duration_seconds"]
    lines = [
        "    const tl = gsap.timeline({paused:true});",
        _set_then_to(
            "#frame-progress-fill", (("scaleX", "0"),),
            (("scaleX", "1"), ("duration", _seconds(total_duration)), ("ease", '"none"')),
            0,
        ),
    ]
    if data["bgm"]:
        fade = min(1.2, max(0.5, total_duration * 0.04))
        lines.append(
            '    tl.to("#bgm",{volume:0,duration:%s,ease:"sine.in"},%s);' % (
                _seconds(fade), _seconds(total_duration - fade)
            )
        )

    for index, scene in enumerate(data["scenes"], 1):
        prefix = "#scene-%02d" % index
        start = scene["start_seconds"]
        duration = scene["duration_seconds"]
        lead = min(0.18, max(0.04, duration * 0.06))
        entrance = min(0.72, max(0.12, duration * 0.22))
        text_duration = min(0.58, max(0.12, duration * 0.19))
        image_x = 30 if index % 2 else -30
        if style == "luxe":
            stage_before = (("opacity", "0"), ("x", "72"))
            stage_after = (
                ("opacity", "1"), ("x", "0"), ("duration", _seconds(entrance)),
                ("ease", '"power3.out"'),
            )
            content_before = (("opacity", "0"), ("x", "-58"))
            content_after = (
                ("opacity", "1"), ("x", "0"), ("duration", _seconds(text_duration)),
                ("ease", '"expo.out"'),
            )
            image_before = (("scale", "1.025"), ("x", str(-image_x // 2)), ("y", "8"))
            image_after = (
                ("scale", "1.105"), ("x", str(image_x)), ("y", "-16"),
                ("duration", _seconds(duration)), ("ease", '"sine.inOut"'),
            )
        elif style == "pop":
            stage_before = (
                ("opacity", "0"), ("y", "68"), ("scale", ".86"), ("rotation", "-3"),
            )
            stage_after = (
                ("opacity", "1"), ("y", "0"), ("scale", "1"), ("rotation", "0"),
                ("duration", _seconds(entrance)), ("ease", '"back.out(1.65)"'),
            )
            content_before = (("opacity", "0"), ("x", "-82"))
            content_after = (
                ("opacity", "1"), ("x", "0"), ("duration", _seconds(text_duration)),
                ("ease", '"power4.out"'),
            )
            image_before = (("scale", "1.16"), ("x", str(image_x)), ("rotation", "1.2"))
            image_after = (
                ("scale", "1.035"), ("x", str(-image_x)), ("rotation", "-0.6"),
                ("duration", _seconds(duration)), ("ease", '"power1.inOut"'),
            )
        else:
            stage_before = (("opacity", "0"), ("x", "-46"))
            stage_after = (
                ("opacity", "1"), ("x", "0"), ("duration", _seconds(entrance)),
                ("ease", '"power2.out"'),
            )
            content_before = (("opacity", "0"), ("x", "54"))
            content_after = (
                ("opacity", "1"), ("x", "0"), ("duration", _seconds(text_duration)),
                ("ease", '"circ.out"'),
            )
            image_before = (("scale", "1.02"), ("x", str(-image_x // 2)), ("y", "0"))
            image_after = (
                ("scale", "1.075"), ("x", str(image_x // 2)), ("y", "-12"),
                ("duration", _seconds(duration)), ("ease", '"none"'),
            )

        lines.extend([
            _set_then_to(
                prefix + "-photo-stage", stage_before, stage_after,
                start, start + lead,
            ),
            _set_then_to(
                prefix + "-content", content_before, content_after,
                start, start + lead * 1.35,
            ),
            _set_then_to(prefix + "-image", image_before, image_after, start),
            _set_then_to(
                prefix + "-eyebrow-rule", (("scaleX", "0"),),
                (
                    ("scaleX", "1"), ("duration", _seconds(text_duration * 0.72)),
                    ("ease", '"power3.out"'),
                ),
                start,
                start + lead + text_duration * 0.28,
            ),
            _set_then_to(
                prefix + "-support", (("opacity", "0"), ("y", "24")),
                (
                    ("opacity", "1"), ("y", "0"), ("duration", _seconds(text_duration)),
                    ("ease", '"sine.out"'),
                ),
                start,
                start + lead + text_duration * 0.48,
            ),
            _set_then_to(
                prefix + "-badge", (("opacity", "0"),),
                (
                    ("opacity", "1"), ("duration", _seconds(max(0.1, text_duration * 0.6))),
                    ("ease", '"none"'),
                ),
                start,
                start + lead + text_duration * 0.34,
            ),
            _set_then_to(
                prefix + "-decor-line", (("scaleX", "0"),),
                (
                    ("scaleX", "1"), ("duration", _seconds(text_duration * 0.88)),
                    ("ease", '"power2.out"'),
                ),
                start,
                start + lead + text_duration * 0.4,
            ),
            _set_then_to(
                prefix + "-ordinal", (("opacity", ".2"),),
                (
                    ("opacity", ".72"), ("duration", _seconds(duration * 0.42)),
                    ("ease", '"sine.out"'),
                ),
                start,
            ),
            _set_then_to(
                prefix + "-atmosphere", (("opacity", ".68"),),
                (
                    ("opacity", "1"), ("duration", _seconds(duration / 2.0)),
                    ("ease", '"sine.inOut"'), ("yoyo", "true"), ("repeat", "1"),
                ),
                start,
            ),
        ])
        cycle = min(1.4, max(0.12, duration / 2.0))
        repeats = max(0, int(math.floor(duration / cycle)) - 1)
        lines.append(
            _set_then_to(
                prefix + "-orb", (("scale", ".94"), ("opacity", ".62")),
                (
                    ("scale", "1.07"), ("opacity", ".95"),
                    ("duration", _seconds(cycle)), ("ease", '"sine.inOut"'),
                    ("yoyo", "true"), ("repeat", str(repeats)),
                ),
                start,
            )
        )
        if style == "clinic":
            lines.append(
                _set_then_to(
                    prefix + "-clinical-bars i", (("scaleX", "0"),),
                    (
                        ("scaleX", "1"), ("duration", _seconds(text_duration * 0.82)),
                        ("ease", '"power2.out"'), ("stagger", ".09"),
                    ),
                    start,
                    start + lead + text_duration * 0.55,
                )
            )
        if index < len(data["scenes"]):
            transition = min(0.58, max(0.12, duration * 0.18))
            at = start + duration - transition
            if style == "pop":
                before = (("opacity", "1"), ("x", str(data["width"])))
                after = (
                    ("opacity", "1"), ("x", "0"), ("duration", _seconds(transition)),
                    ("ease", '"power4.in"'),
                )
            else:
                before = (("opacity", "1"), ("scaleX", "0"))
                after = (
                    ("opacity", "1"), ("scaleX", "1"),
                    ("duration", _seconds(transition)),
                    ("ease", '"power3.in"' if style == "luxe" else '"sine.inOut"'),
                )
            lines.append(_set_then_to(prefix + "-transition", before, after, at))
    lines.append('    window.__timelines["main"] = tl;')
    return "\n".join(lines)


def _copy_images(data, workspace):
    target = workspace / "assets" / "materials"
    target.mkdir(parents=True, exist_ok=True)
    names = []
    for index, source in enumerate(data["materials"], 1):
        name = "scene-%02d%s" % (index, source.suffix.lower())
        shutil.copy2(source, target / name)
        names.append("assets/materials/" + name)
    return names


def prepare_workspace(plan, materials, workspace, voiceover=None, bgm=None):
    """Copy the frozen project and fill its fixed, non-executable markers."""
    data = normalize_plan(plan, materials, voiceover=voiceover, bgm=bgm)
    template = TEMPLATE_ROOT / TEMPLATE_ID
    source = template / "index.template.txt"
    font_source = template / "assets" / "fonts"
    if not source.is_file():
        raise RenderError("文案成片模板不存在")
    if not font_source.is_dir():
        raise RenderError("文案成片字体资源不存在")
    workspace = pathlib.Path(workspace)
    try:
        shutil.copytree(template, workspace, dirs_exist_ok=True)
        material_names = _copy_images(data, workspace)
        audio_markup = _copy_audio(data, workspace)
        template_text = source.read_text(encoding="utf-8")
    except (OSError, shutil.Error) as error:
        raise RenderError("文案成片工作区准备失败") from error

    replacements = {
        "__WIDTH__": str(data["width"]),
        "__HEIGHT__": str(data["height"]),
        "__STYLE__": data["style"],
        "__RATIO_CLASS__": data["ratio_class"],
        "__DURATION__": _seconds(data["duration_seconds"]),
        "__SCENES__": _scene_markup(data, material_names),
        "__AUDIO__": audio_markup,
        "__TIMELINE_SCRIPT__": _timeline_script(data),
    }
    markers = _MARKER_RE.findall(template_text)
    if set(markers) != set(replacements) or any(
            markers.count(marker) < 1 for marker in replacements):
        raise RenderError("文案成片模板变量不匹配")
    marker_pattern = re.compile("|".join(re.escape(marker) for marker in replacements))
    markup = marker_pattern.sub(lambda match: replacements[match.group(0)], template_text)
    try:
        (workspace / "index.html").write_text(markup, encoding="utf-8")
        (workspace / "index.template.txt").unlink()
    except OSError as error:
        raise RenderError("文案成片工作区准备失败") from error
    return data


def _run_command(runner, command, workspace, environment, timeout, error_messages):
    try:
        completed = runner(
            list(command), check=True, timeout=timeout, cwd=str(workspace), env=environment,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    except FileNotFoundError as error:
        raise RenderError("文案成片渲染运行时尚未安装") from error
    except subprocess.TimeoutExpired as error:
        raise RenderError(error_messages[0]) from error
    except subprocess.CalledProcessError as error:
        raise RenderError(error_messages[1]) from error
    except OSError as error:
        raise RenderError("文案成片渲染运行时不可用") from error
    if getattr(completed, "returncode", 0):
        raise RenderError(error_messages[1])
    return completed


def render(
        plan, materials, output_path, voiceover=None, bgm=None, timeout=None, runner=None):
    """Run the fixed HyperFrames check/render pipeline and verify its MP4 contract."""
    try:
        output_path = pathlib.Path(output_path).resolve()
    except (OSError, RuntimeError, ValueError) as error:
        raise RenderError("文案成片输出目录不可用") from error
    if output_path.suffix.lower() != ".mp4":
        raise RenderError("文案成片输出格式必须为 MP4")
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
    except (OSError, RuntimeError, ValueError) as error:
        raise RenderError("文案成片输出目录不可用") from error
    runner = runner or subprocess.run
    try:
        with tempfile.TemporaryDirectory(prefix="hq-smart-montage-") as directory:
            workspace = pathlib.Path(directory) / "project"
            data = prepare_workspace(
                plan, materials, workspace, voiceover=voiceover, bgm=bgm
            )
            command_prefix = list(HYPERFRAMES_COMMAND)
            check_command = command_prefix + ["check", str(workspace)]
            render_command = command_prefix + [
                "render", str(workspace), "--output", str(output_path), "--fps", "30",
                "--quality", "high", "--workers", "1", "--strict", "--quiet",
            ]
            environment = dict(os.environ)
            environment.update({
                "HYPERFRAMES_SKIP_SKILLS": "1",
                "PRODUCER_LOW_MEMORY_MODE": "1",
            })
            node_bin = pathlib.Path(_DEFAULT_NPX).parent
            if pathlib.Path(_DEFAULT_NPX).is_absolute() and node_bin.is_dir():
                environment["PATH"] = (
                    str(node_bin) + os.pathsep + environment.get("PATH", "")
                )
            if (
                "HYPERFRAMES_BROWSER_PATH" not in environment
                and pathlib.Path("/usr/bin/chromium-browser").is_file()
            ):
                environment["HYPERFRAMES_BROWSER_PATH"] = "/usr/bin/chromium-browser"
            command_timeout = timeout or max(300, int(data["duration_seconds"] * 20))
            _run_command(
                runner, check_command, workspace, environment, command_timeout,
                ("文案成片模板检查超时", "文案成片模板检查未通过"),
            )
            _run_command(
                runner, render_command, workspace, environment, command_timeout,
                ("文案成片渲染超时", "文案成片渲染失败"),
            )
    except RenderError:
        raise
    except (OSError, RuntimeError, ValueError) as error:
        raise RenderError("文案成片临时工作区不可用") from error

    try:
        report = media.probe_media(output_path)
    except Exception as error:
        raise RenderError("文案成片输出验证失败") from error
    expected_ms = int(round(data["duration_seconds"] * 1000))
    tolerance_ms = max(1200, int(expected_ms * 0.05))
    if report.get("video_codec") != "h264" or not report.get("has_audio"):
        raise RenderError("文案成片输出编码不符合交付要求")
    if (
        int(report.get("width") or 0) != data["width"]
        or int(report.get("height") or 0) != data["height"]
    ):
        raise RenderError("文案成片输出画面尺寸异常")
    if abs(int(report.get("duration_ms") or 0) - expected_ms) > tolerance_ms:
        raise RenderError("文案成片输出时长异常")
    return {
        "template_id": TEMPLATE_ID,
        "template_version": TEMPLATE_VERSION,
        "style": data["style"],
        "ratio": data["ratio"],
        "file": str(output_path),
        "output": report,
        "render_log": "ok",
    }
