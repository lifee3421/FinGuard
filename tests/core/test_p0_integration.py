from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

from finguard.core.application import ApplicationService
from finguard.core.config import CoreConfig
from finguard.core.llm_client import FakeLLM
from finguard.core.storage import SQLiteRepository
from finguard.finance.engine import FinanceEngine


def _proposal_llm(
    symbol: str = "AAPL", quantity: int = 1, evidence: str | None = None, suffix: str = "1"
) -> FakeLLM:
    evidence = evidence or f"demo:scenario_t0:{symbol}:quote"
    return FakeLLM(
        [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": f"quote-{suffix}",
                        "type": "function",
                        "function": {"name": "get_quote", "arguments": f'{{"symbol":"{symbol}"}}'},
                    }
                ],
            },
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": f"proposal-{suffix}",
                        "type": "function",
                        "function": {
                            "name": "propose_paper_order",
                            "arguments": (
                                f'{{"symbol":"{symbol}","side":"BUY","quantity":{quantity},'
                                f'"rationale":"test","evidence_ids":["{evidence}"]}}'
                            ),
                        },
                    }
                ],
            },
        ]
    )


def _service(db: str, llm: FakeLLM, finance: Any | None = None) -> ApplicationService:
    return ApplicationService(
        repository=SQLiteRepository(db),
        llm=llm,
        finance=finance or FinanceEngine(),
        config=CoreConfig(model="mock", max_steps=8, max_tool_calls=10),
    )


class _RiskFailureFinance:
    def __init__(self, mode: str = "raise") -> None:
        self.base = FinanceEngine()
        self.mode = mode

    async def get_quote(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return await self.base.get_quote(*args, **kwargs)

    def preview_risk(self, **kwargs: Any) -> Any:
        if self.mode == "raise":
            raise RuntimeError("injected risk failure")
        if self.mode == "missing":
            return None
        return {"allowed": True}


class _DeltaFailureFinance(_RiskFailureFinance):
    def preview_risk(self, **kwargs: Any) -> Any:
        from finguard.finance.risk import evaluate_order_risk

        return evaluate_order_risk(**kwargs)

    def calculate_order_delta(self, **kwargs: Any) -> Any:
        raise RuntimeError("injected delta failure")


class _NoDeltaFinance(_RiskFailureFinance):
    def preview_risk(self, **kwargs: Any) -> Any:
        from finguard.finance.risk import evaluate_order_risk

        return evaluate_order_risk(**kwargs)


class _WrongModeEventsFinance(FinanceEngine):
    async def get_company_events(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        events = await super().get_company_events(*args, **kwargs)
        return [{**event, "mode": "live"} for event in events]


def test_risk_exception_and_missing_result_fail_closed_before_pending_proposal() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory() as directory:
            for mode, quantity in (("raise", 200), ("missing", 1)):
                service = _service(
                    str(Path(directory) / f"{mode}.sqlite3"),
                    _proposal_llm(quantity=quantity),
                    _RiskFailureFinance(mode),
                )
                task = await service.start_task("propose", allow_paper_order_proposals=True)
                assert task.pending_proposal is None
                assert (await service.get_portfolio())["positions"] == []
                service.close()

    asyncio.run(run())


def test_delta_exception_and_missing_delta_fail_closed_at_approval() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory() as directory:
            for name, finance in (("raise", _DeltaFailureFinance()), ("missing", _NoDeltaFinance())):
                db = str(Path(directory) / f"{name}.sqlite3")
                service = _service(db, _proposal_llm(), finance)
                pending = await service.start_task("propose", allow_paper_order_proposals=True)
                assert pending.pending_proposal is not None
                decision = await service.decide_proposal(
                    pending.run_id,
                    pending.pending_proposal["proposal_id"],
                    "approve",
                    pending.version,
                )
                assert decision.proposal_status == "BLOCKED"
                assert decision.order_status == "BLOCKED"
                assert decision.receipt["error"]["code"] in {"DELTA_UNAVAILABLE", "DELTA_INVALID"}
                assert (await service.get_portfolio())["positions"] == []
                service.close()

    asyncio.run(run())


def test_two_runs_cannot_commit_buys_that_exceed_single_symbol_weight() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory) / "shared.sqlite3")
            first = _service(db, _proposal_llm(quantity=60, suffix="1"))
            second = _service(db, _proposal_llm(quantity=60, suffix="2"))
            one = await first.start_task("propose", allow_paper_order_proposals=True)
            two = await second.start_task("propose", allow_paper_order_proposals=True)
            assert one.pending_proposal and two.pending_proposal

            decisions = await asyncio.gather(
                first.decide_proposal(one.run_id, one.pending_proposal["proposal_id"], "approve", one.version),
                second.decide_proposal(two.run_id, two.pending_proposal["proposal_id"], "approve", two.version),
            )
            assert sorted(item.proposal_status for item in decisions) == ["APPROVED", "BLOCKED"]
            portfolio = await first.get_portfolio()
            position = next(item for item in portfolio["positions"] if item["symbol"] == "AAPL")
            assert position["quantity"] == 60
            assert float(position["market_value"]) / float(portfolio["equity"]) <= 0.20
            first.close()
            second.close()

    asyncio.run(run())


