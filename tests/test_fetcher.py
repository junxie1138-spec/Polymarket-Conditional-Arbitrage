from __future__ import annotations

import pytest

from polymarket_conditional_arb.fetcher import GammaClobClient


class Response:
    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"status={self.status_code}")

    def json(self):
        return self._data


class Session:
    def __init__(self, *, get_responses=None, post_responses=None):
        self.get_responses = list(get_responses or [])
        self.post_responses = list(post_responses or [])
        self.get_calls = []
        self.post_calls = []

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return self.get_responses.pop(0)

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return self.post_responses.pop(0)


def test_fetch_active_events_uses_closed_false_and_no_tag_filter():
    event = {"id": "e1", "markets": []}
    session = Session(get_responses=[Response([event])])
    client = GammaClobClient(session=session, clob_host="https://clob.example")

    assert client.fetch_active_events() == [event]
    _, kwargs = session.get_calls[0]
    assert kwargs["params"]["closed"] == "false"
    assert "tag_slug" not in kwargs["params"]


def test_fetch_active_events_reports_page_progress():
    first_page = [{"id": f"e{i}", "markets": []} for i in range(100)]
    second_page = [{"id": "final", "markets": []}]
    session = Session(get_responses=[Response(first_page), Response(second_page)])
    client = GammaClobClient(session=session, clob_host="https://clob.example")
    pages = []

    events = client.fetch_active_events(on_page=lambda offset, rows, total: pages.append((offset, rows, total)))

    assert len(events) == 101
    assert pages == [(0, 100, 100), (100, 1, 101)]


def test_fetch_active_events_can_stop_between_pages():
    first_page = [{"id": f"e{i}", "markets": []} for i in range(100)]
    second_page = [{"id": "unused", "markets": []}]
    session = Session(get_responses=[Response(first_page), Response(second_page)])
    client = GammaClobClient(session=session, clob_host="https://clob.example")

    with pytest.raises(InterruptedError, match="active event fetch stopped"):
        client.fetch_active_events(should_continue=lambda: False)

    assert len(session.get_calls) == 1


def test_fetch_active_events_slice_orders_by_volume_and_does_not_paginate():
    events = [{"id": f"e{i}", "markets": []} for i in range(20)]
    session = Session(get_responses=[Response(events)])
    client = GammaClobClient(session=session, clob_host="https://clob.example")

    fetched = client.fetch_active_events_slice(limit=20, order="volume24hr", ascending=False)

    assert fetched == events
    assert len(session.get_calls) == 1
    _, kwargs = session.get_calls[0]
    assert kwargs["params"] == {
        "closed": "false",
        "limit": 20,
        "order": "volume24hr",
        "ascending": "false",
    }


def test_flatten_event_markets_keeps_all_tags_and_attaches_event_context():
    events = [
        {
            "id": "e1",
            "title": "Politics",
            "slug": "politics-event",
            "negRisk": True,
            "tags": [{"slug": "politics"}],
            "markets": [{"id": "m1", "question": "Q"}],
        },
        {
            "id": "e2",
            "title": "Weather",
            "tags": [{"slug": "weather"}],
            "markets": [{"id": "m2", "question": "Q2"}],
        },
    ]

    markets = GammaClobClient.flatten_event_markets(events)

    assert [market["id"] for market in markets] == ["m1", "m2"]
    assert markets[0]["_event_id"] == "e1"
    assert markets[0]["_event_neg_risk"] is True
    assert markets[1]["_event_tags"] == ["weather"]


def test_fetch_ask_books_uses_batch_books_successfully():
    session = Session(
        post_responses=[
            Response(
                [
                    {"asset_id": "token-a", "asks": [{"price": "0.41", "size": "5"}]},
                    {"asset_id": "token-b", "asks": [{"price": "0.42", "size": "6"}]},
                ]
            )
        ]
    )
    client = GammaClobClient(session=session, clob_host="https://clob.example")

    books = client.fetch_ask_books(["token-a", "token-b"])

    assert len(session.post_calls) == 1
    assert session.get_calls == []
    _, kwargs = session.post_calls[0]
    assert kwargs["json"] == [{"token_id": "token-a"}, {"token_id": "token-b"}]
    assert books["token-a"].source == "rest_books_batch"
    assert books["token-a"].best_price == 0.41


def test_fetch_ask_books_reports_batch_progress():
    session = Session(
        post_responses=[
            Response(
                [
                    {"asset_id": "token-a", "asks": [{"price": "0.41", "size": "5"}]},
                    {"asset_id": "token-b", "asks": [{"price": "0.42", "size": "6"}]},
                ]
            ),
            Response(
                [
                    {"asset_id": "token-c", "asks": [{"price": "0.43", "size": "7"}]},
                ]
            ),
        ]
    )
    client = GammaClobClient(session=session, clob_host="https://clob.example", batch_book_limit=2)
    progress = []

    books = client.fetch_ask_books(["token-a", "token-b", "token-c"], on_progress=progress.append)

    assert sorted(books) == ["token-a", "token-b", "token-c"]
    assert progress == [
        {
            "total_tokens": 3,
            "completed_tokens": 2,
            "remaining_tokens": 1,
            "received_books": 2,
            "failed_tokens": 0,
        },
        {
            "total_tokens": 3,
            "completed_tokens": 3,
            "remaining_tokens": 0,
            "received_books": 3,
            "failed_tokens": 0,
        },
    ]


def test_fetch_ask_books_falls_back_to_single_book_on_malformed_batch_response():
    session = Session(
        post_responses=[Response({"bad": "shape"})],
        get_responses=[
            Response({"asset_id": "token-a", "asks": [{"price": "0.43", "size": "5"}]}),
            Response({"asset_id": "token-b", "asks": [{"price": "0.44", "size": "6"}]}),
        ],
    )
    client = GammaClobClient(session=session, clob_host="https://clob.example")

    books = client.fetch_ask_books(["token-a", "token-b"])

    assert len(session.get_calls) == 2
    assert books["token-a"].source == "rest_book_fallback"
    assert books["token-b"].best_price == 0.44


def test_fetch_ask_books_keeps_partial_single_book_fallback_successes():
    session = Session(
        post_responses=[Response({"bad": "shape"})],
        get_responses=[
            Response({"asset_id": "token-a", "asks": [{"price": "0.43", "size": "5"}]}),
            Response([]),
            Response({"asset_id": "token-c", "asks": [{"price": "0.45", "size": "7"}]}),
        ],
    )
    client = GammaClobClient(session=session, clob_host="https://clob.example")

    books = client.fetch_ask_books(["token-a", "token-b", "token-c"])

    assert len(session.get_calls) == 3
    assert sorted(books) == ["token-a", "token-c"]
    assert books["token-a"].source == "rest_book_fallback"
    assert books["token-c"].best_price == 0.45
