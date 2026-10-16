from __future__ import annotations

import asyncio
import json

import httpx

from finguard.core.dispatcher import ToolDispatcher, ToolRegistry, ToolSpec
from finguard.core.models import ToolContext, ToolResult


def test_real_local_http_requests_overlap() -> None:
    async def run() -> None:
        active = 0
        max_active = 0

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            nonlocal active, max_active
            try:
                await reader.readuntil(b"\r\n\r\n")
            except asyncio.IncompleteReadError:
                writer.close()
                await writer.wait_closed()
                return
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.05)
            body = json.dumps({"ok": True}).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + body
            )
            await writer.drain()
            active -= 1
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        client = httpx.AsyncClient()

        async def fetch(args, ctx):
            response = await client.get(f"http://127.0.0.1:{port}/quote/{args['symbol']}")
            return ToolResult.success(response.json())

        registry = ToolRegistry()
        registry.register(
            ToolSpec(
                "fetch_quote",
                "fetch",
                {
                    "type": "object",
                    "properties": {"symbol": {"type": "string"}},
                    "required": ["symbol"],
                    "additionalProperties": False,
                },
                fetch,
                parallel_safe=True,
            )
        )
        try:
            results = await ToolDispatcher(registry, timeout_seconds=2).dispatch_batch(
                [
                    {"call_id": "http-a", "name": "fetch_quote", "arguments": '{"symbol":"AAPL"}'},
                    {"call_id": "http-b", "name": "fetch_quote", "arguments": '{"symbol":"MSFT"}'},
                ],
                ToolContext("run", "demo", 1),
            )
        finally:
            await client.aclose()
            server.close()
            await server.wait_closed()
        assert max_active == 2
        assert [item["tool_call_id"] for item in results] == ["http-a", "http-b"]
        assert all(item["result"].ok for item in results)

    asyncio.run(run())
