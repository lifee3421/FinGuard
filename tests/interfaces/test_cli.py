from __future__ import annotations

from dataclasses import dataclass

from finguard.interfaces.cli import CliRunner, main


@dataclass
class View:
    run_id: str = "run-1"
    status: str = "RUNNING"
    step: str = "round-1"
    data_mode: str = "demo"
    allow_paper_order_proposals: bool = False
    pending_proposal: object | None = None
    final_answer: str | None = None
    error: object | None = None
    version: int = 2


class FakeService:
    def __init__(self):
        self.calls = []

    async def start_task(self, prompt, data_mode="demo", allow_paper_order_proposals=False):
        self.calls.append(("start", prompt, data_mode, allow_paper_order_proposals))
        return View(data_mode=data_mode, allow_paper_order_proposals=allow_paper_order_proposals)

    async def get_task(self, run_id):
        self.calls.append(("get_task", run_id))
        return View(run_id=run_id)

    async def get_events(self, run_id, after_seq=0):
        self.calls.append(("events", run_id, after_seq))
        return []

    async def decide_proposal(self, run_id, proposal_id, decision, expected_version=None):
        self.calls.append(("decide", run_id, proposal_id, decision, expected_version))
        return {"run_id": run_id, "proposal_id": proposal_id, "decision": decision}

    async def resume_task(self, run_id):
        self.calls.append(("resume", run_id))
        return View(run_id=run_id)

    async def get_portfolio(self):
        self.calls.append(("portfolio",))
        return {"cash": "1000.00", "equity": "1000.00", "currency": "USD", "positions": []}


def test_start_defaults_to_demo_and_read_only(capsys):
    service = FakeService()
    assert main(["start", "research AAPL"], service=service) == 0
    assert service.calls == [("start", "research AAPL", "demo", False)]
    assert "PAPER ACCOUNT" in capsys.readouterr().out


def test_approval_passes_expected_version():
    service = FakeService()
    assert main(["approve", "run-1", "proposal-1", "--expected-version", "7"], service=service) == 0
    assert service.calls == [("decide", "run-1", "proposal-1", "approve", 7)]


def test_runner_events_uses_incremental_sequence():
    service = FakeService()
    runner = CliRunner(service, output=lambda _: None)
    import asyncio

    asyncio.run(runner.events("run-2", after_seq=11))
    assert service.calls == [("events", "run-2", 11)]
