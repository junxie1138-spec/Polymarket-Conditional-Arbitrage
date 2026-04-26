from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import socket
import ssl
import struct
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

from . import config, network
from .strategy import token_ids_from_market


logger = logging.getLogger(__name__)

MAX_FRAME_BYTES = 8 * 1024 * 1024
SUBSCRIPTION_CHUNK_SIZE = 100


class WebSocketError(RuntimeError):
    pass


class WebSocketClosed(WebSocketError):
    pass


def _as_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _valid_best_bid_ask(best_bid: float | None, best_ask: float | None) -> bool:
    return (
        best_bid is not None
        and best_ask is not None
        and 0.0 < best_bid <= best_ask < 1.0
    )


def _best_from_side(levels: Iterable[Any], *, is_bid: bool) -> float | None:
    prices: list[float] = []
    for level in levels or []:
        if isinstance(level, dict):
            value = level.get("price")
        elif isinstance(level, (list, tuple)) and level:
            value = level[0]
        else:
            value = None
        price = _as_float(value)
        if price is not None:
            prices.append(price)
    if not prices:
        return None
    return max(prices) if is_bid else min(prices)


def unique_market_token_ids(markets: Iterable[dict], *, max_tokens: int | None = None) -> list[str]:
    token_ids: list[str] = []
    seen: set[str] = set()
    for market in markets:
        for token_id in token_ids_from_market(market):
            if not token_id or token_id in seen:
                continue
            seen.add(token_id)
            token_ids.append(token_id)
            if max_tokens is not None and len(token_ids) >= max_tokens:
                return token_ids
    return token_ids


def unique_market_condition_ids(markets: Iterable[dict]) -> list[str]:
    condition_ids: list[str] = []
    seen: set[str] = set()
    for market in markets:
        condition_id = str(market.get("conditionId") or "").strip()
        if not condition_id or condition_id in seen:
            continue
        seen.add(condition_id)
        condition_ids.append(condition_id)
    return condition_ids


def _chunked(items: Iterable[str], size: int = SUBSCRIPTION_CHUNK_SIZE) -> Iterable[list[str]]:
    chunk: list[str] = []
    for item in items:
        chunk.append(item)
        if len(chunk) >= size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


@dataclass(frozen=True)
class BookQuote:
    token_id: str
    best_bid: float | None
    best_ask: float | None
    updated_at: float
    source: str
    market: str | None = None

    @property
    def midpoint(self) -> float | None:
        if not _valid_best_bid_ask(self.best_bid, self.best_ask):
            return None
        return round((float(self.best_bid) + float(self.best_ask)) / 2.0, 10)


