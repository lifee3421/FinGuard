"""Small typing-only boundary shared by CLI and HTTP adapters.

The core implementation may return dataclasses or JSON-like mappings.  The
adapters therefore intentionally do not prescribe concrete view classes.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol


class ApplicationServiceProtocol(Protocol):
    async def start_task(
        self,
        prompt: str,
        data_mode: Literal["demo", "live"] = "demo",
        allow_paper_order_proposals: bool = False,
    ) -> Any: ...

    async def get_task(self, run_id: str) -> Any: ...

    async def get_events(self, run_id: str, after_seq: int = 0) -> list[Any]: ...

    async def decide_proposal(
        self,
        run_id: str,
        proposal_id: str,
        decision: Literal["approve", "reject"],
        expected_version: int | None = None,
    ) -> Any: ...

    async def resume_task(self, run_id: str) -> Any: ...

    async def get_portfolio(self) -> Any: ...
