#!/usr/bin/env python3
"""Preload and verify the content service's CPU faster-whisper model.

Run this during deployment as the same account that runs huangque-content.
No request-time model download is allowed after the systemd offline flag is on.
"""
import argparse
import os
from pathlib import Path


def prepare(model, cache_dir, device="cpu", compute_type="int8", verify_only=False):
    from faster_whisper import WhisperModel

    cache = Path(cache_dir).expanduser().resolve()
    if cache == Path(cache.anchor):
        raise ValueError("WHISPER_CACHE_DIR 不能是文件系统根目录")
    cache.mkdir(parents=True, exist_ok=True)
    instance = WhisperModel(
        model,
        device=device,
        compute_type=compute_type,
        download_root=str(cache),
        local_files_only=bool(verify_only),
    )
    return {
        "model": str(model),
        "cache_dir": str(cache),
        "device": str(device),
        "compute_type": str(compute_type),
        "loaded": instance is not None,
    }


def main():
    parser = argparse.ArgumentParser(description="预热并验证 huangque-content 字幕模型")
    parser.add_argument("--model", default=os.environ.get("WHISPER_MODEL", "small"))
    parser.add_argument(
        "--cache-dir",
        default=os.environ.get(
            "WHISPER_CACHE_DIR", "/var/cache/huangque/faster-whisper",
        ),
    )
    parser.add_argument("--device", default=os.environ.get("WHISPER_DEVICE", "cpu"))
    parser.add_argument(
        "--compute-type",
        default=os.environ.get("WHISPER_COMPUTE_TYPE", "int8"),
    )
    parser.add_argument(
        "--verify-only", action="store_true",
        help="只使用本地文件验证，不进行任何下载",
    )
    args = parser.parse_args()
    result = prepare(
        args.model, args.cache_dir, args.device, args.compute_type,
        verify_only=args.verify_only,
    )
    print(
        "Whisper ready: model={model} device={device} compute={compute_type} "
        "cache={cache_dir}".format(**result)
    )


if __name__ == "__main__":
    main()
