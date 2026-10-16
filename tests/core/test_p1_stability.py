from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from finguard.core.agent import AgentLoop
from finguard.core.application import ApplicationService
from finguard.core.config import CoreConfig
from finguard.core.dispatcher import ToolDispatcher, ToolRegistry
from finguard.core.llm_client import FakeLLM
from finguard.core.models import ToolResult
from finguard.core.storage import SQLiteRepository
from finguard.finance.engine import FinanceEngine


def _proposal_llm() -> FakeLLM:
    return FakeLLM(
        [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "same-call-id",
                        "type": "function",
                        "function": {"name": "get_quote", "arguments": '{"symbol":"AAPL"}'},
                    }
                ],
            },
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "same-proposal-id",
                        "type": "function",
                        "function": {
                            "name": "propose_paper_order",
                            "arguments": (
                                '{"symbol":"AAPL","side":"BUY","quantity":1,"rationale":"test",'
                                '"evidence_ids":["demo:scenario_t0:AAPL:quote"]}'
                            ),
                        },
                    }
                ],
            },
        ]
    )


def test_same_proposal_tool_call_id_is_scoped_to_run() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory) / "shared.sqlite3")
            first = ApplicationService(
                repository=SQLiteRepository(db),
                finance=FinanceEngine(),
                llm=_proposal_llm(),
                config=CoreConfig(model="mock"),
            )
            second = ApplicationService(
                repository=SQLiteRepository(db),
                finance=FinanceEngine(),
                llm=_proposal_llm(),
                config=CoreConfig(model="mock"),
            )
            one = await first.start_task("propose", allow_paper_order_proposals=True)
            two = await second.start_task("propose", allow_paper_order_proposals=True)
            assert one.pending_proposal is not None
            assert two.pending_proposal is not None
            assert one.pending_proposal["original_tool_call_id"] == two.pending_proposal["original_tool_call_id"]
            first.close()
            second.close()

    asyncio.run(run())


