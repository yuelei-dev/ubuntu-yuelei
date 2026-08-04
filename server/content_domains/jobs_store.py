# -*- coding: utf-8 -*-
"""jobs 表的状态机与退点幂等 —— 三个服务共用的安全网。

content_jobs.db 的 jobs 表被三个进程共写：
    content_api  (8096)  image/copy/audio/video/tryon/xiaole_video
    leadgen_api  (8100)  collect/leads
    imggen_api   (8101)  Nano Banana 作图
而 reaper（超时回收）只在 content_api 里跑。

这意味着任何一个服务写终态时不做 CAS，都会出现：
    reaper 在第 360s 判超时 → 退点 → worker 在第 3686s 跑完 → 无条件写 done
    → 用户既拿到结果又拿回点数（线上 jobs 1170/1164/1118…共 21 条，280 点）

这段逻辑此前在三个文件里各抄了一份，连注释措辞都一样，只有最后调用的退点函数不同。
同一个资金 bug 因此在 core → imggen → leadgen 上依次踩过三次。抽到这里统一维护。

本模块只依赖标准库，不 import core —— 三个服务都能安全 import（leadgen/imggen 本来
就在 `from content_domains import cos / assets_store`）。
"""
import json
import os
import time
import uuid
from contextlib import closing


REFUND_UNCLAIMED = 0
REFUND_CONFIRMED = 1
REFUND_PENDING = 2
REFUND_LEASE_SECONDS = 45

# ship 部署时写入的精确 commit SHA（content_api.py 所在目录，单行文本）。
# 健康检查每次读文件；建任务写 jobs.service_sha 用启动时的缓存值，不每次碰盘。
DEPLOY_VERSION_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".deploy-version")


def read_deploy_sha():
    """读取 .deploy-version；文件不存在或读取失败返回 "unknown"，绝不抛异常。"""
    try:
        with open(DEPLOY_VERSION_FILE, "r", encoding="utf-8") as f:
            return f.read().strip() or "unknown"
    except Exception:
        return "unknown"


def _read_service_sha():
    """启动时读一次 .deploy-version 供 jobs.service_sha 用；读不到存 NULL。"""
    try:
        with open(DEPLOY_VERSION_FILE, "r", encoding="utf-8") as f:
            return f.read().strip() or None
    except Exception:
        return None


SERVICE_SHA = _read_service_sha()


def public_dict(row, phase=None):
    data = {key: row[key] for key in (
        "id", "kind", "username", "cost", "status", "result", "error", "created_at", "updated_at")}
    refund_state = int(row["refunded"] or 0) if "refunded" in row.keys() else 0
    data["refunded"] = refund_state == REFUND_CONFIRMED
    if refund_state == REFUND_PENDING:
        data["refund_pending"] = True
    if data.get("result"):
        try:
            data["result"] = json.loads(data["result"])
        except Exception:
            pass
    terminal_phase = {"done": "done", "error": "failed", "failed": "failed"}.get(data["status"])
    if terminal_phase is not None or phase is not None:
        data["phase"] = terminal_phase or phase
    return data


def ensure_owner_column(jdb):
    """保证 jobs.owner 存在（#511）。三个服务启动时各调一次，谁先起谁建，与部署顺序无关。

    没有这列时，content 的 pending 重排/孤儿回收会把 imggen、leadgen 的任务当成自己的：
    重排会用 content 的处理器去跑别家的 payload，重启回收会把别家正在飞的任务判失败退点。
    历史行 owner 为 NULL —— 那时只有 content 会留 pending/被回收，故 content 侧用
    COALESCE(owner,'content') 把它们仍归自己，语义与建列前完全一致。
    """
    with closing(jdb()) as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(jobs)").fetchall()}
        if "owner" not in cols:
            c.execute("ALTER TABLE jobs ADD COLUMN owner TEXT")
            c.commit()


def ensure_service_sha_column_on_conn(conn):
    """在同一连接上保证 jobs.service_sha 存在（PRAGMA 守卫的 ALTER，不动既有数据）。

    供已持有连接的写入路径（core 建任务、imggen/leadgen 直写 jobs 等）在 INSERT 前兜底：
    服务启动时 ensure_service_sha_column 已建列，这里是第二道保险 —— 任何调用方漏了启动
    ensure，也不至于让 INSERT 直接 500。SQLite 的 ALTER 是事务性的，跟随外层事务提交/回滚。
    历史行 service_sha 为 NULL —— 显示「版本未知」，不回填。
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    if "service_sha" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN service_sha TEXT")


def ensure_service_sha_column(jdb):
    """保证 jobs.service_sha 存在（任务-代码版本绑定）。与 ensure_owner_column 同款：
    共写 jobs 表的服务启动时各调一次，谁先起谁建；不建列则 INSERT 带 service_sha 会直接 500。
    """
    with closing(jdb()) as c:
        ensure_service_sha_column_on_conn(c)
        c.commit()


def ensure_refund_lease_columns_on_conn(conn):
    """增加可恢复退款租约列；旧行保持未认领状态。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    if "refund_lease_token" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN refund_lease_token TEXT")
    if "refund_lease_until" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN refund_lease_until INTEGER DEFAULT 0")


