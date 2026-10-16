from decimal import Decimal

import pytest

from finguard.finance.risk import calculate_order_delta, evaluate_order_risk


def prices():
    return {"AAPL": "190.00", "MSFT": "420.00", "NVDA": "130.00"}


def test_buy_weight_and_cash_boundary():
    allowed = evaluate_order_risk(
        side="BUY", symbol="AAPL", quantity=100, execution_price="190", cash="100000",
        positions={}, prices=prices(), quote_ref="demo:scenario_t1:aapl", reference_price="190",
    )
    assert allowed.allowed
    too_heavy = evaluate_order_risk(
        side="BUY", symbol="AAPL", quantity=106, execution_price="190", cash="100000",
        positions={}, prices=prices(), quote_ref="demo", reference_price="190",
    )
    assert not too_heavy.allowed
    assert any(check.rule_id == "MAX_SINGLE_WEIGHT" and not check.passed for check in too_heavy.checks)


def test_buy_cash_and_sell_does_not_apply_buy_buffer():
    no_cash = evaluate_order_risk(
        side="BUY", symbol="AAPL", quantity=1, execution_price="190", cash="100",
        positions={}, prices=prices(), quote_ref="demo",
    )
    assert not no_cash.allowed
    sell = evaluate_order_risk(
        side="SELL", symbol="AAPL", quantity=10, execution_price="190", cash="100",
        positions={"AAPL": 10}, prices=prices(), quote_ref="demo",
    )
    assert sell.allowed
    naked = evaluate_order_risk(
        side="SELL", symbol="AAPL", quantity=11, execution_price="190", cash="100000",
        positions={"AAPL": 10}, prices=prices(), quote_ref="demo",
    )
    assert not naked.allowed


def test_t0_t1_gap_and_invalid_quotes_block():
    gap = evaluate_order_risk(
        side="BUY", symbol="AAPL", quantity=1, execution_price="200", cash="100000",
        positions={}, prices=prices(), quote_ref="demo:gap", reference_price="190",
    )
    assert not gap.allowed
    assert any(check.rule_id == "PRICE_GAP" and not check.passed for check in gap.checks)
    invalid = evaluate_order_risk(
        side="BUY", symbol="AAPL", quantity=1, execution_price="190", cash="100000",
        positions={}, prices=prices(), quote_ref="demo:stale", quote_valid=False,
    )
    assert not invalid.allowed
    assert any(check.rule_id == "QUOTE_VALID" and not check.passed for check in invalid.checks)


def test_order_delta_is_pure_and_decimal_safe():
    delta = calculate_order_delta(
        side="SELL", symbol="AAPL", quantity=2, execution_price="190.25", cash="100000",
        positions={"AAPL": 2}, prices=prices(), quote_ref="demo:t1",
    )
    assert delta.cash_delta == Decimal("380.50")
    assert delta.quantity_delta == -2
    assert delta.to_dict()["execution_price"] == "190.25"


def test_buy_requires_complete_valuation_for_existing_positions():
    complete = evaluate_order_risk(
        side="BUY", symbol="AAPL", quantity=100, execution_price="20", cash="10000",
        positions={"MSFT": 200}, prices={"MSFT": "100"}, quote_ref="demo:t1",
    )
    assert complete.allowed

    missing_msft_price = evaluate_order_risk(
        side="BUY", symbol="AAPL", quantity=100, execution_price="20", cash="10000",
        positions={"MSFT": 200}, prices={}, quote_ref="demo:t1",
    )
    assert not missing_msft_price.allowed
    valuation = next(check for check in missing_msft_price.checks if check.rule_id == "PORTFOLIO_VALUATION")
    assert valuation.passed is False
    assert "MSFT" in valuation.observed


@pytest.mark.parametrize("quantity", [True, 1.5, Decimal("2.5"), "2"])
def test_risk_rejects_non_integer_order_quantity(quantity):
    result = evaluate_order_risk(
        side="SELL", symbol="AAPL", quantity=quantity, execution_price="190", cash="100000",
        positions={"AAPL": 10}, prices=prices(), quote_ref="demo:t1",
    )
    assert not result.allowed
    assert next(check for check in result.checks if check.rule_id == "ORDER_INPUT").passed is False


@pytest.mark.parametrize("held_quantity", [True, 1.5, -1, Decimal("2.0")])
def test_risk_rejects_non_integer_or_negative_positions(held_quantity):
    result = evaluate_order_risk(
        side="SELL", symbol="AAPL", quantity=1, execution_price="190", cash="100000",
        positions={"AAPL": held_quantity}, prices=prices(), quote_ref="demo:t1",
    )
    assert not result.allowed
    assert next(check for check in result.checks if check.rule_id == "POSITIONS_VALID").passed is False


@pytest.mark.parametrize("quantity", [True, 1.5, Decimal("2.5"), "2", 0, -1])
def test_order_delta_rejects_invalid_quantity_instead_of_coercing(quantity):
    with pytest.raises(ValueError, match="quantity must be a positive int"):
        calculate_order_delta(
            side="BUY", symbol="AAPL", quantity=quantity, execution_price="190", cash="100000",
            positions={}, prices=prices(), quote_ref="demo:t0",
        )
