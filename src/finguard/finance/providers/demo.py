"""Offline deterministic market-data provider.

The fixture is synthetic by design.  ``scenario`` is a trusted constructor
setting for tests/demo scripts; it is never read from model tool arguments.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

from ..indicators import calculate_indicators
from ..models import CompanyEvent, History, Quote, money
from ..risk import calculate_order_delta, evaluate_order_risk

SCENARIO_T0 = "SCENARIO_T0"
SCENARIO_T1 = "SCENARIO_T1"
SCENARIO_NORMAL = "normal"
SCENARIO_PRICE_GAP = "price_gap"
SCENARIO_STALE = "stale"
SCENARIO_UNAVAILABLE = "unavailable"
MAX_HISTORY_ROWS = 365
FIXTURE_PATH = Path(__file__).resolve().parents[4] / "data" / "demo" / "market.json"


class FinanceDataError(RuntimeError):
    """A provider failure with a stable machine-readable error code."""

    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable

    def to_error(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "retryable": self.retryable}


class DemoMarketDataProvider:
    """Synthetic AAPL/MSFT/NVDA provider with T0/T1 approval fixtures."""

    mode: Literal["demo"] = "demo"

    def __init__(
        self,
        scenario: str = SCENARIO_T0,
        *,
        initial_cash: Decimal | str | int = Decimal("100000.00"),
        initial_positions: dict[str, int] | None = None,
    ) -> None:
        self._fixture = self._load_fixture()
        self.source = self._fixture["source"]
        self.as_of = self._fixture["as_of"]
        self._as_of_date = datetime.fromisoformat(self.as_of).date()
        self.scenario = scenario
        self.initial_cash = Decimal(str(initial_cash))
        self.initial_positions = self._validate_initial_positions(initial_positions)

    def _validate_initial_positions(self, positions: Any) -> dict[str, int]:
        if positions is None:
            return {}
        if not isinstance(positions, dict):
            raise ValueError("initial_positions must be a mapping")
        normalized: dict[str, int] = {}
        for raw_symbol, quantity in positions.items():
            if not isinstance(raw_symbol, str) or not raw_symbol.strip():
                raise ValueError("position symbols must be non-empty strings")
            symbol = raw_symbol.strip().upper()
            if symbol in normalized:
                raise ValueError(f"duplicate normalized position symbol: {symbol}")
            if type(quantity) is not int or quantity < 0:
                raise ValueError(f"position quantity for {symbol} must be a non-negative int")
            normalized[symbol] = quantity
        return normalized

    @staticmethod
    def _load_fixture() -> dict[str, Any]:
        try:
            fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FinanceDataError("FIXTURE_UNAVAILABLE", "Demo market fixture could not be read") from exc
        if not isinstance(fixture, dict):
            raise FinanceDataError("FIXTURE_INVALID", "Demo market fixture root must be an object")
        symbols = fixture.get("supported_symbols")
        quotes = fixture.get("quotes")
        if (
            fixture.get("mode") != "demo"
            or not isinstance(fixture.get("source"), str)
            or not fixture["source"].strip()
            or not isinstance(symbols, list)
            or not symbols
            or not isinstance(quotes, dict)
        ):
            raise FinanceDataError("FIXTURE_INVALID", "Demo market fixture metadata is incomplete")
        if any(not isinstance(s, str) or s != s.upper() for s in symbols) or len(set(symbols)) != len(symbols):
            raise FinanceDataError("FIXTURE_INVALID", "Fixture symbols must be unique uppercase strings")
        if set(quotes) != set(symbols):
            raise FinanceDataError("FIXTURE_INVALID", "Fixture quote symbols do not match supported_symbols")
        try:
            as_of = datetime.fromisoformat(fixture["as_of"])
            if as_of.tzinfo is None or as_of.date().weekday() >= 5:
                raise ValueError
        except (KeyError, TypeError, ValueError) as exc:
            raise FinanceDataError("FIXTURE_INVALID", "Fixture as_of must be a timezone-aware weekday timestamp") from exc
        required_scenarios = {SCENARIO_T0, SCENARIO_T1, SCENARIO_PRICE_GAP}
        for symbol in symbols:
            rows = quotes[symbol]
            if not isinstance(rows, dict) or not required_scenarios.issubset(rows):
                raise FinanceDataError("FIXTURE_INVALID", f"Fixture quote scenarios are incomplete for {symbol}")
            for scenario in required_scenarios:
                try:
                    price = Decimal(str(rows[scenario]))
                except (InvalidOperation, TypeError, ValueError) as exc:
                    raise FinanceDataError("FIXTURE_INVALID", f"Invalid {scenario} quote for {symbol}") from exc
                if not price.is_finite() or price <= 0:
                    raise FinanceDataError("FIXTURE_INVALID", f"Invalid {scenario} quote for {symbol}")
        return fixture

    def _check_symbol(self, symbol: str) -> str:
        normalized = str(symbol).strip().upper()
        if normalized not in self._fixture["supported_symbols"]:
            raise FinanceDataError("SYMBOL_UNSUPPORTED", f"demo fixture does not contain {normalized or '<empty>'}")
        return normalized

    async def _yield_io(self) -> None:
        # Keep the provider genuinely awaitable so the dispatcher can schedule
        # independent calls concurrently.  No network is needed for demo data.
        await asyncio.sleep(0)

    def _scenario_price(self, symbol: str, snapshot: str | None = None) -> tuple[Decimal, bool, str | None]:
        scenario = snapshot or self.scenario
        quotes = self._fixture["quotes"][symbol]
        if scenario in {"T0", SCENARIO_T0}:
            return Decimal(quotes[SCENARIO_T0]), True, None
        if scenario in {"T1", SCENARIO_T1, SCENARIO_NORMAL}:
            return Decimal(quotes[SCENARIO_T1]), True, None
        if scenario == SCENARIO_PRICE_GAP:
            return Decimal(quotes[SCENARIO_PRICE_GAP]), True, None
        if scenario == SCENARIO_STALE:
            return Decimal(quotes[SCENARIO_T0]), False, "QUOTE_STALE"
        if scenario == SCENARIO_UNAVAILABLE:
            return Decimal("0"), False, "QUOTE_UNAVAILABLE"
        raise FinanceDataError("INVALID_SCENARIO", f"unknown trusted demo scenario: {scenario}")

    async def get_quote(self, symbol: str, mode: str = "demo", snapshot: str | None = None) -> dict[str, Any]:
        await self._yield_io()
        if mode != "demo":
            raise FinanceDataError("LIVE_UNAVAILABLE", "DemoMarketDataProvider only supports mode=demo")
        normalized = self._check_symbol(symbol)
        price, valid, error_code = self._scenario_price(normalized, snapshot)
        snap = snapshot or self.scenario
        quote = Quote(
            symbol=normalized,
            price=price,
            currency="USD",
            source=self.source,
            mode="demo",
            as_of=self.as_of,
            evidence_id=f"demo:{snap.lower()}:{normalized}:quote",
            snapshot_id=f"{snap}:{normalized}",
            valid=valid,
            error_code=error_code,
        )
        return quote.to_dict()

    async def get_company_events(self, symbol: str, limit: int = 5, mode: str = "demo") -> list[dict[str, Any]]:
        await self._yield_io()
        if mode != "demo":
            raise FinanceDataError("LIVE_UNAVAILABLE", "DemoMarketDataProvider only supports mode=demo")
        normalized = self._check_symbol(symbol)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 20:
            raise FinanceDataError("INVALID_ARGS", "limit must be an integer from 1 to 20")
        event_templates = [
            ("earnings", "Synthetic quarterly earnings update", "Synthetic fixture: reported revenue and margin observations."),
            ("guidance", "Synthetic management outlook", "Synthetic fixture: forward guidance was revised for scenario testing."),
            ("product", "Synthetic product milestone", "Synthetic fixture: product milestone used for deterministic research demos."),
        ]
        return [
            CompanyEvent(
                event_id=f"demo:event:{normalized}:{kind}",
                symbol=normalized,
                headline=headline,
                summary=summary,
                published_at=f"2026-10-{8 - index:02d}T12:00:00+00:00",
                source=self.source,
                mode="demo",
                as_of=self.as_of,
                evidence_id=f"demo:event:{normalized}:{kind}",
                url=None,
            ).to_dict()
            for index, (kind, headline, summary) in enumerate(event_templates[:limit])
        ]

    def _history_rows(self, symbol: str, days: int) -> list[dict[str, str]]:
        base = Decimal(self._fixture["quotes"][symbol][SCENARIO_T0])
        session_dates: list[date] = []
        session_day = self._as_of_date
        while len(session_dates) < MAX_HISTORY_ROWS:
            if session_day.weekday() < 5:
                session_dates.append(session_day)
            session_day -= timedelta(days=1)
        session_dates.reverse()
        rows: list[dict[str, str]] = []
        for index, session_date in enumerate(session_dates):
            close = base + Decimal(index - (MAX_HISTORY_ROWS - 1)) * (base / Decimal("500"))
            symbol_index = self._fixture["supported_symbols"].index(symbol)
            volume = Decimal(1000000 + (index % 5) * 125000 + symbol_index * 50000)
            rows.append({"date": session_date.isoformat(), "close": money(close), "volume": money(volume)})
        return rows[-days:]

    async def get_price_history(self, symbol: str, days: int = 20, mode: str = "demo") -> dict[str, Any]:
        await self._yield_io()
        if mode != "demo":
            raise FinanceDataError("LIVE_UNAVAILABLE", "DemoMarketDataProvider only supports mode=demo")
        normalized = self._check_symbol(symbol)
        if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 365:
            raise FinanceDataError("INVALID_ARGS", "days must be an integer from 1 to 365")
        history = History(
            symbol=normalized,
            prices=self._history_rows(normalized, days),
            days=days,
            source=self.source,
            mode="demo",
            as_of=self.as_of,
            evidence_id=f"demo:history:{normalized}:{days}",
        )
        return history.to_dict()

    async def calculate_indicators(self, symbol: str, window: int = 20, mode: str = "demo") -> dict[str, Any]:
        history = await self.get_price_history(symbol, max(window, 20), mode)
        result = calculate_indicators(history, window)
        # Keep the canonical scalar fields flat while providing the common
        # nested view used by older tool adapters.
        result["indicators"] = {
            "range_return": result["range_return"],
            "window_return": result["range_return"],
            "sma": result["sma"],
            "volume_ratio": result["volume_ratio"],
            "volatility": result["volatility"],
            "daily_return_volatility": result["volatility"],
        }
        return result

    async def get_portfolio(self) -> dict[str, Any]:
        await self._yield_io()
        return {
            "cash": money(self.initial_cash),
            "equity": money(self.initial_cash),
            "currency": "USD",
            "positions": [
                {"symbol": symbol, "quantity": quantity, "market_value": "0.00"}
                for symbol, quantity in sorted(self.initial_positions.items())
            ],
            "updated_at": self.as_of,
            "valuation_ref": None,
        }

    async def get_quote_result(self, symbol: str, mode: str = "demo", snapshot: str | None = None) -> dict[str, Any]:
        """Return the frozen tool envelope for callers that do not catch errors."""
        try:
            return {"ok": True, "data": await self.get_quote(symbol, mode, snapshot), "error": None}
        except FinanceDataError as exc:
            return {"ok": False, "data": None, "error": exc.to_error()}

    @staticmethod
    def preview_risk(**kwargs: Any):
        return evaluate_order_risk(**kwargs)

    @staticmethod
    def calculate_order_delta(**kwargs: Any):
        return calculate_order_delta(**kwargs)
