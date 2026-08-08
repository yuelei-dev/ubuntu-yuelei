"""Private, short-lived image inputs uploaded through HQ CLI."""

import base64
import hashlib
import hmac
import io
import json
import os
import pathlib
import re
import shutil
import stat
import threading
import time
import uuid
import warnings

try:
    from PIL import Image, UnidentifiedImageError
    DecompressionBombError = Image.DecompressionBombError
    DecompressionBombWarning = Image.DecompressionBombWarning
except ImportError:  # Production installs Pillow; keep imports usable in bare dev environments.
    Image = None
    UnidentifiedImageError = OSError
    DecompressionBombError = ValueError
    DecompressionBombWarning = Warning


MAX_BYTES = 10 * 1024 * 1024
MAX_USER_BYTES = 96 * 1024 * 1024
MAX_USER_FILES = 20
MIN_FREE_BYTES = 512 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
TTL = max(600, min(24 * 60 * 60, int(os.environ.get("HQ_CLI_IMAGE_UPLOAD_TTL", "3600") or 3600)))
_DEFAULT_CONTENT_OUT = pathlib.Path(os.environ.get(
    "CONTENT_OUT", str(pathlib.Path(__file__).resolve().parents[1] / "content_out"),
))
UPLOAD_ROOT = pathlib.Path(os.environ.get(
    "HQ_CLI_UPLOAD_DIR", str(_DEFAULT_CONTENT_OUT / "_cli_uploads"),
))
UPLOAD_ID_RE = re.compile(r"^img_[0-9a-f]{32}$")
MIME_EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
MIME_FORMATS = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}
_UPLOAD_LOCK = threading.Lock()


def detect_mime(header):
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    return ""


def _validate_image_bytes(data, content_type):
    """Decode an uploaded raster and return bounded dimensions.

    Magic bytes alone do not prove that the whole file is readable.  Decode the
    complete image here so corrupt files and decompression bombs are rejected
    before they can become paid smart-montage inputs.
    """
    if Image is None:
        raise ValueError("图片解码组件不可用，请稍后重试")
    expected_format = MIME_FORMATS.get(content_type)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                width, height = image.size
                if str(image.format or "").upper() != expected_format:
                    raise ValueError("图片内容与声明格式不一致")
                if int(getattr(image, "n_frames", 1) or 1) != 1:
                    raise ValueError("暂不支持动图，请上传静态图片")
                if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                    raise ValueError("图片分辨率过大，请缩小后再上传")
                image.verify()
            # verify() checks the stream structure; load() also forces pixel
            # decoding so a truncated payload cannot survive validation.
            with Image.open(io.BytesIO(data)) as image:
                image.load()
    except ValueError:
        raise
    except (UnidentifiedImageError, DecompressionBombError, DecompressionBombWarning,
            OSError, SyntaxError) as exc:
        raise ValueError("上传图片无法读取，请重新导出为 PNG、JPG 或 WebP") from exc
    return int(width), int(height)


def _owner_hash(username):
    return hashlib.sha256(str(username or "").encode("utf-8")).hexdigest()


def _paths(upload_id, extension):
    root = UPLOAD_ROOT.resolve()
    data = (root / (upload_id + extension)).resolve()
    meta = (root / (upload_id + ".json")).resolve()
    data.relative_to(root)
    meta.relative_to(root)
    return data, meta


def _delete_upload(upload_id):
    if not UPLOAD_ID_RE.fullmatch(upload_id):
        return
    for suffix in tuple(MIME_EXTENSIONS.values()) + (".json",):
        try:
            (UPLOAD_ROOT / (upload_id + suffix)).unlink(missing_ok=True)
        except OSError:
            pass


def _cleanup(now):
    # ponytail: 试点量小，上传时扫目录；文件量上千后再换 SQLite 索引。
    try:
        for temp_path in UPLOAD_ROOT.glob(".*.tmp"):
            try:
                if temp_path.stat().st_mtime < now - 600:
                    temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        entries = list(UPLOAD_ROOT.glob("img_*.json"))
    except OSError:
        return
    for meta_path in entries:
        try:
            raw_meta = meta_path.read_bytes()
            if len(raw_meta) > 4096:
                raise ValueError("metadata too large")
            expires_at = int(json.loads(raw_meta).get("expires_at") or 0)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            expires_at = 0
        if expires_at <= now:
            _delete_upload(meta_path.stem)
    # A crash between publishing the data file and its metadata can leave a
    # file that no future quota scan can see.  Keep a grace period so an
    # in-flight writer is never mistaken for an orphan.
    for extension in MIME_EXTENSIONS.values():
        try:
            data_entries = list(UPLOAD_ROOT.glob("img_*" + extension))
        except OSError:
            continue
        for data_path in data_entries:
            try:
                if (not (UPLOAD_ROOT / (data_path.stem + ".json")).exists()
                        and data_path.stat().st_mtime < now - 600):
                    data_path.unlink(missing_ok=True)
            except OSError:
                pass


