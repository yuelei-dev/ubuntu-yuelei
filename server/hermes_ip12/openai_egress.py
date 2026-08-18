# -*- coding: utf-8 -*-
"""Conservative outbound routing for Hermes OpenAI chat requests.

Hermes is released as a standalone directory, so it cannot import the content
service's egress package.  This module keeps the existing requests.Response
contract while allowing the operator to select local proxy and relay routes.

Chat requests can be billable.  A request is sent again only when failure before
delivery is provable; HTTP responses, read timeouts/resets and TLS errors are
never retried automatically.
"""
import errno
import os
import socket
import threading
import urllib.parse

import requests


_PRE_DELIVERY_ERRNOS = {
    errno.ECONNREFUSED,
    errno.EHOSTUNREACH,
    errno.ENETUNREACH,
}
_SESSION_LOCAL = threading.local()


def _env(*names):
    for name in names:
        value = str(os.environ.get(name, "") or "").strip()
        if value:
            return value
    return ""


def _chat_url(base):
    value = str(base or "").strip().rstrip("/")
    if not value:
        raise RuntimeError("OpenAI API 地址未配置")
    if value.endswith("/chat/completions"):
        return value
    if not value.endswith("/v1"):
        value += "/v1"
    return value + "/chat/completions"


def _proxy_reachable(proxy, timeout=0.5):
    try:
        parsed = urllib.parse.urlsplit(proxy)
        if not parsed.hostname or not parsed.port:
            return False
        with socket.create_connection((parsed.hostname, parsed.port), timeout=timeout):
            return True
    except Exception:
        return False


def _routes(api_base):
    """Return distinct (label, base, proxy) routes in operator-defined order."""
    official_base = _env("HERMES_OPENAI_OFFICIAL_BASE") or api_base
    relay_base = _env("HERMES_OPENAI_RELAY_BASE")
    candidates = []
    for label, proxy in (
        ("primary-proxy", _env("HERMES_EGRESS_PROXY", "EGRESS_PROXY")),
        ("fallback-proxy", _env(
            "HERMES_EGRESS_PROXY_FALLBACK", "EGRESS_PROXY_FALLBACK"
        )),
    ):
        if proxy:
            candidates.append((label, official_base, proxy))
    if relay_base:
        candidates.append(("relay", relay_base, None))
    candidates.append(("configured-base", api_base, None))

    routes = []
    seen = set()
    for label, base, proxy in candidates:
        key = (_chat_url(base), proxy or "")
        if key not in seen:
            routes.append((label, base, proxy))
            seen.add(key)
    return routes


def _session():
    session = getattr(_SESSION_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        # Only explicit Hermes routes may influence model traffic.  This avoids
        # an unrelated process-wide HTTPS_PROXY silently changing the chain.
        session.trust_env = False
        _SESSION_LOCAL.session = session
    return session


def _post_request(url, *, headers, payload, timeout, stream, proxy):
    proxies = None
    if proxy:
        proxies = {"http": proxy, "https": proxy}
    return _session().post(
        url,
        headers=headers,
        json=payload,
        timeout=timeout,
        stream=stream,
        proxies=proxies,
    )


def _exception_nodes(error):
    pending = [error]
    seen = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        for value in (
            getattr(current, "__cause__", None),
            getattr(current, "__context__", None),
            getattr(current, "reason", None),
            getattr(current, "original_error", None),
        ):
            if isinstance(value, BaseException):
                pending.append(value)
        for value in getattr(current, "args", ()):
            if isinstance(value, BaseException):
                pending.append(value)


def _pre_delivery_failure(error):
    nodes = list(_exception_nodes(error))
    if any(isinstance(node, requests.exceptions.ConnectTimeout) for node in nodes):
        return True
    if any(isinstance(node, requests.exceptions.SSLError) for node in nodes):
        return False
    if any(
        isinstance(node, requests.exceptions.Timeout)
        and not isinstance(node, requests.exceptions.ConnectTimeout)
        for node in nodes
    ):
        return False
    if any(isinstance(node, requests.exceptions.ProxyError) for node in nodes):
        return True
    if any(
        isinstance(node, (ConnectionResetError, TimeoutError, socket.timeout))
        for node in nodes
    ):
        return False
    for node in nodes:
        if isinstance(node, (socket.gaierror, ConnectionRefusedError)):
            return True
        if isinstance(node, OSError) and node.errno in _PRE_DELIVERY_ERRNOS:
            return True
    return False


def post_chat_completions(api_base, api_key, payload, *, stream=False,
                          read_timeout=180, log=None):
    """POST a Chat Completions request and return requests.Response unchanged."""
    connect_timeout = max(
        1.0,
        float(os.environ.get("HERMES_OPENAI_CONNECT_TIMEOUT", "8") or 8),
    )
    routes = _routes(api_base)
    last_error = None

    for number, (label, base, proxy) in enumerate(routes, 1):
        if proxy and not _proxy_reachable(proxy):
            if log:
                log("[hermes-egress] %s 不可达，未发送请求" % label)
            continue
        try:
            response = _post_request(
                _chat_url(base),
                headers={
                    "Authorization": "Bearer " + api_key,
                    "Content-Type": "application/json",
                },
                payload=payload,
                timeout=(connect_timeout, float(read_timeout)),
                stream=stream,
                proxy=proxy,
            )
        except Exception as error:
            last_error = error
            if not _pre_delivery_failure(error):
                if log:
                    log(
                        "[hermes-egress] %s 请求结果不确定，不自动重发: %s"
                        % (label, type(error).__name__)
                    )
                raise
            if log:
                log(
                    "[hermes-egress] %s 在送达前失败，尝试下一条通道: %s"
                    % (label, type(error).__name__)
                )
            continue

        # Any HTTP response proves delivery.  Preserve the old error contract
        # and never resend a possibly billable request because of status code.
        if response.status_code != 200:
            raise RuntimeError(
                "API %s: %s" % (response.status_code, response.text[:300])
            )
        return response

    if last_error is not None:
        raise last_error
    raise RuntimeError("Hermes OpenAI 出境通道不可用")
