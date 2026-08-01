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
import time
from contextlib import closing


def public_dict(row, phase=None):
    data = {key: row[key] for key in (
        "id", "kind", "username", "cost", "status", "result", "error", "created_at", "updated_at")}
    # refunded=2 means the durable refund attempt is still pending.  Only 1 is
    # a confirmed credit and may be shown to callers as refunded.
    data["refunded"] = (int(row["refunded"] or 0) == 1) if "refunded" in row.keys() else False
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


def refund_once(jdb, job_id, username, cost, refund, *,
                recover_pending=False, keep_pending=False):
    """退点 job 级幂等：0=未退，2=处理中，1=已确认到账。

    先持久化处理中，再调用退款；只有退款明确成功后才写 1。进程若在两步之间退出，
    状态会留在 2，使用稳定上游交易键的调用方可通过 recover_pending=True 安全重放。
    旧调用方默认仍在普通失败时把 2 放回 0，保持既有重试语义。

    refund(username, cost) -> 真值表示退点成功。调用方各自决定怎么退：
        content_api  points.safe_refund_points（吞异常，永远算成功）
        imggen_api   auth 的 /api/auth/points/refund（无兜底，失败要回滚）
        leadgen_api  auth 优先 + 直写 users.db 兜底
    """
    try:
        cost = int(cost or 0)
    except (TypeError, ValueError):
        cost = 0
    if cost <= 0:
        return False
    with closing(jdb()) as c:
        # 双重保险：仅当终态确为 error 且尚未退过，才认领退款处理权。
        cur = c.execute(
            "UPDATE jobs SET refunded=2 WHERE id=? AND refunded=0 AND status='error'",
            (job_id,),
        )
        c.commit()
        if cur.rowcount < 1:
            row = c.execute(
                "SELECT status,refunded FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            if not (recover_pending and row and row["status"] == "error"
                    and int(row["refunded"] or 0) == 2):
                return False   # 已确认退款 / 非 error / 其他调用正在处理
    try:
        succeeded = bool(refund(username, cost))
    except Exception:
        succeeded = False
    if succeeded:
        with closing(jdb()) as c:
            cur = c.execute(
                "UPDATE jobs SET refunded=1 WHERE id=? AND refunded=2 AND status='error'",
                (job_id,),
            )
            c.commit()
            if cur.rowcount >= 1:
                return True
            row = c.execute("SELECT refunded FROM jobs WHERE id=?", (job_id,)).fetchone()
            return bool(row and int(row["refunded"] or 0) == 1)
    if keep_pending:
        return False
    with closing(jdb()) as c:   # 兼容旧调用：普通失败释放处理权，留给既有路径重试
        c.execute("UPDATE jobs SET refunded=0 WHERE id=? AND refunded=2", (job_id,))
        c.commit()
    return False
