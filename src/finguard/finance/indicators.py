"""Deterministic price and volume indicators."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, getcontext
from math import sqrt
from typing import Any

getcontext().prec = 28


class IndicatorError(ValueError):
    """Raised when a history cannot support a requested indicator window."""


def _decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise IndicatorError(f"invalid {field}: {value!r}") from exc
    if not result.is_finite():
        raise IndicatorError(f"invalid {field}: {value!r}")
    return result


def _rows(history: Any) -> tuple[str, list[dict[str, Any]], str, str]:
    if isinstance(history, dict):
        symbol = str(history.get("symbol", "")).upper()
        rows = history.get("prices", history.get("candles", []))
        evidence_id = str(history.get("evidence_id", ""))
        as_of = str(history.get("as_of", ""))
    else:
        symbol, rows, evidence_id, as_of = "", history, "", ""
    if not isinstance(rows, list):
        raise IndicatorError("history prices must be a list")
    return symbol, rows, evidence_id, as_of


def calculate_indicators(history: Any, window: int = 20) -> dict[str, Any]:
    """Calculate range return, SMA, volume ratio and daily unannualized volatility.

    ``history`` is the contract History dictionary (or a list of price rows
    for convenience). ``window`` selects the last N closes and therefore
    ``window - 1`` daily close-to-close returns. Volatility is the population
    standard deviation of those daily returns; it is not annualized. Returned
    values are decimal strings.
    """
    if not isinstance(window, int) or isinstance(window, bool) or window < 2:
        raise IndicatorError("window must be an integer >= 2")
    symbol, rows, evidence_id, as_of = _rows(history)
    if len(rows) < window:
        raise IndicatorError(f"INSUFFICIENT_HISTORY: need {window}, got {len(rows)}")
    sample = rows[-window:]
    closes: list[Decimal] = []
    volumes: list[Decimal] = []
    for row in sample:
        if not isinstance(row, dict):
            raise IndicatorError("history row must be an object")
        close = _decimal(row.get("close", row.get("price")), "close")
        if close <= 0:
            raise IndicatorError("close must be positive")
        volume = _decimal(row.get("volume"), "volume")
        if volume < 0:
            raise IndicatorError("volume must be non-negative")
        closes.append(close)
        volumes.append(volume)
    first, last = closes[0], closes[-1]
    range_return = (last - first) / first
    sma = sum(closes, Decimal(0)) / Decimal(window)
    average_volume = sum(volumes, Decimal(0)) / Decimal(window)
    if average_volume == 0:
        raise IndicatorError("ZERO_VOLUME: cannot calculate volume ratio")
    volume_ratio = volumes[-1] / average_volume
    returns = [(closes[i] / closes[i - 1]) - Decimal(1) for i in range(1, len(closes))]
    average_return = sum(returns, Decimal(0)) / Decimal(len(returns))
    variance = sum((value - average_return) ** 2 for value in returns) / Decimal(len(returns))
    volatility = Decimal(str(sqrt(float(variance))))
    return {
        "symbol": symbol,
        "window": window,
        "range_return": format(range_return, "f"),
        "sma": format(sma, "f"),
        "volume_ratio": format(volume_ratio, "f"),
        "volatility": format(volatility, "f"),
        "source": str(history.get("source", "")) if isinstance(history, dict) else "",
        "mode": str(history.get("mode", "")) if isinstance(history, dict) else "",
        "as_of": as_of,
        "evidence_id": evidence_id,
    }


def calculate_indicators_for_symbol(provider: Any, symbol: str, window: int = 20) -> Any:
    """Compatibility helper for synchronous callers; providers are async.

    The harness should call ``await provider.calculate_indicators`` instead.
    This helper intentionally does not run an event loop or perform I/O.
    """
    raise TypeError("use await provider.calculate_indicators(symbol, window)")
