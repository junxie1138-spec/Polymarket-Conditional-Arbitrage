"""DNS-safe HTTP session helpers."""

from __future__ import annotations

import json
import socket
import ssl
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

import requests

_DOH_URL = "https://cloudflare-dns.com/dns-query"
_RESOLVER_IPS = ["1.1.1.1", "1.0.0.1"]
_cache: dict[str, list[str]] = {}
_orig_getaddrinfo = socket.getaddrinfo
_installed = False
RETRYABLE_HTTP_STATUSES = {429, 500, 502, 503, 504}


def _doh_resolve(hostname: str) -> list[str]:
    if hostname in _cache:
        return _cache[hostname]

    last_err: Exception | None = None
    for resolver_ip in _RESOLVER_IPS:
        try:
            url = f"https://{resolver_ip}/dns-query?name={urllib.parse.quote(hostname)}&type=A"
            req = urllib.request.Request(url, headers={"accept": "application/dns-json"})
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with urllib.request.urlopen(req, timeout=10, context=ctx) as response:
                data = json.loads(response.read())
            ips = [a["data"] for a in data.get("Answer", []) if a.get("type") == 1]
            if ips:
                _cache[hostname] = ips
                return ips
        except Exception as exc:  # pragma: no cover - network failure branch
            last_err = exc
    raise RuntimeError(f"DoH resolution failed for {hostname}: {last_err}")


def _patched_getaddrinfo(host, port, *args, **kwargs):
    if isinstance(host, str) and not host.replace(".", "").isdigit() and host not in {"localhost"}:
        try:
            results = []
            for ip in _doh_resolve(host):
                results.append((socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)))
            if results:
                return results
        except Exception:
            pass
    return _orig_getaddrinfo(host, port, *args, **kwargs)


def install() -> None:
    global _installed
    if not _installed:
        socket.getaddrinfo = _patched_getaddrinfo
        _installed = True


def get_session() -> requests.Session:
    install()
    session = requests.Session()
    session.headers["User-Agent"] = "polymarket-conditional-arbitrage/0.1"
    return session


def response_status(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status is not None:
        return int(status)
    status = getattr(exc, "status_code", None)
    if status is not None:
        return int(status)
    return None


def is_retryable_status(status: int | None) -> bool:
    return status in RETRYABLE_HTTP_STATUSES


def is_retryable_exception(exc: Exception) -> bool:
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return True
    return is_retryable_status(response_status(exc))


def sleep_for_attempt(attempt: int, *, base_seconds: float = 1.0, cap_seconds: float = 30.0) -> None:
    time.sleep(min(cap_seconds, base_seconds * (2 ** max(0, attempt - 1))))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _record_request_meta(
    meta: dict[str, Any] | None,
    *,
    started_at: datetime,
    attempts: int,
    backoff_seconds: float,
    status_code: int | None,
    error: Exception | None,
) -> None:
    if meta is None:
        return
    completed_at = _utc_now()
    meta.update(
        {
            "started_at": started_at,
            "completed_at": completed_at,
            "latency_seconds": max(0.0, (completed_at - started_at).total_seconds()),
            "attempts": attempts,
            "retries": max(0, attempts - 1),
            "backoff_seconds": max(0.0, backoff_seconds),
            "status_code": status_code,
            "error": f"{type(error).__name__}: {error}" if error is not None else None,
        }
    )


def get_json_with_retries(
    session: requests.Session,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    timeout: float = 30,
    attempts: int = 3,
    backoff_seconds: float = 1.0,
    meta: dict[str, Any] | None = None,
) -> Any:
    """GET JSON with bounded retry for transient network/server failures."""
    last_exc: Exception | None = None
    started_at = _utc_now()
    total_backoff = 0.0
    for attempt in range(1, attempts + 1):
        try:
            response = session.get(url, params=params, timeout=timeout)
            if is_retryable_status(response.status_code) and attempt < attempts:
                total_backoff += min(30.0, backoff_seconds * (2 ** max(0, attempt - 1)))
                sleep_for_attempt(attempt, base_seconds=backoff_seconds)
                continue
            response.raise_for_status()
            _record_request_meta(
                meta,
                started_at=started_at,
                attempts=attempt,
                backoff_seconds=total_backoff,
                status_code=response.status_code,
                error=None,
            )
            return response.json()
        except Exception as exc:
            last_exc = exc
            status = response_status(exc)
            if attempt == attempts or not is_retryable_exception(exc):
                _record_request_meta(
                    meta,
                    started_at=started_at,
                    attempts=attempt,
                    backoff_seconds=total_backoff,
                    status_code=status,
                    error=exc,
                )
                raise
            total_backoff += min(30.0, backoff_seconds * (2 ** max(0, attempt - 1)))
            sleep_for_attempt(attempt, base_seconds=backoff_seconds)
    raise RuntimeError(f"GET failed after retries: {url}") from last_exc


def post_json_with_retries(
    session: requests.Session,
    url: str,
    *,
    json_body: Any,
    timeout: float = 30,
    attempts: int = 3,
    backoff_seconds: float = 1.0,
    meta: dict[str, Any] | None = None,
) -> Any:
    """POST JSON and parse JSON response with bounded transient retries."""
    last_exc: Exception | None = None
    started_at = _utc_now()
    total_backoff = 0.0
    for attempt in range(1, attempts + 1):
        try:
            response = session.post(url, json=json_body, timeout=timeout)
            if is_retryable_status(response.status_code) and attempt < attempts:
                total_backoff += min(30.0, backoff_seconds * (2 ** max(0, attempt - 1)))
                sleep_for_attempt(attempt, base_seconds=backoff_seconds)
                continue
            response.raise_for_status()
            _record_request_meta(
                meta,
                started_at=started_at,
                attempts=attempt,
                backoff_seconds=total_backoff,
                status_code=response.status_code,
                error=None,
            )
            return response.json()
        except Exception as exc:
            last_exc = exc
            status = response_status(exc)
            if attempt == attempts or not is_retryable_exception(exc):
                _record_request_meta(
                    meta,
                    started_at=started_at,
                    attempts=attempt,
                    backoff_seconds=total_backoff,
                    status_code=status,
                    error=exc,
                )
                raise
            total_backoff += min(30.0, backoff_seconds * (2 ** max(0, attempt - 1)))
            sleep_for_attempt(attempt, base_seconds=backoff_seconds)
    raise RuntimeError(f"POST failed after retries: {url}") from last_exc
