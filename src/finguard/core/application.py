from __future__ import annotations

import inspect
import json
import os
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from .agent import AgentLoop
from .config import CoreConfig
from .dispatcher import ToolDispatcher, ToolRegistry, ToolSpec, validate_json_schema
from .llm_client import FakeLLM, LLMClient, OpenAICompatibleLLMClient
from .models import DecisionView, TaskView, ToolCall, ToolContext, ToolResult
from .storage import SQLiteRepository


PROPOSAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "symbol": {"type": "string", "minLength": 1},
        "side": {"type": "string", "enum": ["BUY", "SELL"]},
        "quantity": {"type": "integer", "minimum": 1},
        "rationale": {"type": "string", "minLength": 1, "maxLength": 4000},
        "evidence_ids": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "minItems": 1,
            "maxItems": 50,
        },
    },
    "required": ["symbol", "side", "quantity", "rationale", "evidence_ids"],
    "additionalProperties": False,
}


SYSTEM_PROMPT = """You are FinGuard, a research assistant for a synthetic paper-trading account.
Use only the supplied tools for market facts. The task data mode is fixed for this run.
Do not claim a real trade or real-world investment advice. Research-only requests may end with NO_TRADE.
Paper order proposals are available only when the user explicitly enabled them; proposing an order never executes it.
"""


