# -*- coding: utf-8 -*-
"""Durable paid-job recovery helpers shared by content workers."""
def refund_once(jdb, jobs_store, points_domain, job_id, username, cost):
    def refund(refund_username, amount):
        try:
            points_domain.refund_points(
                refund_username, amount, "job#%d" % job_id,
                transaction_key="job:%d:refund" % job_id,
            )
            return True
        except Exception as exc:
            print("[refund] job=%d retryable failure: %s" % (
                job_id, type(exc).__name__), flush=True)
            return False
    return jobs_store.refund_once(jdb, job_id, username, cost, refund)


def retry_failed_refunds(jdb, jobs_store, points_domain, limit=100):
    """Retry only persisted unpaid refunds; unrelated errors cannot starve them."""
    rows = jobs_store.pending_refunds(jdb, limit)
    recovered = 0
    for row in rows:
        if refund_once(
                jdb, jobs_store, points_domain,
                row["id"], row["username"], row["cost"]):
            recovered += 1
    return recovered


def reconcile_local_uploads(limit):
    """Best-effort bridge kept here so core remains a thin orchestrator."""
    try:
        from . import breakdown
        return breakdown.reconcile_local_upload_submissions(limit)
    except Exception:
        return 0
