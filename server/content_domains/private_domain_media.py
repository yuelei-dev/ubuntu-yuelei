#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only catalog and file resolver for the private-domain media library.

Only safe, publishable catalog fields are returned.  Source URLs, Feishu file
tokens and other provenance details from index.jsonl never cross the API.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import pathlib
import re
import stat as stat_module
import tempfile
import threading
import time
import urllib.parse


DEFAULT_MATERIAL_ROOT = "/home/ubuntu/material-libraries/huangque-media"
CATALOG_FILE = "index.jsonl"
MAX_CATALOG_ITEMS = 1000
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MEDIA_TYPES = {"图片": "image", "视频": "video", "BGM": "bgm"}
SNAPSHOT_CACHE_MAX_BYTES = 1024 * 1024 * 1024
SNAPSHOT_CACHE_TTL_SECONDS = 6 * 60 * 60
SNAPSHOT_SUFFIX = ".blob"
SNAPSHOT_LOCK_FILE = ".snapshot-cache.lock"
SNAPSHOT_LOCK_TIMEOUT_SECONDS = 30
_CACHE_LOCK = threading.Lock()
_CACHE_KEY = None
_CACHE_ITEMS = ()
_CACHE_WATCHED = ()


class SnapshotTransientError(RuntimeError):
    """A retryable cache failure that must not commit a partial catalog."""


def _file_identity(stat):
    identity = (
        stat.st_dev,
        stat.st_ino,
        stat.st_size,
        stat.st_mtime_ns,
    )
    # Linux ctime catches same-size in-place rewrites even if mtime is restored.
    # Windows reports unstable ctime values while a file is merely being read.
    return identity + (() if os.name == "nt" else (stat.st_ctime_ns,))


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


def _snapshot_identity(stat):
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def _snapshot_cache_root():
    value = os.environ.get("PRIVATE_DOMAIN_SNAPSHOT_CACHE_ROOT")
    root = pathlib.Path(value) if value else (
        pathlib.Path(tempfile.gettempdir()) / "huangque-private-domain-materials"
    )
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise OSError("material snapshot cache root is not a directory")
    root.chmod(0o700)
    return root.resolve()


def _snapshot_path(root, digest):
    if not SHA256_RE.fullmatch(digest):
        raise OSError("invalid material snapshot digest")
    return root / (digest + SNAPSHOT_SUFFIX)


def _snapshot_files(root):
    result = []
    try:
        candidates = root.iterdir()
    except OSError as error:
        raise SnapshotTransientError("material snapshot cache scan failed") from error
    for path in candidates:
        try:
            stat = path.lstat()
        except OSError as error:
            raise SnapshotTransientError(
                "material snapshot cache entry changed during scan"
            ) from error
        if not stat_module.S_ISREG(stat.st_mode):
            continue
        if path.name == SNAPSHOT_LOCK_FILE:
            kind = "lock"
        elif (path.name.startswith(".pending-")
                and path.name.endswith(".part")):
            kind = "pending"
        else:
            digest = path.name[:-len(SNAPSHOT_SUFFIX)]
            if (not path.name.endswith(SNAPSHOT_SUFFIX)
                    or not SHA256_RE.fullmatch(digest)):
                continue
            kind = "snapshot"
        result.append((path, stat, kind))
    return result


@contextlib.contextmanager
def _snapshot_cache_lock(root):
    lock_path = root / SNAPSHOT_LOCK_FILE
    with lock_path.open("a+b") as lock_file:
        if os.name == "nt":
            import msvcrt
            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)

            def acquire():
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)

            def release():
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            def acquire():
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            def release():
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

        deadline = time.monotonic() + SNAPSHOT_LOCK_TIMEOUT_SECONDS
        while True:
            try:
                acquire()
                break
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    raise SnapshotTransientError(
                        "material snapshot cache lock timed out"
                    )
                time.sleep(0.01)
        try:
            yield
        finally:
            release()


def _remove_snapshot(path):
    try:
        path.chmod(0o600)
    except OSError:
        pass
    try:
        path.unlink()
        return True
    except OSError:
        return False


