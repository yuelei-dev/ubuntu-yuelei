#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Runtime pricing registry shared by the admin, content, image and leadgen services.

Prices are positive integer Huangque points. Overrides live in the existing admin
SQLite database and are read at order acceptance time, so changes do not require a
service restart. A successful database read seeds a process-local trusted snapshot;
database failures use that snapshot or fail closed before points can be deducted.
"""

from contextlib import closing
import os
import pathlib
import sqlite3
import threading
import time


BASE = pathlib.Path(__file__).resolve().parent


CATALOG = (
    {"key": "copy", "group": "内容与获客", "label": "AI 文案生成", "unit": "每次", "default": 3},
    {"key": "audio", "group": "内容与获客", "label": "AI 配音生成", "unit": "每次", "default": 10},
    {"key": "avatar", "group": "内容与获客", "label": "创建数字人形象", "unit": "每次", "default": 5},
    {"key": "voice_slot", "group": "内容与获客", "label": "购买个人音色槽位", "unit": "每个", "default": 50},
    {"key": "search", "group": "内容与获客", "label": "关键词搜索", "unit": "每次", "default": 1},
    {"key": "collect.main", "group": "内容与获客", "label": "内容爬取", "unit": "每条", "default": 30},
    {"key": "collect.transcript", "group": "内容与获客", "label": "提取口播文案", "unit": "每条", "default": 6},
    {"key": "leads", "group": "内容与获客", "label": "采集获客", "unit": "每次", "default": 30},
    {"key": "breakdown.per_link", "group": "内容与获客", "label": "爆款拆解 / 提示词反推", "unit": "每个链接", "default": 20},
    {"key": "breakdown.local_upload", "group": "内容与获客", "label": "本地素材反推", "unit": "每个文件", "default": 20},

    {"key": "image.openai.std", "group": "图片生成", "label": "GPT Image 标准", "unit": "每张", "default": 20},
    {"key": "image.openai.hd", "group": "图片生成", "label": "GPT Image 高清", "unit": "每张", "default": 30},
    {"key": "image.xiaole.std", "group": "图片生成", "label": "果肉生图标准", "unit": "每张", "default": 8},
    {"key": "image.xiaole.hd", "group": "图片生成", "label": "果肉生图高清", "unit": "每张", "default": 12},
    {"key": "image.zelong.std", "group": "图片生成", "label": "泽龙生图标准", "unit": "每张", "default": 8},
    {"key": "image.zelong.hd", "group": "图片生成", "label": "泽龙生图高清", "unit": "每张", "default": 12},
    {"key": "image.zelong2.std", "group": "图片生成", "label": "泽龙 2 生图标准", "unit": "每张", "default": 8},
    {"key": "image.zelong2.hd", "group": "图片生成", "label": "泽龙 2 生图高清", "unit": "每张", "default": 12},
    {"key": "image.seedream.std.std", "group": "图片生成", "label": "Seedream 标准版·标准", "unit": "每张", "default": 8},
    {"key": "image.seedream.std.hd", "group": "图片生成", "label": "Seedream 标准版·高清", "unit": "每张", "default": 12},
    {"key": "image.seedream.pro.std", "group": "图片生成", "label": "Seedream Pro·标准", "unit": "每张", "default": 15},
    {"key": "image.seedream.pro.hd", "group": "图片生成", "label": "Seedream Pro·高清", "unit": "每张", "default": 20},
    {"key": "image.default.std", "group": "图片生成", "label": "未知图片渠道兜底·标准", "unit": "每张", "default": 8},
    {"key": "image.default.hd", "group": "图片生成", "label": "未知图片渠道兜底·高清", "unit": "每张", "default": 12},

    {"key": "banana.nb2.std", "group": "Nano Banana", "label": "Nano Banana 2·标准", "unit": "每张", "default": 15},
    {"key": "banana.nb2.hd", "group": "Nano Banana", "label": "Nano Banana 2·高清", "unit": "每张", "default": 25},
    {"key": "banana.pro.std", "group": "Nano Banana", "label": "Nano Banana Pro·标准", "unit": "每张", "default": 25},
    {"key": "banana.pro.hd", "group": "Nano Banana", "label": "Nano Banana Pro·高清", "unit": "每张", "default": 30},
    {"key": "image.reverse", "group": "Nano Banana", "label": "图片反推提示词", "unit": "每次", "default": 2},

    {"key": "talking.per_sec", "group": "视频生成", "label": "数字人口播", "unit": "每秒", "default": 10},
    {"key": "cinematic.motion.per_sec", "group": "视频生成", "label": "单人动作模仿", "unit": "每秒", "default": 30},
    {"key": "cinematic.duo.per_sec", "group": "视频生成", "label": "双人动作模仿", "unit": "每秒", "default": 30},
    {"key": "cinematic.open.per_sec", "group": "视频生成", "label": "开放式电影化身", "unit": "每秒", "default": 30},
    {"key": "tryon.single", "group": "视频生成", "label": "AI 换装", "unit": "每次", "default": 25},
    {"key": "tryon.combo", "group": "视频生成", "label": "AI 换装 + 换背景", "unit": "每次", "default": 40},
    {"key": "xiaole_video.per_sec", "group": "视频生成", "label": "果肉 / 豆姐 / 欧米视频", "unit": "每秒", "default": 30},
    {"key": "grok_video.v1.480p.per_sec", "group": "视频生成", "label": "Grok Video 1.0·480p", "unit": "每秒", "default": 10},
    {"key": "grok_video.v1.720p.per_sec", "group": "视频生成", "label": "Grok Video 1.0·720p", "unit": "每秒", "default": 12},
    {"key": "grok_video.v1_5.480p.per_sec", "group": "视频生成", "label": "Grok Video 1.5·480p", "unit": "每秒", "default": 15},
    {"key": "grok_video.v1_5.720p.per_sec", "group": "视频生成", "label": "Grok Video 1.5·720p", "unit": "每秒", "default": 25},
    {"key": "grok_video.v1_5.1080p.per_sec", "group": "视频生成", "label": "Grok Video 1.5·1080p", "unit": "每秒", "default": 44},
)

_BY_KEY = {item["key"]: dict(item) for item in CATALOG}


class PricingConflict(ValueError):
    pass


class PricingUnavailable(RuntimeError):
    """No trusted price is available, so order acceptance must stop."""


_CACHE_LOCK = threading.RLock()
_TRUSTED_SNAPSHOTS = {}
_INITIALIZED_DBS = set()


def db_path():
    return pathlib.Path(os.environ.get("PRICING_DB") or os.environ.get("ADMIN_DB") or str(BASE / "admin_config.db"))


def _connect():
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    timeout = max(0.01, float(os.environ.get("PRICING_DB_TIMEOUT") or 1.0))
    conn = sqlite3.connect(str(path), timeout=timeout)
    conn.row_factory = sqlite3.Row
    return conn


def _init(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS admin_pricing_config(
        pricing_key TEXT PRIMARY KEY,
        points INTEGER NOT NULL CHECK(points > 0),
        configured INTEGER NOT NULL DEFAULT 1 CHECK(configured IN (0, 1)),
        updated_by TEXT NOT NULL,
        updated_at INTEGER NOT NULL,
        revision INTEGER NOT NULL DEFAULT 0
    )""")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(admin_pricing_config)").fetchall()}
    if "configured" not in columns:
        conn.execute("ALTER TABLE admin_pricing_config ADD COLUMN configured INTEGER NOT NULL DEFAULT 1")
    if "revision" not in columns:
        conn.execute("ALTER TABLE admin_pricing_config ADD COLUMN revision INTEGER NOT NULL DEFAULT 0")
    # Existing pre-revision rows keep their last timestamp only as a migration
    # seed. Every subsequent mutation receives a database-generated +1 revision.
    conn.execute("UPDATE admin_pricing_config SET revision=updated_at WHERE revision=0 AND updated_at>0")
    conn.execute("""CREATE TABLE IF NOT EXISTS admin_pricing_meta(
        id INTEGER PRIMARY KEY CHECK(id=1),
        revision INTEGER NOT NULL
    )""")
    conn.execute("INSERT OR IGNORE INTO admin_pricing_meta(id, revision) VALUES(1, 0)")
    conn.execute("""UPDATE admin_pricing_meta
                    SET revision=MAX(revision, COALESCE(
                        (SELECT MAX(revision) FROM admin_pricing_config), 0))
                    WHERE id=1""")
    conn.execute("""CREATE TABLE IF NOT EXISTS admin_pricing_audit(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pricing_key TEXT NOT NULL,
        action TEXT NOT NULL,
        before_points INTEGER NOT NULL,
        after_points INTEGER NOT NULL,
        actor TEXT NOT NULL,
        reason TEXT NOT NULL,
        created_at INTEGER NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pricing_audit_created ON admin_pricing_audit(created_at DESC, id DESC)")


def _ensure_initialized(conn):
    key = _cache_key()
    with _CACHE_LOCK:
        if key in _INITIALIZED_DBS:
            return
        _init(conn)
        conn.commit()
        _INITIALIZED_DBS.add(key)


def default_price(key):
    item = _BY_KEY.get(str(key or ""))
    if not item:
        raise KeyError("unknown pricing key: %s" % key)
    return int(item["default"])


def _cache_key():
    return str(db_path().resolve())


def _remember(snapshot):
    with _CACHE_LOCK:
        _TRUSTED_SNAPSHOTS[_cache_key()] = dict(snapshot)


def _trusted_snapshot():
    with _CACHE_LOCK:
        snapshot = _TRUSTED_SNAPSHOTS.get(_cache_key())
        return dict(snapshot) if snapshot is not None else None


def _clear_cache_for_tests():
    with _CACHE_LOCK:
        _TRUSTED_SNAPSHOTS.clear()
        _INITIALIZED_DBS.clear()


def _snapshot_from_rows(rows):
    result = {key: int(item["default"]) for key, item in _BY_KEY.items()}
    for row in rows:
        key = row["pricing_key"]
        if key in result and int(row["points"] or 0) > 0:
            result[key] = int(row["points"])
    return result


def _read_values():
    with closing(_connect()) as conn:
        _ensure_initialized(conn)
        rows = conn.execute(
            "SELECT pricing_key, points FROM admin_pricing_config"
        ).fetchall()
    snapshot = _snapshot_from_rows(rows)
    _remember(snapshot)
    return snapshot


def values():
    try:
        return _read_values()
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        snapshot = _trusted_snapshot()
        if snapshot is not None:
            return snapshot
        raise PricingUnavailable("收费配置暂不可用，未扣点，请稍后重试") from exc


def get_price(key, fallback=None):
    """Return a trusted positive integer price or stop order acceptance."""
    key = str(key or "")
    item = _BY_KEY.get(key)
    if not item:
        if fallback is None:
            raise KeyError("unknown pricing key: %s" % key)
        return max(1, int(fallback))
    return int(values()[key])


def public_catalog():
    current = values()
    return {
        "values": current,
        "items": [
            {"key": item["key"], "group": item["group"], "label": item["label"],
             "unit": item["unit"], "points": current[item["key"]]}
            for item in CATALOG
        ],
    }


def admin_catalog(audit_limit=50):
    overrides = {}
    audit = []
    with closing(_connect()) as conn:
        _ensure_initialized(conn)
        overrides = {
            row["pricing_key"]: dict(row)
            for row in conn.execute("SELECT * FROM admin_pricing_config").fetchall()
        }
        audit = [dict(row) for row in conn.execute(
            """SELECT pricing_key, action, before_points, after_points, actor, reason, created_at
               FROM admin_pricing_audit ORDER BY id DESC LIMIT ?""",
            (max(1, min(200, int(audit_limit or 50))),),
        ).fetchall()]
    _remember(_snapshot_from_rows(overrides.values()))
    items = []
    for source in CATALOG:
        item = dict(source)
        override = overrides.get(item["key"])
        item.update({
            "default_points": int(item.pop("default")),
            "points": int(override["points"]) if override else int(source["default"]),
            "configured": bool(override and override["configured"]),
            "updated_by": override["updated_by"] if override else "",
            "updated_at": int(override["updated_at"]) if override else 0,
            "version": int(override["revision"]) if override else 0,
        })
        items.append(item)
    return {"items": items, "audit": audit}


def _positive_int(value):
    if isinstance(value, bool):
        raise ValueError("收费点数必须是正整数")
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError("收费点数必须是正整数")
    if str(value).strip() not in {str(parsed), "+" + str(parsed)}:
        raise ValueError("收费点数必须是正整数")
    if parsed < 1 or parsed > 100000:
        raise ValueError("收费点数必须在 1 到 100000 之间")
    return parsed


def save(actor, body):
    body = body if isinstance(body, dict) else {}
    key = str(body.get("key") or "").strip()
    if key not in _BY_KEY:
        raise ValueError("未知收费项目")
    action = str(body.get("action") or "set").strip().lower()
    if action not in {"set", "reset"}:
        raise ValueError("未知操作")
    reason = str(body.get("reason") or "").strip()
    if len(reason) < 2 or len(reason) > 300:
        raise ValueError("请填写 2 到 300 字的调整原因")
    actor = str(actor or "admin").strip()[:120] or "admin"
    expected = body.get("version", 0)
    try:
        expected = int(expected or 0)
    except (TypeError, ValueError):
        raise ValueError("收费版本无效，请刷新后重试")
    target = default_price(key) if action == "reset" else _positive_int(body.get("points"))
    now = int(time.time() * 1000)

    with closing(_connect()) as conn:
        _ensure_initialized(conn)
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT points, configured, revision FROM admin_pricing_config WHERE pricing_key=?", (key,)
        ).fetchone()
        current_version = int(row["revision"]) if row else 0
        before = int(row["points"]) if row else default_price(key)
        if current_version != expected:
            raise PricingConflict("收费标准已被其他管理员修改，请刷新后重试")
        conn.execute("UPDATE admin_pricing_meta SET revision=revision+1 WHERE id=1")
        version = int(conn.execute(
            "SELECT revision FROM admin_pricing_meta WHERE id=1"
        ).fetchone()["revision"])
        configured = 0 if action == "reset" else 1
        conn.execute(
            """INSERT INTO admin_pricing_config
               (pricing_key, points, configured, updated_by, updated_at, revision)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(pricing_key) DO UPDATE SET
                 points=excluded.points, configured=excluded.configured,
                 updated_by=excluded.updated_by, updated_at=excluded.updated_at,
                 revision=excluded.revision""",
            (key, target, configured, actor, now, version),
        )
        conn.execute(
            """INSERT INTO admin_pricing_audit
               (pricing_key, action, before_points, after_points, actor, reason, created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (key, action, before, target, actor, reason, now),
        )
        snapshot = _snapshot_from_rows(conn.execute(
            "SELECT pricing_key, points FROM admin_pricing_config"
        ).fetchall())
        conn.commit()
    _remember(snapshot)
    return {"key": key, "points": target, "default_points": default_price(key),
            "configured": action != "reset", "updated_by": actor,
            "updated_at": now, "version": version}
