from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from .models import AuditEvent, DecisionView, TaskView, ToolResult, jsonable, utc_now


SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    prompt TEXT NOT NULL,
    status TEXT NOT NULL,
    data_mode TEXT NOT NULL CHECK (data_mode IN ('demo', 'live')),
    allow_paper_order_proposals INTEGER NOT NULL CHECK (allow_paper_order_proposals IN (0, 1)),
    step INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 0,
    model_name TEXT,
    final_answer TEXT,
    error_json TEXT,
    pending_proposal_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    message_seq INTEGER NOT NULL,
    role TEXT NOT NULL,
    tool_call_id TEXT,
    name TEXT,
    content_json TEXT NOT NULL,
    step INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, message_seq),
    UNIQUE (run_id, tool_call_id)
);

CREATE TABLE IF NOT EXISTS proposals (
    proposal_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    original_tool_call_id TEXT NOT NULL,
    status TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    rationale TEXT NOT NULL,
    evidence_ids_json TEXT NOT NULL,
    reference_price TEXT NOT NULL,
    reference_quote_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    decision TEXT,
    decision_reason TEXT,
    version INTEGER NOT NULL DEFAULT 0,
    UNIQUE (run_id, original_tool_call_id)
);

CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL UNIQUE REFERENCES proposals(proposal_id),
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    decision TEXT NOT NULL CHECK (decision IN ('approve', 'reject')),
    expected_version INTEGER,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_accounts (
    account_id TEXT PRIMARY KEY,
    currency TEXT NOT NULL,
    cash TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    account_id TEXT NOT NULL REFERENCES paper_accounts(account_id),
    symbol TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity >= 0),
    average_price TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (account_id, symbol)
);

CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL UNIQUE REFERENCES proposals(proposal_id),
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    status TEXT NOT NULL CHECK (status IN ('EXECUTED', 'BLOCKED')),
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    quantity INTEGER NOT NULL,
    execution_price TEXT,
    execution_quote_ref TEXT,
    blocked_code TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger_entries (
    entry_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES orders(order_id),
    account_id TEXT NOT NULL REFERENCES paper_accounts(account_id),
    symbol TEXT NOT NULL,
    cash_delta TEXT NOT NULL,
    quantity_delta INTEGER NOT NULL,
    price TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    seq INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    step INTEGER NOT NULL,
    tool_call_id TEXT,
    elapsed_ms REAL,
    status TEXT NOT NULL,
    error_code TEXT,
    payload_json TEXT NOT NULL,
    mode TEXT NOT NULL,
    model TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);

