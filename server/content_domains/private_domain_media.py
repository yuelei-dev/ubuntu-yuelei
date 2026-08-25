#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only catalog and file resolver for the private-domain media library.

Only safe, publishable catalog fields are returned.  Source URLs, Feishu file
tokens and other provenance details from index.jsonl never cross the API.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import tempfile
import threading
import urllib.parse


DEFAULT_MATERIAL_ROOT = "/home/ubuntu/material-libraries/huangque-media"
CATALOG_FILE = "index.jsonl"
MAX_CATALOG_ITEMS = 1000
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MEDIA_TYPES = {"图片": "image", "视频": "video", "BGM": "bgm"}
_CACHE_LOCK = threading.Lock()
_CACHE_KEY = None
_CACHE_ITEMS = ()
_CACHE_WATCHED = ()


def _file_identity(stat):
    return (
        stat.st_dev,
        stat.st_ino,
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
    )


def _sha256_open_file(source):
    digest = hashlib.sha256()
    source.seek(0)
    while True:
        chunk = source.read(1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    source.seek(0)
    return digest.hexdigest()


def _snapshot_material(path):
    snapshot = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            before = _file_identity(os.fstat(source.fileno()))
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                snapshot.write(chunk)
            after = _file_identity(os.fstat(source.fileno()))
        snapshot.seek(0)
        return snapshot, before, after, digest.hexdigest()
    except OSError:
        snapshot.close()
        raise


class VerifiedMaterial:
    """An already verified file handle; streaming reuses these exact bytes."""

    def __init__(self, path, source):
        self.path = path
        self._source = source

    @property
    def name(self):
        return self.path.name

    def stat(self):
        return os.fstat(self._source.fileno())

    def open(self, mode):
        if mode != "rb" or self._source.closed:
            raise OSError("verified material is not readable")
        return self._source

    def close(self):
        self._source.close()

    def __str__(self):
        return str(self.path)


def _material_root():
    value = os.environ.get("PRIVATE_DOMAIN_MATERIAL_ROOT", DEFAULT_MATERIAL_ROOT)
    return pathlib.Path(value).expanduser().resolve()


def _safe_material_path(root, relative_path):
    value = str(relative_path or "").replace("\\", "/").strip("/")
    pure = pathlib.PurePosixPath(value)
    if (not value or pure.is_absolute() or not pure.parts
            or pure.parts[0] != "files"
            or any(part in {"", ".", ".."} for part in pure.parts)):
        return None
    candidate = root.joinpath(*pure.parts).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def _string_list(value, limit=24):
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text[:80])
        if len(result) >= limit:
            break
    return result


def _safe_record(root, record):
    if not isinstance(record, dict) or record.get("状态") != "可使用":
        return None
    media_type = MEDIA_TYPES.get(record.get("素材类型"))
    relative_path = str(record.get("server_relative_path") or "")
    digest = str(record.get("SHA256") or "").lower()
    path = _safe_material_path(root, relative_path)
    if not media_type or path is None or not SHA256_RE.fullmatch(digest):
        return None
    try:
        with path.open("rb") as source:
            before = _file_identity(os.fstat(source.fileno()))
            actual_digest = _sha256_open_file(source)
            after = _file_identity(os.fstat(source.fileno()))
    except OSError:
        return None
    if before != after or actual_digest != digest:
        return None
    title = str(record.get("素材名称") or path.stem).strip()[:160]
    return {
        "id": digest,
        "title": title or path.stem[:160],
        "media_type": media_type,
        "relative_path": relative_path,
        "sha256": digest,
        "stream_url": (
            "/api/gen/private-domain/material?path="
            + urllib.parse.quote(relative_path, safe="")
        ),
        "category": str(record.get("一级场景") or "").strip()[:80],
        "scene": str(record.get("二级场景") or "").strip()[:80],
        "usage": _string_list(record.get("使用环节")),
        "tags": _string_list(record.get("标签")),
        "content_safety": str(record.get("内容安全") or "").strip()[:80],
        "license": str(record.get("许可类型") or "").strip()[:80],
        "_identity": after,
    }


def _record_watch(root, record):
    if not isinstance(record, dict):
        return None
    relative_path = str(record.get("server_relative_path") or "")
    value = relative_path.replace("\\", "/").strip("/")
    pure = pathlib.PurePosixPath(value)
    if (not value or pure.is_absolute() or not pure.parts
            or pure.parts[0] != "files"
            or any(part in {"", ".", ".."} for part in pure.parts)):
        return None
    path = _safe_material_path(root, value)
    if path is None:
        return value, None
    try:
        return value, _file_identity(path.stat())
    except OSError:
        return value, None


def _cached_items_current(root):
    for relative_path, expected_identity in _CACHE_WATCHED:
        path = _safe_material_path(root, relative_path)
        if path is None:
            identity = None
        else:
            try:
                identity = _file_identity(path.stat())
            except OSError:
                identity = None
        if identity != expected_identity:
            return False
    return True


def _catalog_items():
    global _CACHE_KEY, _CACHE_ITEMS, _CACHE_WATCHED
    root = _material_root()
    catalog = root / CATALOG_FILE
    try:
        stat = catalog.stat()
        catalog.resolve().relative_to(root)
        key = (str(root), stat.st_mtime_ns, stat.st_size)
    except (OSError, ValueError):
        return []
    with _CACHE_LOCK:
        if key == _CACHE_KEY and _cached_items_current(root):
            return list(_CACHE_ITEMS)
        items = []
        watched = {}
        try:
            with catalog.open("r", encoding="utf-8") as source:
                for line in source:
                    if len(items) >= MAX_CATALOG_ITEMS:
                        break
                    try:
                        record = json.loads(line)
                        watch = _record_watch(root, record)
                        if watch is not None:
                            watched[watch[0]] = watch[1]
                        item = _safe_record(root, record)
                    except (UnicodeError, json.JSONDecodeError):
                        continue
                    if item is not None:
                        items.append(item)
        except OSError:
            return []
        _CACHE_KEY = key
        _CACHE_ITEMS = tuple(items)
        _CACHE_WATCHED = tuple(watched.items())
        return list(items)


def list_materials(limit=300):
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 300
    items = _catalog_items()[:max(1, min(500, limit))]
    return [
        {key: value for key, value in item.items() if not key.startswith("_")}
        for item in items
    ]


def resolve_material(relative_path):
    relative_path = str(relative_path or "").replace("\\", "/").strip("/")
    item = next(
        (item for item in _catalog_items()
         if item["relative_path"] == relative_path),
        None,
    )
    if item is None:
        return None
    path = _safe_material_path(_material_root(), relative_path)
    if path is None:
        return None
    try:
        source, before, after, actual_digest = _snapshot_material(path)
    except OSError:
        return None
    if (before != after or after != item["_identity"]
            or actual_digest != item["sha256"]):
        source.close()
        return None
    return VerifiedMaterial(path, source)
