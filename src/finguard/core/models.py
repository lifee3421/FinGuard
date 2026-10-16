from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal

TaskStatus = Literal[
    "CREATED",
    "RUNNING",
    "WAITING_APPROVAL",
    "COMPLETED",
    "FAILED",
    "CANCELLED",
]
ProposalStatus = Literal["PENDING", "APPROVED", "REJECTED", "BLOCKED"]
OrderStatus = Literal["EXECUTED", "BLOCKED"]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def jsonable(value: Any) -> Any:
    """Convert DTO values to JSON-safe values without exposing credentials."""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "to_dict"):
        return jsonable(value.to_dict())
    if hasattr(value, "__dataclass_fields__"):
        return jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value


@dataclass(slots=True)
class ToolError:
    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return jsonable(asdict(self))


@dataclass(slots=True)
class ToolResult:
    ok: bool
    data: Any = None
    error: ToolError | None = None

    @classmethod
    def success(cls, data: Any = None) -> "ToolResult":
        return cls(ok=True, data=data)

    @classmethod
    def failure(
        cls,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> "ToolResult":
        return cls(ok=False, error=ToolError(code, message, retryable, details))

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "data": jsonable(self.data) if self.ok else None,
            "error": self.error.to_dict() if self.error else None,
        }


@dataclass(slots=True)
class TaskView:
    run_id: str
    status: TaskStatus
    step: int
    data_mode: str
    allow_paper_order_proposals: bool
    pending_proposal: dict[str, Any] | None = None
    final_answer: str | None = None
    error: dict[str, Any] | None = None
    version: int = 0

    def to_dict(self) -> dict[str, Any]:
        return jsonable(asdict(self))


@dataclass(slots=True)
class DecisionView:
    run_id: str
    proposal_id: str
    decision: str
    proposal_status: str
    order_status: str | None
    task_status: str
    receipt: dict[str, Any] | None
    version: int
    error: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return jsonable(asdict(self))


@dataclass(slots=True)
class AuditEvent:
    run_id: str
    seq: int
    event_type: str
    step: int
    tool_call_id: str | None
    elapsed_ms: float | None
    status: str
    error_code: str | None
    payload: dict[str, Any]
    mode: str
    model: str | None
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return jsonable(asdict(self))


@dataclass(slots=True)
class ToolCall:
    call_id: str
    name: str
    arguments: str


@dataclass(slots=True)
class ToolContext:
    run_id: str
    data_mode: str
    step: int
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ProposalDraft:
    proposal_id: str
    run_id: str
    tool_call_id: str
    symbol: str
    side: str
    quantity: int
    rationale: str
    evidence_ids: list[str]
    reference_price: str
    reference_quote_ref: str
    status: str = "PENDING"

    def to_dict(self) -> dict[str, Any]:
        return jsonable(asdict(self))
