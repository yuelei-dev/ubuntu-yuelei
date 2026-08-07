# -*- coding: utf-8 -*-
"""Deterministic planning for copy-driven, multi-style montage videos.

The planner deliberately has no provider, database, filesystem, or clock
dependencies.  A request can therefore be planned before charging points and
the exact same JSON can be persisted with the job for a later render worker.
"""

import json
import math
import re
import unicodedata


PLANNER_VERSION = "script_video_montage_v1"
MIN_DURATION_SECONDS = 10
MAX_DURATION_SECONDS = 90
MIN_SCENES = 3
MAX_SCENES = 20
# 90 秒中文商业旁白可完整朗读的安全上限。超长输入必须显式拆单，
# 不能在用户不知情时截掉大部分文案。
MAX_COPY_CHARACTERS = 320
SUPPORTED_RATIOS = ("16:9", "9:16")

STYLE_PROFILES = {
    "luxe": {
        "seconds_per_scene": 5.2,
        "minimum_scene_seconds": 2.8,
        "headline_limit": 12,
        "visual_direction": (
            "高端美业品牌广告，香槟金、奶油白与深黑配色，柔和轮廓光，"
            "高级材质细节，克制留白，电影感写实摄影"
        ),
        "shot_language": (
            "缓慢推进的中近景", "对称构图的精致特写", "侧逆光下的半身镜头",
            "带前景虚化的细节镜头", "低机位缓慢横移", "柔焦转清晰的产品近景",
        ),
    },
    "pop": {
        "seconds_per_scene": 3.0,
        "minimum_scene_seconds": 2.0,
        "headline_limit": 14,
        "visual_direction": (
            "潮流美业社交短视频，高饱和玫红、电光蓝与明黄撞色，"
            "大胆图形构图，年轻有活力，清晰锐利的商业摄影"
        ),
        "shot_language": (
            "广角近距离冲击构图", "俯拍桌面快速定格", "倾斜机位人物半身镜头",
            "超近景质感特写", "横向动势的全身镜头", "低机位仰拍英雄镜头",
            "中心爆发式构图", "左右分区的对比构图",
        ),
    },
    "clinic": {
        "seconds_per_scene": 4.2,
        "minimum_scene_seconds": 2.4,
        "headline_limit": 13,
        "visual_direction": (
            "专业美业护理空间，洁净白、浅灰与医疗蓝配色，均匀柔光，"
            "可信赖、清晰、理性的编辑式商业摄影"
        ),
        "shot_language": (
            "稳定平视的中景", "护理步骤的手部微距", "整洁空间的广角镜头",
            "侧面四十五度人物近景", "仪器与材质的俯拍特写", "自然表情的半身镜头",
            "前后对照感的双区域构图",
        ),
    },
}

_ALLOWED_FIELDS = {"copy", "script", "text", "style", "styles", "ratio"}
_MAJOR_PAUSE_RE = re.compile(r"[。！？!?]")
_MINOR_PAUSE_RE = re.compile(r"[，,、；;：:]")
_HAN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_LATIN_WORD_RE = re.compile(r"[A-Za-z]+(?:['’-][A-Za-z]+)*")
_DIGIT_RE = re.compile(r"\d")
_SENTENCE_RE = re.compile(r"[^。！？!?；;\n]+[。！？!?；;]?", re.UNICODE)
_TRIM_PUNCTUATION = " \t\r\n，,。！？!?；;：:、—-…·|/\\"
_BIDI_CONTROLS = {
    "\u061c", "\u200e", "\u200f", "\u202a", "\u202b", "\u202c",
    "\u202d", "\u202e", "\u2066", "\u2067", "\u2068", "\u2069",
    "\ufeff",
}


class MontagePlanError(ValueError):
    """A user-correctable montage planning error."""


def _normalize_copy(value):
    if not isinstance(value, str):
        raise MontagePlanError("文案必须是字符串")
    # NFC keeps Chinese full-width punctuation intact while still composing
    # canonically equivalent characters into a stable representation.
    text = unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n")
    for character in text:
        category = unicodedata.category(character)
        if category in {"Cc", "Cs"} and character not in {"\n", "\t"}:
            raise MontagePlanError("文案包含不支持的控制字符")
    text = "".join(character for character in text if character not in _BIDI_CONTROLS)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n+ *", "\n", text).strip()
    if not text:
        raise MontagePlanError("请输入成片文案")
    if len(text) > MAX_COPY_CHARACTERS:
        raise MontagePlanError("文案最多支持 %d 个字符，请拆分后生成" % MAX_COPY_CHARACTERS)
    if not _HAN_RE.search(text):
        raise MontagePlanError("当前仅支持包含中文的成片文案")
    if _speech_units(text) < 6:
        raise MontagePlanError("文案过短，请至少输入 6 个有效字符")
    return text


def _copy_from_payload(payload):
    for field in ("copy", "script", "text"):
        if field in payload:
            return _normalize_copy(payload[field])
    raise MontagePlanError("请输入成片文案")


