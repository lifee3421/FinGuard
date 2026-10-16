from __future__ import annotations

import asyncio

from finguard.core.dispatcher import ToolDispatcher, ToolRegistry, ToolSpec
from finguard.core.models import ToolContext, ToolResult


def test_independent_read_tools_overlap_and_preserve_order() -> None:
    async def run() -> None:
        active = 0
        max_active = 0

        async def read_tool(args, ctx):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.03)
            active -= 1
            return ToolResult.success({"symbol": args["symbol"]})

        registry = ToolRegistry()
        registry.register(
            ToolSpec(
                "read",
                "read",
                {
                    "type": "object",
                    "properties": {"symbol": {"type": "string"}},
                    "required": ["symbol"],
                    "additionalProperties": False,
                },
                read_tool,
                parallel_safe=True,
            )
        )
        dispatcher = ToolDispatcher(registry)
        results = await dispatcher.dispatch_batch(
            [
                {"call_id": "a", "name": "read", "arguments": '{"symbol":"AAPL"}'},
                {"call_id": "b", "name": "read", "arguments": '{"symbol":"MSFT"}'},
            ],
            ToolContext("run", "demo", 1),
        )
        assert max_active == 2
        assert [row["tool_call_id"] for row in results] == ["a", "b"]
        assert [row["result"].data["symbol"] for row in results] == ["AAPL", "MSFT"]

    asyncio.run(run())


def test_invalid_and_unknown_tools_are_structured() -> None:
    async def run() -> None:
        registry = ToolRegistry()

        async def echo(args, ctx):
            return ToolResult.success(args)

        async def slow(args, ctx):
            await asyncio.sleep(0.05)
            return ToolResult.success({})

        registry.register(
            ToolSpec(
                "echo",
                "echo",
                {
                    "type": "object",
                    "properties": {"symbol": {"type": "string"}},
                    "required": ["symbol"],
                    "additionalProperties": False,
                },
                echo,
            )
        )
        registry.register(
            ToolSpec(
                "slow",
                "slow",
                {"type": "object", "properties": {}, "additionalProperties": False},
                slow,
            )
        )
        dispatcher = ToolDispatcher(registry, timeout_seconds=0.01)
        results = await dispatcher.dispatch_batch(
            [
                {"call_id": "bad-json", "name": "unknown", "arguments": "{"},
                {"call_id": "unknown", "name": "missing", "arguments": "{}"},
                {"call_id": "invalid-json", "name": "echo", "arguments": "{"},
                {"call_id": "invalid-args", "name": "echo", "arguments": "{}"},
                {"call_id": "timeout", "name": "slow", "arguments": "{}"},
            ],
            ToolContext("run", "demo", 1),
        )
        assert results[0]["result"].error.code == "UNKNOWN_TOOL"
        assert results[1]["result"].error.code == "UNKNOWN_TOOL"
        assert results[2]["result"].error.code == "INVALID_JSON"
        assert results[3]["result"].error.code == "INVALID_ARGS"
        assert results[4]["result"].error.code == "TIMEOUT"

    asyncio.run(run())
