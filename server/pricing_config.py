#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Runtime pricing registry shared by the admin, content, image and leadgen services.

Prices are positive integer Huangque points. Overrides live in the existing admin
SQLite database and are read at order acceptance time, so changes do not require a
service restart. The code catalog remains the fail-safe source for defaults.
"""

from contextlib import closing
import os
import pathlib
import sqlite3
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
    {"key": "sora.sora_2.720p.per_sec", "group": "视频生成", "label": "Sora 2·720p", "unit": "每秒", "default": 30},
    {"key": "sora.sora_2_pro.720p.per_sec", "group": "视频生成", "label": "Sora 2 Pro·720p", "unit": "每秒", "default": 90},
    {"key": "sora.sora_2_pro.1024p.per_sec", "group": "视频生成", "label": "Sora 2 Pro·1024p", "unit": "每秒", "default": 150},
    {"key": "sora.sora_2_pro.1080p.per_sec", "group": "视频生成", "label": "Sora 2 Pro·1080p", "unit": "每秒", "default": 210},
)

_BY_KEY = {item["key"]: dict(item) for item in CATALOG}


class PricingConflict(ValueError):
    pass


def db_path():
    return pathlib.Path(os.environ.get("PRICING_DB") or os.environ.get("ADMIN_DB") or str(BASE / "admin_config.db"))


def _connect():
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _init(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS admin_pricing_config(
        pricing_key TEXT PRIMARY KEY,
        points INTEGER NOT NULL CHECK(points > 0),
        updated_by TEXT NOT NULL,
        updated_at INTEGER NOT NULL
    )""")
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


def default_price(key):
    item = _BY_KEY.get(str(key or ""))
    if not item:
        raise KeyError("unknown pricing key: %s" % key)
    return int(item["default"])


def get_price(key, fallback=None):
    """Return the current positive integer price; fail safely to the code default."""
    key = str(key or "")
    item = _BY_KEY.get(key)
    if not item:
        if fallback is None:
            raise KeyError("unknown pricing key: %s" % key)
        return max(1, int(fallback))
    try:
        with closing(_connect()) as conn:
            _init(conn)
            row = conn.execute(
                "SELECT points FROM admin_pricing_config WHERE pricing_key=?", (key,)
            ).fetchone()
        value = int(row["points"]) if row else int(item["default"])
        return value if value > 0 else int(item["default"])
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return int(item["default"])


def values():
    result = {key: int(item["default"]) for key, item in _BY_KEY.items()}
    try:
        with closing(_connect()) as conn:
            _init(conn)
            rows = conn.execute("SELECT pricing_key, points FROM admin_pricing_config").fetchall()
        for row in rows:
            if row["pricing_key"] in result and int(row["points"] or 0) > 0:
                result[row["pricing_key"]] = int(row["points"])
    except (OSError, sqlite3.Error, TypeError, ValueError):
        pass
    return result


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
        _init(conn)
        overrides = {
            row["pricing_key"]: dict(row)
            for row in conn.execute("SELECT * FROM admin_pricing_config").fetchall()
        }
        audit = [dict(row) for row in conn.execute(
            """SELECT pricing_key, action, before_points, after_points, actor, reason, created_at
               FROM admin_pricing_audit ORDER BY id DESC LIMIT ?""",
            (max(1, min(200, int(audit_limit or 50))),),
        ).fetchall()]
    items = []
    for source in CATALOG:
        item = dict(source)
        override = overrides.get(item["key"])
        item.update({
            "default_points": int(item.pop("default")),
            "points": int(override["points"]) if override else int(source["default"]),
            "configured": bool(override),
            "updated_by": override["updated_by"] if override else "",
            "updated_at": int(override["updated_at"]) if override else 0,
            "version": int(override["updated_at"]) if override else 0,
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
        _init(conn)
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT points, updated_at FROM admin_pricing_config WHERE pricing_key=?", (key,)
        ).fetchone()
        current_version = int(row["updated_at"]) if row else 0
        before = int(row["points"]) if row else default_price(key)
        if current_version != expected:
            raise PricingConflict("收费标准已被其他管理员修改，请刷新后重试")
        if action == "reset":
            conn.execute("DELETE FROM admin_pricing_config WHERE pricing_key=?", (key,))
            version = 0
        else:
            conn.execute(
                """INSERT INTO admin_pricing_config(pricing_key, points, updated_by, updated_at)
                   VALUES(?,?,?,?)
                   ON CONFLICT(pricing_key) DO UPDATE SET
                     points=excluded.points, updated_by=excluded.updated_by, updated_at=excluded.updated_at""",
                (key, target, actor, now),
            )
            version = now
        conn.execute(
            """INSERT INTO admin_pricing_audit
               (pricing_key, action, before_points, after_points, actor, reason, created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (key, action, before, target, actor, reason, now),
        )
        conn.commit()
    return {"key": key, "points": target, "default_points": default_price(key),
            "configured": action != "reset", "updated_by": actor,
            "updated_at": now, "version": version}
