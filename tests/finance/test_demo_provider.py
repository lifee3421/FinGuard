import asyncio
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from finguard.finance.providers.demo import DemoMarketDataProvider, FinanceDataError


def run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("symbol", ["AAPL", "msft", "NVDA"])
def test_demo_quote_has_frozen_provenance(symbol):
    quote = run(DemoMarketDataProvider().get_quote(symbol, snapshot="SCENARIO_T0"))
    assert quote["symbol"] == symbol.upper()
    assert quote["mode"] == "demo"
    assert quote["source"] == "synthetic-demo-fixture"
    assert quote["price"]
    assert quote["evidence_id"].startswith("demo:scenario_t0:")


def test_unsupported_symbol_is_structured_domain_error():
    with pytest.raises(FinanceDataError) as exc_info:
        run(DemoMarketDataProvider().get_quote("TSLA"))
    assert exc_info.value.code == "SYMBOL_UNSUPPORTED"


def test_live_mode_is_rejected_without_demo_fallback():
    provider = DemoMarketDataProvider()
    with pytest.raises(FinanceDataError) as exc_info:
        run(provider.get_quote("AAPL", mode="live"))
    assert exc_info.value.code == "LIVE_UNAVAILABLE"


def test_t1_scenarios_are_deterministic_and_distinct():
    provider = DemoMarketDataProvider()
    t0 = run(provider.get_quote("AAPL", snapshot="SCENARIO_T0"))
    t1 = run(provider.get_quote("AAPL", snapshot="SCENARIO_T1"))
    gap = run(provider.get_quote("AAPL", snapshot="price_gap"))
    assert t0["price"] == "190.00"
    assert t1["price"] == "191.50"
    assert gap["price"] == "200.00"


def test_stale_and_unavailable_quotes_are_not_valid():
    provider = DemoMarketDataProvider()
    stale = run(provider.get_quote("MSFT", snapshot="stale"))
    unavailable = run(provider.get_quote("MSFT", snapshot="unavailable"))
    assert stale["valid"] is False and stale["error_code"] == "QUOTE_STALE"
    assert unavailable["valid"] is False and unavailable["error_code"] == "QUOTE_UNAVAILABLE"


def test_quote_failure_has_frozen_structured_error_envelope():
    result = run(DemoMarketDataProvider().get_quote_result("TSLA"))
    assert result == {
        "ok": False,
        "data": None,
        "error": {
            "code": "SYMBOL_UNSUPPORTED",
            "message": "demo fixture does not contain TSLA",
            "retryable": False,
        },
    }


def test_history_and_events_are_synthetic_and_sized():
    provider = DemoMarketDataProvider()
    history = run(provider.get_price_history("NVDA", days=20))
    events = run(provider.get_company_events("NVDA", limit=2))
    assert len(history["prices"]) == 20
    assert history["mode"] == "demo"
    assert all("close" in row and "volume" in row for row in history["prices"])
    assert len(events) == 2
    assert all(event["source"] == "synthetic-demo-fixture" for event in events)


def test_history_dates_are_weekdays_not_after_as_of_and_window_is_stable():
    provider = DemoMarketDataProvider()
    history_20 = run(provider.get_price_history("AAPL", days=20))
    history_365 = run(provider.get_price_history("AAPL", days=365))
    as_of_date = datetime.fromisoformat(history_365["as_of"]).date()
    dates = [date.fromisoformat(row["date"]) for row in history_365["prices"]]

    assert len(dates) == 365
    assert all(item.weekday() < 5 for item in dates)
    assert all(item <= as_of_date for item in dates)
    assert dates[-1] == as_of_date
    assert history_20["prices"] == history_365["prices"][-20:]


def test_market_json_is_the_single_authoritative_quote_source():
    fixture_path = Path(__file__).resolve().parents[2] / "data" / "demo" / "market.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    provider = DemoMarketDataProvider()

    for symbol in fixture["supported_symbols"]:
        for scenario in ("SCENARIO_T0", "SCENARIO_T1", "price_gap"):
            quote = run(provider.get_quote(symbol, snapshot=scenario))
            assert quote["price"] == fixture["quotes"][symbol][scenario]


def test_provider_rejects_incomplete_market_fixture(monkeypatch, tmp_path):
    fixture_path = Path(__file__).resolve().parents[2] / "data" / "demo" / "market.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    del fixture["quotes"]["AAPL"]["SCENARIO_T1"]
    broken_fixture = tmp_path / "market.json"
    broken_fixture.write_text(json.dumps(fixture), encoding="utf-8")
    monkeypatch.setattr("finguard.finance.providers.demo.FIXTURE_PATH", broken_fixture)

    with pytest.raises(FinanceDataError) as exc_info:
        DemoMarketDataProvider()
    assert exc_info.value.code == "FIXTURE_INVALID"


def test_provider_exposes_pure_risk_and_order_delta_adapters():
    provider = DemoMarketDataProvider()
    kwargs = {
        "side": "BUY",
        "symbol": "AAPL",
        "quantity": 1,
        "execution_price": "190.00",
        "cash": "100000.00",
        "positions": {},
        "prices": {},
        "reference_price": "190.00",
    }

    risk = provider.preview_risk(**kwargs)
    delta = provider.calculate_order_delta(**kwargs)
    assert risk.allowed
    assert delta.risk.allowed
    assert delta.cash_delta == Decimal("-190.00")


@pytest.mark.parametrize("quantity", [True, 1.5, Decimal("2.0"), -1])
def test_provider_rejects_invalid_initial_position_quantities(quantity):
    with pytest.raises(ValueError, match="non-negative int"):
        DemoMarketDataProvider(initial_positions={"AAPL": quantity})
