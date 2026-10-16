"""Small value objects for the financial boundary.

Provider methods expose ``to_dict`` values so they can be put directly in a
tool result.  Decimal is intentionally converted to a string only at this
boundary; risk calculations remain decimal-exact.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


def money(value: Decimal | int | str) -> str:
    """Return a stable, non-scientific decimal string."""
    amount = value if isinstance(value, Decimal) else Decimal(str(value))
    return format(amount, "f")


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: Decimal
    currency: str
    source: str
    mode: str
    as_of: str
    evidence_id: str
    snapshot_id: str | None = None
    valid: bool = True
    error_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "price": money(self.price),
            "currency": self.currency,
            "source": self.source,
            "mode": self.mode,
            "as_of": self.as_of,
            "evidence_id": self.evidence_id,
            "snapshot_id": self.snapshot_id,
            "valid": self.valid,
            "error_code": self.error_code,
        }


@dataclass(frozen=True)
class CompanyEvent:
    event_id: str
    symbol: str
    headline: str
    summary: str
    published_at: str
    source: str
    mode: str
    as_of: str
    evidence_id: str
    url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "symbol": self.symbol,
            "headline": self.headline,
            "summary": self.summary,
            "published_at": self.published_at,
            "source": self.source,
            "mode": self.mode,
            "as_of": self.as_of,
            "evidence_id": self.evidence_id,
            "url": self.url,
        }


@dataclass(frozen=True)
class History:
    symbol: str
    prices: list[dict[str, str]]
    days: int
    source: str
    mode: str
    as_of: str
    evidence_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "prices": self.prices,
            "days": self.days,
            "source": self.source,
            "mode": self.mode,
            "as_of": self.as_of,
            "evidence_id": self.evidence_id,
        }


@dataclass(frozen=True)
class RiskCheck:
    rule_id: str
    passed: bool
    observed: str
    limit: str
    reason: str

    def to_dict(self) -> dict[str, str | bool]:
        return {
            "rule_id": self.rule_id,
            "passed": self.passed,
            "observed": self.observed,
            "limit": self.limit,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class RiskResult:
    allowed: bool
    checks: list[RiskCheck] = field(default_factory=list)
    quote_ref: str = ""
    evaluated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "checks": [check.to_dict() for check in self.checks],
            "quote_ref": self.quote_ref,
            "evaluated_at": self.evaluated_at,
        }


@dataclass(frozen=True)
class OrderDelta:
    """Account mutation proposal; this object does not write any storage."""

    symbol: str
    side: str
    quantity: int
    execution_price: Decimal
    cash_delta: Decimal
    quantity_delta: int
    risk: RiskResult

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "execution_price": money(self.execution_price),
            "cash_delta": money(self.cash_delta),
            "quantity_delta": self.quantity_delta,
            "risk": self.risk.to_dict(),
        }
