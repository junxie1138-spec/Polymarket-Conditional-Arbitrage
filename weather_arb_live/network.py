"""DNS-safe HTTP session helpers.

This machine has unreliable DNS for outbound HTTPS. Install a small
DNS-over-HTTPS resolver shim before creating requests sessions so every HTTP
call made by this package and the CLOB SDK uses the patched resolver.
"""

from __future__ import annotations

import json
import socket
import ssl
import urllib.parse
import urllib.request

import requests

_DOH_URL = "https://cloudflare-dns.com/dns-query"
_RESOLVER_IPS = ["1.1.1.1", "1.0.0.1"]
_cache: dict[str, list[str]] = {}
_orig_getaddrinfo = socket.getaddrinfo
_installed = False


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
    session.headers["User-Agent"] = "polymarket-weather-live-bot/0.1"
    return session
