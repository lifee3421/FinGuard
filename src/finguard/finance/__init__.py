"""Deterministic financial domain services used by the FinGuard harness.

The finance package deliberately has no persistence or LLM dependencies.  It
returns JSON-safe dictionaries at provider boundaries and uses ``Decimal`` for
all monetary calculations internally.
"""

from .indicators import calculate_indicators
from .engine import FinanceEngine
from .models import OrderDelta
from .providers.demo import DemoMarketDataProvider
from .risk import evaluate_order_risk, calculate_order_delta

__all__ = [
    "DemoMarketDataProvider",
    "FinanceEngine",
    "OrderDelta",
    "calculate_indicators",
    "evaluate_order_risk",
    "calculate_order_delta",
]
