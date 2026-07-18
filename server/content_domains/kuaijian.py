# -*- coding: utf-8 -*-
"""文字快剪：口播视频 → FunASR 字级转写 → 按文稿删句/去停顿 → ffmpeg 重剪 → 成片。

两段式 job（同一个 kind，op 区分）：
  op=transcribe  上传视频 → 抽 16k 音轨 → 调 huangque-asr → 字级时间戳 → 句级聚合 → 返回文稿。
                 原视频与 transcript 暂存 content_out/kuaijian/<job_id>/，供 cut 复用。
  op=cut         source_job_id + 删除句序号 + 去停顿开关 → 算保留段 → concat 重编码 → COS。

为什么拆两个 job 而不是合并：用户要在两次调用之间看文稿、删句子，是典型的人机分段
交互；剪辑只读暂存文件，不必把 ≤50MB 的视频再传一遍。

字级时间戳来自 huangque-asr（FunASR paraformer-large，见 server/asr_service.py）。
选型实测（2026-07-18，验证机 20 核 CPU）：152s 口播 43.5s 转写、718/718 字全有
毫秒级时间戳、零交叉；对照 faster-whisper 慢 2.6 倍、无标点、中文错字更多。
"""
import base64
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.request

from .core import _NOPROXY, _file_url, _out_path, jdb, public_url

# ---- 配置（env 可覆盖，真值只在 content.env） ----
ASR_BASE = os.environ.get("ASR_BASE", "").rstrip("/")          # 如 http://8.148.158.106:8102
ASR_INTERNAL_TOKEN = os.environ.get("ASR_INTERNAL_TOKEN", "")
ASR_TIMEOUT = int(os.environ.get("KUAIJIAN_ASR_TIMEOUT", "600"))

MAX_VIDEO_BYTES = 50 * 1024 * 1024   # nginx /api/gen/ 80m 上限 − base64 膨胀 1.37 ≈ 55MB，留余量
MAX_DURATION_S = float(os.environ.get("KUAIJIAN_MAX_DURATION", "300"))
PAUSE_MS = int(os.environ.get("KUAIJIAN_PAUSE_MS", "300"))        # ≥此值算停顿
PAUSE_KEEP_MS = int(os.environ.get("KUAIJIAN_PAUSE_KEEP_MS", "75"))  # 停顿收紧后两侧各留的气口
CUT_PAD_MS = int(os.environ.get("KUAIJIAN_CUT_PAD_MS", "30"))     # 删句切点向句间空隙的护边

KUAIJIAN_DIR = "kuaijian"
_VALID_MIMES = {"video/mp4", "video/quicktime"}
_PUNCT = set("，。！？、；：,.!?;:\"\"''（）()【】…—·~ ")
_SENT_END = set("。！？!?；;")


# ==================== 提交校验（do_POST 调用，扣费前） ====================

def validate_kuaijian_payload(body, username):
    """校验 + 规范化 payload。cost_of 在之后读 body['duration'] 计费。"""
    if not isinstance(body, dict):
        raise ValueError("请求格式错误")
    op = str(body.get("op") or "transcribe").strip()
    if op == "transcribe":
        data_url = str(body.get("video_data") or "").strip()
        raw = _decode_data_video(data_url)          # mime/base64/大小校验
        duration = _probe_duration(raw)             # 服务端 ffprobe，不信浏览器
        if duration > MAX_DURATION_S:
            raise ValueError("视频 %.0f 秒，超过 %d 秒上限，请剪短后再上传" % (duration, MAX_DURATION_S))
        if duration < 1.0:
            raise ValueError("视频太短，无法转写")
        filename = re.sub(r"[^\w.一-龥-]+", "_", str(body.get("filename") or "口播视频"))[:40]
        return {"op": "transcribe", "video_data": data_url, "duration": duration, "filename": filename}
    if op == "cut":
        try:
            src_id = int(body.get("source_job_id"))
        except (TypeError, ValueError):
            raise ValueError("缺少 source_job_id")
        src = _load_transcribe_result(src_id, username)
        sentences = src.get("sentences") or []
        if not sentences:
            raise ValueError("转写文稿为空，无法剪辑")
        try:
            deletes = sorted({int(i) for i in (body.get("deletes") or [])})
        except (TypeError, ValueError):
            raise ValueError("deletes 必须是句序号数组")
        if any(i < 0 or i >= len(sentences) for i in deletes):
            raise ValueError("deletes 含越界句序号")
        if len(deletes) >= len(sentences):
            raise ValueError("不能全部删除，至少保留一句")
        return {"op": "cut", "source_job_id": src_id, "deletes": deletes,
                "tighten_pauses": bool(body.get("tighten_pauses")),
                "duration": float(src.get("duration") or 0),
                "filename": src.get("filename")}
    raise ValueError("op 仅支持 transcribe / cut")


