from __future__ import annotations

import importlib.util

import pytest

if importlib.util.find_spec("fastapi") is None:
    pytest.skip("FastAPI is an optional interface dependency", allow_module_level=True)

from fastapi.testclient import TestClient

from finguard.interfaces.api import create_app


class FakeService:
    async def start_task(self, prompt, data_mode="demo", allow_paper_order_proposals=False):
        return {"run_id": "r1", "status": "RUNNING", "step": "start", "version": 1}

    async def get_task(self, run_id):
        return {"run_id": run_id, "status": "RUNNING", "version": 1}

    async def get_events(self, run_id, after_seq=0):
        return [{"seq": after_seq + 1, "event_type": "TEST"}]

    async def decide_proposal(self, run_id, proposal_id, decision, expected_version=None):
        return {"run_id": run_id, "proposal_id": proposal_id, "decision": decision, "version": expected_version}

    async def resume_task(self, run_id):
        return {"run_id": run_id, "status": "RUNNING"}

    async def get_portfolio(self):
        return {"cash": "100.00", "equity": "100.00", "currency": "USD", "positions": []}


def test_http_adapter_validates_decision_and_forwards_fields():
    client = TestClient(create_app(FakeService()))
    response = client.post("/api/tasks", json={"prompt": "research", "allow_paper_order_proposals": False})
    assert response.status_code == 200
    assert response.json()["run_id"] == "r1"
    response = client.post("/api/proposals/p1/decision", json={"run_id": "r1", "decision": "later"})
    assert response.status_code == 422
    response = client.post("/api/proposals/p1/decision", json={"run_id": "r1", "decision": "approve", "expected_version": 4})
    assert response.status_code == 200
    assert response.json()["version"] == 4


def test_public_config_does_not_advertise_unimplemented_live_provider():
    client = TestClient(create_app(FakeService()))
    payload = client.get("/api/config/public").json()
    assert payload["supports_live_data"] is False
    assert payload["paper_account"] is True
    assert payload["capabilities"]["model_configuration"] is False
    assert "LIVE_DATA" in payload["unavailable"]
    assert "api" not in str(payload).lower()


class BlockedAndConflictService(FakeService):
    async def decide_proposal(self, run_id, proposal_id, decision, expected_version=None):
        if expected_version == 7:
            return {"run_id": run_id, "proposal_id": proposal_id, "decision": decision, "order_status": "BLOCKED", "blocked_code": "PRICE_GAP_EXCEEDED", "version": 7}
        return {"error": {"code": "VERSION_CONFLICT", "message": "stale task version"}}


def test_backend_result_preserves_price_gap_block_and_version_conflict_payload():
    client = TestClient(create_app(BlockedAndConflictService()))
    blocked = client.post("/api/proposals/p1/decision", json={"run_id": "r1", "decision": "approve", "expected_version": 7})
    assert blocked.status_code == 200
    assert blocked.json()["order_status"] == "BLOCKED"
    assert blocked.json()["blocked_code"] == "PRICE_GAP_EXCEEDED"
    conflict = client.post("/api/proposals/p1/decision", json={"run_id": "r1", "decision": "approve", "expected_version": 6})
    assert conflict.status_code == 200
    assert conflict.json()["error"]["code"] == "VERSION_CONFLICT"
