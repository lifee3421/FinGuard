from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from finguard.core.application import ApplicationService
from finguard.core.config import CoreConfig
from finguard.core.llm_client import FakeLLM
from finguard.core.storage import SQLiteRepository
from finguard.finance.providers.demo import DemoMarketDataProvider


def _service(path: str, llm: FakeLLM) -> ApplicationService:
    return ApplicationService(
        repository=SQLiteRepository(path),
        llm=llm,
        config=CoreConfig(model="mock", max_steps=8, max_tool_calls=10),
    )


def test_start_task_runs_model_and_tool_until_completion() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = _service(
                str(Path(directory) / "state.sqlite3"),
                FakeLLM(
                    [
                        {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": "quote-1",
                                    "type": "function",
                                    "function": {"name": "get_quote", "arguments": '{"symbol":"AAPL"}'},
                                }
                            ],
                        },
                        {"role": "assistant", "content": "NO_TRADE"},
                    ]
                ),
            )
            task = await service.start_task("Research AAPL only")
            assert task.status == "COMPLETED"
            assert task.final_answer == "NO_TRADE"
            assert any(event.event_type == "TOOL_RESULT" for event in await service.get_events(task.run_id))
            service.close()

    asyncio.run(run())


def test_proposal_pauses_then_approval_is_restartable_and_idempotent() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory) / "state.sqlite3")
            service = _service(
                db,
                FakeLLM(
                    [
                        {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": "quote-1",
                                    "type": "function",
                                    "function": {"name": "get_quote", "arguments": '{"symbol":"AAPL"}'},
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
                                        "arguments": '{"symbol":"AAPL","side":"BUY","quantity":1,"rationale":"test","evidence_ids":["demo:scenario_t0:AAPL:quote"]}',
                                    },
                                }
                            ],
                        },
                    ]
                ),
            )
            pending = await service.start_task("Propose one paper order", allow_paper_order_proposals=True)
            assert pending.status == "WAITING_APPROVAL"
            proposal_id = pending.pending_proposal["proposal_id"]
            expected_version = pending.version
            service.close()

            restarted = _service(db, FakeLLM([{"role": "assistant", "content": "Done."}]))
            decision = await restarted.decide_proposal(
                pending.run_id, proposal_id, "approve", expected_version=expected_version
            )
            assert decision.proposal_status == "APPROVED"
            assert decision.order_status == "EXECUTED"
            assert (await restarted.get_task(pending.run_id)).status == "COMPLETED"
            repeated = await restarted.decide_proposal(pending.run_id, proposal_id, "approve", expected_version=0)
            assert repeated.proposal_status == "APPROVED"
            assert repeated.order_status == "EXECUTED"
            assert repeated.error is None
            assert (await restarted.get_portfolio())["positions"][0]["quantity"] == 1
            restarted.close()

    asyncio.run(run())


def test_stale_version_is_rejected_before_approval_quote() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = _service(
                str(Path(directory) / "state.sqlite3"),
                FakeLLM(
                    [
                        {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": "quote-1",
                                    "type": "function",
                                    "function": {"name": "get_quote", "arguments": '{"symbol":"AAPL"}'},
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
                                        "arguments": '{"symbol":"AAPL","side":"BUY","quantity":1,"rationale":"test","evidence_ids":["demo:scenario_t0:AAPL:quote"]}',
                                    },
                                }
                            ],
                        },
                    ]
                ),
            )
            pending = await service.start_task("Propose", allow_paper_order_proposals=True)
            result = await service.decide_proposal(
                pending.run_id,
                pending.pending_proposal["proposal_id"],
                "approve",
                expected_version=0,
            )
            assert result.error["code"] == "VERSION_CONFLICT"
            assert result.proposal_status == "PENDING"
            service.close()

    asyncio.run(run())


def test_approval_t1_price_gap_is_blocked_without_ledger_change() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory) / "state.sqlite3")
            service = ApplicationService(
                repository=SQLiteRepository(db),
                finance=DemoMarketDataProvider(scenario="price_gap"),
                llm=FakeLLM(
                    [
                        {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": "quote-1",
                                    "type": "function",
                                    "function": {"name": "get_quote", "arguments": '{"symbol":"AAPL"}'},
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
                                        "arguments": '{"symbol":"AAPL","side":"BUY","quantity":1,"rationale":"test","evidence_ids":["demo:scenario_t0:AAPL:quote"]}',
                                    },
                                }
                            ],
                        },
                    ]
                ),
                config=CoreConfig(model="mock", max_steps=8),
            )
            pending = await service.start_task("Propose", allow_paper_order_proposals=True)
            proposal_id = pending.pending_proposal["proposal_id"]
            decision = await service.decide_proposal(pending.run_id, proposal_id, "approve", pending.version)
            assert decision.proposal_status == "BLOCKED"
            assert decision.order_status == "BLOCKED"
            assert decision.receipt["error"]["code"] == "PRICE_GAP_EXCEEDED"
            assert (await service.get_portfolio())["positions"] == []
            service.close()

    asyncio.run(run())
