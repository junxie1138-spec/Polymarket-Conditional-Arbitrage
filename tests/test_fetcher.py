from __future__ import annotations

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