def _prepare_snapshot_capacity(root, required_bytes, protected_digests):
    if required_bytes < 0 or required_bytes > SNAPSHOT_CACHE_MAX_BYTES:
        raise OSError("material snapshot exceeds cache capacity")
    now = time.time()
    entries = _snapshot_files(root)
    retained = []
    for path, stat, kind in entries:
        if kind == "pending" and _remove_snapshot(path):
            continue
        digest = path.name[:-len(SNAPSHOT_SUFFIX)] if kind == "snapshot" else None
        expired = (kind == "snapshot"
                   and now - stat.st_mtime > SNAPSHOT_CACHE_TTL_SECONDS)
        if (expired and digest not in protected_digests
                and _remove_snapshot(path)):
            continue
        retained.append((path, stat, kind))
    total = sum(stat.st_size for _, stat, _ in retained)
    for path, stat, kind in sorted(
            retained, key=lambda entry: entry[1].st_mtime):
        if total + required_bytes <= SNAPSHOT_CACHE_MAX_BYTES:
            break
        if kind != "snapshot":
            continue
        digest = path.name[:-len(SNAPSHOT_SUFFIX)]
        if digest in protected_digests:
            continue
        if _remove_snapshot(path):
            total -= stat.st_size
    if total + required_bytes > SNAPSHOT_CACHE_MAX_BYTES:
        raise OSError("material snapshot cache capacity is exhausted")


def _verified_existing_snapshot(path, digest):
    try:
        stat = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise SnapshotTransientError(
            "material snapshot metadata is temporarily unavailable"
        ) from error
    try:
        if (not stat_module.S_ISREG(stat.st_mode)
                or time.time() - stat.st_mtime > SNAPSHOT_CACHE_TTL_SECONDS):
            return None
        with path.open("rb") as source:
            before = _snapshot_identity(os.fstat(source.fileno()))
            actual_digest = _sha256_open_file(source)
            after = _snapshot_identity(os.fstat(source.fileno()))
    except OSError as error:
        raise SnapshotTransientError(
            "material snapshot is temporarily unreadable"
        ) from error
    if before != after or actual_digest != digest:
        if not _remove_snapshot(path):
            raise SnapshotTransientError(
                "invalid material snapshot could not be removed"
            )
        return None
    return after


def _materialize_snapshot(path, digest, protected_digests):
    try:
        cache_root = _snapshot_cache_root()
        with _snapshot_cache_lock(cache_root):
            return _materialize_snapshot_locked(
                cache_root, path, digest, protected_digests
            )
    except SnapshotTransientError:
        raise
    except OSError as error:
        raise SnapshotTransientError(
            "material snapshot cache is temporarily unavailable"
        ) from error


def _materialize_snapshot_locked(
        cache_root, path, digest, protected_digests):
    cache_path = _snapshot_path(cache_root, digest)
    try:
        _prepare_snapshot_capacity(
            cache_root, 0, set(protected_digests) | {digest}
        )
    except OSError as error:
        raise SnapshotTransientError(
            "material snapshot capacity check failed"
        ) from error
    snapshot_identity = _verified_existing_snapshot(cache_path, digest)
    if snapshot_identity is not None:
        try:
            with path.open("rb") as source:
                before = _file_identity(os.fstat(source.fileno()))
                actual_digest = _sha256_open_file(source)
                after = _file_identity(os.fstat(source.fileno()))
        except OSError as error:
            raise SnapshotTransientError(
                "material source is temporarily unreadable"
            ) from error
        try:
            current = _file_identity(path.stat())
        except OSError as error:
            raise SnapshotTransientError(
                "material source identity is temporarily unavailable"
            ) from error
        if before != after or current != after or actual_digest != digest:
            return None
        return after, cache_path, snapshot_identity

    try:
        source_size = path.stat().st_size
        if source_size > SNAPSHOT_CACHE_MAX_BYTES:
            return None
        _prepare_snapshot_capacity(cache_root, source_size, protected_digests)
    except OSError as error:
        raise SnapshotTransientError(
            "material snapshot capacity reservation failed"
        ) from error

    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w+b", dir=cache_root, prefix=".pending-",
                suffix=".part", delete=False) as snapshot:
            temporary_path = pathlib.Path(snapshot.name)
            digest_builder = hashlib.sha256()
            with path.open("rb") as source:
                before = _file_identity(os.fstat(source.fileno()))
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    digest_builder.update(chunk)
                    snapshot.write(chunk)
                after = _file_identity(os.fstat(source.fileno()))
            snapshot.flush()
            os.fsync(snapshot.fileno())
        try:
            current = _file_identity(path.stat())
        except OSError as error:
            raise SnapshotTransientError(
                "material source identity is temporarily unavailable"
            ) from error
        if (before != after or current != after
                or digest_builder.hexdigest() != digest):
            return None
        temporary_path.chmod(0o400)
        os.replace(temporary_path, cache_path)
        temporary_path = None
        snapshot_stat = cache_path.lstat()
        if not stat_module.S_ISREG(snapshot_stat.st_mode):
            _remove_snapshot(cache_path)
            return None
        return after, cache_path, _snapshot_identity(snapshot_stat)
    except SnapshotTransientError:
        raise
    except OSError as error:
        raise SnapshotTransientError(
            "material snapshot creation failed"
        ) from error
    finally:
        if temporary_path is not None:
            try:
                temporary_path.chmod(0o600)
            except OSError:
                pass
            try:
                temporary_path.unlink()
            except OSError:
                pass