CREATE TABLE IF NOT EXISTS run_leases (
    run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
    owner_id TEXT NOT NULL,
    expires_at REAL NOT NULL
);
"""


class StorageError(RuntimeError):
    pass


class SQLiteRepository:
    """SQLite authority for run state, approvals, paper ledger and audit events."""

    def __init__(self, path: str = ":memory:", *, initial_cash: str = "100000.00") -> None:
        self.path = path
        self.initial_cash = initial_cash
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            path,
            check_same_thread=False,
            isolation_level=None,
            timeout=30,
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate_proposal_tool_call_scope()
            self._conn.execute(
                "INSERT OR IGNORE INTO paper_accounts(account_id,currency,cash,version,updated_at) VALUES(?,?,?,?,?)",
                ("paper-default", "USD", initial_cash, 0, utc_now()),
            )

    def _migrate_proposal_tool_call_scope(self) -> None:
        """Migrate the pre-P1 global proposal tool-call index to per-run scope."""
        indexes = self._conn.execute("PRAGMA index_list('proposals')").fetchall()
        for index in indexes:
            if not bool(index["unique"]):
                continue
            columns = self._conn.execute(f"PRAGMA index_info({index['name']!r})").fetchall()
            if [row["name"] for row in columns] != ["original_tool_call_id"]:
                continue
            self._conn.execute("PRAGMA foreign_keys=OFF")
            try:
                self._conn.execute("BEGIN")
                self._conn.execute("ALTER TABLE proposals RENAME TO proposals_legacy")
                self._conn.execute(
                    """CREATE TABLE proposals (
                        proposal_id TEXT PRIMARY KEY,
                        run_id TEXT NOT NULL REFERENCES runs(run_id),
                        original_tool_call_id TEXT NOT NULL,
                        status TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
                        quantity INTEGER NOT NULL CHECK (quantity > 0),
                        rationale TEXT NOT NULL,
                        evidence_ids_json TEXT NOT NULL,
                        reference_price TEXT NOT NULL,
                        reference_quote_ref TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        decided_at TEXT,
                        decision TEXT,
                        decision_reason TEXT,
                        version INTEGER NOT NULL DEFAULT 0,
                        UNIQUE (run_id, original_tool_call_id)
                    )"""
                )
                self._conn.execute(
                    """INSERT INTO proposals
                    SELECT proposal_id,run_id,original_tool_call_id,status,symbol,side,quantity,
                           rationale,evidence_ids_json,reference_price,reference_quote_ref,created_at,
                           decided_at,decision,decision_reason,version
                    FROM proposals_legacy"""
                )
                self._conn.execute("DROP TABLE proposals_legacy")
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            finally:
                self._conn.execute("PRAGMA foreign_keys=ON")
            break

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(jsonable(value), ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _parse(value: str | None) -> Any:
        if value is None:
            return None
        return json.loads(value)

    def _next_seq_locked(self, run_id: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM audit_events WHERE run_id=?",
            (run_id,),
        ).fetchone()
        return int(row["next_seq"])

    def _append_audit_locked(
        self,
        run_id: str,
        *,
        event_type: str,
        step: int,
        status: str,
        mode: str,
        model: str | None,
        payload: dict[str, Any] | None = None,
        tool_call_id: str | None = None,
        elapsed_ms: float | None = None,
        error_code: str | None = None,
    ) -> int:
        seq = self._next_seq_locked(run_id)
        self._conn.execute(
            """INSERT INTO audit_events
            (run_id,seq,event_type,step,tool_call_id,elapsed_ms,status,error_code,payload_json,mode,model,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id,
                seq,
                event_type,
                step,
                tool_call_id,
                elapsed_ms,
                status,
                error_code,
                self._json(payload or {}),
                mode,
                model,
                utc_now(),
            ),
        )
        return seq

    def create_run(
        self,
        *,
        prompt: str,
        data_mode: str,
        allow_paper_order_proposals: bool,
        model_name: str | None,
        system_prompt: str,
    ) -> str:
        run_id = str(uuid.uuid4())
        now = utc_now()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    """INSERT INTO runs
                    (run_id,prompt,status,data_mode,allow_paper_order_proposals,step,version,model_name,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        run_id,
                        prompt,
                        "RUNNING",
                        data_mode,
                        int(allow_paper_order_proposals),
                        0,
                        0,
                        model_name,
                        now,
                        now,
                    ),
                )
                self._append_message_locked(run_id, {"role": "system", "content": system_prompt}, 0)
                self._append_message_locked(run_id, {"role": "user", "content": prompt}, 0)
                self._append_audit_locked(
                    run_id,
                    event_type="RUN_CREATED",
                    step=0,
                    status="ok",
                    mode=data_mode,
                    model=model_name,
                    payload={"allow_paper_order_proposals": allow_paper_order_proposals},
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return run_id

    def _append_message_locked(self, run_id: str, message: dict[str, Any], step: int) -> int:
        seq_row = self._conn.execute(
            "SELECT COALESCE(MAX(message_seq), 0) + 1 AS next_seq FROM messages WHERE run_id=?",
            (run_id,),
        ).fetchone()
        seq = int(seq_row["next_seq"])
        role = str(message.get("role", ""))
        tool_call_id = message.get("tool_call_id")
        name = message.get("name")
        self._conn.execute(
            """INSERT INTO messages(run_id,message_seq,role,tool_call_id,name,content_json,step,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (run_id, seq, role, tool_call_id, name, self._json(message), step, utc_now()),
        )
        return seq

    def append_message(self, run_id: str, message: dict[str, Any], step: int) -> int:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                seq = self._append_message_locked(run_id, message, step)
                self._conn.execute(
                    "UPDATE runs SET version=version+1, updated_at=? WHERE run_id=?",
                    (utc_now(), run_id),
                )
                self._conn.execute("COMMIT")
                return seq
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def append_assistant_and_audit(
        self,
        run_id: str,
        message: dict[str, Any],
        *,
        step: int,
        mode: str,
        model: str | None,
        elapsed_ms: float | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._append_message_locked(run_id, message, step)
                self._conn.execute(
                    "UPDATE runs SET step=?, version=version+1, updated_at=? WHERE run_id=?",
                    (step, utc_now(), run_id),
                )
                self._append_audit_locked(
                    run_id,
                    event_type="MODEL_RESPONSE",
                    step=step,
                    status="ok",
                    mode=mode,
                    model=model,
                    payload={"has_tool_calls": bool(message.get("tool_calls"))},
                    elapsed_ms=elapsed_ms,
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def append_tool_messages(
        self,
        run_id: str,
        messages: list[dict[str, Any]],
        *,
        step: int,
        mode: str,
        model: str | None,
        results: list[dict[str, Any]],
    ) -> None:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for message in messages:
                    self._append_message_locked(run_id, message, step)
                self._conn.execute(
                    "UPDATE runs SET step=?, version=version+1, updated_at=? WHERE run_id=?",
                    (step, utc_now(), run_id),
                )
                for item in results:
                    result = item.get("result") or {}
                    error = result.get("error") or {}
                    self._append_audit_locked(
                        run_id,
                        event_type="TOOL_RESULT",
                        step=step,
                        status="ok" if result.get("ok") else "error",
                        mode=mode,
                        model=model,
                        tool_call_id=item.get("tool_call_id"),
                        elapsed_ms=item.get("elapsed_ms"),
                        error_code=error.get("code"),
                        payload={"tool_name": item.get("name"), "result": result},
                    )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def set_run_state(
        self,
        run_id: str,
        *,
        status: str | None = None,
        step: int | None = None,
        final_answer: str | None = None,
        error: dict[str, Any] | None = None,
        pending_proposal_id: str | None = None,
        event_type: str | None = None,
        event_payload: dict[str, Any] | None = None,
        event_status: str = "ok",
        error_code: str | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                fields: list[str] = ["version=version+1", "updated_at=?"]
                values: list[Any] = [utc_now()]
                if status is not None:
                    fields.append("status=?")
                    values.append(status)
                if step is not None:
                    fields.append("step=?")
                    values.append(step)
                if final_answer is not None:
                    fields.append("final_answer=?")
                    values.append(final_answer)
                if error is not None:
                    fields.append("error_json=?")
                    values.append(self._json(error))
                if pending_proposal_id is not None or status != "WAITING_APPROVAL":
                    fields.append("pending_proposal_id=?")
                    values.append(pending_proposal_id)
                values.append(run_id)
                self._conn.execute(f"UPDATE runs SET {', '.join(fields)} WHERE run_id=?", values)
                if event_type:
                    row = self._conn.execute("SELECT data_mode,model_name,step FROM runs WHERE run_id=?", (run_id,)).fetchone()
                    self._append_audit_locked(
                        run_id,
                        event_type=event_type,
                        step=int(row["step"]),
                        status=event_status,
                        mode=row["data_mode"],
                        model=row["model_name"],
                        payload=event_payload,
                        error_code=error_code,
                    )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def get_run(self, run_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()

    def get_messages(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT content_json FROM messages WHERE run_id=? ORDER BY message_seq",
                (run_id,),
            ).fetchall()
        return [self._parse(row["content_json"]) for row in rows]

    def has_tool_receipt(self, run_id: str, tool_call_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM messages WHERE run_id=? AND role='tool' AND tool_call_id=?",
                (run_id, tool_call_id),
            ).fetchone()
            return row is not None

    def get_task_view(self, run_id: str) -> TaskView:
        row = self.get_run(run_id)
        if row is None:
            raise KeyError(f"unknown run_id: {run_id}")
        pending = None
        if row["pending_proposal_id"]:
            pending = self.get_proposal(row["pending_proposal_id"])
        return TaskView(
            run_id=row["run_id"],
            status=row["status"],
            step=int(row["step"]),
            data_mode=row["data_mode"],
            allow_paper_order_proposals=bool(row["allow_paper_order_proposals"]),
            pending_proposal=pending,
            final_answer=row["final_answer"],
            error=self._parse(row["error_json"]),
            version=int(row["version"]),
        )

    def get_events(self, run_id: str, after_seq: int = 0) -> list[AuditEvent]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM audit_events WHERE run_id=? AND seq>? ORDER BY seq",
                (run_id, after_seq),
            ).fetchall()
        return [
            AuditEvent(
                run_id=row["run_id"],
                seq=int(row["seq"]),
                event_type=row["event_type"],
                step=int(row["step"]),
                tool_call_id=row["tool_call_id"],
                elapsed_ms=row["elapsed_ms"],
                status=row["status"],
                error_code=row["error_code"],
                payload=self._parse(row["payload_json"]) or {},
                mode=row["mode"],
                model=row["model"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def evidence_records_for_run(self, run_id: str) -> dict[str, list[dict[str, Any]]]:
        """Return evidence provenance captured by successful tool messages.

        Company-event tools return a list, while quote/history/indicator tools
        return a single DTO.  Keep both shapes in one index so proposal
        validation can enforce run, symbol and data-mode ownership.
        """
        evidence: dict[str, list[dict[str, Any]]] = {}

        def add_record(evidence_id: Any, data: dict[str, Any]) -> None:
            if not isinstance(evidence_id, str) or not evidence_id:
                return
            record = {
                "evidence_id": evidence_id,
                "symbol": data.get("symbol") if isinstance(data.get("symbol"), str) else None,
                "data_mode": data.get("mode") if isinstance(data.get("mode"), str) else None,
                "event_id": data.get("event_id") if isinstance(data.get("event_id"), str) else None,
            }
            evidence.setdefault(evidence_id, []).append(record)

        for message in self.get_messages(run_id):
            if message.get("role") != "tool":
                continue
            try:
                content = json.loads(message.get("content", "{}"))
            except (TypeError, json.JSONDecodeError):
                continue
            data = content.get("data") if isinstance(content, dict) else None
            if isinstance(data, dict):
                if isinstance(data.get("evidence_id"), str):
                    add_record(data["evidence_id"], data)
                if isinstance(data.get("evidence_ids"), list):
                    for item in data["evidence_ids"]:
                        add_record(item, data)
                if isinstance(data.get("event_id"), str):
                    add_record(data["event_id"], data)
            elif isinstance(data, list):
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    add_record(item.get("evidence_id"), item)
                    add_record(item.get("event_id"), item)
        return evidence

    def evidence_ids_for_run(self, run_id: str) -> set[str]:
        """Return all evidence IDs emitted by tools in this run."""
        return set(self.evidence_records_for_run(run_id))

    def get_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
        if row is None:
            return None
        return {
            "proposal_id": row["proposal_id"],
            "run_id": row["run_id"],
            "original_tool_call_id": row["original_tool_call_id"],
            "status": row["status"],
            "symbol": row["symbol"],
            "side": row["side"],
            "quantity": int(row["quantity"]),
            "rationale": row["rationale"],
            "evidence_ids": self._parse(row["evidence_ids_json"]),
            "reference_price": row["reference_price"],
            "reference_quote_ref": row["reference_quote_ref"],
            "created_at": row["created_at"],
            "decided_at": row["decided_at"],
            "decision": row["decision"],
            "decision_reason": row["decision_reason"],
            "version": int(row["version"]),
        }

    def get_proposal_by_tool_call_id(self, run_id: str, tool_call_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT proposal_id FROM proposals WHERE run_id=? AND original_tool_call_id=?",
                (run_id, tool_call_id),
            ).fetchone()
        return self.get_proposal(row["proposal_id"]) if row else None

    def create_proposal(
        self,
        *,
        proposal_id: str,
        run_id: str,
        tool_call_id: str,
        symbol: str,
        side: str,
        quantity: int,
        rationale: str,
        evidence_ids: list[str],
        reference_price: str,
        reference_quote_ref: str,
        step: int,
    ) -> dict[str, Any]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                now = utc_now()
                self._conn.execute(
                    """INSERT INTO proposals
                    (proposal_id,run_id,original_tool_call_id,status,symbol,side,quantity,rationale,evidence_ids_json,reference_price,reference_quote_ref,created_at,version)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0)""",
                    (
                        proposal_id,
                        run_id,
                        tool_call_id,
                        "PENDING",
                        symbol,
                        side,
                        quantity,
                        rationale,
                        self._json(evidence_ids),
                        reference_price,
                        reference_quote_ref,
                        now,
                    ),
                )
                self._conn.execute(
                    "UPDATE runs SET status='WAITING_APPROVAL',pending_proposal_id=?,step=?,version=version+1,updated_at=? WHERE run_id=?",
                    (proposal_id, step, now, run_id),
                )
                row = self._conn.execute("SELECT data_mode,model_name FROM runs WHERE run_id=?", (run_id,)).fetchone()
                self._append_audit_locked(
                    run_id,
                    event_type="WAITING_APPROVAL",
                    step=step,
                    status="ok",
                    mode=row["data_mode"],
                    model=row["model_name"],
                    tool_call_id=tool_call_id,
                    payload={"proposal_id": proposal_id},
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return self.get_proposal(proposal_id) or {}

    def _receipt_for_proposal_locked(self, proposal: sqlite3.Row) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT content_json FROM messages WHERE run_id=? AND role='tool' AND tool_call_id=?",
            (proposal["run_id"], proposal["original_tool_call_id"]),
        ).fetchone()
        if row is None:
            return None
        content = self._parse(row["content_json"])
        try:
            return json.loads(content.get("content", "{}"))
        except (AttributeError, TypeError, json.JSONDecodeError):
            return None

    def _decision_view_locked(self, proposal: sqlite3.Row) -> DecisionView:
        approval = self._conn.execute(
            "SELECT * FROM approvals WHERE proposal_id=?", (proposal["proposal_id"],)
        ).fetchone()
        order = self._conn.execute(
            "SELECT * FROM orders WHERE proposal_id=?", (proposal["proposal_id"],)
        ).fetchone()
        run = self._conn.execute("SELECT * FROM runs WHERE run_id=?", (proposal["run_id"],)).fetchone()
        return DecisionView(
            run_id=proposal["run_id"],
            proposal_id=proposal["proposal_id"],
            decision=approval["decision"] if approval else (proposal["decision"] or "unknown"),
            proposal_status=proposal["status"],
            order_status=order["status"] if order else None,
            task_status=run["status"],
            receipt=self._receipt_for_proposal_locked(proposal),
            version=int(run["version"]),
        )

    def finalize_proposal(
        self,
        *,
        run_id: str,
        proposal_id: str,
        decision: str,
        expected_version: int | None,
        execution: dict[str, Any] | None,
        risk_revalidator: Callable[[sqlite3.Row, dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
        actor: str = "user",
    ) -> DecisionView:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                proposal = self._conn.execute(
                    "SELECT * FROM proposals WHERE proposal_id=? AND run_id=?",
                    (proposal_id, run_id),
                ).fetchone()
                if proposal is None:
                    raise KeyError(f"unknown proposal: {proposal_id}")
                if proposal["status"] != "PENDING":
                    view = self._decision_view_locked(proposal)
                    self._conn.execute("COMMIT")
                    return view
                run = self._conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
                if expected_version is not None and int(run["version"]) != expected_version:
                    self._conn.execute("ROLLBACK")
                    return DecisionView(
                        run_id=run_id,
                        proposal_id=proposal_id,
                        decision=decision,
                        proposal_status="PENDING",
                        order_status=None,
                        task_status=run["status"],
                        receipt=None,
                        version=int(run["version"]),
                        error={
                            "code": "VERSION_CONFLICT",
                            "message": f"expected version {expected_version}, current {run['version']}",
                            "retryable": False,
                        },
                    )
                if decision not in {"approve", "reject"}:
                    self._conn.execute("ROLLBACK")
                    raise ValueError("decision must be approve or reject")
                now = utc_now()
                order_status: str | None = None
                if decision == "reject":
                    proposal_status = "REJECTED"
                    receipt = ToolResult.failure(
                        "USER_REJECTED", "User rejected the paper order proposal."
                    ).to_dict()
                    decision_reason = "USER_REJECTED"
                else:
                    execution = execution or {"status": "BLOCKED", "blocked_code": "EXECUTION_UNAVAILABLE"}
                    order_status = execution.get("status", "BLOCKED")
                    proposal_status = "APPROVED" if order_status == "EXECUTED" else "BLOCKED"
                    blocked_code = execution.get("blocked_code")
                    if order_status == "EXECUTED":
                        if risk_revalidator is not None:
                            try:
                                account_state = self._account_state_locked()
                                risk = risk_revalidator(proposal, execution, account_state)
                            except Exception as exc:
                                risk = {
                                    "allowed": False,
                                    "error_code": "RISK_UNAVAILABLE",
                                    "checks": [],
                                    "reason": f"transactional risk recheck failed: {exc}",
                                }
                            if not isinstance(risk, dict) or risk.get("allowed") is not True:
                                order_status = "BLOCKED"
                                proposal_status = "BLOCKED"
                                blocked_code = str(
                                    (risk or {}).get("error_code")
                                    or self._risk_failure_code(risk)
                                    or "RISK_REJECTED"
                                )
                                execution = {
                                    **execution,
                                    "status": "BLOCKED",
                                    "blocked_code": blocked_code,
                                    "reason": (risk or {}).get("reason")
                                    or "Current account state failed deterministic risk checks.",
                                    "details": risk,
                                }
                        try:
                            if order_status == "EXECUTED":
                                self._apply_ledger_locked(proposal, execution, now)
                        except StorageError as exc:
                            # A concurrent account mutation can invalidate a
                            # pre-transaction risk snapshot. Persist a clear
                            # block instead of leaking a partial approval.
                            order_status = "BLOCKED"
                            proposal_status = "BLOCKED"
                            blocked_code = "ACCOUNT_STATE_CONFLICT"
                            execution = {
                                **execution,
                                "status": "BLOCKED",
                                "blocked_code": blocked_code,
                                "reason": str(exc),
                            }
                        if order_status == "EXECUTED":
                            receipt = ToolResult.success(
                                {
                                    "proposal_id": proposal_id,
                                    "status": "EXECUTED",
                                    "symbol": proposal["symbol"],
                                    "side": proposal["side"],
                                    "quantity": int(proposal["quantity"]),
                                    "price": execution.get("execution_price"),
                                    "quote_ref": execution.get("quote_ref"),
                                }
                            ).to_dict()
                            decision_reason = "EXECUTED"
                    if order_status != "EXECUTED":
                        receipt = ToolResult.failure(
                            str(blocked_code or "ORDER_BLOCKED"),
                            str(execution.get("reason") or "Paper order was blocked."),
                            details=execution.get("details"),
                        ).to_dict()
                        decision_reason = str(blocked_code or "ORDER_BLOCKED")
                        self._conn.execute(
                            """INSERT INTO orders(order_id,proposal_id,run_id,status,symbol,side,quantity,execution_price,execution_quote_ref,blocked_code,created_at)
                               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                            (
                                str(uuid.uuid4()),
                                proposal_id,
                                run_id,
                                "BLOCKED",
                                proposal["symbol"],
                                proposal["side"],
                                int(proposal["quantity"]),
                                execution.get("execution_price"),
                                execution.get("quote_ref"),
                                blocked_code,
                                now,
                            ),
                        )
                self._conn.execute(
                    "INSERT INTO approvals(approval_id,proposal_id,run_id,decision,expected_version,actor,created_at) VALUES(?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), proposal_id, run_id, decision, expected_version, actor, now),
                )
                self._conn.execute(
                    "UPDATE proposals SET status=?,decided_at=?,decision=?,decision_reason=?,version=version+1 WHERE proposal_id=?",
                    (proposal_status, now, decision, decision_reason, proposal_id),
                )
                receipt_message = {
                    "role": "tool",
                    "tool_call_id": proposal["original_tool_call_id"],
                    "name": "propose_paper_order",
                    "content": self._json(receipt),
                }
                self._append_message_locked(receipt_message.get("run_id", run_id), receipt_message, int(run["step"]))
                self._conn.execute(
                    "UPDATE runs SET status='RUNNING',pending_proposal_id=NULL,version=version+1,updated_at=? WHERE run_id=?",
                    (now, run_id),
                )
                row = self._conn.execute("SELECT data_mode,model_name,step FROM runs WHERE run_id=?", (run_id,)).fetchone()
                self._append_audit_locked(
                    run_id,
                    event_type="PROPOSAL_DECIDED",
                    step=int(row["step"]),
                    status="ok" if proposal_status != "BLOCKED" else "blocked",
                    mode=row["data_mode"],
                    model=row["model_name"],
                    tool_call_id=proposal["original_tool_call_id"],
                    error_code=None if proposal_status != "BLOCKED" else decision_reason,
                    payload={"proposal_id": proposal_id, "decision": decision, "receipt": receipt},
                )
                self._conn.execute("COMMIT")
                proposal = self._conn.execute("SELECT * FROM proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
                return self._decision_view_locked(proposal)
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    @staticmethod
    def _risk_failure_code(risk: Any) -> str | None:
        if not isinstance(risk, dict):
            return None
        code = risk.get("error_code")
        return str(code) if code else None

    def _account_state_locked(self) -> dict[str, Any]:
        account = self._conn.execute(
            "SELECT * FROM paper_accounts WHERE account_id='paper-default'"
        ).fetchone()
        rows = self._conn.execute(
            "SELECT symbol,quantity,average_price FROM positions WHERE account_id='paper-default' ORDER BY symbol"
        ).fetchall()
        return {
            "cash": account["cash"],
            "currency": account["currency"],
            "positions": [
                {
                    "symbol": row["symbol"],
                    "quantity": int(row["quantity"]),
                    "average_price": row["average_price"],
                    "market_value": format(Decimal(row["average_price"]) * int(row["quantity"]), "f"),
                }
                for row in rows
            ],
        }

    def _apply_ledger_locked(self, proposal: sqlite3.Row, execution: dict[str, Any], now: str) -> None:
        account = self._conn.execute(
            "SELECT * FROM paper_accounts WHERE account_id='paper-default'"
        ).fetchone()
        price = Decimal(str(execution.get("execution_price")))
        cash_delta = Decimal(str(execution.get("cash_delta", "0")))
        quantity_delta = int(execution.get("quantity_delta", 0))
        expected_quantity_delta = int(proposal["quantity"]) if proposal["side"] == "BUY" else -int(proposal["quantity"])
        if quantity_delta != expected_quantity_delta:
            raise StorageError("financial engine returned an invalid quantity delta")
        expected_cash_delta = price * int(proposal["quantity"])
        if proposal["side"] == "BUY":
            expected_cash_delta = -expected_cash_delta
        if cash_delta != expected_cash_delta:
            raise StorageError("financial engine returned an invalid cash delta")
        new_cash = Decimal(account["cash"]) + cash_delta
        if new_cash < 0:
            raise StorageError("ledger execution would make cash negative")
        position = self._conn.execute(
            "SELECT * FROM positions WHERE account_id='paper-default' AND symbol=?",
            (proposal["symbol"],),
        ).fetchone()
        old_quantity = int(position["quantity"]) if position else 0
        new_quantity = old_quantity + quantity_delta
        if new_quantity < 0:
            raise StorageError("ledger execution would create a short position")
        if new_quantity == 0:
            self._conn.execute(
                "DELETE FROM positions WHERE account_id='paper-default' AND symbol=?",
                (proposal["symbol"],),
            )
        else:
            old_avg = Decimal(position["average_price"]) if position else Decimal("0")
            if proposal["side"] == "BUY":
                new_avg = ((old_avg * old_quantity) + (price * int(proposal["quantity"]))) / new_quantity
            else:
                new_avg = old_avg
            self._conn.execute(
                """INSERT INTO positions(account_id,symbol,quantity,average_price,updated_at) VALUES(?,?,?,?,?)
                   ON CONFLICT(account_id,symbol) DO UPDATE SET quantity=excluded.quantity,average_price=excluded.average_price,updated_at=excluded.updated_at""",
                ("paper-default", proposal["symbol"], new_quantity, format(new_avg, "f"), now),
            )
        order_id = str(uuid.uuid4())
        self._conn.execute(
            """INSERT INTO orders(order_id,proposal_id,run_id,status,symbol,side,quantity,execution_price,execution_quote_ref,blocked_code,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                order_id,
                proposal["proposal_id"],
                proposal["run_id"],
                "EXECUTED",
                proposal["symbol"],
                proposal["side"],
                int(proposal["quantity"]),
                format(price, "f"),
                execution.get("quote_ref"),
                None,
                now,
            ),
        )
        self._conn.execute(
            "INSERT INTO ledger_entries(entry_id,order_id,account_id,symbol,cash_delta,quantity_delta,price,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                str(uuid.uuid4()),
                order_id,
                "paper-default",
                proposal["symbol"],
                format(cash_delta, "f"),
                quantity_delta,
                format(price, "f"),
                now,
            ),
        )
        self._conn.execute(
            "UPDATE paper_accounts SET cash=?,version=version+1,updated_at=? WHERE account_id='paper-default'",
            (format(new_cash, "f"), now),
        )

    def get_decision(self, run_id: str, proposal_id: str) -> DecisionView | None:
        with self._lock:
            proposal = self._conn.execute(
                "SELECT * FROM proposals WHERE proposal_id=? AND run_id=?",
                (proposal_id, run_id),
            ).fetchone()
            if proposal is None or proposal["status"] == "PENDING":
                return None
            return self._decision_view_locked(proposal)

    def get_portfolio(self) -> dict[str, Any]:
        with self._lock:
            account = self._conn.execute(
                "SELECT * FROM paper_accounts WHERE account_id='paper-default'"
            ).fetchone()
            positions = self._conn.execute(
                "SELECT symbol,quantity,average_price FROM positions WHERE account_id='paper-default' ORDER BY symbol"
            ).fetchall()
        cash = Decimal(account["cash"])
        position_views = []
        equity = cash
        for row in positions:
            value = Decimal(row["average_price"]) * int(row["quantity"])
            equity += value
            position_views.append(
                {
                    "symbol": row["symbol"],
                    "quantity": int(row["quantity"]),
                    "average_price": format(Decimal(row["average_price"]), "f"),
                    "market_value": format(value, "f"),
                }
            )
        return {
            "cash": format(cash, "f"),
            "equity": format(equity, "f"),
            "currency": account["currency"],
            "positions": position_views,
            "updated_at": account["updated_at"],
            "valuation_ref": "paper-average-price",
        }

    def try_acquire_run_lease(self, run_id: str, owner_id: str, ttl_seconds: float = 30.0) -> bool:
        now = datetime.now(timezone.utc).timestamp()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute("SELECT * FROM run_leases WHERE run_id=?", (run_id,)).fetchone()
                if row and row["owner_id"] != owner_id and float(row["expires_at"]) > now:
                    self._conn.execute("ROLLBACK")
                    return False
                self._conn.execute(
                    "INSERT INTO run_leases(run_id,owner_id,expires_at) VALUES(?,?,?) ON CONFLICT(run_id) DO UPDATE SET owner_id=excluded.owner_id,expires_at=excluded.expires_at",
                    (run_id, owner_id, now + ttl_seconds),
                )
                self._conn.execute("COMMIT")
                return True
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def renew_run_lease(self, run_id: str, owner_id: str, ttl_seconds: float = 30.0) -> bool:
        now = datetime.now(timezone.utc).timestamp()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT owner_id,expires_at FROM run_leases WHERE run_id=?", (run_id,)
                ).fetchone()
                if row is None or row["owner_id"] != owner_id or float(row["expires_at"]) <= now:
                    self._conn.execute("ROLLBACK")
                    return False
                self._conn.execute(
                    "UPDATE run_leases SET expires_at=? WHERE run_id=? AND owner_id=?",
                    (now + ttl_seconds, run_id, owner_id),
                )
                self._conn.execute("COMMIT")
                return True
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def release_run_lease(self, run_id: str, owner_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM run_leases WHERE run_id=? AND owner_id=?",
                (run_id, owner_id),
            )