def cleanup_expired_uploads(now=None):
    """Run the temporary-upload janitor even during a quiet upload period."""
    now = int(time.time() if now is None else now)
    with _UPLOAD_LOCK:
        try:
            UPLOAD_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(UPLOAD_ROOT, 0o700)
        except OSError:
            return
        _cleanup(now)


def _active_usage(owner_hash, now):
    count = total = 0
    for meta_path in UPLOAD_ROOT.glob("img_*.json"):
        try:
            raw_meta = meta_path.read_bytes()
            if len(raw_meta) > 4096:
                continue
            meta = json.loads(raw_meta)
            if int(meta.get("expires_at") or 0) > now and hmac.compare_digest(
                    str(meta.get("owner_hash") or ""), owner_hash):
                count += 1
                total += int(meta.get("bytes") or 0)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
    return count, total


def store_image(stream, length, username, content_type, expected_sha256, now=None):
    now = int(time.time() if now is None else now)
    if not username:
        raise ValueError("缺少上传账号")
    if content_type not in MIME_EXTENSIONS:
        raise ValueError("只支持 PNG / JPG / WebP")
    if not isinstance(length, int) or length <= 0 or length > MAX_BYTES:
        raise ValueError("图片大小必须在 1B 到 10MB 之间")
    expected_sha256 = str(expected_sha256 or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("缺少有效的图片摘要")

    with _UPLOAD_LOCK:
        return _store_image(stream, length, username, content_type, expected_sha256, now)


def _store_image(stream, length, username, content_type, expected_sha256, now):
    UPLOAD_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(UPLOAD_ROOT, 0o700)
    _cleanup(now)
    count, total = _active_usage(_owner_hash(username), now)
    if count >= MAX_USER_FILES or total + length > MAX_USER_BYTES:
        raise ValueError("当前账号的临时图片已达上限，请等待过期后重试")
    if shutil.disk_usage(UPLOAD_ROOT).free - length < MIN_FREE_BYTES:
        raise OSError("图片临时空间不足")
    upload_id = "img_" + uuid.uuid4().hex
    extension = MIME_EXTENSIONS[content_type]
    data_path, meta_path = _paths(upload_id, extension)
    temp_data = UPLOAD_ROOT / ("." + upload_id + ".tmp")
    temp_meta = UPLOAD_ROOT / ("." + upload_id + ".json.tmp")
    digest = hashlib.sha256()
    header = b""
    remaining = length
    descriptor = os.open(str(temp_data), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            while remaining:
                chunk = stream.read(min(64 * 1024, remaining))
                if not chunk:
                    raise ValueError("图片上传不完整")
                if len(header) < 16:
                    header += chunk[:16 - len(header)]
                handle.write(chunk)
                digest.update(chunk)
                remaining -= len(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        actual_sha256 = digest.hexdigest()
        if not hmac.compare_digest(actual_sha256, expected_sha256):
            raise ValueError("图片上传过程中发生变化，请重新上传")
        if detect_mime(header) != content_type:
            raise ValueError("图片内容与声明格式不一致")
        width, height = _validate_image_bytes(temp_data.read_bytes(), content_type)
        meta = {
            "version": 1,
            "owner_hash": _owner_hash(username),
            "mime": content_type,
            "extension": extension,
            "bytes": length,
            "sha256": actual_sha256,
            "width": width,
            "height": height,
            "expires_at": now + TTL,
        }
        temp_meta.write_text(json.dumps(meta, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        os.chmod(temp_meta, 0o600)
        os.replace(temp_data, data_path)
        os.replace(temp_meta, meta_path)
    except Exception:
        temp_data.unlink(missing_ok=True)
        temp_meta.unlink(missing_ok=True)
        data_path.unlink(missing_ok=True)
        meta_path.unlink(missing_ok=True)
        raise
    return {
        "upload_id": upload_id,
        "mime": content_type,
        "bytes": length,
        "sha256": expected_sha256,
        "expires_at": now + TTL,
        "expires_in": TTL,
    }


def _inspect_image(upload_id, username, now):
    now = int(time.time() if now is None else now)
    upload_id = str(upload_id or "").strip().lower()
    if not UPLOAD_ID_RE.fullmatch(upload_id):
        raise ValueError("图片 upload_id 格式不合法")
    _, meta_path = _paths(upload_id, ".png")
    try:
        raw_meta = meta_path.read_bytes()
        if len(raw_meta) > 4096:
            raise ValueError("图片 upload_id 元数据异常")
        meta = json.loads(raw_meta)
        extension = str(meta.get("extension") or "")
        data_path, _ = _paths(upload_id, extension)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        raise ValueError("图片 upload_id 不存在或已失效")
    if (meta.get("version") != 1 or extension not in MIME_EXTENSIONS.values()
            or MIME_EXTENSIONS.get(str(meta.get("mime") or "")) != extension):
        raise ValueError("图片 upload_id 元数据异常")
    if not hmac.compare_digest(str(meta.get("owner_hash") or ""), _owner_hash(username)):
        raise ValueError("图片 upload_id 不存在或已失效")
    if int(meta.get("expires_at") or 0) <= now:
        data_path.unlink(missing_ok=True)
        meta_path.unlink(missing_ok=True)
        raise ValueError("图片 upload_id 已过期，请重新上传")
    try:
        file_stat = data_path.stat()
        with data_path.open("rb") as handle:
            header = handle.read(16)
    except OSError:
        raise ValueError("图片 upload_id 不存在或已失效")
    expected_bytes = int(meta.get("bytes") or -1)
    if (not stat.S_ISREG(file_stat.st_mode) or not 0 < expected_bytes <= MAX_BYTES
            or file_stat.st_size != expected_bytes):
        raise ValueError("图片 upload_id 文件异常")
    if detect_mime(header) != meta.get("mime"):
        raise ValueError("图片 upload_id 文件格式异常")
    if not re.fullmatch(r"[0-9a-f]{64}", str(meta.get("sha256") or "")):
        raise ValueError("图片 upload_id 元数据异常")
    width, height = int(meta.get("width") or 0), int(meta.get("height") or 0)
    if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
        raise ValueError("图片 upload_id 尺寸校验失败")
    return data_path, dict(meta, width=width, height=height)


def inspect_image(upload_id, username, now=None):
    """Return immutable upload metadata without repeatedly decoding pixels."""
    _, meta = _inspect_image(upload_id, username, now)
    return meta


def read_image_bytes(upload_id, username, now=None):
    """Return verified private upload bytes and metadata for its owner."""
    data_path, meta = _inspect_image(upload_id, username, now)
    try:
        data = data_path.read_bytes()
    except OSError:
        raise ValueError("图片 upload_id 不存在或已失效")
    if len(data) != int(meta.get("bytes") or -1):
        raise ValueError("图片 upload_id 文件异常")
    digest = hashlib.sha256(data).hexdigest()
    if not hmac.compare_digest(digest, str(meta.get("sha256") or "")):
        raise ValueError("图片 upload_id 文件校验失败")
    width, height = _validate_image_bytes(data, str(meta.get("mime") or ""))
    if meta.get("width") not in (None, width) or meta.get("height") not in (None, height):
        raise ValueError("图片 upload_id 尺寸校验失败")
    return data, dict(meta, width=width, height=height)


def freeze_image(upload_id, username, destination, purpose, expected_sha256, now=None):
    """Hard-link one approved immutable upload into a task-owned destination.

    Both stores live below ``content_out`` in production, so linking is O(1),
    keeps the bytes alive if the short-lived upload is discarded, and avoids
    multiplying disk usage for multi-style batches.
    """
    now = int(time.time() if now is None else now)
    destination = pathlib.Path(destination)
    temp_path = destination.parent / ("." + destination.name + ".part-" + uuid.uuid4().hex)
    with _UPLOAD_LOCK:
        data_path, meta = _inspect_image(upload_id, username, now)
        if str(meta.get("approved_for") or "") != str(purpose or ""):
            raise ValueError("图片素材未通过当前功能校验")
        if not hmac.compare_digest(
                str(meta.get("sha256") or ""), str(expected_sha256 or "")):
            raise ValueError("图片 upload_id 文件校验失败")
        try:
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(destination.parent, 0o700)
            os.link(data_path, temp_path)
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, destination)
        finally:
            temp_path.unlink(missing_ok=True)
    return dict(meta)


def discard_image(upload_id, username):
    """Delete one temporary upload only when it belongs to ``username``."""
    upload_id = str(upload_id or "").strip().lower()
    if not UPLOAD_ID_RE.fullmatch(upload_id):
        return False
    _, meta_path = _paths(upload_id, ".png")
    try:
        raw_meta = meta_path.read_bytes()
        if len(raw_meta) > 4096:
            return False
        meta = json.loads(raw_meta)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False
    if not hmac.compare_digest(str(meta.get("owner_hash") or ""), _owner_hash(username)):
        return False
    with _UPLOAD_LOCK:
        _delete_upload(upload_id)
    return True


def approve_image(upload_id, username, purpose, now=None, lease_seconds=None):
    """Mark a verified upload for one authenticated product workflow."""
    purpose = str(purpose or "").strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_-]{1,31}", purpose):
        raise ValueError("图片用途无效")
    now = int(time.time() if now is None else now)
    # Decode and ownership-check immediately before changing the metadata.
    _, checked = read_image_bytes(upload_id, username, now=now)
    upload_id = str(upload_id or "").strip().lower()
    _, meta_path = _paths(upload_id, ".png")
    temp_meta = UPLOAD_ROOT / ("." + upload_id + ".approve.tmp")
    with _UPLOAD_LOCK:
        try:
            raw_meta = meta_path.read_bytes()
            if len(raw_meta) > 4096:
                raise ValueError("图片 upload_id 元数据异常")
            meta = json.loads(raw_meta)
            if not hmac.compare_digest(
                    str(meta.get("owner_hash") or ""), _owner_hash(username)):
                raise ValueError("图片 upload_id 不存在或已失效")
            if str(meta.get("sha256") or "") != str(checked.get("sha256") or ""):
                raise ValueError("图片 upload_id 文件校验失败")
            meta["approved_for"] = purpose
            meta["approved_at"] = now
            if lease_seconds is not None:
                lease_seconds = max(600, min(24 * 60 * 60, int(lease_seconds)))
                meta["expires_at"] = max(
                    int(meta.get("expires_at") or 0), now + lease_seconds,
                )
            temp_meta.write_text(
                json.dumps(meta, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            os.chmod(temp_meta, 0o600)
            os.replace(temp_meta, meta_path)
        except Exception:
            temp_meta.unlink(missing_ok=True)
            raise
    return dict(meta)


def _load_image(upload_id, username, now):
    data, meta = read_image_bytes(upload_id, username, now=now)
    return base64.b64encode(data).decode("ascii"), meta


def expand_image_payload(payload, username, now=None):
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 JSON 对象")
    body = dict(payload)
    image_id = body.pop("image_upload_id", None)
    mask_id = body.pop("mask_upload_id", None)
    reference_ids = body.pop("reference_upload_ids", None)
    if image_id is None and mask_id is None and reference_ids is None:
        return body
    if body.get("image") or body.get("mask") or body.get("reference_images"):
        raise ValueError("upload_id 不能与 base64 图片字段同时使用")
    if image_id and reference_ids:
        raise ValueError("单参考图和多参考图不能同时使用")
    if mask_id and not image_id:
        raise ValueError("蒙版必须同时提供原图 upload_id")
    provider = str(body.get("provider") or "openai").strip().lower()
    if reference_ids is not None:
        limits = {"openai": 16, "seedream": 10, "xiaole": 4,
                  "grok": 7, "micro": 9, "omni": 6}
        target = str(body.get("channel") or provider).strip().lower()
        limit = limits.get(target, 1)
        if not isinstance(reference_ids, list) or not 1 <= len(reference_ids) <= limit:
            raise ValueError("reference_upload_ids 必须包含 1-%d 项" % limit)
    if mask_id and provider != "openai":
        raise ValueError("蒙版局部修改仅支持 OpenAI 图片引擎")

    now = int(time.time() if now is None else now)
    if image_id:
        body["image"] = _load_image(image_id, username, now)[0]
    if mask_id:
        mask, meta = _load_image(mask_id, username, now)
        if meta.get("mime") != "image/png":
            raise ValueError("蒙版必须是 PNG 图片")
        body["mask"] = mask
    if reference_ids is not None:
        body["reference_images"] = [_load_image(item, username, now)[0] for item in reference_ids]
    return body