class VerifiedMaterial:
    """A stable content-addressed handle with a live source-identity guard."""

    def __init__(self, path, source, source_identity, snapshot_identity):
        self.path = path
        self._source = source
        self._source_identity = source_identity
        self._snapshot_identity = snapshot_identity

    def _identities_are_current(self):
        try:
            source_identity = _file_identity(self.path.stat())
            snapshot_identity = _snapshot_identity(os.fstat(self._source.fileno()))
        except OSError:
            return False
        return (source_identity == self._source_identity
                and snapshot_identity == self._snapshot_identity)

    @property
    def name(self):
        return self.path.name

    def stat(self):
        if not self._identities_are_current():
            raise OSError("material identity changed")
        return os.fstat(self._source.fileno())

    def open(self, mode):
        if (mode != "rb" or self._source.closed
                or not self._identities_are_current()):
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


def _safe_record(root, record, protected_digests):
    if not isinstance(record, dict) or record.get("状态") != "可使用":
        return None
    media_type = MEDIA_TYPES.get(record.get("素材类型"))
    relative_path = str(record.get("server_relative_path") or "")
    digest = str(record.get("SHA256") or "").lower()
    path = _safe_material_path(root, relative_path)
    if not media_type or path is None or not SHA256_RE.fullmatch(digest):
        return None
    snapshot = _materialize_snapshot(path, digest, protected_digests)
    if snapshot is None:
        return None
    source_identity, snapshot_path, snapshot_identity = snapshot
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
        "_identity": source_identity,
        "_snapshot_path": snapshot_path,
        "_snapshot_identity": snapshot_identity,
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
    now = time.time()
    for item in _CACHE_ITEMS:
        try:
            snapshot_stat = item["_snapshot_path"].lstat()
        except OSError:
            return False
        if (not stat_module.S_ISREG(snapshot_stat.st_mode)
                or _snapshot_identity(snapshot_stat) != item["_snapshot_identity"]
                or now - snapshot_stat.st_mtime > SNAPSHOT_CACHE_TTL_SECONDS):
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
        protected_digests = set()
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
                        item = _safe_record(root, record, protected_digests)
                    except (UnicodeError, json.JSONDecodeError):
                        continue
                    if item is not None:
                        items.append(item)
                        protected_digests.add(item["sha256"])
        except (OSError, SnapshotTransientError):
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
        source_identity = _file_identity(path.stat())
        if source_identity != item["_identity"]:
            return None
        source = item["_snapshot_path"].open("rb")
        snapshot_identity = _snapshot_identity(os.fstat(source.fileno()))
    except OSError:
        return None
    if snapshot_identity != item["_snapshot_identity"]:
        source.close()
        return None
    return VerifiedMaterial(
        path, source, item["_identity"], item["_snapshot_identity"]
    )
