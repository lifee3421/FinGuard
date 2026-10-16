from __future__ import annotations

import asyncio
import inspect
import json
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .models import ToolContext, ToolError, ToolResult


ToolHandler = Callable[[dict[str, Any], ToolContext], Awaitable[ToolResult | dict[str, Any]] | ToolResult | dict[str, Any]]


@dataclass(slots=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler
    parallel_safe: bool = False
    requires_approval: bool = False

    def as_openai_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._specs:
            raise ValueError(f"tool already registered: {spec.name}")
        self._specs[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def names(self) -> list[str]:
        return list(self._specs)

    def openai_tools(self, *, include_approval_tools: bool = True) -> list[dict[str, Any]]:
        return [
            spec.as_openai_tool()
            for spec in self._specs.values()
            if include_approval_tools or not spec.requires_approval
        ]


def _type_matches(value: Any, expected: str) -> bool:
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }.get(expected, True)


def validate_json_schema(value: Any, schema: dict[str, Any], path: str = "$") -> str | None:
    expected = schema.get("type")
    if expected and not _type_matches(value, expected):
        return f"{path} must be {expected}"
    if "enum" in schema and value not in schema["enum"]:
        return f"{path} must be one of {schema['enum']}"
    if isinstance(value, str):
        if "minLength" in schema and len(value) < int(schema["minLength"]):
            return f"{path} is shorter than minLength"
        if "maxLength" in schema and len(value) > int(schema["maxLength"]):
            return f"{path} is longer than maxLength"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            return f"{path} is below minimum"
        if "maximum" in schema and value > schema["maximum"]:
            return f"{path} is above maximum"
    if isinstance(value, list):
        if "minItems" in schema and len(value) < int(schema["minItems"]):
            return f"{path} has too few items"
        if "maxItems" in schema and len(value) > int(schema["maxItems"]):
            return f"{path} has too many items"
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                error = validate_json_schema(item, item_schema, f"{path}[{index}]")
                if error:
                    return error
    if isinstance(value, dict):
        required = schema.get("required", [])
        for key in required:
            if key not in value:
                return f"{path}.{key} is required"
        if schema.get("additionalProperties") is False:
            allowed = set(schema.get("properties", {}))
            extra = set(value) - allowed
            if extra:
                return f"{path} has unknown properties: {sorted(extra)}"
        for key, child_schema in schema.get("properties", {}).items():
            if key in value and isinstance(child_schema, dict):
                error = validate_json_schema(value[key], child_schema, f"{path}.{key}")
                if error:
                    return error
    return None


class ToolDispatcher:
    def __init__(self, registry: ToolRegistry, *, timeout_seconds: float = 20.0) -> None:
        self.registry = registry
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def _normalize_result(result: ToolResult | dict[str, Any]) -> ToolResult:
        if isinstance(result, ToolResult):
            return result
        if isinstance(result, dict) and "ok" in result:
            error = result.get("error")
            return ToolResult(
                ok=bool(result.get("ok")),
                data=result.get("data"),
                error=(
                    ToolError(
                        code=error.get("code", "FAILED_TOOL"),
                        message=error.get("message", "tool failed"),
                        retryable=bool(error.get("retryable", False)),
                        details=error.get("details"),
                    )
                    if isinstance(error, dict)
                    else None
                ),
            )
        return ToolResult.success(result)

    async def _one(
        self,
        call_id: str,
        name: str,
        raw_arguments: str,
        context: ToolContext,
        *,
        force_error: ToolResult | None = None,
    ) -> dict[str, Any]:
        started = time.monotonic()
        if force_error is not None:
            result = force_error
        else:
            spec = self.registry.get(name)
            if spec is None:
                result = ToolResult.failure("UNKNOWN_TOOL", f"Unknown tool: {name}")
            else:
                try:
                    arguments = json.loads(raw_arguments)
                except (TypeError, json.JSONDecodeError):
                    result = ToolResult.failure("INVALID_JSON", "Tool arguments are not valid JSON.")
                else:
                    if not isinstance(arguments, dict):
                        result = ToolResult.failure("INVALID_ARGS", "Tool arguments must be a JSON object.")
                    else:
                        error = validate_json_schema(arguments, spec.parameters)
                        if error:
                            result = ToolResult.failure("INVALID_ARGS", error)
                        else:
                            try:
                                value = spec.handler(arguments, context)
                                if inspect.isawaitable(value):
                                    value = await asyncio.wait_for(value, timeout=self.timeout_seconds)
                                result = self._normalize_result(value)
                            except asyncio.TimeoutError:
                                result = ToolResult.failure(
                                    "TIMEOUT",
                                    f"Tool {name} timed out after {self.timeout_seconds:.1f}s.",
                                    retryable=True,
                                )
                            except Exception as exc:  # tool failures become model-visible data
                                result = ToolResult.failure(
                                    "FAILED_TOOL",
                                    f"Tool {name} failed: {type(exc).__name__}: {exc}",
                                    retryable=True,
                                )
        return {
            "tool_call_id": call_id,
            "name": name,
            "result": result,
            "elapsed_ms": (time.monotonic() - started) * 1000,
        }

    async def dispatch_batch(
        self,
        calls: list[dict[str, str]],
        context: ToolContext,
        *,
        proposal_names: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        proposal_names = proposal_names or {"propose_paper_order"}
        seen: set[str] = set()
        duplicate_ids: set[str] = set()
        for call in calls:
            call_id = call.get("call_id", "")
            if call_id in seen:
                duplicate_ids.add(call_id)
            seen.add(call_id)
        has_proposal = any(call.get("name") in proposal_names for call in calls)
        mixed_proposals = has_proposal and len(calls) > 1

        async def run(index: int, call: dict[str, str]) -> dict[str, Any]:
            call_id = call.get("call_id", "")
            name = call.get("name", "")
            if call_id in duplicate_ids:
                return await self._one(
                    call_id,
                    name,
                    call.get("arguments", ""),
                    context,
                    force_error=ToolResult.failure("DUPLICATE_TOOL_CALL_ID", f"Duplicate tool_call_id: {call_id}"),
                )
            if mixed_proposals and name in proposal_names:
                return await self._one(
                    call_id,
                    name,
                    call.get("arguments", ""),
                    context,
                    force_error=ToolResult.failure(
                        "ORDER_PROPOSAL_MUST_BE_SOLO",
                        "Paper order proposal must be the only tool call in a model turn.",
                    ),
                )
            return await self._one(call_id, name, call.get("arguments", ""), context)

        results: list[dict[str, Any] | None] = [None] * len(calls)
        parallel_indices: list[int] = []
        serial_indices: list[int] = []
        for index, call in enumerate(calls):
            spec = self.registry.get(call.get("name", ""))
            if spec and spec.parallel_safe and not spec.requires_approval and not (
                mixed_proposals and call.get("name") in proposal_names
            ):
                parallel_indices.append(index)
            else:
                serial_indices.append(index)
        if parallel_indices:
            parallel = await asyncio.gather(*(run(index, calls[index]) for index in parallel_indices))
            for index, result in zip(parallel_indices, parallel):
                results[index] = result
        for index in serial_indices:
            results[index] = await run(index, calls[index])
        return [result for result in results if result is not None]