def _decode_data_video(data_url):
    """data URL → 原始字节。mime/大小/base64 三重校验（video.py _is_valid_data_url 同款）。"""
    if not data_url.startswith("data:") or "," not in data_url:
        raise ValueError("视频数据格式错误")
    meta, encoded = data_url.split(",", 1)
    if ";base64" not in meta.lower():
        raise ValueError("视频必须 base64 编码")
    mime = meta.split(";", 1)[0].replace("data:", "", 1).lower()
    if mime not in _VALID_MIMES:
        raise ValueError("仅支持 mp4 / mov 视频")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except Exception:
        raise ValueError("视频 base64 解码失败")
    if not raw:
        raise ValueError("视频内容为空")
    if len(raw) > MAX_VIDEO_BYTES:
        raise ValueError("视频超过 %dMB 上限，请压缩后再上传" % (MAX_VIDEO_BYTES // 1024 // 1024))
    return raw


def _probe_duration(raw):
    path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as fh:
            fh.write(raw)
            path = fh.name
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            raise ValueError("视频无法解析，请确认是有效的 mp4/mov 文件")
        return float((proc.stdout or "0").strip() or 0)
    except (OSError, subprocess.SubprocessError):
        raise ValueError("视频无法解析，请确认是有效的 mp4/mov 文件")
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass


def _load_transcribe_result(src_id, username):
    """cut 的源 job：必须是本人的已完成 transcribe。"""
    from contextlib import closing
    with closing(jdb()) as c:
        row = c.execute("SELECT username, status, result FROM jobs WHERE id=? AND kind='kuaijian'",
                        (src_id,)).fetchone()
    if not row or row["username"] != username:
        raise ValueError("转写任务不存在")
    if row["status"] != "done":
        raise ValueError("转写尚未完成，请稍后再试")
    try:
        result = json.loads(row["result"] or "{}")
    except Exception:
        result = {}
    if result.get("op") != "transcribe":
        raise ValueError("该任务不是转写任务")
    return result


# ==================== job 处理（run_job 调用，全生命周期自动） ====================

def gen_kuaijian(payload):
    if payload.get("op") == "cut":
        return _cut(payload)
    return _transcribe(payload)


def _transcribe(payload):
    from .breakdown import _heartbeat

    job_id = payload.get("_job_id")
    raw = _decode_data_video(payload["video_data"])
    work = _out_path("%s/%d" % (KUAIJIAN_DIR, job_id))
    work.mkdir(parents=True, exist_ok=True)
    src = work / "src.mp4"
    with open(str(src), "wb") as f:
        f.write(raw)

    _heartbeat(job_id, "extracting")
    wav = work / "audio.wav"
    _run_ffmpeg(["-y", "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", str(wav)], 120)

    _heartbeat(job_id, "transcribing")
    asr = _asr_transcribe(str(wav))
    sentences = _aggregate_sentences(asr.get("text") or "", asr.get("timestamp_ms") or [])
    if not sentences:
        raise ValueError("没有识别到语音内容，请确认视频有人声")
    transcript = {"text": asr.get("text") or "", "timestamp_ms": asr.get("timestamp_ms") or [],
                  "sentences": sentences}
    with open(str(work / "transcript.json"), "w", encoding="utf-8") as f:
        json.dump(transcript, f, ensure_ascii=False)

    pauses = _sentence_pauses(sentences)
    return {"type": "kuaijian", "op": "transcribe",
            "duration": payload.get("duration"),
            "filename": payload.get("filename"),
            "sentences": sentences,
            "pause_count": len(pauses),
            "src_file": "%s/%d/src.mp4" % (KUAIJIAN_DIR, job_id),
            "infer_s": asr.get("infer_s")}


def _cut(payload):
    from .breakdown import _heartbeat

    job_id = payload.get("_job_id")
    src_id = int(payload.get("source_job_id"))
    sdir = _out_path("%s/%d" % (KUAIJIAN_DIR, src_id))
    try:
        with open(str(sdir / "transcript.json"), encoding="utf-8") as f:
            transcript = json.load(f)
    except (OSError, ValueError):
        raise ValueError("源素材已被清理，请重新上传转写")
    src_mp4 = sdir / "src.mp4"
    if not src_mp4.is_file():
        raise ValueError("源视频已被清理，请重新上传转写")

    _heartbeat(job_id, "cutting")
    chars = _chars_with_ts(transcript.get("text") or "", transcript.get("timestamp_ms") or [])
    sentences = transcript.get("sentences") or []
    duration = float(payload.get("duration") or 0)
    segs = _build_keep_segments(chars, sentences, set(payload.get("deletes") or []),
                                bool(payload.get("tighten_pauses")), duration)
    if not segs:
        raise ValueError("剪辑结果为空，请调整删除范围")

    work = _out_path("%s/%d" % (KUAIJIAN_DIR, job_id))
    work.mkdir(parents=True, exist_ok=True)
    out_mp4 = work / "out.mp4"
    fc = _filter_script(segs)
    fc_path = work / "fc.txt"
    with open(str(fc_path), "w") as f:
        f.write(fc)
    _run_ffmpeg(["-y", "-v", "error", "-i", str(src_mp4),
                 "-filter_complex_script", str(fc_path), "-map", "[v]", "-map", "[a]",
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
                 "-c:a", "aac", "-movflags", "+faststart", str(out_mp4)], 600)

    _heartbeat(job_id, "uploading")
    rel = "%s/%d/out.mp4" % (KUAIJIAN_DIR, job_id)
    url = public_url(rel, "video/mp4")   # COS 直链；未配置/失败回退 /api/gen/file/（私有鉴权）
    duration_after = _probe_duration_file(str(out_mp4))
    return {"type": "kuaijian", "op": "cut",
            "filename": payload.get("filename"),
            "url": url, "file": rel,
            "source_job_id": src_id,
            "duration_before": duration,
            "duration_after": duration_after,
            "removed_s": round(duration - duration_after, 1),
            "deleted_sentences": len(payload.get("deletes") or []),
            "tighten_pauses": bool(payload.get("tighten_pauses"))}


# ==================== ASR 调用 ====================

def _asr_transcribe(wav_path):
    """POST 16k wav 裸字节到 huangque-asr。直连绕过代理（_NOPROXY）：主站默认
    HTTPS_PROXY 走 mihomo 出墙，ASR 是国内阿里云，绕代理反而慢且可能被封。"""
    if not ASR_BASE:
        raise RuntimeError("ASR 服务未配置（content.env 缺 ASR_BASE）")
    with open(wav_path, "rb") as f:
        data = f.read()
    req = urllib.request.Request(
        ASR_BASE + "/transcribe", data=data, method="POST",
        headers={"X-HQ-Internal-Token": ASR_INTERNAL_TOKEN,
                 "Content-Type": "application/octet-stream"})
    try:
        with _NOPROXY.open(req, timeout=ASR_TIMEOUT) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        detail = (e.read() or b"").decode("utf-8", "replace")[:200]
        raise RuntimeError("ASR 转写失败: HTTP %s %s" % (e.code, detail))
    except Exception as e:
        raise RuntimeError("ASR 服务不可用: %s" % str(e)[:120])


# ==================== 文稿聚合与剪辑段计算（纯函数，单测直接喂） ====================

def _chars_with_ts(text, ts):
    """去标点字 ↔ 字级时间戳逐项对齐。

    paraformer 的字戳与去标点文本逐字对应；实测句尾末字可能掉戳（8s 样片 51 字
    50 戳），缺戳字用前字 end+120ms 插值兜底。"""
    chars = []
    ti = 0
    for ch in text:
        if ch in _PUNCT:
            continue
        if ti < len(ts):
            s, e = int(ts[ti][0]), int(ts[ti][1])
        elif chars:
            s, e = chars[-1][2] + 120, chars[-1][2] + 240
        else:
            s, e = 0, 120
        chars.append((ch, s, e))
        ti += 1
    return chars


def _aggregate_sentences(text, ts):
    """字级时间戳 → 句级列表 [{i,text,start,end}]（ms）。句末标点断句，逗号不断。"""
    chars = _chars_with_ts(text, ts)
    sentences = []
    buf, start, end, ci = "", None, None, 0
    for ch in text:
        if ch in _PUNCT:
            buf += ch
            if ch in _SENT_END and start is not None:
                sentences.append({"i": len(sentences), "text": buf.strip(), "start": start, "end": end})
                buf, start, end = "", None, None
            continue
        _, s, e = chars[ci]
        ci += 1
        if start is None:
            start = s
        end = e
        buf += ch
    if start is not None and buf.strip():
        sentences.append({"i": len(sentences), "text": buf.strip(), "start": start, "end": end})
    return sentences


def _sentence_pauses(sentences):
    """句间 ≥PAUSE_MS 的空隙 [(after_sentence_i, gap_ms)]，前端展示用。"""
    out = []
    for i in range(1, len(sentences)):
        gap = sentences[i]["start"] - sentences[i - 1]["end"]
        if gap >= PAUSE_MS:
            out.append((i - 1, gap))
    return out


def _subtract(segs, s, e):
    """从保留段列表挖掉 [s,e)。ms 整数区间运算。"""
    out = []
    for a, b in segs:
        if e <= a or s >= b:
            out.append([a, b])
            continue
        if s > a:
            out.append([a, min(s, b)])
        if e < b:
            out.append([max(e, a), b])
    return out


def _build_keep_segments(chars, sentences, deletes, tighten_pauses, duration_s,
                         pause_ms=PAUSE_MS, keep_ms=PAUSE_KEEP_MS, pad_ms=CUT_PAD_MS):
    """保留段计算（秒级输出，给 ffmpeg trim 用）。

    删句：切点向句间空隙扩 pad_ms，但钳在相邻【保留】句的边界上——只吃空隙、不伤邻句语音。
    去停顿：保留区内字间 ≥pause_ms 的空隙挖掉中段，两侧各留 keep_ms 气口。"""
    dur_ms = int(float(duration_s) * 1000)
    keep = [[0, dur_ms]]
    # 连续被删句合并成一个删除区间：句间沉默跟着一起挖（否则删 0+1 句会留一段废气口）
    runs = []
    for idx in sorted(deletes):
        if not (0 <= idx < len(sentences)):
            continue
        if runs and idx == runs[-1][1] + 1:
            runs[-1][1] = idx
        else:
            runs.append([idx, idx])
    for first, last in runs:
        prev_end = max([sentences[j]["end"] for j in range(first) if j not in deletes] or [0])
        next_start = min([sentences[j]["start"] for j in range(last + 1, len(sentences))
                          if j not in deletes] or [dur_ms])
        s = max(sentences[first]["start"] - pad_ms, prev_end)
        e = min(sentences[last]["end"] + pad_ms, next_start)
        keep = _subtract(keep, s, e)
    if tighten_pauses:
        for i in range(1, len(chars)):
            gap_s, gap_e = chars[i - 1][2], chars[i][1]
            if gap_e - gap_s >= pause_ms:
                keep = _subtract(keep, gap_s + keep_ms, gap_e - keep_ms)
    return [[a / 1000.0, b / 1000.0] for a, b in keep if b - a >= 40]   # 丢弃 <40ms 碎段


def _filter_script(segs):
    lines = []
    for i, (s, e) in enumerate(segs):
        lines.append("[0:v]trim=start=%.3f:end=%.3f,setpts=PTS-STARTPTS[v%d];" % (s, e, i))
        lines.append("[0:a]atrim=start=%.3f:end=%.3f,asetpts=PTS-STARTPTS[a%d];" % (s, e, i))
    cat = "".join("[v%d][a%d]" % i for i in range(len(segs)))
    lines.append("%sconcat=n=%d:v=1:a=1[v][a]" % (cat, len(segs)))
    return "\n".join(lines)


# ==================== 小工具 ====================

def _run_ffmpeg(args, timeout):
    proc = subprocess.run(["ffmpeg"] + args, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg 处理失败: %s" % (proc.stderr or "")[:200])


def _probe_duration_file(path):
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, timeout=30)
    try:
        return round(float((proc.stdout or "0").strip() or 0), 1)
    except ValueError:
        return 0.0


HANDLERS = {"kuaijian": gen_kuaijian}