class BestBidAskCache:
    def __init__(self) -> None:
        self._quotes: dict[str, BookQuote] = {}
        self._lock = threading.RLock()

    def update_quote(
        self,
        token_id: str,
        *,
        best_bid: Any,
        best_ask: Any,
        source: str,
        market: str | None = None,
        now: float | None = None,
    ) -> bool:
        normalized_token = str(token_id or "").strip()
        if not normalized_token:
            return False
        bid = _as_float(best_bid)
        ask = _as_float(best_ask)
        quote = BookQuote(
            token_id=normalized_token,
            best_bid=bid,
            best_ask=ask,
            updated_at=time.time() if now is None else now,
            source=source,
            market=market,
        )
        with self._lock:
            self._quotes[normalized_token] = quote
        return quote.midpoint is not None

    def apply_message(self, payload: Any, *, now: float | None = None) -> int:
        if isinstance(payload, list):
            return sum(self.apply_message(item, now=now) for item in payload)
        if not isinstance(payload, dict):
            return 0

        event_type = str(payload.get("event_type") or payload.get("type") or "").lower()
        if event_type == "book":
            token_id = str(payload.get("asset_id") or payload.get("assetId") or "")
            if not token_id:
                return 0
            return int(
                self.update_quote(
                    token_id,
                    best_bid=_best_from_side(payload.get("bids") or payload.get("buy") or [], is_bid=True),
                    best_ask=_best_from_side(payload.get("asks") or payload.get("sell") or [], is_bid=False),
                    source="book",
                    market=str(payload.get("market") or "") or None,
                    now=now,
                )
            )

        if event_type == "best_bid_ask":
            token_id = str(payload.get("asset_id") or payload.get("assetId") or "")
            if not token_id:
                return 0
            return int(
                self.update_quote(
                    token_id,
                    best_bid=payload.get("best_bid"),
                    best_ask=payload.get("best_ask"),
                    source="best_bid_ask",
                    market=str(payload.get("market") or "") or None,
                    now=now,
                )
            )

        if event_type == "price_change":
            updated = 0
            for change in payload.get("price_changes") or []:
                if not isinstance(change, dict):
                    continue
                token_id = str(change.get("asset_id") or change.get("assetId") or "")
                if not token_id:
                    continue
                updated += int(
                    self.update_quote(
                        token_id,
                        best_bid=change.get("best_bid"),
                        best_ask=change.get("best_ask"),
                        source="price_change",
                        market=str(payload.get("market") or "") or None,
                        now=now,
                    )
                )
            return updated
        return 0

    def quote(self, token_id: str, *, max_age_seconds: float | None = None, now: float | None = None) -> BookQuote | None:
        current_time = time.time() if now is None else now
        with self._lock:
            quote = self._quotes.get(str(token_id))
        if quote is None:
            return None
        if max_age_seconds is not None and current_time - quote.updated_at > max_age_seconds:
            return None
        return quote

    def midpoint(self, token_id: str, *, max_age_seconds: float | None = None, now: float | None = None) -> float | None:
        quote = self.quote(token_id, max_age_seconds=max_age_seconds, now=now)
        return None if quote is None else quote.midpoint

    def snapshot(self) -> dict[str, BookQuote]:
        with self._lock:
            return dict(self._quotes)


class SimpleWebSocket:
    def __init__(self, sock: socket.socket):
        self._sock = sock
        self._send_lock = threading.Lock()

    @classmethod
    def connect(cls, url: str, *, timeout: float = 10.0) -> "SimpleWebSocket":
        network.install()
        parsed = urlparse(url)
        if parsed.scheme not in {"ws", "wss"}:
            raise WebSocketError(f"unsupported websocket scheme: {parsed.scheme}")
        host = parsed.hostname
        if not host:
            raise WebSocketError(f"missing websocket host: {url}")
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"

        raw_sock = socket.create_connection((host, port), timeout=timeout)
        try:
            sock = raw_sock
            if parsed.scheme == "wss":
                sock = ssl.create_default_context().wrap_socket(raw_sock, server_hostname=host)
            sock.settimeout(timeout)

            sec_key = base64.b64encode(os.urandom(16)).decode("ascii")
            request = (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {host}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {sec_key}\r\n"
                "Sec-WebSocket-Version: 13\r\n"
                "User-Agent: polymarket-weather-live-bot/0.1\r\n"
                "\r\n"
            )
            sock.sendall(request.encode("ascii"))
            headers = cls._read_http_headers(sock)
            status_line = headers.split("\r\n", 1)[0]
            if " 101 " not in status_line:
                raise WebSocketError(f"websocket upgrade failed: {status_line}")
            expected_accept = base64.b64encode(
                hashlib.sha1((sec_key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
            ).decode("ascii")
            if f"sec-websocket-accept: {expected_accept.lower()}" not in headers.lower():
                raise WebSocketError("websocket upgrade missing expected accept key")
            return cls(sock)
        except Exception:
            raw_sock.close()
            raise

    @staticmethod
    def _read_http_headers(sock: socket.socket) -> str:
        data = bytearray()
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                raise WebSocketClosed("connection closed during websocket upgrade")
            data.extend(chunk)
            if len(data) > 65536:
                raise WebSocketError("websocket upgrade response too large")
        return data.split(b"\r\n\r\n", 1)[0].decode("iso-8859-1", errors="replace")

    def send_text(self, message: str) -> None:
        self._send_frame(0x1, message.encode("utf-8"))

    def _send_frame(self, opcode: int, payload: bytes = b"") -> None:
        length = len(payload)
        header = bytearray([0x80 | opcode])
        if length < 126:
            header.append(0x80 | length)
        elif length <= 0xFFFF:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        with self._send_lock:
            self._sock.sendall(bytes(header) + mask + masked)

    def recv_text(self, *, timeout: float = 1.0) -> str:
        self._sock.settimeout(timeout)
        fragments: list[bytes] = []
        while True:
            first = self._recv_exact(2)
            fin = bool(first[0] & 0x80)
            opcode = first[0] & 0x0F
            masked = bool(first[1] & 0x80)
            length = first[1] & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._recv_exact(8))[0]
            if length > MAX_FRAME_BYTES:
                raise WebSocketError(f"websocket frame too large: {length}")
            mask = self._recv_exact(4) if masked else b""
            payload = self._recv_exact(length) if length else b""
            if masked:
                payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))

            if opcode == 0x8:
                self._send_frame(0x8)
                raise WebSocketClosed("websocket close frame received")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode not in {0x0, 0x1}:
                continue
            fragments.append(payload)
            if fin:
                return b"".join(fragments).decode("utf-8", errors="replace")

    def _recv_exact(self, length: int) -> bytes:
        data = bytearray()
        while len(data) < length:
            chunk = self._sock.recv(length - len(data))
            if not chunk:
                raise WebSocketClosed("websocket connection closed")
            data.extend(chunk)
        return bytes(data)

    def close(self) -> None:
        try:
            self._send_frame(0x8)
        except Exception:
            pass
        try:
            self._sock.close()
        except Exception:
            pass

    def __enter__(self) -> "SimpleWebSocket":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()


