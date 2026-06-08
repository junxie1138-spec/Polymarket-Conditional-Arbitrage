import json
from decimal import Decimal
from pathlib import Path

from weather_arb_live import dashboard
from weather_arb_live.wallet_balance import WalletBalance


def _patch_dashboard_paths(monkeypatch) -> tuple[Path, Path, list[Path]]:
    data_dir = Path("data")
    log_dir = Path("logs")
    data_dir.mkdir(exist_ok=True)
    log_dir.mkdir(exist_ok=True)

    weather_cache_path = data_dir / "test_dashboard_weather_cache.json"
    residuals_path = data_dir / "test_dashboard_empirical_residuals.json"
    sigma_path = data_dir / "test_dashboard_sigma_cache.json"
    calibration_path = data_dir / "test_dashboard_calibration_table.json"
    patched_positions_path = data_dir / "test_dashboard_live_positions.json"
    patched_pnl_history_path = data_dir / "test_dashboard_pnl_history.json"
    patched_log_path = log_dir / "test_dashboard_live_bot.log"

    monkeypatch.setattr(dashboard.config, "DATA_DIR", data_dir)
    monkeypatch.setattr(dashboard.config, "LOG_DIR", log_dir)
    monkeypatch.setattr(dashboard.config, "POSITIONS_PATH", patched_positions_path)
    monkeypatch.setattr(dashboard.config, "WEATHER_CACHE_PATH", weather_cache_path)
    monkeypatch.setattr(dashboard.config, "RESIDUALS_CACHE_PATH", residuals_path)
    monkeypatch.setattr(dashboard.config, "SIGMA_CACHE_PATH", sigma_path)
    monkeypatch.setattr(dashboard.config, "CALIBRATION_PATH", calibration_path)
    monkeypatch.setattr(dashboard, "LOG_PATH", patched_log_path)
    monkeypatch.setattr(dashboard.config, "PNL_HISTORY_PATH", patched_pnl_history_path)
    cleanup_paths = [
        weather_cache_path,
        residuals_path,
        sigma_path,
        calibration_path,
        patched_positions_path,
        patched_pnl_history_path,
        patched_log_path,
    ]
    return patched_positions_path, patched_log_path, cleanup_paths


