import json

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