class _SubscriptionStream:
    def __init__(
        self,
        *,
        name: str,
        url: str,
        initial_payload: Callable[[list[str]], dict[str, Any]],
        subscribe_payload: Callable[[list[str]], dict[str, Any]],
        unsubscribe_payload: Callable[[list[str]], dict[str, Any]],
        message_handler: Callable[[Any], None],
        logger_: logging.Logger | None = None,
        heartbeat_seconds: float | None = None,
        reconnect_min_seconds: float | None = None,
        reconnect_max_seconds: float | None = None,
    ):
        self.name = name
        self.url = url
        self._initial_payload = initial_payload
        self._subscribe_payload = subscribe_payload
        self._unsubscribe_payload = unsubscribe_payload
        self._message_handler = message_handler
        self._logger = logger_ or logger
        self._heartbeat_seconds = heartbeat_seconds or config.ws_heartbeat_seconds()
        self._reconnect_min_seconds = reconnect_min_seconds or config.ws_reconnect_min_seconds()
        self._reconnect_max_seconds = reconnect_max_seconds or config.ws_reconnect_max_seconds()
        self._desired_ids: tuple[str, ...] = ()
        self._lock = threading.RLock()
        self._changed = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._connect_count = 0
        self._reconnect_count = 0
        self._last_connected_at: float | None = None
        self._last_error: str | None = None

    @property
    def connect_count(self) -> int:
        with self._lock:
            return self._connect_count

    @property
    def reconnect_count(self) -> int:
        with self._lock:
            return self._reconnect_count

    @property
    def last_error(self) -> str | None:
        with self._lock:
            return self._last_error

    def set_ids(self, ids: Iterable[str]) -> int:
        unique_ids = tuple(dict.fromkeys(str(item).strip() for item in ids if str(item or "").strip()))
        with self._lock:
            changed = unique_ids != self._desired_ids
            self._desired_ids = unique_ids
            if self._thread is None or not self._thread.is_alive():
                self._stop.clear()
                self._thread = threading.Thread(
                    target=self._run,
                    name=f"weather-arb-{self.name}-ws",
                    daemon=True,
                )
                self._thread.start()
        if changed:
            self._changed.set()
        return len(unique_ids)

    def stop(self, *, timeout: float = 2.0) -> None:
        self._stop.set()
        self._changed.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout)

    def _desired_snapshot(self) -> tuple[str, ...]:
        with self._lock:
            return self._desired_ids

    def _record_connected(self) -> None:
        with self._lock:
            self._connect_count += 1
            if self._connect_count > 1:
                self._reconnect_count += 1
            self._last_connected_at = time.time()
            self._last_error = None

    def _record_error(self, exc: Exception) -> None:
        with self._lock:
            self._last_error = str(exc)

    def _run(self) -> None:
        backoff_seconds = self._reconnect_min_seconds
        while not self._stop.is_set():
            desired = self._desired_snapshot()
            if not desired:
                self._changed.wait(timeout=1.0)
                self._changed.clear()
                continue
            try:
                with SimpleWebSocket.connect(self.url, timeout=10.0) as ws:
                    self._record_connected()
                    self._logger.info("%s_ws_connected subscriptions=%s", self.name, len(desired))
                    subscribed = self._send_initial(ws, desired)
                    backoff_seconds = self._reconnect_min_seconds
                    self._loop_connected(ws, subscribed)
            except Exception as exc:
                if self._stop.is_set():
                    break
                self._record_error(exc)
                self._logger.warning(
                    "%s_ws_disconnected reconnect_after=%.1f error=%s",
                    self.name,
                    backoff_seconds,
                    exc,
                )
                self._stop.wait(timeout=backoff_seconds)
                backoff_seconds = min(self._reconnect_max_seconds, backoff_seconds * 2)

    def _send_initial(self, ws: SimpleWebSocket, ids: tuple[str, ...]) -> set[str]:
        subscribed: set[str] = set()
        for chunk in _chunked(ids):
            ws.send_text(json.dumps(self._initial_payload(chunk), separators=(",", ":")))
            subscribed.update(chunk)
        return subscribed

    def _loop_connected(self, ws: SimpleWebSocket, subscribed: set[str]) -> None:
        last_ping = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            if now - last_ping >= self._heartbeat_seconds:
                ws.send_text("PING")
                last_ping = now
            subscribed = self._sync_subscription(ws, subscribed)
            try:
                text = ws.recv_text(timeout=1.0)
            except socket.timeout:
                continue
            if text.strip().upper() == "PONG":
                continue
            self._handle_text(text)

    def _sync_subscription(self, ws: SimpleWebSocket, subscribed: set[str]) -> set[str]:
        if self._changed.is_set():
            self._changed.clear()
        desired = set(self._desired_snapshot())
        to_subscribe = desired - subscribed
        to_unsubscribe = subscribed - desired
        for chunk in _chunked(to_subscribe):
            ws.send_text(json.dumps(self._subscribe_payload(chunk), separators=(",", ":")))
            subscribed.update(chunk)
        for chunk in _chunked(to_unsubscribe):
            ws.send_text(json.dumps(self._unsubscribe_payload(chunk), separators=(",", ":")))
            subscribed.difference_update(chunk)
        return subscribed

    def _handle_text(self, text: str) -> None:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            self._logger.debug("%s_ws_non_json message=%r", self.name, text[:200])
            return
        self._message_handler(payload)


