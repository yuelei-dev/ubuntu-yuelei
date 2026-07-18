#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""huangque-asr：FunASR 字级时间戳转写服务（独立于主站部署）。

为什么不塞进 content_api：paraformer-large 常驻约 3GB 内存，CPU 推理吃核
（152s 音频在 20 核机上实测 43.5s），放进主站会拖累全站快任务。独立机部署后
content_api 经 HTTP 调用，模型/依赖/负载全隔离；服务无状态，水平扩容加机器、
把 ASR_BASE 换成轮询即可。

接口（除 /health 外都要 X-HQ-Internal-Token）：
  GET  /health     → {"ok": true, "model_loaded": true}
  POST /transcribe → body 为 16kHz 单声道 wav 裸字节
                   ← {"text": "...", "timestamp_ms": [[start_ms, end_ms], ...], "infer_s": 12.3}

选型实测（2026-07-18 验证机，20 核 CPU）：152s 真人实拍口播 43.5s 转写
（约 3.5 倍实时速），718/718 字全有毫秒级时间戳、零交叉，跨引擎锚点对齐 ±0.25s；
对照 faster-whisper small 慢 2.6 倍、中文错字更多、无标点、输出繁体。
模型组合（modelscope，免费）：
  iic/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch
  iic/speech_fsmn_vad_zh-cn-16k-common-pytorch
  iic/punc_ct-transformer_zh-cn-common-vocab272727-pytorch
"""
import json
import os
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("ASR_PORT", "8102"))
INTERNAL_TOKEN = os.environ.get("ASR_INTERNAL_TOKEN", "")
# 16kHz 单声道 wav ≈ 32KB/s：30MB ≈ 15 分钟，远大于业务上限 300s
MAX_AUDIO_BYTES = int(os.environ.get("ASR_MAX_AUDIO_BYTES", str(30 * 1024 * 1024)))

_lock = threading.Lock()   # CPU 推理全局串行：并发抢核只会双双变慢
_model = None


def _load_model():
    global _model
    from funasr import AutoModel
    t0 = time.time()
    _model = AutoModel(
        model="iic/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
        vad_model="iic/speech_fsmn_vad_zh-cn-16k-common-pytorch",
        punc_model="iic/punc_ct-transformer_zh-cn-common-vocab272727-pytorch",
        device="cpu",
        disable_update=True,   # 生产不做版本检查，启动不依赖外网
    )
    print("[asr] model loaded in %.1fs" % (time.time() - t0), flush=True)


class H(BaseHTTPRequestHandler):
    server_version = "HuangqueASR/1.0"

    def _send(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self):
        return bool(INTERNAL_TOKEN) and self.headers.get("X-HQ-Internal-Token") == INTERNAL_TOKEN

    def log_message(self, fmt, *args):
        print("[asr] %s %s" % (self.address_string(), fmt % args), flush=True)

    def do_GET(self):
        # /health 不鉴权：给 systemd/拨测用，不泄露任何信息
        if self.path.split("?")[0] == "/health":
            return self._send(200, {"ok": True, "model_loaded": _model is not None})
        return self._send(404, {"detail": "not found"})

    def do_POST(self):
        if self.path.split("?")[0] != "/transcribe":
            return self._send(404, {"detail": "not found"})
        if not self._authorized():
            return self._send(401, {"detail": "unauthorized"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 44:   # 连 wav 头都不够
            return self._send(400, {"detail": "empty audio"})
        if n > MAX_AUDIO_BYTES:
            return self._send(413, {"detail": "audio too large"})
        if _model is None:
            return self._send(503, {"detail": "model not ready"})
        tmp = None
        try:
            data = self.rfile.read(n)
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as fh:
                fh.write(data)
                tmp = fh.name
            t0 = time.time()
            with _lock:
                res = _model.generate(input=tmp, batch_size_s=300)
            infer = time.time() - t0
            r = (res or [{}])[0]
            return self._send(200, {
                "text": r.get("text") or "",
                "timestamp_ms": r.get("timestamp") or [],
                "infer_s": round(infer, 2),
            })
        except Exception as e:
            return self._send(500, {"detail": str(e)[:300]})
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass


def main():
    if not INTERNAL_TOKEN:
        raise SystemExit("ASR_INTERNAL_TOKEN 未配置（写在 asr.env，600 权限）")
    _load_model()
    print("[asr] huangque-asr on 0.0.0.0:%d" % PORT, flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()


if __name__ == "__main__":
    main()
