"""Small runtime protocol used by the CLI and the HTTP adapter.

Keeping this protocol here lets the interface layer type-check independently
of the core implementation while still accepting A's concrete service object.
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


class ServiceUnavailable(RuntimeError):
    """Raised when the core ApplicationService cannot be loaded."""


def _default_service() -> ApplicationServiceProtocol:
    """Load the core's conventional service factory without importing it early.

    A can expose either ``finguard.core.application:ApplicationService`` or a
    ``create_application_service`` factory.  This delayed lookup keeps `--help`
    and static web assets usable before the core package is installed.
    """

    try:
        from finguard.core import application as core_application
    except ImportError as exc:
        raise ServiceUnavailable(
            "ApplicationService is unavailable. Install/run the FinGuard core first "
            "or inject a service instance."
        ) from exc

    factory = getattr(core_application, "create_application_service", None)
    if not callable(factory):
        factory = getattr(core_application, "ApplicationService", None)
    if not callable(factory):
        raise ServiceUnavailable(
            "finguard.core.application must export ApplicationService or "
            "create_application_service"
        )
    try:
        return factory()  # type: ignore[call-arg,return-value]
    except TypeError:
        # A concrete implementation may require a configured database path;
        # let its own environment/configuration error reach the caller.
        raise
