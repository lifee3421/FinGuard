"""Rich/plain terminal client for the frozen ApplicationService contract.

The command boundary is synchronous for normal shell use and enters the async
service with :func:`asyncio.run`.  This module never talks to an LLM or opens
SQLite directly; tests and embedding applications can pass a service object to
``main(service=...)`` or ``CliRunner``.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable, Sequence
from typing import Any

from .formatting import json_text, one_line_status, to_jsonable, view_field
from .service_loader import ServiceUnavailable, load_application_service

try:  # Optional presentation dependency; the CLI remains usable without it.
    from rich.console import Console
except ImportError:  # pragma: no cover - depends on the user's install set
    Console = None  # type: ignore[assignment,misc]


class CliRunner:
    """Thin presentation adapter around one ApplicationService instance."""

    def __init__(self, service: Any, output: Callable[[str], Any] = print) -> None:
        self.service = service
        self.output = output

    async def start(
        self,
        prompt: str,
        data_mode: str = "demo",
        allow_paper_order_proposals: bool = False,
    ) -> Any:
        view = await self.service.start_task(
            prompt,
            data_mode=data_mode,
            allow_paper_order_proposals=allow_paper_order_proposals,
        )
        self.show_task(view)
        return view

    async def status(self, run_id: str) -> Any:
        view = await self.service.get_task(run_id)
        self.show_task(view)
        return view

    async def events(self, run_id: str, after_seq: int = 0) -> list[Any]:
        events = await self.service.get_events(run_id, after_seq=after_seq)
        if not events:
            self.output("No events.")
        for event in events:
            self.show_event(event)
        return events

    async def decide(
        self,
        run_id: str,
        proposal_id: str,
        decision: str,
        expected_version: int | None = None,
    ) -> Any:
        result = await self.service.decide_proposal(
            run_id,
            proposal_id,
            decision,
            expected_version=expected_version,
        )
        self.output(json_text(result))
        return result

    async def resume(self, run_id: str) -> Any:
        view = await self.service.resume_task(run_id)
        self.show_task(view)
        return view

    async def portfolio(self) -> Any:
        view = await self.service.get_portfolio()
        self.output("PAPER PORTFOLIO (NON-LIVE ACCOUNT)")
        self.output(json_text(view))
        return view

    def show_task(self, view: Any) -> None:
        self.output(one_line_status(view))
        mode = view_field(view, "data_mode")
        if mode:
            self.output(f"data_mode={mode} | PAPER ACCOUNT (simulation only)")
        final_answer = view_field(view, "final_answer")
        if final_answer:
            self.output(f"FINAL ANSWER:\n{final_answer}")
        error = view_field(view, "error")
        if error:
            self.output(f"ERROR:\n{json_text(error)}")
        pending = view_field(view, "pending_proposal")
        if pending:
            self.show_pending(pending)

    def show_pending(self, proposal: Any) -> None:
        self.output("PENDING APPROVAL (no order has been executed)")
        fields = ("proposal_id", "symbol", "side", "quantity", "reference_price", "reference_quote_ref", "evidence_ids", "version")
        for field in fields:
            value = view_field(proposal, field)
            if value is not None:
                self.output(f"  {field}: {json_text(value) if isinstance(value, (dict, list)) else value}")

    def show_event(self, event: Any) -> None:
        seq = view_field(event, "seq", "?")
        event_type = view_field(event, "event_type", "?")
        status = view_field(event, "status", "")
        step = view_field(event, "step", "")
        code = view_field(event, "error_code")
        suffix = f" error={code}" if code else ""
        self.output(f"[{seq}] {event_type} status={status} step={step}{suffix}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="finguard", description="FinGuard paper research agent")
    sub = parser.add_subparsers(dest="command")

    start = sub.add_parser("start", help="start a research task")
    start.add_argument("prompt", nargs="?", help="natural-language research task")
    start.add_argument("--prompt", dest="prompt_option", help="natural-language research task")
    start.add_argument("--data-mode", choices=("demo", "live"), default="demo")
    start.add_argument(
        "--allow-proposals",
        "--allow-paper-order-proposals",
        dest="allow_proposals",
        action="store_true",
        help="allow paper-order proposals; default is read-only",
    )

    status = sub.add_parser("status", aliases=("get", "task"), help="show a task snapshot")
    status.add_argument("run_id")
    status.add_argument("--events", action="store_true", help="include persisted audit events")
    status.add_argument("--after-seq", type=int, default=0)

    events = sub.add_parser("events", help="show persisted audit events")
    events.add_argument("run_id")
    events.add_argument("--after-seq", type=int, default=0)

    for name, decision in (("approve", "approve"), ("reject", "reject")):
        command = sub.add_parser(name, help=f"{name} a pending proposal")
        command.set_defaults(decision=decision)
        command.add_argument("run_id")
        command.add_argument("proposal_id")
        command.add_argument("--expected-version", type=int)

    decide = sub.add_parser("decide", help="approve or reject a pending proposal")
    decide.add_argument("run_id")
    decide.add_argument("proposal_id")
    decide.add_argument("decision", choices=("approve", "reject"))
    decide.add_argument("--expected-version", type=int)

    later = sub.add_parser("later", help="leave a pending proposal untouched")
    later.add_argument("run_id")

    resume = sub.add_parser("resume", help="recover a persisted run after restart")
    resume.add_argument("run_id")
    sub.add_parser("portfolio", help="show PAPER portfolio")
    sub.add_parser("interactive", help="guided terminal session")
    return parser


def _run(coro: Any) -> Any:
    """CLI boundary for async service calls."""
    return asyncio.run(coro)


def run_interactive(runner: CliRunner, input_fn: Callable[[str], str] = input) -> int:
    prompt = input_fn("Research task (blank to exit): ").strip()
    if not prompt:
        return 0
    mode = input_fn("Data mode [demo/live] (demo): ").strip().lower() or "demo"
    if mode not in {"demo", "live"}:
        runner.output("Invalid data mode; use demo or live.")
        return 2
    allow = input_fn("Allow paper-order proposals? [y/N]: ").strip().lower() in {"y", "yes"}
    view = _run(runner.start(prompt, mode, allow))
    pending = view_field(view, "pending_proposal")
    if pending:
        proposal_id = view_field(pending, "proposal_id")
        answer = input_fn("Approval [approve/reject/later]: ").strip().lower()
        if answer in {"approve", "reject"}:
            _run(runner.decide(str(view_field(view, "run_id")), str(proposal_id), answer, view_field(view, "version")))
        elif answer != "later":
            runner.output("Unknown choice; proposal remains pending.")
    return 0


def main(argv: Sequence[str] | None = None, service: Any = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command is None:
        args.command = "interactive"
    try:
        if service is None:
            service = load_application_service()
    except ServiceUnavailable as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if Console is not None:
        rich_console = Console()
        runner = CliRunner(service, output=lambda text: rich_console.print(text, markup=False))
    else:
        runner = CliRunner(service)
    try:
        if args.command == "start":
            prompt = (args.prompt_option or args.prompt or "").strip()
            if not prompt:
                print("ERROR: a research prompt is required", file=sys.stderr)
                return 2
            _run(runner.start(prompt, args.data_mode, args.allow_proposals))
        elif args.command in {"status", "get", "task"}:
            _run(runner.status(args.run_id))
            if args.events:
                _run(runner.events(args.run_id, args.after_seq))
        elif args.command == "events":
            _run(runner.events(args.run_id, args.after_seq))
        elif args.command in {"approve", "reject", "decide"}:
            decision = args.decision if args.command == "decide" else args.decision
            _run(runner.decide(args.run_id, args.proposal_id, args.decision, args.expected_version))
        elif args.command == "later":
            runner.output(f"run_id={args.run_id}: proposal left pending; no service mutation performed")
        elif args.command == "resume":
            _run(runner.resume(args.run_id))
        elif args.command == "portfolio":
            _run(runner.portfolio())
        elif args.command == "interactive":
            return run_interactive(runner)
        return 0
    except Exception as exc:  # keep structured core errors visible without fake success
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover - exercised by shell invocation
    raise SystemExit(main())