class PolymarketMarketStream:
    def __init__(
        self,
        *,
        cache: BestBidAskCache,
        base_url: str | None = None,
        logger_: logging.Logger | None = None,
        max_tokens: int | None = None,
    ):
        self.cache = cache
        self.max_tokens = max_tokens if max_tokens is not None else config.ws_market_max_tokens()
        stream_url = f"{(base_url or config.polymarket_ws_base_url()).rstrip('/')}/market"
        self._logger = logger_ or logger
        self._stream = _SubscriptionStream(
            name="market",
            url=stream_url,
            initial_payload=lambda ids: {
                "assets_ids": ids,
                "type": "market",
                "custom_feature_enabled": True,
            },
            subscribe_payload=lambda ids: {
                "assets_ids": ids,
                "operation": "subscribe",
                "custom_feature_enabled": True,
            },
            unsubscribe_payload=lambda ids: {
                "assets_ids": ids,
                "operation": "unsubscribe",
            },
            message_handler=self._handle_message,
            logger_=self._logger,
        )
        self._last_requested = 0
        self._last_subscribed = 0

    @property
    def reconnect_count(self) -> int:
        return self._stream.reconnect_count

    def set_market_candidates(self, markets: Iterable[dict]) -> tuple[int, int]:
        all_token_ids = unique_market_token_ids(markets)
        requested = len(all_token_ids)
        token_ids = all_token_ids[: self.max_tokens]
        self._last_requested = requested
        self._last_subscribed = self._stream.set_ids(token_ids)
        if requested > self._last_subscribed:
            self._logger.warning(
                "market_ws_subscription_capped requested_tokens=%s subscribed_tokens=%s",
                requested,
                self._last_subscribed,
            )
        else:
            self._logger.info("market_ws_subscription tokens=%s", self._last_subscribed)
        return requested, self._last_subscribed

    def warmup(self, seconds: float | None = None) -> None:
        delay = config.ws_market_warmup_seconds() if seconds is None else seconds
        if delay > 0 and self._last_subscribed > 0:
            time.sleep(delay)

    def stop(self) -> None:
        self._stream.stop()

    def _handle_message(self, payload: Any) -> None:
        updated = self.cache.apply_message(payload)
        if updated:
            self._logger.debug("market_ws_price_update quotes=%s", updated)
        elif isinstance(payload, dict) and payload.get("event_type") in {"market_resolved", "new_market"}:
            self._logger.info(
                "market_ws_event event_type=%s market=%s",
                payload.get("event_type"),
                payload.get("market") or payload.get("condition_id") or payload.get("id"),
            )


