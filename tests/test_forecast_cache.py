import json
from datetime import date

import requests

from weather_arb_live import forecast


def test_load_cache_treats_json_null_as_empty_dict(monkeypatch):
    path = forecast.CACHE_PATH
    original_cache = forecast._cache
    original_exists = path.exists()
    original_text = path.read_text(encoding="utf-8") if original_exists else None

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("null", encoding="utf-8")
        monkeypatch.setattr(forecast, "_cache", None)

        assert forecast._load_cache() == {}
    finally:
        monkeypatch.setattr(forecast, "_cache", original_cache)
        if original_exists:
            path.write_text(original_text, encoding="utf-8")
        elif path.exists():
            path.unlink()


def test_save_cache_never_writes_json_null(monkeypatch):
    path = forecast.CACHE_PATH
    original_cache = forecast._cache
    original_exists = path.exists()
    original_text = path.read_text(encoding="utf-8") if original_exists else None

    try:
        monkeypatch.setattr(forecast, "_cache", None)
        forecast._save_cache()

        assert json.loads(path.read_text(encoding="utf-8")) == {}
    finally:
        monkeypatch.setattr(forecast, "_cache", original_cache)
        if original_exists:
            path.write_text(original_text, encoding="utf-8")
        elif path.exists():
            path.unlink()


def test_transient_forecast_failure_does_not_poison_cache(monkeypatch):
    original_cache = forecast._cache

    class OfflineSession:
        def get(self, *_args, **_kwargs):
            raise requests.ConnectionError("offline")

    try:
        monkeypatch.setattr(forecast, "_cache", {})
        monkeypatch.setattr(forecast, "_session", OfflineSession())

        result = forecast._fetch_forecast_response(
            1.234,
            2.345,
            "UTC",
            date(2026, 4, 27),
            "F",
        )

        key = "pr2|1.23,2.35|2026-04-27|F|gfs_seamless"
        assert result is None
        assert key not in forecast._cache
    finally:
        monkeypatch.setattr(forecast, "_cache", original_cache)