def _styles_from_payload(payload):
    has_style = "style" in payload
    has_styles = "styles" in payload
    if has_style and has_styles:
        raise MontagePlanError("style 与 styles 不能同时提交")
    if not has_style and not has_styles:
        raise MontagePlanError("请选择至少一种成片风格")
    if has_style:
        value = payload.get("style")
        if not isinstance(value, str):
            raise MontagePlanError("成片风格格式无效")
        style = value.strip().lower()
        if style not in STYLE_PROFILES:
            raise MontagePlanError("不支持的成片风格")
        return [style], False

    values = payload.get("styles")
    if not isinstance(values, list) or not values or len(values) > len(STYLE_PROFILES):
        raise MontagePlanError("成片风格列表格式无效")
    styles = []
    for value in values:
        if not isinstance(value, str):
            raise MontagePlanError("成片风格格式无效")
        style = value.strip().lower()
        if style not in STYLE_PROFILES:
            raise MontagePlanError("不支持的成片风格")
        if style in styles:
            raise MontagePlanError("成片风格不能重复")
        styles.append(style)
    return styles, True


def _ratio_from_payload(payload):
    value = payload.get("ratio", "16:9")
    if not isinstance(value, str):
        raise MontagePlanError("画幅格式无效")
    ratio = value.strip()
    if ratio not in SUPPORTED_RATIOS:
        raise MontagePlanError("仅支持 16:9 或 9:16 画幅")
    return ratio


def _speech_units(text):
    """Approximate Mandarin narration units without a tokenizer dependency."""
    han = len(_HAN_RE.findall(text))
    latin_words = len(_LATIN_WORD_RE.findall(text))
    digits = len(_DIGIT_RE.findall(text))
    return float(han) + latin_words * 1.6 + digits * 0.8


def _estimate_duration_seconds(copy):
    # 3.55 Mandarin characters per second is a natural commercial voice pace.
    # Sentence punctuation adds breathing room and the fixed second covers the
    # opening/closing visual handles needed by the edit.
    narration = _speech_units(copy) / 3.55
    pauses = len(_MAJOR_PAUSE_RE.findall(copy)) * 0.34
    pauses += len(_MINOR_PAUSE_RE.findall(copy)) * 0.14
    pauses += copy.count("\n") * 0.24
    estimated = int(math.ceil(narration + pauses + 1.0))
    return max(MIN_DURATION_SECONDS, min(MAX_DURATION_SECONDS, estimated))


def _clean_fragment(value):
    value = re.sub(r"\s+", " ", str(value or "")).strip(_TRIM_PUNCTUATION)
    return value.strip()


def _initial_fragments(copy):
    fragments = []
    for raw in _SENTENCE_RE.findall(copy):
        sentence = _clean_fragment(raw)
        if not sentence:
            continue
        # Commas make useful visual beats, but tiny clauses are kept together.
        clauses = re.split(r"(?<=[，,、：:])", sentence)
        buffer = ""
        for clause in clauses:
            clause = _clean_fragment(clause)
            if not clause:
                continue
            candidate = (buffer + clause).strip()
            if buffer and _speech_units(candidate) > 28:
                fragments.append(buffer)
                buffer = clause
            else:
                buffer = candidate
        if buffer:
            fragments.append(buffer)
    return fragments or [_clean_fragment(copy)]


def _split_fragment(fragment):
    if len(fragment) < 2:
        return None
    midpoint = len(fragment) / 2.0
    candidates = [
        match.end() for match in re.finditer(r"[，,、：:]", fragment)
        if 0 < match.end() < len(fragment)
    ]
    if candidates:
        position = min(candidates, key=lambda value: (abs(value - midpoint), value))
    else:
        position = max(1, min(len(fragment) - 1, int(round(midpoint))))
    left = _clean_fragment(fragment[:position])
    right = _clean_fragment(fragment[position:])
    if not left or not right:
        return None
    return left, right


def _fragments_for_scene_count(copy, scene_count):
    fragments = _initial_fragments(copy)
    while len(fragments) < scene_count:
        candidates = [
            (len(_clean_fragment(fragment)), index)
            for index, fragment in enumerate(fragments)
            if _split_fragment(fragment)
        ]
        if not candidates:
            break
        _, index = max(candidates, key=lambda item: (item[0], -item[1]))
        split = _split_fragment(fragments[index])
        fragments[index:index + 1] = list(split)

    while len(fragments) > scene_count:
        _, index = min(
            (
                _speech_units(fragments[pos]) + _speech_units(fragments[pos + 1]),
                pos,
            )
            for pos in range(len(fragments) - 1)
        )
        fragments[index:index + 2] = [
            _clean_fragment(fragments[index] + "，" + fragments[index + 1])
        ]

    if len(fragments) != scene_count or any(not value for value in fragments):
        raise MontagePlanError("文案无法拆分为有效分镜，请补充更多内容")
    return fragments