class ApplicationService:
    """Single async facade used by CLI and HTTP transports."""

    def __init__(
        self,
        repository: SQLiteRepository | None = None,
        llm: LLMClient | None = None,
        finance: Any | None = None,
        config: CoreConfig | None = None,
        db_path: str | None = None,
    ) -> None:
        self.config = config or CoreConfig.from_env()
        self.repository = repository or SQLiteRepository(
            db_path or os.getenv("FIN_GUARD_DB_PATH", "finguard.sqlite3"),
            initial_cash=self.config.initial_cash,
        )
        self.finance = finance or self._default_finance()
        self.llm = llm or self._default_llm()
        self.registry = ToolRegistry()
        self._register_tools()
        self.dispatcher = ToolDispatcher(self.registry, timeout_seconds=self.config.tool_timeout_seconds)
        self.loop = AgentLoop(
            repository=self.repository,
            dispatcher=self.dispatcher,
            llm=self.llm,
            config=self.config,
            proposal_handler=self._handle_proposal,
        )

    @staticmethod
    def _default_finance() -> Any:
        from finguard.finance.engine import FinanceEngine

        scenario = os.getenv("FIN_GUARD_DEMO_SCENARIO", "SCENARIO_T0")
        if scenario in {"price_gap", "stale", "unavailable"}:
            engine = FinanceEngine(mode="demo", scenario="SCENARIO_T0")
            # Keep proposal data at T0 while allowing trusted test fixtures to
            # select the approval-time T1 failure scenario.
            engine.approval_scenario = scenario
            return engine
        return FinanceEngine(mode="demo", scenario=scenario)

    def _default_llm(self) -> LLMClient:
        if self.config.api_key:
            return OpenAICompatibleLLMClient(
                base_url=self.config.base_url,
                api_key=self.config.api_key,
                timeout_seconds=self.config.model_timeout_seconds,
            )
        # Offline startup remains useful for demo/CLI smoke checks. This is a
        # mock path and must be labelled as such in any evidence report.
        return FakeLLM([{"role": "assistant", "content": "NO_TRADE"}])

    @staticmethod
    def _schema(
        properties: dict[str, Any],
        required: list[str],
    ) -> dict[str, Any]:
        return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}

    def _register_tools(self) -> None:
        self.registry.register(
            ToolSpec(
                "get_quote",
                "Get the current quote for a supported symbol.",
                self._schema({"symbol": {"type": "string", "minLength": 1}}, ["symbol"]),
                self._tool_get_quote,
                parallel_safe=True,
            )
        )
        self.registry.register(
            ToolSpec(
                "get_company_events",
                "Get provider-backed company events for a supported symbol.",
                self._schema(
                    {
                        "symbol": {"type": "string", "minLength": 1},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                    },
                    ["symbol"],
                ),
                self._tool_get_events,
                parallel_safe=True,
            )
        )
        self.registry.register(
            ToolSpec(
                "get_price_history",
                "Get deterministic price history for indicators.",
                self._schema(
                    {
                        "symbol": {"type": "string", "minLength": 1},
                        "days": {"type": "integer", "minimum": 1, "maximum": 365},
                    },
                    ["symbol"],
                ),
                self._tool_get_history,
                parallel_safe=True,
            )
        )
        self.registry.register(
            ToolSpec(
                "calculate_indicators",
                "Calculate indicators from provider-backed history.",
                self._schema(
                    {
                        "symbol": {"type": "string", "minLength": 1},
                        "window": {"type": "integer", "minimum": 2, "maximum": 365},
                    },
                    ["symbol"],
                ),
                self._tool_calculate_indicators,
                parallel_safe=True,
            )
        )
        self.registry.register(
            ToolSpec(
                "get_portfolio",
                "Read the current simulated paper portfolio.",
                self._schema({}, []),
                self._tool_get_portfolio,
                parallel_safe=True,
            )
        )
        self.registry.register(
            ToolSpec(
                "propose_paper_order",
                "Create a paper-order proposal for explicit human approval; never executes directly.",
                PROPOSAL_SCHEMA,
                self._unreachable_proposal_handler,
                parallel_safe=False,
                requires_approval=True,
            )
        )

    async def _call_provider(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        method = getattr(self.finance, method_name, None)
        if method is None:
            raise RuntimeError(f"finance provider does not implement {method_name}")
        try:
            value = method(*args, **kwargs)
        except TypeError:
            # Keep a narrow compatibility path for providers that bind mode at
            # construction time. It does not alter the frozen DTO semantics.
            kwargs.pop("mode", None)
            kwargs.pop("snapshot", None)
            value = method(*args, **kwargs)
        if inspect.isawaitable(value):
            return await value
        return value

    @staticmethod
    def _provider_error(exc: Exception) -> ToolResult:
        code = getattr(exc, "code", "FAILED_TOOL")
        message = getattr(exc, "message", str(exc))
        retryable = bool(getattr(exc, "retryable", False))
        return ToolResult.failure(str(code), str(message), retryable=retryable)

    async def _tool_get_quote(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            snapshot = "SCENARIO_T0" if ctx.data_mode == "demo" else None
            data = await self._call_provider("get_quote", args["symbol"], mode=ctx.data_mode, snapshot=snapshot)
            return ToolResult.success(data)
        except Exception as exc:
            return self._provider_error(exc)

    async def _tool_get_events(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            data = await self._call_provider(
                "get_company_events", args["symbol"], int(args.get("limit", 5)), mode=ctx.data_mode
            )
            if isinstance(data, list):
                # Older provider DTOs omitted mode; the run context is the
                # authoritative provenance and is safe to add at this boundary.
                data = [
                    {**item, "mode": item.get("mode") or ctx.data_mode}
                    if isinstance(item, dict)
                    else item
                    for item in data
                ]
            return ToolResult.success(data)
        except Exception as exc:
            return self._provider_error(exc)

    async def _tool_get_history(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            data = await self._call_provider(
                "get_price_history", args["symbol"], int(args.get("days", 20)), mode=ctx.data_mode
            )
            return ToolResult.success(data)
        except Exception as exc:
            return self._provider_error(exc)

    async def _tool_calculate_indicators(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            data = await self._call_provider(
                "calculate_indicators", args["symbol"], int(args.get("window", 20)), mode=ctx.data_mode
            )
            return ToolResult.success(data)
        except Exception as exc:
            return self._provider_error(exc)

    async def _tool_get_portfolio(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult.success(self.repository.get_portfolio())

    async def _unreachable_proposal_handler(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult.failure(
            "ORDER_PROPOSAL_REQUIRES_HITL",
            "The harness must handle a solo paper-order proposal through the approval checkpoint.",
        )

    async def _handle_proposal(
        self,
        run_id: str,
        call: ToolCall,
        ctx: ToolContext,
    ) -> ToolResult | None:
        row = self.repository.get_run(run_id)
        if row is None:
            return ToolResult.failure("UNKNOWN_RUN", f"Unknown run_id: {run_id}")
        if not bool(row["allow_paper_order_proposals"]):
            return ToolResult.failure(
                "ORDER_PROPOSALS_DISABLED",
                "This run did not grant permission to propose paper orders.",
            )
        try:
            args = json.loads(call.arguments)
        except (TypeError, json.JSONDecodeError):
            return ToolResult.failure("INVALID_JSON", "Tool arguments are not valid JSON.")
        if not isinstance(args, dict):
            return ToolResult.failure("INVALID_ARGS", "Tool arguments must be a JSON object.")
        schema_error = validate_json_schema(args, PROPOSAL_SCHEMA)
        if schema_error:
            return ToolResult.failure("INVALID_ARGS", schema_error)
        symbol = str(args["symbol"]).strip().upper()
        side = str(args["side"]).upper()
        quantity = int(args["quantity"])
        evidence_ids = [str(item) for item in args["evidence_ids"]]
        evidence_records = self.repository.evidence_records_for_run(run_id)
        missing = sorted(set(evidence_ids) - set(evidence_records))
        if missing:
            return ToolResult.failure(
                "EVIDENCE_NOT_FOUND",
                "Every evidence_id must refer to data returned in this run.",
                details={"missing": missing},
            )
        invalid_provenance = []
        for evidence_id in evidence_ids:
            records = evidence_records.get(evidence_id, [])
            if not any(
                record.get("symbol") == symbol and record.get("data_mode") == ctx.data_mode
                for record in records
            ):
                invalid_provenance.append(evidence_id)
        if invalid_provenance:
            return ToolResult.failure(
                "EVIDENCE_PROVENANCE_MISMATCH",
                "Evidence must belong to the proposed symbol and this run's data mode.",
                details={"invalid": sorted(set(invalid_provenance)), "symbol": symbol, "data_mode": ctx.data_mode},
            )
        try:
            quote = await self._call_provider("get_quote", symbol, mode=ctx.data_mode, snapshot="SCENARIO_T0")
        except Exception as exc:
            return self._provider_error(exc)
        if not isinstance(quote, dict):
            return ToolResult.failure("QUOTE_UNAVAILABLE", "Provider returned an invalid quote payload.")
        if not quote.get("valid", True):
            return ToolResult.failure(
                str(quote.get("error_code") or "QUOTE_UNAVAILABLE"),
                "Proposal requires a valid T0 quote.",
            )
        price = str(quote.get("price"))
        quote_ref = str(quote.get("evidence_id") or quote.get("snapshot_id") or "")
        preliminary = await self._evaluate_risk(
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            quote=quote,
            reference_price=price,
        )
        if not preliminary.get("allowed", False):
            return ToolResult.failure(
                self._risk_code(preliminary),
                "Paper-order proposal failed deterministic risk checks.",
                details=preliminary,
            )
        self.repository.create_proposal(
            proposal_id=str(uuid.uuid4()),
            run_id=run_id,
            tool_call_id=call.call_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            rationale=str(args["rationale"]),
            evidence_ids=evidence_ids,
            reference_price=price,
            reference_quote_ref=quote_ref,
            step=ctx.step,
        )
        return None

    def _portfolio_inputs(self) -> tuple[dict[str, Any], dict[str, int], dict[str, str]]:
        portfolio = self.repository.get_portfolio()
        positions = {str(item["symbol"]): int(item["quantity"]) for item in portfolio.get("positions", [])}
        prices = {
            str(item["symbol"]): str(item.get("average_price", item.get("market_value", "0")))
            for item in portfolio.get("positions", [])
        }
        return portfolio, positions, prices

    @staticmethod
    def _risk_failure(code: str, reason: str) -> dict[str, Any]:
        return {
            "allowed": False,
            "checks": [
                {
                    "rule_id": code,
                    "passed": False,
                    "observed": "unavailable",
                    "limit": "required",
                    "reason": reason,
                }
            ],
            "quote_ref": "",
            "evaluated_at": "",
            "error_code": code,
            "reason": reason,
        }

    @classmethod
    def _normalize_risk(cls, result: Any) -> dict[str, Any]:
        if hasattr(result, "to_dict"):
            result = result.to_dict()
        if not isinstance(result, dict) or not isinstance(result.get("allowed"), bool):
            return cls._risk_failure("RISK_INVALID", "Financial Engine returned an invalid risk result.")
        checks = result.get("checks")
        if not isinstance(checks, list) or not checks:
            return cls._risk_failure("RISK_INVALID", "Financial Engine risk checks are missing or malformed.")
        for check in checks:
            if not isinstance(check, dict):
                return cls._risk_failure("RISK_INVALID", "Financial Engine returned a malformed risk check.")
            if not isinstance(check.get("rule_id"), str) or not check["rule_id"]:
                return cls._risk_failure("RISK_INVALID", "Financial Engine risk check has no rule_id.")
            if not isinstance(check.get("passed"), bool):
                return cls._risk_failure("RISK_INVALID", "Financial Engine risk check has no boolean passed value.")
            for key in ("observed", "limit", "reason"):
                if key not in check:
                    return cls._risk_failure("RISK_INVALID", f"Financial Engine risk check is missing {key}.")
        if not isinstance(result.get("quote_ref", ""), str) or not isinstance(result.get("evaluated_at", ""), str):
            return cls._risk_failure("RISK_INVALID", "Financial Engine risk quote_ref is malformed.")
        if result["allowed"] != all(check["passed"] for check in checks):
            return cls._risk_failure("RISK_INVALID", "Financial Engine risk allowed flag disagrees with its checks.")
        return result

    def _risk_kwargs(
        self,
        *,
        symbol: str,
        side: str,
        quantity: int,
        price: str,
        quote: dict[str, Any],
        reference_price: str,
        portfolio: dict[str, Any],
        positions: dict[str, int],
        prices: dict[str, str],
    ) -> dict[str, Any]:
        return {
            "side": side,
            "symbol": symbol,
            "quantity": quantity,
            "execution_price": price,
            "cash": portfolio["cash"],
            "positions": positions,
            "prices": prices,
            "quote_ref": str(quote.get("evidence_id") or quote.get("snapshot_id") or ""),
            "quote_valid": bool(quote.get("valid", True)),
            "quote_currency": quote.get("currency", "USD"),
            "currency": portfolio.get("currency", "USD"),
            "reference_price": reference_price,
        }

    async def _evaluate_risk(
        self,
        *,
        symbol: str,
        side: str,
        quantity: int,
        price: str,
        quote: dict[str, Any],
        reference_price: str,
    ) -> dict[str, Any]:
        portfolio, positions, prices = self._portfolio_inputs()
        return await self._evaluate_risk_for_state(
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            quote=quote,
            reference_price=reference_price,
            portfolio=portfolio,
            positions=positions,
            prices=prices,
        )

    async def _evaluate_risk_for_state(
        self,
        *,
        symbol: str,
        side: str,
        quantity: int,
        price: str,
        quote: dict[str, Any],
        reference_price: str,
        portfolio: dict[str, Any],
        positions: dict[str, int],
        prices: dict[str, str],
    ) -> dict[str, Any]:
        method = getattr(self.finance, "preview_risk", None)
        kwargs = self._risk_kwargs(
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            quote=quote,
            reference_price=reference_price,
            portfolio=portfolio,
            positions=positions,
            prices=prices,
        )
        if method is None:
            try:
                from finguard.finance.risk import evaluate_order_risk

                result = evaluate_order_risk(**kwargs)
            except Exception as exc:
                return self._risk_failure("RISK_UNAVAILABLE", f"Financial Engine risk evaluation failed: {exc}")
        else:
            try:
                result = method(**kwargs)
                if inspect.isawaitable(result):
                    result = await result
            except Exception as exc:
                return self._risk_failure("RISK_UNAVAILABLE", f"Financial Engine risk evaluation failed: {exc}")
        return self._normalize_risk(result)

    @staticmethod
    def _risk_code(risk: dict[str, Any]) -> str:
        if risk.get("error_code"):
            return str(risk["error_code"])
        for check in risk.get("checks", []):
            if isinstance(check, dict) and not check.get("passed"):
                rule = str(check.get("rule_id", "RISK_REJECTED"))
                if rule == "PRICE_GAP":
                    return "PRICE_GAP_EXCEEDED"
                return rule
        return "RISK_REJECTED"

    async def _approval_execution(self, proposal: dict[str, Any], data_mode: str) -> dict[str, Any]:
        snapshot = self._trusted_t1_snapshot() if data_mode == "demo" else None
        try:
            quote = await self._call_provider(
                "get_quote", proposal["symbol"], mode=data_mode, snapshot=snapshot
            )
        except Exception as exc:
            return {"status": "BLOCKED", "blocked_code": getattr(exc, "code", "QUOTE_UNAVAILABLE"), "reason": str(exc)}
        if not isinstance(quote, dict) or not quote.get("valid", True):
            return {
                "status": "BLOCKED",
                "blocked_code": str((quote or {}).get("error_code", "QUOTE_UNAVAILABLE")),
                "reason": "Approval requires a valid fresh quote.",
            }
        reference = proposal["reference_price"]
        risk = await self._evaluate_risk(
            symbol=proposal["symbol"],
            side=proposal["side"],
            quantity=proposal["quantity"],
            price=str(quote.get("price")),
            quote=quote,
            reference_price=reference,
        )
        if not risk.get("allowed", False):
            return {
                "status": "BLOCKED",
                "blocked_code": self._risk_code(risk),
                "reason": "Approval-time deterministic risk check failed.",
                "details": risk,
                "execution_price": str(quote.get("price")),
                "quote_ref": str(quote.get("evidence_id") or quote.get("snapshot_id") or ""),
            }
        try:
            price = Decimal(str(quote.get("price")))
        except (TypeError, ValueError, ArithmeticError) as exc:
            return {
                "status": "BLOCKED",
                "blocked_code": "QUOTE_UNAVAILABLE",
                "reason": f"Approval quote price is invalid: {exc}",
            }
        quantity = int(proposal["quantity"])
        side = proposal["side"]
        delta_method = getattr(self.finance, "calculate_order_delta", None)
        if delta_method is None:
            return {
                "status": "BLOCKED",
                "blocked_code": "DELTA_UNAVAILABLE",
                "reason": "Financial Engine did not provide an order delta.",
                "execution_price": str(quote.get("price")),
                "quote_ref": str(quote.get("evidence_id") or quote.get("snapshot_id") or ""),
            }
        portfolio, positions, prices = self._portfolio_inputs()
        try:
            delta = delta_method(
                side=side,
                symbol=proposal["symbol"],
                quantity=quantity,
                execution_price=str(price),
                cash=portfolio["cash"],
                positions=positions,
                prices=prices,
                quote_ref=str(quote.get("evidence_id") or quote.get("snapshot_id") or ""),
                quote_valid=bool(quote.get("valid", True)),
                quote_currency=quote.get("currency", "USD"),
                currency=portfolio.get("currency", "USD"),
                reference_price=proposal["reference_price"],
            )
            if inspect.isawaitable(delta):
                delta = await delta
        except Exception as exc:
            return {
                "status": "BLOCKED",
                "blocked_code": "DELTA_UNAVAILABLE",
                "reason": f"Financial Engine order delta failed: {exc}",
                "execution_price": str(quote.get("price")),
                "quote_ref": str(quote.get("evidence_id") or quote.get("snapshot_id") or ""),
            }
        if hasattr(delta, "to_dict"):
            delta = delta.to_dict()
        if not isinstance(delta, dict):
            return {
                "status": "BLOCKED",
                "blocked_code": "DELTA_INVALID",
                "reason": "Financial Engine returned an invalid order delta.",
                "execution_price": str(quote.get("price")),
                "quote_ref": str(quote.get("evidence_id") or quote.get("snapshot_id") or ""),
            }
        delta_risk = self._normalize_risk(delta.get("risk"))
        if not delta_risk.get("allowed", False):
            return {
                "status": "BLOCKED",
                "blocked_code": self._risk_code(delta_risk),
                "reason": "Financial Engine order delta risk check failed.",
                "details": delta_risk,
                "execution_price": str(quote.get("price")),
                "quote_ref": str(quote.get("evidence_id") or quote.get("snapshot_id") or ""),
            }
        try:
            delta_price = Decimal(str(delta["execution_price"]))
            delta_cash = Decimal(str(delta["cash_delta"]))
            delta_quantity = int(delta["quantity_delta"])
            expected_quantity = quantity if side == "BUY" else -quantity
            expected_cash = delta_price * quantity * (Decimal("1") if side == "SELL" else Decimal("-1"))
            if delta_price != price or delta_quantity != expected_quantity or delta_cash != expected_cash or delta_price <= 0:
                raise ValueError("delta values do not match the approved order")
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            return {
                "status": "BLOCKED",
                "blocked_code": "DELTA_INVALID",
                "reason": f"Financial Engine returned an invalid order delta: {exc}",
                "execution_price": str(quote.get("price")),
                "quote_ref": str(quote.get("evidence_id") or quote.get("snapshot_id") or ""),
            }
        return {
            "status": "EXECUTED",
            "execution_price": str(delta_price),
            "cash_delta": str(delta_cash),
            "quantity_delta": delta_quantity,
            "quote_ref": str(quote.get("evidence_id") or quote.get("snapshot_id") or ""),
            "_risk_context": {"quote": quote, "reference_price": proposal["reference_price"]},
        }

    def _revalidate_execution_locked(
        self,
        proposal: Any,
        execution: dict[str, Any],
        account_state: dict[str, Any],
    ) -> dict[str, Any]:
        """Re-run pure Financial Engine risk against the transaction snapshot."""
        context = execution.get("_risk_context")
        if not isinstance(context, dict) or not isinstance(context.get("quote"), dict):
            return self._risk_failure("RISK_INVALID", "Execution is missing its quote risk context.")
        quote = context["quote"]
        positions = {
            str(item["symbol"]): int(item["quantity"])
            for item in account_state.get("positions", [])
            if isinstance(item, dict) and "symbol" in item and "quantity" in item
        }
        prices = {
            str(item["symbol"]): str(item.get("average_price", "0"))
            for item in account_state.get("positions", [])
            if isinstance(item, dict) and "symbol" in item
        }
        method = getattr(self.finance, "preview_risk", None)
        kwargs = self._risk_kwargs(
            symbol=str(proposal["symbol"]),
            side=str(proposal["side"]),
            quantity=int(proposal["quantity"]),
            price=str(execution.get("execution_price")),
            quote=quote,
            reference_price=str(context.get("reference_price", proposal["reference_price"])),
            portfolio=account_state,
            positions=positions,
            prices=prices,
        )
        try:
            if method is None:
                from finguard.finance.risk import evaluate_order_risk

                result = evaluate_order_risk(**kwargs)
            else:
                result = method(**kwargs)
            if inspect.isawaitable(result):
                close = getattr(result, "close", None)
                if callable(close):
                    close()
                return self._risk_failure(
                    "RISK_UNAVAILABLE", "Transactional risk revalidation must be synchronous."
                )
        except Exception as exc:
            return self._risk_failure("RISK_UNAVAILABLE", f"Financial Engine risk evaluation failed: {exc}")
        return self._normalize_risk(result)

    def _trusted_t1_snapshot(self) -> str:
        """Select the execution fixture from service configuration only."""
        configured = os.getenv("FIN_GUARD_DEMO_T1_SCENARIO")
        provider = getattr(self.finance, "_provider", None)
        provider_scenario = getattr(provider, "scenario", None) or getattr(self.finance, "scenario", None)
        candidate = configured or getattr(self.finance, "approval_scenario", None)
        if candidate is None and provider_scenario in {"price_gap", "stale", "unavailable"}:
            candidate = provider_scenario
        if candidate in {"price_gap", "stale", "unavailable"}:
            return str(candidate)
        return "SCENARIO_T1"

    async def start_task(
        self,
        prompt: str,
        data_mode: Literal["demo", "live"] = "demo",
        allow_paper_order_proposals: bool = False,
    ) -> TaskView:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be a non-empty string")
        if data_mode not in {"demo", "live"}:
            raise ValueError("data_mode must be demo or live")
        run_id = self.repository.create_run(
            prompt=prompt.strip(),
            data_mode=data_mode,
            allow_paper_order_proposals=allow_paper_order_proposals,
            model_name=self.config.model,
            system_prompt=SYSTEM_PROMPT,
        )
        return await self.loop.run(run_id)

    async def get_task(self, run_id: str) -> TaskView:
        return self.repository.get_task_view(run_id)

    async def get_events(self, run_id: str, after_seq: int = 0) -> list[Any]:
        return self.repository.get_events(run_id, after_seq)

    async def decide_proposal(
        self,
        run_id: str,
        proposal_id: str,
        decision: Literal["approve", "reject"],
        expected_version: int | None = None,
    ) -> DecisionView:
        existing = self.repository.get_decision(run_id, proposal_id)
        if existing is not None:
            return existing
        row = self.repository.get_run(run_id)
        if row is None:
            raise KeyError(f"unknown run_id: {run_id}")
        if expected_version is not None and int(row["version"]) != expected_version:
            return DecisionView(
                run_id=run_id,
                proposal_id=proposal_id,
                decision=decision,
                proposal_status="PENDING",
                order_status=None,
                task_status=row["status"],
                receipt=None,
                version=int(row["version"]),
                error={
                    "code": "VERSION_CONFLICT",
                    "message": f"expected version {expected_version}, current {row['version']}",
                    "retryable": False,
                },
            )
        proposal = self.repository.get_proposal(proposal_id)
        if proposal is None or proposal["run_id"] != run_id:
            raise KeyError(f"unknown proposal: {proposal_id}")
        execution = None if decision == "reject" else await self._approval_execution(proposal, row["data_mode"])
        view = self.repository.finalize_proposal(
            run_id=run_id,
            proposal_id=proposal_id,
            decision=decision,
            expected_version=expected_version,
            execution=execution,
            risk_revalidator=self._revalidate_execution_locked,
        )
        if view.error:
            return view
        await self.loop.run(run_id)
        return self.repository.get_decision(run_id, proposal_id) or view

    async def resume_task(self, run_id: str) -> TaskView:
        return await self.loop.run(run_id)

    async def get_portfolio(self) -> dict[str, Any]:
        return self.repository.get_portfolio()

    def close(self) -> None:
        self.repository.close()


def create_application_service() -> ApplicationService:
    return ApplicationService()
