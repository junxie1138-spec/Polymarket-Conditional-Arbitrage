import requests

from weather_arb_live import network
from weather_arb_live.live_fetcher import LiveFetcher, midpoint_from_book


def test_midpoint_from_two_sided_book():
    book = {
        "bids": [{"price": "0.40", "size": "10"}, {"price": "0.39", "size": "20"}],
        "asks": [{"price": "0.44", "size": "10"}, {"price": "0.45", "size": "20"}],
    }

    assert midpoint_from_book(book) == 0.42


def test_midpoint_returns_none_for_one_sided_book():
    assert midpoint_from_book({"bids": [{"price": "0.40"}], "asks": []}) is None
    assert midpoint_from_book({"bids": [], "asks": [{"price": "0.44"}]}) is None


def test_flatten_event_markets_attaches_event_context():
    events = [
        {
            "id": "e1",
            "title": "Weather",
            "endDate": "2026-04-28T00:00:00Z",
            "tags": [{"slug": "weather"}],
            "markets": [{"id": "m1", "question": "Q"}],
        }
    ]

    markets = LiveFetcher.flatten_event_markets(events)

    assert len(markets) == 1
    assert markets[0]["_event_title"] == "Weather"
    assert markets[0]["_event_id"] == "e1"
    assert markets[0]["_event_tags"] == ["weather"]


def test_fetch_midpoint_retries_transient_connection(monkeypatch):
    monkeypatch.setattr(network, "sleep_for_attempt", lambda *_args, **_kwargs: None)
    calls = []

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "bids": [{"price": "0.40"}],
                "asks": [{"price": "0.44"}],
            }

    class Session:
        def get(self, *_args, **_kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise requests.ConnectionError("offline")
            return Response()

    fetcher = LiveFetcher(session=Session(), clob_host="https://example.invalid")

    assert fetcher.fetch_midpoint("token") == 0.42
    assert len(calls) == 2
