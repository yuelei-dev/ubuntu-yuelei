# -*- coding: utf-8 -*-
"""口播「网感模板」字幕的 ASR 子进程入口：faster-whisper 逐句时间轴 → JSON。

为什么不直接挂在 content_api 进程里（旧的 white/variety/bar 字幕是进程内模型）：
- 内存：small/int8 峰值约 1GB，服务器只有 3.4GB；子进程跑完即释放，模型不常驻主服务；
- 资源闸：父进程用 systemd-run --scope MemoryMax / nice 包住本进程
  （见 video._run_talking_asr），OOM 只杀这个 scope，不拖垮口播以外的任务。

用法: python -m content_domains.talking_asr_cli <audio_wav> <out.json>
输出: [{"start": 毫秒, "end": 毫秒, "text": "..."}]
"""
import json
import os
import sys

# 与旧字幕的 WHISPER_MODEL(默认 base) 分开配：模板字幕要拿逐句时间轴去对齐口播原文，
# small 的断句/标点明显好于 base，口播最长几分钟，int8+2线程的 CPU 开销可接受。
ASR_MODEL_NAME = os.environ.get("TALKING_ASR_MODEL", "small")


def transcribe(wav_path):
    """转写 → [{start,end,text}]（毫秒）。任何失败以非零退出码带给父进程。"""
    # import 放函数里：CI/本地没装 faster-whisper，模块级 import 会让 import 方直接炸
    # （同 video._get_whisper_model 的约定）。
    # 清代理同理：服务继承了全局 SOCKS 代理(ALL_PROXY)，huggingface_hub 的 httpx 会因缺
    # socksio 报错；模型加载完即恢复。HF_ENDPOINT/HF_HUB_OFFLINE 不碰（透传）——
    # 首次下载走镜像 / 强制离线都由部署环境注入，代码不替环境做决定。
    proxy_keys = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                  "http_proxy", "https_proxy", "all_proxy")
    saved = {k: os.environ.pop(k) for k in proxy_keys if k in os.environ}
    try:
        from faster_whisper import WhisperModel  # 服务器已装；CI 不触发 import
        model = WhisperModel(ASR_MODEL_NAME, device="cpu", compute_type="int8", cpu_threads=2)
    finally:
        os.environ.update(saved)
    segments, _info = model.transcribe(
        wav_path,
        language="zh",
        # 简体偏见提示：压繁体、补标点（字幕要按标点断双行、按句对齐原文）
        initial_prompt="以下是简体中文的口播视频转写，使用简体中文和规范标点。",
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 400},
    )
    return [
        {"start": int(s.start * 1000), "end": int(s.end * 1000), "text": (s.text or "").strip()}
        for s in segments
        if (s.text or "").strip()
    ]


def main(argv):
    if len(argv) < 3:
        print("usage: python -m content_domains.talking_asr_cli <audio_wav> <out.json>", file=sys.stderr)
        return 2
    segs = transcribe(argv[1])
    with open(argv[2], "w", encoding="utf-8") as f:
        json.dump(segs, f, ensure_ascii=False)
    print("TALKING_ASR_OK %d segments" % len(segs))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
