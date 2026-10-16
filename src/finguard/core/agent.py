from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from .config import CoreConfig
from .dispatcher import ToolDispatcher
from .llm_client import LLMClient, normalize_assistant
from .models import TaskView, ToolCall, ToolContext, ToolResult
from .storage import SQLiteRepository


ProposalHandler = Callable[[str, ToolCall, ToolContext], Awaitable[ToolResult | None]]


class AgentLoop:
    """The application-owned model -> tools -> model control loop."""

    def __init__(
        self,
        *,
        repository: SQLiteRepository,
        dispatcher: ToolDispatcher,
        llm: LLMClient,
        config: CoreConfig,
        proposal_handler: ProposalHandler,
    ) -> None:
        self.repository = repository
        self.dispatcher = dispatcher
        self.llm = llm
        self.config = config
        self.proposal_handler = proposal_handler
        self._run_locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, run_id: str) -> asyncio.Lock:
        lock = self._run_locks.get(run_id)
        if lock is None:
            lock = asyncio.Lock()
            self._run_locks[run_id] = lock
        return lock

    async def _lease_heartbeat(
        self,
        run_id: str,
        owner_id: str,
        ttl_seconds: float,
        stop: asyncio.Event,
        lost: asyncio.Event,
    ) -> None:
        interval = max(0.01, ttl_seconds / 3.0)
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except asyncio.TimeoutError:
                pass
            if not self.repository.renew_run_lease(run_id, owner_id, ttl_seconds=ttl_seconds):
                lost.set()
                return

    async def run(self, run_id: str) -> TaskView:
        lock = self._lock_for(run_id)
        if lock.locked():
            return self.repository.get_task_view(run_id)
        async with lock:
            owner_id = f"{id(asyncio.current_task())}:{uuid.uuid4()}"
            lease_ttl = max(0.01, self.config.run_lease_ttl_seconds)
            if not self.repository.try_acquire_run_lease(run_id, owner_id, ttl_seconds=lease_ttl):
                return self.repository.get_task_view(run_id)
            heartbeat_stop = asyncio.Event()
            lease_lost = asyncio.Event()
            heartbeat = asyncio.create_task(
                self._lease_heartbeat(run_id, owner_id, lease_ttl, heartbeat_stop, lease_lost)
            )
            try:
                return await self._run_locked(run_id, lease_lost)
            finally:
                heartbeat_stop.set()
                await heartbeat
                self.repository.release_run_lease(run_id, owner_id)

    @staticmethod
    def _missing_tool_calls(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
        assistant = next(
            (message for message in reversed(messages) if message.get("role") == "assistant"),
            None,
        )
        if not assistant or not assistant.get("tool_calls"):
            return []
        receipt_ids = {
            str(message.get("tool_call_id"))
            for message in messages
            if message.get("role") == "tool" and message.get("tool_call_id")
        }
        pending: list[dict[str, str]] = []
        for item in assistant.get("tool_calls") or []:
            function = item.get("function") or {}
            call_id = str(item.get("id") or "")
            if call_id and call_id not in receipt_ids:
                pending.append(
                    {
                        "call_id": call_id,
                        "name": str(function.get("name") or ""),
                        "arguments": function.get("arguments", ""),
                    }
                )
        return pending

    async def _recover_missing_tool_receipts(
        self,
        run_id: str,
        row: Any,
        step: int,
        calls: list[dict[str, str]],
    ) -> bool:
        """Replay only calls whose durable tool receipt is missing."""
        proposal_calls = [call for call in calls if call["name"] == "propose_paper_order"]
        if len(calls) == 1 and len(proposal_calls) == 1:
            call = proposal_calls[0]
            existing = self.repository.get_proposal_by_tool_call_id(run_id, call["call_id"])
            if existing and existing["status"] == "PENDING":
                return False
            if existing:
                decision = self.repository.get_decision(run_id, existing["proposal_id"])
                if decision and decision.receipt is not None:
                    result_dict = decision.receipt
                else:
                    return False
            else:
                try:
                    result = await self.proposal_handler(
                        run_id,
                        ToolCall(call["call_id"], call["name"], call["arguments"]),
                        ToolContext(run_id, row["data_mode"], step),
                    )
                except Exception as exc:
                    result = ToolResult.failure(
                        "FAILED_TOOL", f"Proposal handler failed: {type(exc).__name__}: {exc}", retryable=False
                    )
                if result is None:
                    return False
                result_dict = result.to_dict()
            self.repository.append_tool_messages(
                run_id,
                [
                    {
                        "role": "tool",
                        "tool_call_id": call["call_id"],
                        "name": call["name"],
                        "content": json.dumps(result_dict, ensure_ascii=False, separators=(",", ":")),
                    }
                ],
                step=step,
                mode=row["data_mode"],
                model=row["model_name"],
                results=[
                    {
                        "tool_call_id": call["call_id"],
                        "name": call["name"],
                        "result": result_dict,
                        "elapsed_ms": 0.0,
                    }
                ],
            )
            return True

        context = ToolContext(run_id, row["data_mode"], step)
        results = await self.dispatcher.dispatch_batch(calls, context)
        tool_messages: list[dict[str, Any]] = []
        result_rows: list[dict[str, Any]] = []
        for item in results:
            result: ToolResult = item["result"]
            result_dict = result.to_dict()
            tool_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": item["tool_call_id"],
                    "name": item["name"],
                    "content": json.dumps(result_dict, ensure_ascii=False, separators=(",", ":")),
                }
            )
            result_rows.append(
                {
                    "tool_call_id": item["tool_call_id"],
                    "name": item["name"],
                    "result": result_dict,
                    "elapsed_ms": item["elapsed_ms"],
                }
            )
        self.repository.append_tool_messages(
            run_id,
            tool_messages,
            step=step,
            mode=row["data_mode"],
            model=row["model_name"],
            results=result_rows,
        )
        return True

    async def _run_locked(self, run_id: str, lease_lost: asyncio.Event | None = None) -> TaskView:
        task = self.repository.get_task_view(run_id)
        if task.status in {"WAITING_APPROVAL", "COMPLETED", "FAILED", "CANCELLED"}:
            return task
        tool_call_count = 0
        while task.step < self.config.max_steps:
            if lease_lost is not None and lease_lost.is_set():
                return self.repository.get_task_view(run_id)
            row = self.repository.get_run(run_id)
            if row is None:
                raise KeyError(f"unknown run_id: {run_id}")
            if row["status"] != "RUNNING":
                return self.repository.get_task_view(run_id)
            step = int(row["step"]) + 1
            messages = self.repository.get_messages(run_id)
            missing_calls = self._missing_tool_calls(messages)
            if missing_calls:
                recovered = await self._recover_missing_tool_receipts(
                    run_id, row, int(row["step"]), missing_calls
                )
                if not recovered:
                    return self.repository.get_task_view(run_id)
                task = self.repository.get_task_view(run_id)
                continue
            include_approval_tools = bool(row["allow_paper_order_proposals"])
            tools = self.dispatcher.registry.openai_tools(include_approval_tools=include_approval_tools)
            started = time.monotonic()
            try:
                response = await asyncio.wait_for(
                    self.llm.complete(messages=messages, tools=tools, model=row["model_name"] or self.config.model),
                    timeout=self.config.model_timeout_seconds,
                )
            except asyncio.TimeoutError:
                self.repository.set_run_state(
                    run_id,
                    status="FAILED",
                    error={"code": "MODEL_TIMEOUT", "message": "LLM response timed out."},
                    event_type="MODEL_ERROR",
                    event_status="error",
                    error_code="MODEL_TIMEOUT",
                    event_payload={},
                )
                return self.repository.get_task_view(run_id)
            except Exception as exc:
                self.repository.set_run_state(
                    run_id,
                    status="FAILED",
                    error={"code": "MODEL_FAILED", "message": f"{type(exc).__name__}: {exc}"},
                    event_type="MODEL_ERROR",
                    event_status="error",
                    error_code="MODEL_FAILED",
                    event_payload={},
                )
                return self.repository.get_task_view(run_id)
            if lease_lost is not None and lease_lost.is_set():
                return self.repository.get_task_view(run_id)
            assistant = normalize_assistant(response)
            self.repository.append_assistant_and_audit(
                run_id,
                assistant,
                step=step,
                mode=row["data_mode"],
                model=row["model_name"],
                elapsed_ms=(time.monotonic() - started) * 1000,
            )
            if lease_lost is not None and lease_lost.is_set():
                return self.repository.get_task_view(run_id)
            tool_calls = assistant.get("tool_calls") or []
            if not tool_calls:
                content = assistant.get("content")
                if not isinstance(content, str) or not content.strip():
                    self.repository.set_run_state(
                        run_id,
                        status="FAILED",
                        error={"code": "EMPTY_MODEL_RESPONSE", "message": "Model returned neither text nor tool calls."},
                        event_type="RUN_FAILED",
                        event_status="error",
                        error_code="EMPTY_MODEL_RESPONSE",
                        event_payload={},
                    )
                else:
                    self.repository.set_run_state(
                        run_id,
                        status="COMPLETED",
                        final_answer=content,
                        event_type="RUN_COMPLETED",
                        event_payload={"answer_length": len(content)},
                    )
                return self.repository.get_task_view(run_id)

            calls: list[dict[str, str]] = []
            for item in tool_calls:
                function = item.get("function") or {}
                calls.append(
                    {
                        "call_id": str(item.get("id") or ""),
                        "name": str(function.get("name") or ""),
                        "arguments": function.get("arguments", ""),
                    }
                )
            tool_call_count += len(calls)
            if tool_call_count > self.config.max_tool_calls:
                self.repository.set_run_state(
                    run_id,
                    status="FAILED",
                    error={"code": "TOOL_CALL_LIMIT", "message": "Maximum tool calls exceeded."},
                    event_type="RUN_FAILED",
                    event_status="error",
                    error_code="TOOL_CALL_LIMIT",
                    event_payload={"limit": self.config.max_tool_calls},
                )
                return self.repository.get_task_view(run_id)

            proposal_calls = [call for call in calls if call["name"] == "propose_paper_order"]
            if len(calls) == 1 and len(proposal_calls) == 1:
                call = proposal_calls[0]
                context = ToolContext(run_id, row["data_mode"], step)
                try:
                    proposal_result = await self.proposal_handler(
                        run_id,
                        ToolCall(call["call_id"], call["name"], call["arguments"]),
                        context,
                    )
                except Exception as exc:
                    proposal_result = ToolResult.failure(
                        "FAILED_TOOL", f"Proposal handler failed: {type(exc).__name__}: {exc}", retryable=False
                    )
                if proposal_result is None:
                    return self.repository.get_task_view(run_id)
                if lease_lost is not None and lease_lost.is_set():
                    return self.repository.get_task_view(run_id)
                result_dict = proposal_result.to_dict()
                tool_message = {
                    "role": "tool",
                    "tool_call_id": call["call_id"],
                    "name": call["name"],
                    "content": json.dumps(result_dict, ensure_ascii=False, separators=(",", ":")),
                }
                self.repository.append_tool_messages(
                    run_id,
                    [tool_message],
                    step=step,
                    mode=row["data_mode"],
                    model=row["model_name"],
                    results=[
                        {
                            "tool_call_id": call["call_id"],
                            "name": call["name"],
                            "result": result_dict,
                            "elapsed_ms": 0.0,
                        }
                    ],
                )
                task = self.repository.get_task_view(run_id)
                continue

            context = ToolContext(run_id, row["data_mode"], step)
            results = await self.dispatcher.dispatch_batch(calls, context)
            if lease_lost is not None and lease_lost.is_set():
                return self.repository.get_task_view(run_id)
            tool_messages: list[dict[str, Any]] = []
            result_rows: list[dict[str, Any]] = []
            for item in results:
                result: ToolResult = item["result"]
                result_dict = result.to_dict()
                tool_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": item["tool_call_id"],
                        "name": item["name"],
                        "content": json.dumps(result_dict, ensure_ascii=False, separators=(",", ":")),
                    }
                )
                result_rows.append(
                    {
                        "tool_call_id": item["tool_call_id"],
                        "name": item["name"],
                        "result": result_dict,
                        "elapsed_ms": item["elapsed_ms"],
                    }
                )
            self.repository.append_tool_messages(
                run_id,
                tool_messages,
                step=step,
                mode=row["data_mode"],
                model=row["model_name"],
                results=result_rows,
            )
            task = self.repository.get_task_view(run_id)
        if lease_lost is not None and lease_lost.is_set():
            return self.repository.get_task_view(run_id)
        self.repository.set_run_state(
            run_id,
            status="FAILED",
            error={"code": "STEP_LIMIT", "message": "Maximum model steps exceeded."},
            event_type="RUN_FAILED",
            event_status="error",
            error_code="STEP_LIMIT",
            event_payload={"limit": self.config.max_steps},
        )
        return self.repository.get_task_view(run_id)
