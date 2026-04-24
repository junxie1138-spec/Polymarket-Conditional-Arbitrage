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
