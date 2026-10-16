"""Formatting helpers for interface output.

No helper in this module derives or mutates state.  It only makes service
views (dicts, dataclasses, or simple objects) safe to display as JSON.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping
from decimal import Decimal
from typing import Any


def to_jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {field.name: to_jsonable(getattr(value, field.name)) for field in dataclasses.fields(value)}
    if isinstance(value, Mapping):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "model_dump"):
        return to_jsonable(value.model_dump())
    if hasattr(value, "dict") and callable(value.dict):
        return to_jsonable(value.dict())
    if hasattr(value, "__dict__") and not isinstance(value, (str, bytes)):
        return {str(key): to_jsonable(item) for key, item in vars(value).items() if not key.startswith("_")}
    return value


def view_field(view: Any, name: str, default: Any = None) -> Any:
    """Read a field from a mapping or object without changing its semantics."""
    if isinstance(view, Mapping):
        return view.get(name, default)
    return getattr(view, name, default)


def json_text(value: Any) -> str:
    return json.dumps(to_jsonable(value), ensure_ascii=False, indent=2, default=str)


def one_line_status(view: Any) -> str:
    run_id = view_field(view, "run_id", "?")
    status = view_field(view, "status", "?")
    step = view_field(view, "step", "?")
    version = view_field(view, "version", "?")
    return f"run_id={run_id} status={status} step={step} version={version}"