def test_dashboard_state_summarizes_positions_and_hides_secret_values(monkeypatch):
    positions_path, log_path, cleanup_paths = _patch_dashboard_paths(monkeypatch)
    try:
        positions_path.write_text(
            json.dumps(
                {
                    "m1": {
                        "market_id": "m1",
                        "token_id": "yes-token",
                        "question": "Will NYC hit 80F?",
                        "city": "New York",
                        "target_date": "2026-04-27",
                        "entry_price": 0.5,
                        "shares": 100,
                        "position_usd": 50,
                        "forecast_prob": 0.8,
                        "edge": 0.5,
                        "entry_time": "2026-04-25T08:00:00+00:00",
                        "dry_run": True,
                        "order_response": {"dry_run": True},
                    },
                    "m2": {
                        "market_id": "m2",
                        "token_id": "no-token",
                        "side": "NO",
                        "question": "Will Chicago stay below 70F?",
                        "city": "Chicago",
                        "target_date": "2026-04-28",
                        "entry_price": 0.5,
                        "shares": 50,
                        "position_usd": 25,
                        "forecast_prob": 0.7,
                        "edge": 0.2,
                        "entry_time": "2026-04-25T09:00:00+00:00",
                        "dry_run": False,
                        "order_response": {"posted": "unknown"},
                        "reconciliation": {
                            "status": "missing_exchange_match",
                            "requires_manual_review": True,
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        log_path.write_text(
            "\n".join(
                [
                    "2026-04-25 08:00:00,000 INFO weather_arb_live cycle_start at=2026-04-25T08:00:00+00:00",
                    "2026-04-25 08:00:01,000 WARNING weather_arb_live cycle_retry_after seconds=60",
                    "2026-04-25 08:00:02,000 INFO weather_arb_live cycle_end positions=2",
                ]
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("DRY_RUN", "false")
        monkeypatch.setenv("POLL_INTERVAL_MINUTES", "10")
        monkeypatch.setenv("MAX_POSITION_USD", "25")
        monkeypatch.setenv("POLYMARKET_API_KEY", "secret-api-key")
        monkeypatch.delenv("POLYMARKET_API_SECRET", raising=False)
        monkeypatch.delenv("POLYMARKET_API_PASSPHRASE", raising=False)
        monkeypatch.delenv("POLYMARKET_PRIVATE_KEY", raising=False)

        state = dashboard.build_dashboard_state(
            log_limit=10,
            mark_prices={"yes-token": 0.6, "no-token": 0.4},
            account_snapshot={
                "status": "ok",
                "status_label": "Connected",
                "balance_usd": 123.45,
                "allowance_usd": 100.0,
                "error": None,
                "updated_at": "2026-04-25T08:00:03+00:00",
            },
        )

        summary = state["positions"]["summary"]
        assert summary["total"] == 2
        assert summary["dry_run"] == 1
        assert summary["live"] == 1
        assert summary["yes_count"] == 1
        assert summary["no_count"] == 1
        assert summary["unknown_posted"] == 1
        assert summary["manual_review"] == 1
        assert summary["total_position_usd"] == 50
        assert summary["total_recorded_position_usd"] == 75
        assert summary["pnl_count"] == 2
        assert summary["total_pnl_usd"] == 0
        assert summary["win_count"] == 1
        assert summary["loss_count"] == 1
        assert summary["flat_count"] == 0
        assert summary["win_rate_count"] == 2
        assert summary["win_rate"] == 0.5
        assert state["positions"]["pnl_curve"] == [
            {
                "entry_time": "2026-04-25T08:00:00+00:00",
                "market_id": "m1",
                "question": "Will NYC hit 80F?",
                "side": "YES",
                "pnl_usd": 5,
                "cumulative_pnl_usd": 5,
            },
            {
                "entry_time": "2026-04-25T09:00:00+00:00",
                "market_id": "m2",
                "question": "Will Chicago stay below 70F?",
                "side": "NO",
                "pnl_usd": -5,
                "cumulative_pnl_usd": 0,
            },
        ]
        assert state["positions"]["pnl_history"][-1]["pnl_usd"] == 0
        assert state["positions"]["pnl_history"][-1]["position_usd"] == 50
        assert state["positions"]["pnl_history"][-1]["position_count"] == 2
        assert state["positions"]["pnl_history"][-1]["source"] == "live_marks"
        assert state["positions"]["recent"][0]["market_id"] == "m2"
        assert state["positions"]["recent"][0]["position_usd"] == 25
        assert state["positions"]["recent"][0]["pnl_usd"] == -5
        assert state["positions"]["recent"][1]["side"] == "YES"
        assert state["positions"]["recent"][1]["position_usd"] == 25
        assert state["positions"]["recent"][1]["recorded_position_usd"] == 50
        assert state["positions"]["recent"][1]["shares"] == 50
        assert state["positions"]["recent"][1]["pnl_usd"] == 5
        assert "over_max_position" not in state["positions"]["recent"][1]
        assert state["logs"]["level_counts"]["WARNING"] == 1
        assert state["logs"]["last_cycle_end"] == "2026-04-25 08:00:02,000"
        assert state["environment"]["live_credentials_ready"] is False
        assert state["account"]["balance_usd"] == 123.45
        assert state["account"]["allowance_usd"] == 100.0
        assert "POLYMARKET_API_SECRET" in state["environment"]["missing_required"]
        assert "secret-api-key" not in json.dumps(state)
        assert any(
            variable["name"] == "POLYMARKET_API_KEY" and variable["present"]
            for variable in state["environment"]["variables"]
        )
        assert any(
            variable["name"] == "MAX_POSITION_USD" and variable["present"]
            for variable in state["environment"]["variables"]
        )
    finally:
        for path in cleanup_paths:
            path.unlink(missing_ok=True)


def test_pnl_history_read_error_does_not_overwrite_file():
    path = Path("data/test_dashboard_corrupt_pnl_history.json")
    original = "{not-json"
    path.write_text(original, encoding="utf-8")
    try:
        history, error = dashboard.record_pnl_history_snapshot(
            {"total_pnl_usd": 1, "total_position_usd": 10, "total": 1, "pnl_count": 1},
            generated_at="2026-04-25T08:00:00+00:00",
            mark_count=0,
            path=path,
        )

        assert history == []
        assert error
        assert path.read_text(encoding="utf-8") == original
    finally:
        path.unlink(missing_ok=True)


def test_pnl_history_preserves_points_envelope_metadata():
    path = Path("data/test_dashboard_enveloped_pnl_history.json")
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "points": [
                    {
                        "timestamp": "2026-04-25T08:00:00+00:00",
                        "pnl_usd": 0,
                        "position_usd": 10,
                        "position_count": 1,
                        "pnl_count": 1,
                        "mark_count": 0,
                        "source": "ledger",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    try:
        history, error = dashboard.record_pnl_history_snapshot(
            {"total_pnl_usd": 2, "total_position_usd": 10, "total": 1, "pnl_count": 1},
            generated_at="2026-04-25T08:01:00+00:00",
            mark_count=0,
            path=path,
        )
        payload = json.loads(path.read_text(encoding="utf-8"))

        assert error is None
        assert len(history) == 2
        assert payload["schema_version"] == 1
        assert len(payload["points"]) == 2
        assert payload["points"][-1]["pnl_usd"] == 2
    finally:
        path.unlink(missing_ok=True)


def test_tail_lines_returns_only_requested_recent_lines():
    path = Path("logs/test_dashboard_tail.log")
    path.parent.mkdir(exist_ok=True)
    try:
        path.write_text("\n".join(f"line {index}" for index in range(10)), encoding="utf-8")

        lines, error = dashboard.tail_lines(path, 3)

        assert error is None
        assert lines == ["line 7", "line 8", "line 9"]
    finally:
        path.unlink(missing_ok=True)


def test_parse_log_lines_keeps_unstructured_lines():
    parsed = dashboard.parse_log_lines(
        [
            "2026-04-25 08:00:00,000 INFO weather_arb_live cycle_start at=now",
            "partial line without formatter",
        ]
    )

    assert parsed["entries"][0]["level"] == "INFO"
    assert parsed["last_cycle_start"] == "2026-04-25 08:00:00,000"
    assert parsed["entries"][1]["message"] == "partial line without formatter"


def test_dashboard_account_fetch_can_be_disabled(monkeypatch):
    positions_path, log_path, cleanup_paths = _patch_dashboard_paths(monkeypatch)
    try:
        called = False

        def fail_fetch(_runtime):
            nonlocal called
            called = True
            raise AssertionError("account fetch should not run")

        monkeypatch.setattr(dashboard, "fetch_account_snapshot", fail_fetch)
        state = dashboard.build_dashboard_state(include_account=False)

        assert called is False
        assert state["account"]["status"] == "disabled"
        assert state["account"]["balance_usd"] is None
    finally:
        for path in [positions_path, log_path, *cleanup_paths]:
            path.unlink(missing_ok=True)


def test_dashboard_account_fetch_uses_snapshot(monkeypatch):
    positions_path, log_path, cleanup_paths = _patch_dashboard_paths(monkeypatch)
    try:
        expected = {
            "status": "ok",
            "status_label": "Connected",
            "balance_usd": 77.0,
            "allowance_usd": 50.0,
            "error": None,
            "updated_at": "2026-04-25T08:00:00+00:00",
        }

        def fake_fetch(_runtime):
            return expected

        monkeypatch.setattr(dashboard, "fetch_account_snapshot", fake_fetch)
        state = dashboard.build_dashboard_state(include_account=True)

        assert state["account"] == expected
    finally:
        for path in [positions_path, log_path, *cleanup_paths]:
            path.unlink(missing_ok=True)


def test_dashboard_account_fetch_prefers_wallet_collateral_when_clob_balance_is_zero(monkeypatch):
    captured = {}

    class FakeClobClient:
        def __init__(self, **kwargs):
            captured["kwargs"] = kwargs

        def get_address(self):
            return "0x2222222222222222222222222222222222222222"

        def update_balance_allowance(self, _params):
            return None

        def get_balance_allowance(self, _params):
            return {"balance": "0", "allowances": {"0xabc": "0"}}

    monkeypatch.setenv("POLYMARKET_API_KEY", "api-key")
    monkeypatch.setenv("POLYMARKET_API_SECRET", "api-secret")
    monkeypatch.setenv("POLYMARKET_API_PASSPHRASE", "api-passphrase")
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "private-key")
    monkeypatch.setenv("POLYMARKET_FUNDER_ADDRESS", "0x1111111111111111111111111111111111111111")
    monkeypatch.setenv("POLYMARKET_SIGNATURE_TYPE", "2")
    monkeypatch.setattr("py_clob_client_v2.ClobClient", FakeClobClient)
    monkeypatch.setattr(
        dashboard.wallet_balance,
        "fetch_cached_collateral_balance",
        lambda address, ttl_seconds: WalletBalance(
            address=address,
            token_address="0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
            token_symbol="USDC.e",
            balance=Decimal("83.376702"),
            rpc_url="https://rpc.test",
        ),
    )

    snapshot = dashboard._fetch_account_snapshot_once({"clob_host": "https://clob.example"})

    assert captured["kwargs"]["funder"] == "0x1111111111111111111111111111111111111111"
    assert captured["kwargs"]["signature_type"] == 2
    assert snapshot["balance_usd"] == 83.38
    assert snapshot["wallet_balance_usd"] == 83.38
    assert snapshot["clob_balance_usd"] == 0.0
    assert snapshot["balance_source"] == "wallet_collateral"
    assert snapshot["funder_address"] == "0x1111...1111"
    assert snapshot["signer_address"] == "0x2222...2222"
    assert "CLOB balance endpoint reported 0" in snapshot["warning"]


def test_dashboard_html_uses_design_system_shell():
    assert "Polymarket Weather" in dashboard.DASHBOARD_HTML
    assert "kpi-grid" in dashboard.DASHBOARD_HTML
    assert "mode-dot" in dashboard.DASHBOARD_HTML
    assert "drawer-backdrop" in dashboard.DASHBOARD_HTML
    assert "tail / live_bot.log" in dashboard.DASHBOARD_HTML
    assert "PnL History" in dashboard.DASHBOARD_HTML
    assert "pnlChart" in dashboard.DASHBOARD_HTML
    assert "pnlRange" in dashboard.DASHBOARD_HTML
    assert "data-pnl-range" in dashboard.DASHBOARD_HTML
    assert "pnlChartReadout" in dashboard.DASHBOARD_HTML
    assert "Win rate" in dashboard.DASHBOARD_HTML
    assert "winRateMetric" in dashboard.DASHBOARD_HTML
    assert "Account balance" in dashboard.DASHBOARD_HTML
    assert "accountDetails" in dashboard.DASHBOARD_HTML
    assert "PnL" in dashboard.DASHBOARD_HTML
    assert "Over max" not in dashboard.DASHBOARD_HTML
