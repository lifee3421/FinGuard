import pytest
from math import sqrt

from finguard.finance.indicators import IndicatorError, calculate_indicators


def history(closes, volumes=None):
    volumes = volumes or [100 for _ in closes]
    return {
        "symbol": "AAPL",
        "prices": [{"close": str(close), "volume": str(volume)} for close, volume in zip(closes, volumes)],
        "source": "test",
        "mode": "demo",
        "as_of": "2026-10-10T00:00:00+00:00",
        "evidence_id": "test:history",
    }


def test_indicators_return_exact_decimal_strings():
    result = calculate_indicators(history(["100", "105", "110"], [100, 200, 300]), window=3)
    assert result["range_return"] == "0.1"
    assert result["sma"] == "105"
    assert result["volume_ratio"] == "1.5"
    assert result["volatility"]


def test_indicators_reject_insufficient_history_and_zero_volume():
    with pytest.raises(IndicatorError, match="INSUFFICIENT_HISTORY"):
        calculate_indicators(history([100]), window=2)
    with pytest.raises(IndicatorError, match="ZERO_VOLUME"):
        calculate_indicators(history([100, 101], [0, 0]), window=2)


def test_indicators_reject_bad_values():
    with pytest.raises(IndicatorError, match="close must be positive"):
        calculate_indicators(history([100, 0]), window=2)
    with pytest.raises(IndicatorError, match="volume must be non-negative"):
        calculate_indicators(history([100, 101], [10, -1]), window=2)


def test_indicator_window_uses_only_the_last_requested_rows():
    result = calculate_indicators(history([100, 1000, 105, 110]), window=3)
    assert result["range_return"] == "-0.89"
    assert result["sma"] == "405"


def test_volatility_is_daily_population_stddev_and_not_annualized():
    result = calculate_indicators(history([100, 110, 100]), window=3)
    daily_returns = [0.1, 100 / 110 - 1]
    mean_return = sum(daily_returns) / len(daily_returns)
    daily_population_stddev = sqrt(sum((value - mean_return) ** 2 for value in daily_returns) / len(daily_returns))
    observed = float(result["volatility"])
    assert observed == pytest.approx(daily_population_stddev, rel=1e-12)
    assert observed < daily_population_stddev * sqrt(252)
