from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol


class LLMClient(Protocol):
    async def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str,
    ) -> dict[str, Any]: ...


def normalize_assistant(response: Any) -> dict[str, Any]:
    """Accept canonical messages and OpenAI-compatible response envelopes."""
    if isinstance(response, dict) and isinstance(response.get("choices"), list):
        if not response["choices"]:
            return {"role": "assistant", "content": None}
        message = response["choices"][0].get("message", {})
    else:
        message = response
    if not isinstance(message, dict):
        return {"role": "assistant", "content": None}
    normalized: dict[str, Any] = {
        "role": "assistant",
        "content": message.get("content"),
    }
    tool_calls = message.get("tool_calls") or []
    if tool_calls:
        normalized["tool_calls"] = []
        for item in tool_calls:
            function = item.get("function", {}) if isinstance(item, dict) else {}
            normalized["tool_calls"].append(
                {
                    "id": item.get("id", "") if isinstance(item, dict) else "",
                    "type": item.get("type", "function") if isinstance(item, dict) else "function",
                    "function": {
                        "name": function.get("name", ""),
                        "arguments": function.get("arguments", ""),
                    },
                }
            )
    return normalized


class OpenAICompatibleLLMClient:
    """Minimal Chat Completions client; it does not own any agent control flow."""

    def __init__(self, *, base_url: str, api_key: str, timeout_seconds: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    async def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str,
    ) -> dict[str, Any]:
        if not self.api_key:
            raise RuntimeError("LLM_API_KEY is not configured")
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - dependency issue is environment-specific
            raise RuntimeError("httpx is required for the OpenAI-compatible client") from exc
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            return normalize_assistant(response.json())


@dataclass
class FakeLLM:
    """Deterministic test client. Its traces must be labelled mock in reports."""

    responses: list[dict[str, Any]]
    calls: int = 0

    async def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str,
    ) -> dict[str, Any]:
        await asyncio.sleep(0)
        if self.calls >= len(self.responses):
            return {"role": "assistant", "content": "NO_TRADE"}
        value = self.responses[self.calls]
        self.calls += 1
        return normalize_assistant(value)