def test_company_event_evidence_is_provenance_checked() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory() as directory:
            # The event tool result is a list; its first event is a legal AAPL
            # evidence reference for the same demo run.
            valid_llm = FakeLLM(
                [
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "events-1",
                                "type": "function",
                                "function": {"name": "get_company_events", "arguments": '{"symbol":"AAPL"}'},
                            }
                        ],
                    },
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "proposal-1",
                                "type": "function",
                                "function": {
                                    "name": "propose_paper_order",
                                    "arguments": (
                                        '{"symbol":"AAPL","side":"BUY","quantity":1,"rationale":"event",'
                                        '"evidence_ids":["demo:event:AAPL:earnings"]}'
                                    ),
                                },
                            }
                        ],
                    },
                ]
            )
            service = _service(str(Path(directory) / "valid.sqlite3"), valid_llm)
            valid = await service.start_task("propose", allow_paper_order_proposals=True)
            assert valid.pending_proposal is not None
            service.close()

            # Same-run event evidence cannot be reused for another symbol.
            symbol_llm = FakeLLM(
                [
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "events-symbol",
                                "type": "function",
                                "function": {"name": "get_company_events", "arguments": '{"symbol":"AAPL"}'},
                            }
                        ],
                    },
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "proposal-symbol",
                                "type": "function",
                                "function": {
                                    "name": "propose_paper_order",
                                    "arguments": (
                                        '{"symbol":"MSFT","side":"BUY","quantity":1,"rationale":"wrong",'
                                        '"evidence_ids":["demo:event:AAPL:earnings"]}'
                                    ),
                                },
                            }
                        ],
                    },
                ]
            )
            service = _service(str(Path(directory) / "symbol.sqlite3"), symbol_llm)
            result = await service.start_task("propose", allow_paper_order_proposals=True)
            assert result.pending_proposal is None
            service.close()

            # An evidence ID from another run is not visible in this run.
            source_llm = FakeLLM(
                [
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "events-source",
                                "type": "function",
                                "function": {"name": "get_company_events", "arguments": '{"symbol":"AAPL"}'},
                            }
                        ],
                    },
                    {"role": "assistant", "content": "NO_TRADE"},
                ]
            )
            db = str(Path(directory) / "cross.sqlite3")
            source = _service(db, source_llm)
            await source.start_task("research")
            cross_run_llm = _proposal_llm(evidence="demo:event:AAPL:earnings", suffix="cross")
            service = _service(db, cross_run_llm)
            result = await service.start_task("propose", allow_paper_order_proposals=True)
            assert result.pending_proposal is None
            source.close()
            service.close()

            # A provider result tagged with another data mode is rejected.
            mode_llm = FakeLLM(
                [
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "events-mode",
                                "type": "function",
                                "function": {"name": "get_company_events", "arguments": '{"symbol":"AAPL"}'},
                            }
                        ],
                    },
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "proposal-mode",
                                "type": "function",
                                "function": {
                                    "name": "propose_paper_order",
                                    "arguments": (
                                        '{"symbol":"AAPL","side":"BUY","quantity":1,"rationale":"wrong mode",'
                                        '"evidence_ids":["demo:event:AAPL:earnings"]}'
                                    ),
                                },
                            }
                        ],
                    },
                ]
            )
            service = _service(
                str(Path(directory) / "mode.sqlite3"), mode_llm, _WrongModeEventsFinance()
            )
            result = await service.start_task("propose", allow_paper_order_proposals=True)
            assert result.pending_proposal is None
            service.close()

            for label, symbol, evidence in (("unknown", "AAPL", "demo:event:AAPL:unknown"),):
                service = _service(str(Path(directory) / f"{label}.sqlite3"), _proposal_llm(symbol, evidence=evidence))
                result = await service.start_task("propose", allow_paper_order_proposals=True)
                assert result.pending_proposal is None
                service.close()

    asyncio.run(run())
