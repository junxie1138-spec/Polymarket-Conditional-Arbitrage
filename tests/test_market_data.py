from __future__ import annotations

import asyncio
import json
import logging
import unittest
from datetime import datetime, timezone

from polymarket_conditional_arb.market_data import (
    MarketDataCache,
    MarketWebSocketManager,
    MarketWebSocketSettings,
    chunk_asset_ids,
)
from polymarket_conditional_arb.order_book import asks_from_book

AS_OF = datetime(2026, 6, 8, 12, tzinfo=timezone.utc)


async def wait_for(predicate, *, timeout: float = 1.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.001)
    raise AssertionError("condition was not met before timeout")


class FakeWebSocket:
    def __init__(self, *, disconnect_immediately: bool = False) -> None:
        self.disconnect_immediately = disconnect_immediately
        self.sent: list[str] = []
        self.closed = asyncio.Event()

    async def send(self, message: str) -> None:
        self.sent.append(message)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.disconnect_immediately:
            raise StopAsyncIteration
        await self.closed.wait()
        raise StopAsyncIteration

    async def close(self) -> None:
        self.closed.set()


class FakeConnection:
    def __init__(self, websocket: FakeWebSocket) -> None:
        self.websocket = websocket

    async def __aenter__(self) -> FakeWebSocket:
        return self.websocket

    async def __aexit__(self, *_exc_info) -> None:
        await self.websocket.close()


class FakeConnectFactory:
    def __init__(self, *, disconnect_immediately: bool = False) -> None:
        self.disconnect_immediately = disconnect_immediately
        self.websockets: list[FakeWebSocket] = []

    def __call__(self, _endpoint: str) -> FakeConnection:
        websocket = FakeWebSocket(disconnect_immediately=self.disconnect_immediately)
        self.websockets.append(websocket)
        return FakeConnection(websocket)


def test_book_snapshot_creates_sorted_ask_and_bid_cache():
    cache = MarketDataCache()
    updated = cache.apply_message(
        {
            "event_type": "book",
            "asset_id": "token-a",
            "bids": [{"price": "0.42", "size": "5"}, {"price": "0.44", "size": "1"}],
            "asks": [{"price": "0.49", "size": "2"}, {"price": "0.47", "size": "3"}],
        },
        received_at=AS_OF,
    )

    assert updated == {"token-a"}
    asks = cache.book_side("token-a", "ask")
    bids = cache.book_side("token-a", "bid")
    assert asks is not None
    assert bids is not None
    assert [level.price for level in asks.levels] == [0.47, 0.49]
    assert [level.price for level in bids.levels] == [0.44, 0.42]
    assert asks.source == "ws_book"


def test_price_change_updates_removes_zero_size_levels_and_preserves_ordering():
    cache = MarketDataCache()
    cache.apply_message(
        {
            "event_type": "book",
            "asset_id": "token-a",
            "asks": [{"price": "0.50", "size": "3"}, {"price": "0.55", "size": "4"}],
        },
        received_at=AS_OF,
    )

    cache.apply_message(
        {
            "event_type": "price_change",
            "price_changes": [
                {"asset_id": "token-a", "side": "SELL", "price": "0.48", "size": "2"},
                {"asset_id": "token-a", "side": "SELL", "price": "0.55", "size": "0"},
            ],
        },
        received_at=AS_OF,
    )

    asks = cache.book_side("token-a", "ask")
    assert asks is not None
    assert [(level.price, level.size) for level in asks.levels] == [(0.48, 2.0), (0.50, 3.0)]
    assert asks.source == "ws_price_change"