def _allocate_duration_units(total_seconds, fragments, minimum_scene_seconds):
    """Allocate exact deciseconds; minimums keep every generated asset visible."""
    total_units = int(total_seconds) * 10
    count = len(fragments)
    minimum_units = int(round(float(minimum_scene_seconds) * 10))
    if minimum_units * count > total_units:
        minimum_units = max(1, total_units // count)
    remaining = total_units - minimum_units * count
    weights = [max(1.0, _speech_units(fragment)) for fragment in fragments]
    weight_sum = sum(weights)
    raw_extra = [remaining * weight / weight_sum for weight in weights]
    extras = [int(math.floor(value)) for value in raw_extra]
    remainder = remaining - sum(extras)
    order = sorted(
        range(count), key=lambda index: (-(raw_extra[index] - extras[index]), index)
    )
    for index in order[:remainder]:
        extras[index] += 1
    units = [minimum_units + extra for extra in extras]
    if sum(units) != total_units or any(value <= 0 for value in units):
        raise MontagePlanError("分镜时长规划失败")
    return units


def _headline(fragment, style, limit):
    plain = re.sub(r"[\s，,。！？!?；;：:、]+", "", fragment)
    title = plain[:limit] or "美丽新灵感"
    if style == "pop" and len(title) < limit:
        return title + "!"
    if style == "clinic" and len(title) + 2 <= limit:
        return "解析" + title
    return title


def _image_prompt(fragment, style, ratio, index):
    profile = STYLE_PROFILES[style]
    shots = profile["shot_language"]
    shot = shots[(index - 1) % len(shots)]
    direction = "横向" if ratio == "16:9" else "竖向"
    subject = fragment[:220]
    return (
        "%s。第%d幕，%s%s，画面主题:%s。主体与动作明确，肤色自然，"
        "空间层次清晰，保留标题安全区；单一完整画面，不含文字、数字、标志、水印或拼贴，"
        "不得复用其他分镜的构图。" % (
            profile["visual_direction"], index, direction, shot, subject,
        )
    )


def _style_plan(copy, style, ratio, duration_seconds):
    profile = STYLE_PROFILES[style]
    scene_count = int(math.ceil(duration_seconds / profile["seconds_per_scene"]))
    scene_count = max(MIN_SCENES, min(MAX_SCENES, scene_count))
    fragments = _fragments_for_scene_count(copy, scene_count)
    duration_units = _allocate_duration_units(
        duration_seconds, fragments, profile["minimum_scene_seconds"]
    )
    cursor = 0
    scenes = []
    for index, (fragment, units) in enumerate(zip(fragments, duration_units), start=1):
        scenes.append({
            "index": index,
            "start_seconds": round(cursor / 10.0, 1),
            "duration_seconds": round(units / 10.0, 1),
            "headline": _headline(fragment, style, profile["headline_limit"]),
            "supporting_copy": fragment,
            "image_prompt": _image_prompt(fragment, style, ratio, index),
        })
        cursor += units
    return {"style": style, "scene_count": scene_count, "scenes": scenes}


def plan_script_video(payload):
    """Validate a JSON-shaped payload and return a deterministic montage plan.

    ``copy`` is canonical; ``script`` and ``text`` are accepted as migration
    aliases.  A single ``style`` returns a flat style plan.  A ``styles`` list
    returns the aggregate shape used by the one-click planning endpoint.
    """
    if not isinstance(payload, dict):
        raise MontagePlanError("请求体必须是 JSON 对象")
    unknown = sorted(set(payload) - _ALLOWED_FIELDS)
    if unknown:
        raise MontagePlanError("请求包含未支持字段: %s" % ", ".join(unknown))
    copy = _copy_from_payload(payload)
    styles, aggregate = _styles_from_payload(payload)
    ratio = _ratio_from_payload(payload)
    duration_seconds = _estimate_duration_seconds(copy)
    variants = [
        _style_plan(copy, style, ratio, duration_seconds) for style in styles
    ]
    base = {
        "copy": copy,
        "ratio": ratio,
        "duration_seconds": duration_seconds,
        "planner_version": PLANNER_VERSION,
    }
    if aggregate:
        base["styles"] = variants
    else:
        base.update(variants[0])
    return base


def canonical_plan_json(plan):
    """Serialize a plan deterministically without raw control/script bytes."""
    if not isinstance(plan, dict):
        raise MontagePlanError("成片计划必须是 JSON 对象")
    try:
        encoded = json.dumps(
            plan,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        # A persisted plan is sometimes embedded in a template script block.
        # JSON itself permits these ASCII bytes, but escaping them prevents a
        # user-provided ``</script>`` sequence from terminating that block.
        return encoded.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    except (TypeError, ValueError) as error:
        raise MontagePlanError("成片计划包含不可序列化的数据") from error


__all__ = [
    "MAX_DURATION_SECONDS", "MAX_SCENES", "MIN_DURATION_SECONDS", "MIN_SCENES",
    "MontagePlanError", "PLANNER_VERSION", "STYLE_PROFILES",
    "canonical_plan_json", "plan_script_video",
]
