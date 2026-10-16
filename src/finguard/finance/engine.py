"""Compatibility facade for the harness' simple FinanceEngine protocol.

All behavior is delegated to :class:`DemoMarketDataProvider`; this module is
not a second market-data implementation.  The facade is useful when an
adapter wants to bind mode and the trusted T0/T1 snapshot once at startup.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from .providers.demo import DemoMarketDataProvider, FinanceDataError
from .risk import calculate_order_delta, evaluate_order_risk

DataMode = Literal["demo", "live"]


class FinanceEngine:
    def __init__(
        self,
        *,
        mode: DataMode = "demo",
        scenario: Literal["T0", "T1", "SCENARIO_T0", "SCENARIO_T1"] = "T0",
        fixture_path: str | Path | None = None,
    ) -> None:
        if mode not in {"demo", "live"}:
            raise ValueError("mode must be 'demo' or 'live'")
        if fixture_path is not None:
            raise ValueError("DemoMarketDataProvider uses its bundled deterministic fixture")
        if scenario in {"T0", "SCENARIO_T0"}:
            snapshot = "SCENARIO_T0"
        elif scenario in {"T1", "SCENARIO_T1"}:
            snapshot = "SCENARIO_T1"
        else:
            raise ValueError("scenario must be T0 or T1")
        self.mode = mode
        self.scenario = scenario
        self._provider = DemoMarketDataProvider(snapshot)

    async def get_quote(
        self,
        symbol: str,
        mode: DataMode | None = None,
        snapshot: str | None = None,
    ) -> dict[str, object]:
        """Read a quote, optionally selecting a trusted T0/T1 snapshot."""
        selected_mode = mode or self.mode
        selected_snapshot = snapshot or str(self._provider.scenario)
        return await self._provider.get_quote(symbol, selected_mode, selected_snapshot)

    async def get_company_events(self, symbol: str, limit: int = 5, mode: DataMode | None = None) -> list[dict[str, object]]:
        return await self._provider.get_company_events(symbol, limit, mode or self.mode)

    async def get_price_history(self, symbol: str, days: int = 20, mode: DataMode | None = None) -> dict[str, object]:
        return await self._provider.get_price_history(symbol, days, mode or self.mode)

    async def calculate_indicators(self, symbol: str, window: int = 20, mode: DataMode | None = None) -> dict[str, object]:
        return await self._provider.calculate_indicators(symbol, window, mode or self.mode)

    @staticmethod
    def preview_risk(**kwargs: Any):
        return evaluate_order_risk(**kwargs)

    @staticmethod
    def calculate_order_delta(**kwargs: Any):
        return calculate_order_delta(**kwargs)
