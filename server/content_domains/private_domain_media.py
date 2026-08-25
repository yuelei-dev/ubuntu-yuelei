#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only catalog and file resolver for the private-domain media library.

Only safe, publishable catalog fields are returned.  Source URLs, Feishu file
tokens and other provenance details from index.jsonl never cross the API.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
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
    }


def _catalog_items():
    global _CACHE_KEY, _CACHE_ITEMS
    root = _material_root()
    catalog = root / CATALOG_FILE
    try:
        stat = catalog.stat()
        catalog.resolve().relative_to(root)
        key = (str(root), stat.st_mtime_ns, stat.st_size)
    except (OSError, ValueError):
        return []
    with _CACHE_LOCK:
        if key == _CACHE_KEY:
            return list(_CACHE_ITEMS)
        items = []
        try:
            with catalog.open("r", encoding="utf-8") as source:
                for line in source:
                    if len(items) >= MAX_CATALOG_ITEMS:
                        break
                    try:
                        item = _safe_record(root, json.loads(line))
                    except (UnicodeError, json.JSONDecodeError):
                        continue
                    if item is not None:
                        items.append(item)
        except OSError:
            return []
        _CACHE_KEY = key
        _CACHE_ITEMS = tuple(items)
        return list(items)


def list_materials(limit=300):
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 300
    return _catalog_items()[:max(1, min(500, limit))]


def resolve_material(relative_path):
    relative_path = str(relative_path or "").replace("\\", "/").strip("/")
    allowed = {item["relative_path"] for item in _catalog_items()}
    if relative_path not in allowed:
        return None
    return _safe_material_path(_material_root(), relative_path)