class RecentUserEvents:
    def __init__(self, *, maxlen: int = 200):
        self._events: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._lock = threading.RLock()

    def append(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self._events.append(dict(payload))

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._events)


class PolymarketUserStream:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        logger_: logging.Logger | None = None,
        events: RecentUserEvents | None = None,
    ):
        self.events = events or RecentUserEvents()
        stream_url = f"{(base_url or config.polymarket_ws_base_url()).rstrip('/')}/user"
        self._logger = logger_ or logger
        self._stream = _SubscriptionStream(
            name="user",
            url=stream_url,
            initial_payload=self._initial_payload,
            subscribe_payload=lambda ids: {"markets": ids, "operation": "subscribe"},
            unsubscribe_payload=lambda ids: {"markets": ids, "operation": "unsubscribe"},
            message_handler=self._handle_message,
            logger_=self._logger,
        )

    @staticmethod
    def credentials_ready() -> bool:
        return all(
            os.getenv(name)
            for name in (
                "POLYMARKET_API_KEY",
                "POLYMARKET_API_SECRET",
                "POLYMARKET_API_PASSPHRASE",
            )
        )

    @property
    def reconnect_count(self) -> int:
        return self._stream.reconnect_count

    def set_market_candidates(self, markets: Iterable[dict]) -> int:
        condition_ids = unique_market_condition_ids(markets)
        subscribed = self._stream.set_ids(condition_ids)
        self._logger.info("user_ws_subscription markets=%s", subscribed)
        return subscribed

    def stop(self) -> None:
        self._stream.stop()

    def _initial_payload(self, ids: list[str]) -> dict[str, Any]:
        return {
            "auth": {
                "apiKey": os.environ["POLYMARKET_API_KEY"],
                "secret": os.environ["POLYMARKET_API_SECRET"],
                "passphrase": os.environ["POLYMARKET_API_PASSPHRASE"],
            },
            "markets": ids,
            "type": "user",
        }

    def _handle_message(self, payload: Any) -> None:
        if isinstance(payload, list):
            for item in payload:
                self._handle_message(item)
            return
        if not isinstance(payload, dict):
            return
        event_type = str(payload.get("event_type") or payload.get("type") or "").lower()
        if event_type not in {"order", "trade", "placement", "update", "cancellation"}:
            return
        compact = {
            key: payload.get(key)
            for key in (
                "event_type",
                "type",
                "id",
                "market",
                "asset_id",
                "side",
                "status",
                "price",
                "size",
                "original_size",
                "size_matched",
                "timestamp",
            )
            if key in payload
        }
        self.events.append(compact)
        self._logger.info(
            "user_ws_event event_type=%s type=%s market=%s asset_id=%s id=%s status=%s",
            payload.get("event_type"),
            payload.get("type"),
            payload.get("market"),
            payload.get("asset_id"),
            payload.get("id"),
            payload.get("status"),
        )
