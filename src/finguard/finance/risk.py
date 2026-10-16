"""Pure deterministic paper-order checks and account delta calculation."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from .models import OrderDelta, RiskCheck, RiskResult, money

MAX_SINGLE_WEIGHT = Decimal("0.20")
MIN_CASH_WEIGHT = Decimal("0.10")
MAX_PRICE_GAP = Decimal("0.03")


def _d(value: Any, name: str) -> Decimal:
    try:
        value = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"invalid {name}") from exc
    if not value.is_finite():
        raise ValueError(f"invalid {name}")
    return value


def _check(rule_id: str, passed: bool, observed: Decimal | int | str, limit: Decimal | int | str, reason: str) -> RiskCheck:
    def display(value: Decimal | int | str) -> str:
        # ``observed`` may intentionally carry a status such as ``valid``;
        # numeric values still use the canonical decimal serializer.
        if isinstance(value, str):
            try:
                return money(Decimal(value))
            except Exception:
                return value
        return money(value)

    return RiskCheck(rule_id, passed, display(observed), display(limit), reason)


def _normalize_positions(positions: Any) -> tuple[dict[str, int], bool]:
    if positions is None:
        return {}, True
    if not isinstance(positions, dict):
        return {}, False

    normalized: dict[str, int] = {}
    valid = True
    for raw_symbol, quantity in positions.items():
        if not isinstance(raw_symbol, str) or not raw_symbol.strip():
            valid = False
            continue
        symbol = raw_symbol.strip().upper()
        if symbol in normalized or type(quantity) is not int or quantity < 0:
            valid = False
            continue
        normalized[symbol] = quantity
    return normalized, valid


def _normalize_prices(prices: Any) -> tuple[dict[str, Decimal], bool]:
    if prices is None:
        return {}, True
    if not isinstance(prices, dict):
        return {}, False

    normalized: dict[str, Decimal] = {}
    valid = True
    for raw_symbol, raw_price in prices.items():
        if not isinstance(raw_symbol, str) or not raw_symbol.strip():
            valid = False
            continue
        symbol = raw_symbol.strip().upper()
        if symbol in normalized:
            valid = False
            continue
        try:
            price = _d(raw_price, "price")
        except ValueError:
            valid = False
            continue
        if price <= 0:
            valid = False
            continue
        normalized[symbol] = price
    return normalized, valid


def evaluate_order_risk(
    *,
    side: str,
    symbol: str,
    quantity: int,
    execution_price: Decimal | str | int,
    cash: Decimal | str | int,
    positions: dict[str, int] | None = None,
    prices: dict[str, Decimal | str | int] | None = None,
    quote_ref: str = "",
    evaluated_at: str | None = None,
    quote_valid: bool = True,
    quote_currency: str = "USD",
    currency: str = "USD",
    reference_price: Decimal | str | int | None = None,
    max_price_gap: Decimal | str = MAX_PRICE_GAP,
) -> RiskResult:
    """Apply hard BUY/SELL rules without changing account state.

    ``cash`` and all prices are decimal-like.  Positions are whole shares.
    A missing quote is represented by ``quote_valid=False`` and always blocks.
    """
    checks: list[RiskCheck] = []
    side = str(side).upper()
    symbol = str(symbol).upper()
    normalized_positions, positions_valid = _normalize_positions(positions)
    normalized_prices, prices_valid = _normalize_prices(prices)
    quantity_valid = type(quantity) is int and quantity > 0
    quantity_int = quantity if quantity_valid else 0
    price = _d(execution_price, "execution_price")
    cash_dec = _d(cash, "cash")
    max_gap = _d(max_price_gap, "max_price_gap")
    old_quantity = normalized_positions.get(symbol, 0)
    missing_price_symbols = [
        held_symbol
        for held_symbol, held_quantity in normalized_positions.items()
        if held_quantity > 0 and held_symbol not in normalized_prices
    ]
    valuation_valid = positions_valid and prices_valid and not missing_price_symbols
    old_values = (
        sum(
            (Decimal(qty) * normalized_prices[sym] for sym, qty in normalized_positions.items()),
            Decimal(0),
        )
        if valuation_valid
        else Decimal(0)
    )
    equity_before = cash_dec + old_values
    checks.append(_check("QUOTE_VALID", quote_valid and quote_currency == currency == "USD" and price > 0, "valid" if quote_valid else "invalid", "valid", "quote must be valid, USD, and positive"))
    checks.append(_check("ORDER_INPUT", side in {"BUY", "SELL"} and quantity_valid and price > 0, quantity_int, 1, "side, quantity and execution price must be valid; quantity must be a positive int"))
    checks.append(_check("POSITIONS_VALID", positions_valid, "valid" if positions_valid else "invalid", "non-negative int quantities", "positions must contain only non-negative integer quantities"))
    if side == "SELL":
        checks.append(_check("NO_NAKED_SHORT", quantity_int <= old_quantity, quantity_int, old_quantity, "sell quantity cannot exceed current holdings"))
    else:
        checks.append(_check("BUY_CASH", cash_dec >= Decimal(quantity_int) * price, cash_dec, Decimal(quantity_int) * price, "cash must cover the purchase"))
    if side == "BUY":
        checks.append(_check(
            "PORTFOLIO_VALUATION",
            valuation_valid,
            "valid" if valuation_valid else ", ".join(missing_price_symbols) or "invalid position/price data",
            "valid price for every non-zero position",
            "cannot calculate equity safely when a held position lacks a positive valid price",
        ))
        if valuation_valid and equity_before > 0:
            new_value = Decimal(old_quantity + quantity_int) * price
            checks.append(_check("MAX_SINGLE_WEIGHT", new_value / equity_before <= MAX_SINGLE_WEIGHT, new_value / equity_before, MAX_SINGLE_WEIGHT, "post-trade single-symbol weight must be at most 20%"))
            cash_after = cash_dec - Decimal(quantity_int) * price
            checks.append(_check("MIN_CASH_BUFFER", cash_after / equity_before >= MIN_CASH_WEIGHT, cash_after / equity_before, MIN_CASH_WEIGHT, "post-trade cash must remain at least 10% of equity"))
        else:
            checks.append(_check("MAX_SINGLE_WEIGHT", False, 0, MAX_SINGLE_WEIGHT, "equity must be positive and fully valued"))
            checks.append(_check("MIN_CASH_BUFFER", False, 0, MIN_CASH_WEIGHT, "equity must be positive and fully valued"))
    if reference_price is not None:
        reference = _d(reference_price, "reference_price")
        gap = abs(price - reference) / reference if reference > 0 else Decimal("Infinity")
        checks.append(_check("PRICE_GAP", gap <= max_gap, gap, max_gap, "execution price must be within 3% of proposal price"))
    allowed = all(check.passed for check in checks)
    return RiskResult(allowed, checks, quote_ref, evaluated_at or datetime.now(timezone.utc).isoformat())


def calculate_order_delta(
    *,
    side: str,
    symbol: str,
    quantity: int,
    execution_price: Decimal | str | int,
    cash: Decimal | str | int,
    positions: dict[str, int] | None = None,
    prices: dict[str, Decimal | str | int] | None = None,
    quote_ref: str = "",
    evaluated_at: str | None = None,
    quote_valid: bool = True,
    quote_currency: str = "USD",
    currency: str = "USD",
    reference_price: Decimal | str | int | None = None,
) -> OrderDelta:
    """Return a proposed account delta, even when risk blocks it.

    The caller must only persist the delta when ``delta.risk.allowed`` is true.
    """
    side = str(side).upper()
    symbol = str(symbol).upper()
    if type(quantity) is not int or quantity <= 0:
        raise ValueError("quantity must be a positive int")
    quantity_int = quantity
    price = _d(execution_price, "execution_price")
    risk = evaluate_order_risk(
        side=side, symbol=symbol, quantity=quantity_int, execution_price=price,
        cash=cash, positions=positions, prices=prices, quote_ref=quote_ref,
        evaluated_at=evaluated_at, quote_valid=quote_valid,
        quote_currency=quote_currency, currency=currency,
        reference_price=reference_price,
    )
    cash_sign = Decimal(1) if side == "SELL" else Decimal(-1)
    quantity_sign = -1 if side == "SELL" else 1
    return OrderDelta(
        symbol,
        side,
        quantity_int,
        price,
        cash_sign * price * quantity_int,
        quantity_sign * quantity_int,
        risk,
    )
