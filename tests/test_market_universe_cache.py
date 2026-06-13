from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from polymarket_conditional_arb import market_universe_cache as cache_module
from polymarket_conditional_arb.arb_models import BinaryMarket
from polymarket_conditional_arb.market_universe_cache import (
    load_market_universe_cache,
    write_market_universe_cache,
)


def binary_market(market_id: str, yes_token_id: str, no_token_id: str) -> BinaryMarket:
    return BinaryMarket(
        market_id=market_id,
        condition_id=f"c-{market_id}",
        question=f"Question {market_id}",
        yes_token_id=yes_token_id,
        no_token_id=no_token_id,
        metadata={"_event_id": f"e-{market_id}", "volume": "123.45"},
    )


def test_market_universe_cache_round_trips_binary_markets(tmp_path):
    path = tmp_path / "market_universe_cache.json"
    fetched_at = datetime(2026, 6, 10, 12, tzinfo=timezone.utc)
    markets = [
        binary_market("m1", "yes-1", "no-1"),
        binary_market("m2", "yes-2", "no-2"),
    ]

    write_market_universe_cache(
        path,
        markets=markets,
        events_fetched=2,
        raw_markets=3,
        gamma_query={"closed": "false", "order": "volume24hr"},
        fetched_at=fetched_at,
    )
    loaded = load_market_universe_cache(
        path,
        max_age_seconds=3600,
        now=fetched_at + timedelta(seconds=5),
    )

    assert loaded is not None
    assert loaded.events_fetched == 2
    assert loaded.raw_markets == 3
    assert loaded.gamma_query["order"] == "volume24hr"
    assert [market.market_id for market in loaded.markets] == ["m1", "m2"]
    assert [market.yes_token_id for market in loaded.markets] == ["yes-1", "yes-2"]
    assert [market.no_token_id for market in loaded.markets] == ["no-1", "no-2"]


def test_market_universe_cache_writer_does_not_build_one_large_json_string(tmp_path, monkeypatch):
    path = tmp_path / "market_universe_cache.json"
    fetched_at = datetime(2026, 6, 10, 12, tzinfo=timezone.utc)

    def fail_json_dumps(*_args, **_kwargs):
        raise AssertionError("write_market_universe_cache should stream with json.dump")

    monkeypatch.setattr(cache_module.json, "dumps", fail_json_dumps)

    write_market_universe_cache(
        path,
        markets=[binary_market("m1", "yes-1", "no-1")],
        events_fetched=1,
        raw_markets=1,
        gamma_query={"closed": "false"},
        fetched_at=fetched_at,
    )

    assert path.exists()


def test_market_universe_cache_ignores_stale_cache_with_warning(tmp_path, caplog):
    path = tmp_path / "market_universe_cache.json"
    fetched_at = datetime(2026, 6, 10, 12, tzinfo=timezone.utc)
    write_market_universe_cache(
        path,
        markets=[binary_market("m1", "yes-1", "no-1")],
        events_fetched=1,
        raw_markets=1,
        gamma_query={"closed": "false"},
        fetched_at=fetched_at,
    )
    logger = logging.getLogger("test_market_universe_cache_stale")

    with caplog.at_level(logging.WARNING, logger="test_market_universe_cache_stale"):
        loaded = load_market_universe_cache(
            path,
            max_age_seconds=3600,
            logger=logger,
            now=fetched_at + timedelta(seconds=3601),
        )

    assert loaded is None
    assert "market_universe_cache_ignored reason=stale" in caplog.text


def test_market_universe_cache_ignores_corrupt_cache_with_warning(tmp_path, caplog):
    path = tmp_path / "market_universe_cache.json"
    path.write_text("{not-json", encoding="utf-8")
    logger = logging.getLogger("test_market_universe_cache_corrupt")

    with caplog.at_level(logging.WARNING, logger="test_market_universe_cache_corrupt"):
        loaded = load_market_universe_cache(path, max_age_seconds=3600, logger=logger)

    assert loaded is None
    assert "market_universe_cache_ignored reason=invalid" in caplog.text
