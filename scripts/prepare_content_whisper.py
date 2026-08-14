#!/usr/bin/env python3
"""Preload and verify the content service's CPU faster-whisper model.

Run this during deployment as the same account that runs huangque-content.
No request-time model download is allowed after the systemd offline flag is on.
"""
import argparse
import os
from pathlib import Path


DEFAULT_CACHE_ROOT = Path("/var/cache/huangque")
CACHE_MODE = 0o750


def _cache_path(cache_dir):
    cache = Path(cache_dir).expanduser().resolve()
    if cache == Path(cache.anchor):
        raise ValueError("WHISPER_CACHE_DIR 不能是文件系统根目录")
    return cache


def require_cache_ready(cache_dir, access_fn=os.access):
    cache = _cache_path(cache_dir)
    if not cache.is_dir():
        raise FileNotFoundError(
            "Whisper 缓存目录尚未预置；请先以 root 运行 --prepare-cache"
        )
    if not access_fn(cache, os.W_OK | os.X_OK):
        raise PermissionError("Whisper 缓存目录对内容服务账号不可写")
    return cache


def prepare_cache_directory(
        cache_dir, uid, gid, *, allowed_root=DEFAULT_CACHE_ROOT,
        chown_fn=None, chmod_fn=os.chmod):
    raw_cache = Path(cache_dir).expanduser()
    raw_root = Path(allowed_root).expanduser()
    if raw_cache.is_symlink() or raw_root.is_symlink():
        raise ValueError("Whisper 缓存目录不得使用符号链接")
    cache = _cache_path(raw_cache)
    root = raw_root.resolve()
    if cache.parent != root:
        raise ValueError("Whisper 缓存必须是专用缓存根目录的直接子目录")
    chown_fn = chown_fn or getattr(os, "chown", None)
    if chown_fn is None:
        raise OSError("当前系统不支持设置 Whisper 缓存目录所有权")
    cache.mkdir(parents=True, exist_ok=True)
    chown_fn(str(cache), int(uid), int(gid))
    chmod_fn(str(cache), CACHE_MODE)
    return {"cache_dir": str(cache), "uid": int(uid), "gid": int(gid)}


def prepare_cache_for_service(
        cache_dir, service_user, service_group, *,
        effective_uid_fn=None, user_lookup=None, group_lookup=None,
        allowed_root=DEFAULT_CACHE_ROOT, chown_fn=None,
        chmod_fn=os.chmod):
    effective_uid_fn = effective_uid_fn or getattr(os, "geteuid", None)
    if effective_uid_fn is None or int(effective_uid_fn()) != 0:
        raise PermissionError("--prepare-cache 必须以 root 执行")
    if user_lookup is None or group_lookup is None:
        import grp
        import pwd
        user_lookup = user_lookup or pwd.getpwnam
        group_lookup = group_lookup or grp.getgrnam
    uid = int(user_lookup(service_user).pw_uid)
    gid = int(group_lookup(service_group).gr_gid)
    return prepare_cache_directory(
        cache_dir, uid, gid, allowed_root=allowed_root,
        chown_fn=chown_fn, chmod_fn=chmod_fn,
    )


def prepare(
        model, cache_dir, device="cpu", compute_type="int8",
        verify_only=False, access_fn=os.access):
    cache = require_cache_ready(cache_dir, access_fn=access_fn)
    from faster_whisper import WhisperModel

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
    parser.add_argument(
        "--prepare-cache", action="store_true",
        help="以 root 创建专用缓存目录并授权给内容服务账号",
    )
    parser.add_argument(
        "--service-user", default=os.environ.get("WHISPER_SERVICE_USER", "ubuntu"),
    )
    parser.add_argument(
        "--service-group", default=os.environ.get("WHISPER_SERVICE_GROUP", "ubuntu"),
    )
    args = parser.parse_args()
    if args.prepare_cache:
        result = prepare_cache_for_service(
            args.cache_dir, args.service_user, args.service_group,
        )
        print(
            "Whisper cache ready: cache={cache_dir} uid={uid} gid={gid}"
            .format(**result)
        )
        return
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
