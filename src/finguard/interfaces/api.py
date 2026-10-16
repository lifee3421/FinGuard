"""FastAPI transport for the ApplicationService.

This module is intentionally a transport adapter only.  It does not expose
the model client, accept arbitrary code, or write the paper account itself.
Run with ``uvicorn finguard.interfaces.api:app`` after the core service is
available (or construct ``create_app(service)`` in a host application).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .formatting import to_jsonable
from .service_loader import ServiceUnavailable, load_application_service

try:  # FastAPI is an optional runtime dependency until A finalizes packaging.
    from fastapi import Body, FastAPI, HTTPException, Query
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles
except ImportError:  # pragma: no cover - exercised only without optional dependency
    FastAPI = None  # type: ignore[assignment,misc]


def create_app(service: Any = None) -> Any:
    if FastAPI is None:
        raise RuntimeError("FastAPI is not installed; install the project's interface dependencies")
    app = FastAPI(title="FinGuard Agent Workspace", docs_url="/docs", redoc_url=None)
    static_dir = Path(__file__).resolve().parents[3] / "web"
    static_assets = static_dir / "static"
    if static_assets.exists():
        app.mount("/static", StaticFiles(directory=static_assets), name="static")
    # The checked-in workspace keeps browser assets under ``web/assets`` so
    # they remain separate from Python code.  Serve them without introducing
    # a second application or a frontend state store.
    assets_dir = static_dir / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    def get_service() -> Any:
        if service is not None:
            return service
        try:
            return load_application_service()
        except ServiceUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/", include_in_schema=False)
    async def index() -> Any:
        index_file = static_dir / "index.html"
        if not index_file.exists():
            raise HTTPException(status_code=404, detail="workspace UI is not installed")
        return FileResponse(index_file)

    @app.post("/api/tasks")
    async def start_task(payload: dict[str, Any] = Body(...)) -> Any:
        prompt = payload.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise HTTPException(status_code=422, detail="prompt must be a non-empty string")
        mode = payload.get("data_mode", "demo")
        if mode not in {"demo", "live"}:
            raise HTTPException(status_code=422, detail="data_mode must be demo or live")
        allow = payload.get("allow_paper_order_proposals", False)
        if not isinstance(allow, bool):
            raise HTTPException(status_code=422, detail="allow_paper_order_proposals must be boolean")
        result = await get_service().start_task(
            prompt.strip(), data_mode=mode, allow_paper_order_proposals=allow
        )
        return to_jsonable(result)

    @app.get("/api/tasks/{run_id}")
    async def get_task(run_id: str) -> Any:
        return to_jsonable(await get_service().get_task(run_id))

    @app.get("/api/tasks/{run_id}/events")
    async def get_events(run_id: str, after_seq: int = Query(0, ge=0)) -> Any:
        return to_jsonable(await get_service().get_events(run_id, after_seq=after_seq))

    @app.post("/api/proposals/{proposal_id}/decision")
    async def decide_proposal(proposal_id: str, payload: dict[str, Any] = Body(...)) -> Any:
        run_id = payload.get("run_id")
        decision = payload.get("decision")
        expected_version = payload.get("expected_version")
        if not isinstance(run_id, str) or not run_id:
            raise HTTPException(status_code=422, detail="run_id is required")
        if decision not in {"approve", "reject"}:
            raise HTTPException(status_code=422, detail="decision must be approve or reject")
        if expected_version is not None and (isinstance(expected_version, bool) or not isinstance(expected_version, int)):
            raise HTTPException(status_code=422, detail="expected_version must be an integer or null")
        result = await get_service().decide_proposal(
            run_id, proposal_id, decision, expected_version=expected_version
        )
        return to_jsonable(result)

    @app.post("/api/tasks/{run_id}/resume")
    async def resume_task(run_id: str) -> Any:
        return to_jsonable(await get_service().resume_task(run_id))

    @app.get("/api/portfolio")
    async def get_portfolio() -> Any:
        return to_jsonable(await get_service().get_portfolio())

    @app.get("/api/config/public")
    async def public_config() -> dict[str, Any]:
        # Deliberately excludes environment values and credentials.
        return {
            "service": "FinGuard",
            "paper_account": True,
            "supports_live_data": False,
            "capabilities": {
                "model_configuration": False,
                "connection_check": False,
                "automatic_authorization": False,
                "research_archive": False,
                "evidence_details": False,
                "task_history": False,
            },
            "unavailable": [
                "LIVE_DATA",
                "MODEL_CONFIGURATION",
                "CONNECTION_CHECK",
                "AUTOMATIC_AUTHORIZATION",
                "RESEARCH_ARCHIVE",
                "EVIDENCE_DETAILS",
                "TASK_HISTORY",
            ],
        }

    return app


# Importing this module is safe before core integration; service resolution is
# deferred until a request arrives.
app = create_app() if FastAPI is not None else None