def test_legacy_global_proposal_index_is_migrated() -> None:
    with tempfile.TemporaryDirectory() as directory:
        db = str(Path(directory) / "legacy.sqlite3")
        connection = sqlite3.connect(db)
        connection.executescript(
            """
            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY, prompt TEXT NOT NULL, status TEXT NOT NULL,
                data_mode TEXT NOT NULL, allow_paper_order_proposals INTEGER NOT NULL,
                step INTEGER NOT NULL, version INTEGER NOT NULL, model_name TEXT,
                final_answer TEXT, error_json TEXT, pending_proposal_id TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE proposals (
                proposal_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
                original_tool_call_id TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
                symbol TEXT NOT NULL, side TEXT NOT NULL, quantity INTEGER NOT NULL,
                rationale TEXT NOT NULL, evidence_ids_json TEXT NOT NULL,
                reference_price TEXT NOT NULL, reference_quote_ref TEXT NOT NULL,
                created_at TEXT NOT NULL, decided_at TEXT, decision TEXT,
                decision_reason TEXT, version INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        connection.commit()
        connection.close()
        repository = SQLiteRepository(db)
        first = repository.create_run(
            prompt="one", data_mode="demo", allow_paper_order_proposals=True,
            model_name="mock", system_prompt="system"
        )
        second = repository.create_run(
            prompt="two", data_mode="demo", allow_paper_order_proposals=True,
            model_name="mock", system_prompt="system"
        )
        for run_id, proposal_id in ((first, "proposal-one"), (second, "proposal-two")):
            repository.create_proposal(
                proposal_id=proposal_id, run_id=run_id, tool_call_id="same-id",
                symbol="AAPL", side="BUY", quantity=1, rationale="test",
                evidence_ids=[], reference_price="190", reference_quote_ref="quote", step=1
            )
        assert repository.get_proposal("proposal-one") is not None
        assert repository.get_proposal("proposal-two") is not None
        repository.close()


def test_resume_replays_only_missing_tool_receipts() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = SQLiteRepository(str(Path(directory) / "state.sqlite3"))
            run_id = repository.create_run(
                prompt="recover",
                data_mode="demo",
                allow_paper_order_proposals=False,
                model_name="mock",
                system_prompt="system",
            )
            repository.append_assistant_and_audit(
                run_id,
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "already-receipted",
                            "type": "function",
                            "function": {"name": "get_quote", "arguments": '{"symbol":"AAPL"}'},
                        },
                        {
                            "id": "missing-receipt",
                            "type": "function",
                            "function": {"name": "get_quote", "arguments": '{"symbol":"MSFT"}'},
                        },
                    ],
                },
                step=1,
                mode="demo",
                model="mock",
            )
            existing = ToolResult.success(
                {
                    "symbol": "AAPL",
                    "price": "190.00",
                    "currency": "USD",
                    "mode": "demo",
                    "evidence_id": "manual:aapl",
                }
            ).to_dict()
            repository.append_tool_messages(
                run_id,
                [
                    {
                        "role": "tool",
                        "tool_call_id": "already-receipted",
                        "name": "get_quote",
                        "content": '{"ok":true,"data":{"symbol":"AAPL"},"error":null}',
                    }
                ],
                step=1,
                mode="demo",
                model="mock",
                results=[
                    {
                        "tool_call_id": "already-receipted",
                        "name": "get_quote",
                        "result": existing,
                        "elapsed_ms": 0.0,
                    }
                ],
            )
            service = ApplicationService(
                repository=repository,
                finance=FinanceEngine(),
                llm=FakeLLM([{"role": "assistant", "content": "recovered"}]),
                config=CoreConfig(model="mock"),
            )
            task = await service.resume_task(run_id)
            assert task.status == "COMPLETED"
            messages = repository.get_messages(run_id)
            receipts = [message for message in messages if message.get("role") == "tool"]
            assert [message["tool_call_id"] for message in receipts].count("already-receipted") == 1
            assert [message["tool_call_id"] for message in receipts].count("missing-receipt") == 1
            service.close()

    asyncio.run(run())


def test_long_run_renews_lease_and_blocks_second_executor() -> None:
    class SlowLLM:
        async def complete(self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]], model: str) -> dict[str, Any]:
            await asyncio.sleep(0.15)
            return {"role": "assistant", "content": "done"}

    async def run() -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory) / "state.sqlite3")
            repository = SQLiteRepository(db)
            service = ApplicationService(
                repository=repository,
                finance=FinanceEngine(),
                llm=SlowLLM(),
                config=CoreConfig(model="slow", model_timeout_seconds=1.0, run_lease_ttl_seconds=0.03),
            )
            run_id = repository.create_run(
                prompt="slow",
                data_mode="demo",
                allow_paper_order_proposals=False,
                model_name="slow",
                system_prompt="system",
            )
            running = asyncio.create_task(service.resume_task(run_id))
            await asyncio.sleep(0.09)
            assert repository.try_acquire_run_lease(run_id, "second-executor", ttl_seconds=0.03) is False
            task = await running
            assert task.status == "COMPLETED"
            service.close()

    asyncio.run(run())


def test_config_loads_dotenv_without_overriding_explicit_environment(tmp_path: Path, monkeypatch: Any) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        'LLM_API_KEY="fixture-value"\nLLM_BASE_URL=https://file.example/v1\nLLM_MODEL=file-model\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.setenv("LLM_MODEL", "explicit-model")
    monkeypatch.setenv("FIN_GUARD_ENV_FILE", str(env_file))
    try:
        config = CoreConfig.from_env()
        assert config.api_key == "fixture-value"
        assert config.base_url == "https://file.example/v1"
        assert config.model == "explicit-model"
    finally:
        os.environ.pop("LLM_API_KEY", None)
        os.environ.pop("LLM_BASE_URL", None)