def test_price_change_without_ready_snapshot_is_ignored(caplog):
    cache = MarketDataCache()
    logger = logging.getLogger("test_market_data")

    with caplog.at_level(logging.WARNING, logger="test_market_data"):
        updated = cache.apply_message(
            {
                "event_type": "price_change",
                "price_changes": [
                    {"asset_id": "token-a", "side": "SELL", "price": "0.48", "size": "2"},
                ],
            },
            received_at=AS_OF,
            logger=logger,
        )

    assert updated == set()
    assert cache.book_side("token-a", "ask") is None
    assert cache.is_snapshot_ready("token-a") is False
    assert "market_ws_price_change_without_ready_snapshot" in caplog.text


def test_price_change_without_ready_snapshot_warning_is_throttled(caplog):
    cache = MarketDataCache(skipped_snapshot_warning_interval_seconds=60.0)
    logger = logging.getLogger("test_market_data_throttled")
    message = {
        "event_type": "price_change",
        "price_changes": [
            {"asset_id": "token-a", "side": "SELL", "price": "0.48", "size": "2"},
        ],
    }

    with caplog.at_level(logging.WARNING, logger="test_market_data_throttled"):
        assert cache.apply_message(message, received_at=AS_OF, logger=logger) == set()
        assert cache.apply_message(message, received_at=AS_OF, logger=logger) == set()
        assert cache.apply_message(message, received_at=AS_OF, logger=logger) == set()

    warnings = [
        record
        for record in caplog.records
        if "market_ws_price_change_without_ready_snapshot" in record.getMessage()
    ]
    assert len(warnings) == 1
    assert "suppressed_since_last=0" in warnings[0].getMessage()


def test_price_change_after_stale_marking_waits_for_rest_reseed():
    cache = MarketDataCache()
    cache.apply_message(
        {
            "event_type": "book",
            "asset_id": "token-a",
            "asks": [{"price": "0.50", "size": "3"}],
        },
        received_at=AS_OF,
    )
    assert cache.is_snapshot_ready("token-a") is True
    first_generation = cache.snapshot_generation("token-a")

    cache.mark_tokens_stale(["token-a"], stale_at=AS_OF)
    ignored = cache.apply_message(
        {
            "event_type": "price_change",
            "price_changes": [
                {"asset_id": "token-a", "side": "SELL", "price": "0.48", "size": "2"},
            ],
        },
        received_at=AS_OF,
    )
    stale_asks = cache.book_side("token-a", "ask")

    assert ignored == set()
    assert cache.is_snapshot_ready("token-a") is False
    assert cache.snapshot_generation("token-a") > first_generation
    assert stale_asks is not None
    assert stale_asks.source.endswith("_stale")
    assert [(level.price, level.size) for level in stale_asks.levels] == [(0.50, 3.0)]

    cache.seed_ask_books(
        {
            "token-a": asks_from_book(
                {"asks": [{"price": "0.51", "size": "4"}]},
                token_id="token-a",
                updated_at=AS_OF,
            )
        }
    )
    updated = cache.apply_message(
        {
            "event_type": "price_change",
            "price_changes": [
                {"asset_id": "token-a", "side": "SELL", "price": "0.49", "size": "1"},
            ],
        },
        received_at=AS_OF,
    )
    asks = cache.book_side("token-a", "ask")

    assert updated == {"token-a"}
    assert asks is not None
    assert [(level.price, level.size) for level in asks.levels] == [(0.49, 1.0), (0.51, 4.0)]


def test_malformed_messages_are_logged_and_ignored(caplog):
    cache = MarketDataCache()
    logger = logging.getLogger("test_market_data")

    with caplog.at_level(logging.WARNING, logger="test_market_data"):
        updated = cache.apply_message("{not-json", logger=logger)

    assert updated == set()
    assert "market_ws_malformed_message" in caplog.text


def test_chunk_asset_ids_uses_stable_500_token_style_partitions():
    assert chunk_asset_ids(["b", "a", "a", "c"], 2) == [["a", "b"], ["c"]]


class MarketWebSocketManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        manager = getattr(self, "manager", None)
        if manager is not None:
            await manager.stop()

    async def test_initial_subscribe_payload_uses_market_assets_and_custom_feature(self):
        factory = FakeConnectFactory()
        self.manager = MarketWebSocketManager(
            settings=MarketWebSocketSettings(endpoint="ws://example", heartbeat_seconds=1, max_assets_per_connection=500),
            cache=MarketDataCache(),
            logger=logging.getLogger("test_market_ws"),
            connect_factory=factory,
        )

        await self.manager.start(["token-b", "token-a"])
        await wait_for(lambda: len(factory.websockets) == 1 and len(factory.websockets[0].sent) >= 1)

        payload = json.loads(factory.websockets[0].sent[0])
        assert payload == {
            "assets_ids": ["token-a", "token-b"],
            "type": "market",
            "custom_feature_enabled": True,
        }

    async def test_token_chunking_creates_multiple_connections(self):
        factory = FakeConnectFactory()
        self.manager = MarketWebSocketManager(
            settings=MarketWebSocketSettings(endpoint="ws://example", heartbeat_seconds=1, max_assets_per_connection=2),
            cache=MarketDataCache(),
            logger=logging.getLogger("test_market_ws"),
            connect_factory=factory,
        )

        await self.manager.start(["token-a", "token-b", "token-c"])
        await wait_for(lambda: len(factory.websockets) == 2 and all(ws.sent for ws in factory.websockets))

        assert self.manager.connection_count == 2
        assert [json.loads(ws.sent[0])["assets_ids"] for ws in factory.websockets] == [
            ["token-a", "token-b"],
            ["token-c"],
        ]

    async def test_heartbeat_sends_text_ping(self):
        factory = FakeConnectFactory()
        self.manager = MarketWebSocketManager(
            settings=MarketWebSocketSettings(endpoint="ws://example", heartbeat_seconds=0.01, max_assets_per_connection=500),
            cache=MarketDataCache(),
            logger=logging.getLogger("test_market_ws"),
            connect_factory=factory,
        )

        await self.manager.start(["token-a"])
        await wait_for(lambda: len(factory.websockets) == 1 and "PING" in factory.websockets[0].sent)

        assert "PING" in factory.websockets[0].sent

    async def test_reconnect_resubscribes_after_disconnect(self):
        factory = FakeConnectFactory(disconnect_immediately=True)
        self.manager = MarketWebSocketManager(
            settings=MarketWebSocketSettings(
                endpoint="ws://example",
                heartbeat_seconds=1,
                max_assets_per_connection=500,
                reconnect_initial_seconds=0.01,
            ),
            cache=MarketDataCache(),
            logger=logging.getLogger("test_market_ws"),
            connect_factory=factory,
        )

        await self.manager.start(["token-a"])
        await wait_for(lambda: len(factory.websockets) >= 2 and all(ws.sent for ws in factory.websockets[:2]))

        assert [json.loads(ws.sent[0])["assets_ids"] for ws in factory.websockets[:2]] == [
            ["token-a"],
            ["token-a"],
        ]

    async def test_dynamic_subscribe_and_unsubscribe_updates_token_universe(self):
        factory = FakeConnectFactory()
        self.manager = MarketWebSocketManager(
            settings=MarketWebSocketSettings(endpoint="ws://example", heartbeat_seconds=1, max_assets_per_connection=500),
            cache=MarketDataCache(),
            logger=logging.getLogger("test_market_ws"),
            connect_factory=factory,
        )

        await self.manager.start(["token-a"])
        await wait_for(lambda: len(factory.websockets) == 1 and factory.websockets[0].sent)
        await self.manager.update_tokens(["token-b"])

        sent_payloads = [json.loads(message) for message in factory.websockets[0].sent if message != "PING"]
        assert sent_payloads[1:] == [
            {"assets_ids": ["token-b"], "operation": "subscribe", "custom_feature_enabled": True},
            {"assets_ids": ["token-a"], "operation": "unsubscribe"},
        ]
