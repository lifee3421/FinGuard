# FinGuard — Technical and product design notes

## Problem to solve

An AI research assistant can produce a polished recommendation while hiding data assumptions, failed tool calls or execution authority. FinGuard separates **research**, **trace**, **risk**, **approval**, and **paper bookkeeping** so a user can inspect the path from evidence to action.

## Responsibilities

- **Core** — application orchestration, model turn loop, typed tool-call validation, event store, approval lifecycle, lease/heartbeat, and SQLite persistence.
- **Finance** — synthetic market provider, deterministic indicator and risk calculation; does not write the ledger directly.
- **Interfaces** — CLI and FastAPI routes are adapters over the same `ApplicationService`. The browser does not store API secrets or mutate portfolio balances.

## User journey and state

1. Create a research run with a question and optional permission to **propose** a paper order.
2. Dispatch model-requested tools by registry and schema; persist receipts and events.
3. If the model proposes an order, persist a `WAITING_APPROVAL` checkpoint with a proposal identifier.
4. On user approval, re-read a trusted current quote, re-evaluate hard risk restrictions, and apply only an accepted paper transaction to SQLite. Handle duplicate approvals idempotently.
5. Resume model execution with the original tool-call receipt; expose state and event history through the API.

Tool calls **do not** grant permission to transact. A paper-order proposal is explicitly distinct from a committed transaction; no real broker service is connected.

## Product decisions and trade-offs

| Decision | Rationale / trade-off |
| --- | --- |
| Separate model reasoning from deterministic risk | A model suggestion is not a risk-control result |
| Explicit user approval checkpoint | Makes control visible and testable, at the cost of manual intervention |
| SQLite event history and ledger | Small, inspectable local persistence; not production multi-tenant infrastructure |
| Fake model fallback only for smoke checks | UI/backend can be explored without credentials, but mock output must not be presented as a real LLM run |
| Expose status and technical Trace separately | Helps users understand the workflow without losing engineering observability |
| Demo-market fixtures by default | Reproducible tests; not current investment-grade information |

## What is not built

Production authentication, hosted multi-user service, actual exchange/broker connectivity, guaranteed real-time quote service, autonomous trading authorization, and evidence-backed performance or returns. The UI may include visible conceptual designs for future capabilities, but they are not equivalent to a shipped backend.