def ensure_refund_lease_columns(jdb):
    with closing(jdb()) as c:
        ensure_refund_lease_columns_on_conn(c)
        c.commit()


def set_terminal(jdb, job_id, status, result=None, error=None, from_states=("running",)):
    """CAS 抢终态：仅当当前状态在 from_states 内才迁移，返回是否抢到(rowcount>=1)。

    败者不写状态、不做副作用 —— 谁先抢到谁定终态，reaper 与 worker 之间无竞态。

    from_states 默认只认 running（与 reaper 竞争的正常路径）。run_job 的 except 分支要传
    ("pending","running")：若异常发生在把任务改成 running 之前(如 SQLite 锁冲突)，任务还停在
    pending，只认 running 会让 CAS 失败 → 不退点 → 预扣的点永久丢失，而 reaper 只扫 running、
    从不回收 pending，没人能救它。

    jdb: 返回 sqlite3 连接的工厂函数（各服务连的是同一个 content_jobs.db）。
    """
    now = int(time.time())
    holes = ",".join("?" * len(from_states))
    with closing(jdb()) as c:
        if status == "done":
            cur = c.execute(
                "UPDATE jobs SET status='done', result=?, updated_at=? WHERE id=? AND status IN (%s)" % holes,
                (json.dumps(result, ensure_ascii=False), now, job_id) + tuple(from_states))
        else:
            cur = c.execute(
                "UPDATE jobs SET status='error', error=?, updated_at=? WHERE id=? AND status IN (%s)" % holes,
                (str(error or "")[:300], now, job_id) + tuple(from_states))
        c.commit()
        return cur.rowcount >= 1


def claim_running(jdb, job_id):
    """CAS 认领：只有 pending 才能被本次执行接管。返回是否抢到。

    防同一个 job 被两个 worker 跑两遍（重启恢复 + 正常入队可能撞车）。
    """
    with closing(jdb()) as c:
        cur = c.execute("UPDATE jobs SET status='running', updated_at=? WHERE id=? AND status='pending'",
                        (int(time.time()), job_id))
        c.commit()
        return cur.rowcount >= 1


def _release_refund_lease(jdb, job_id, lease_token):
    with closing(jdb()) as c:
        c.execute(
            "UPDATE jobs SET refunded=?,refund_lease_token=NULL,refund_lease_until=0 "
            "WHERE id=? AND refunded=? AND refund_lease_token=?",
            (REFUND_UNCLAIMED, job_id, REFUND_PENDING, lease_token))
        c.commit()


def refund_once(jdb, job_id, username, cost, refund, lease_seconds=REFUND_LEASE_SECONDS):
    """可恢复退点：0=未处理、2=租约处理中、1=上游已确认。

    只有持有当前随机租约的调用方可落最终 1。进程在 CAS 后、发网前退出时会保留状态 2；
    租约过期后 scanner 使用同一稳定交易键重放，避免永久漏退和双 scanner 重复执行。
    """
    try:
        cost = int(cost or 0)
    except (TypeError, ValueError):
        cost = 0
    if cost <= 0:
        return False
    try:
        lease_seconds = max(1, int(lease_seconds or REFUND_LEASE_SECONDS))
    except (TypeError, ValueError):
        lease_seconds = REFUND_LEASE_SECONDS
    now = int(time.time())
    lease_token = uuid.uuid4().hex
    with closing(jdb()) as c:
        c.execute("BEGIN IMMEDIATE")
        ensure_refund_lease_columns_on_conn(c)
        cur = c.execute(
            "UPDATE jobs SET refunded=?,refund_lease_token=?,refund_lease_until=? "
            "WHERE id=? AND status='error' AND (COALESCE(refunded,0)=? OR "
            "(refunded=? AND COALESCE(refund_lease_until,0)<=?))",
            (REFUND_PENDING, lease_token, now + lease_seconds, job_id,
             REFUND_UNCLAIMED, REFUND_PENDING, now))
        c.commit()
        if cur.rowcount < 1:
            return False
    try:
        succeeded = bool(refund(username, cost))
    except Exception:
        _release_refund_lease(jdb, job_id, lease_token)
        raise
    if not succeeded:
        _release_refund_lease(jdb, job_id, lease_token)
        return False
    with closing(jdb()) as c:
        cur = c.execute(
            "UPDATE jobs SET refunded=?,refund_lease_token=NULL,refund_lease_until=0 "
            "WHERE id=? AND refunded=? AND refund_lease_token=?",
            (REFUND_CONFIRMED, job_id, REFUND_PENDING, lease_token))
        c.commit()
        return cur.rowcount >= 1


def pending_refunds(jdb, limit=100):
    """只返回到期的退款任务，避免被无关 error 行饿死。"""
    bounded = max(1, min(int(limit or 100), 500))
    now = int(time.time())
    with closing(jdb()) as c:
        ensure_refund_lease_columns_on_conn(c)
        c.commit()
        return c.execute(
            "SELECT id,username,cost FROM jobs WHERE status='error' AND cost>0 AND "
            "(COALESCE(refunded,0)=? OR (refunded=? AND COALESCE(refund_lease_until,0)<=?)) "
            "ORDER BY id ASC LIMIT ?",
            (REFUND_UNCLAIMED, REFUND_PENDING, now, bounded)).fetchall()
