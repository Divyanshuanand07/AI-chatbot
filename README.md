# Cars24 AI Operations Assistant

A backend service that answers natural-language questions from the operations
team about vehicle orders — payment status, delivery, customer details, order
history, and **why an order is stuck** — by calling read-only tools against
PostgreSQL.

The LLM extracts intent, selects tools, and writes the answer. It is never the
source of truth. Every operational fact in every answer comes from a database
lookup, and the response lists exactly which lookups ran.

```
Operator → Django/DRF → Orchestrator → LLM (tool calling) → Tools → Selectors
                              ↑                                        ↓
                              └──────── tool results ←──── PostgreSQL / pgvector
```

---

## Table of contents

- [Quick start](#quick-start)
- [What it does](#what-it-does)
- [Design decisions that matter](#design-decisions-that-matter)
- [API](#api)
- [Roles and permissions](#roles-and-permissions)
- [Configuration](#configuration)
- [Testing](#testing)
- [Deviations from the brief](#deviations-from-the-brief)
- [Limitations and next steps](#limitations-and-next-steps)

Design rationale lives in [ARCHITECTURE.md](ARCHITECTURE.md).

---

## Quick start

### Docker (recommended)

```bash
docker compose up --build
```

Brings up PostgreSQL 17 + pgvector, Redis, and the app on
**http://localhost:8000**. On first boot it migrates, seeds 200 realistic
orders, and indexes the SOP corpus.

Then run the scripted walkthrough of all six use cases:

```bash
python scripts/demo.py
```

### Local (no Docker)

Requires PostgreSQL 17 with the `pgvector` extension and Python 3.12.

```bash
# One-time: make the extension available to new databases (including the
# test database, which Django creates from template1).
psql -d template1 -c "CREATE EXTENSION IF NOT EXISTS vector;"
createdb ops_ai

make install          # virtualenv + dependencies
cp .env.example .env  # defaults work as-is
make migrate
make seed             # 200 orders, 379 payments, 3,755 timeline events
make knowledge        # index 7 SOP/policy documents into pgvector
make run
```

Neither path needs an API key. The defaults (`LLM_PROVIDER=stub`,
`EMBEDDING_PROVIDER=hashing`) run the whole stack offline and free. Set
`LLM_PROVIDER=anthropic` and `ANTHROPIC_API_KEY=...` for live model calls.


## What it does

Six use cases, all driven end to end by `scripts/demo.py`:

| # | Use case | Example | How it is answered |
|---|---|---|---|
| 1 | Simple lookup | *"What is the payment status of order #1243?"* | One tool call against the payments ledger |
| 2 | Multi-fact summary | *"Give me a complete summary of order #2325"* | One composite tool instead of six round trips |
| 3 | Root cause | *"Why is order #2325 stuck?"* | Deterministic rule engine; the model only narrates |
| 4 | Narrative + follow-up | *"What happened with it?"* → *"and what about its payment?"* | Order id resolved from conversation context, then re-read from the database |
| 5 | Policy / terminology | *"What does a PENDING payment status mean?"* | pgvector search over the SOP corpus, with the source cited |
| 6 | Guardrails | unknown order, missing order id, insufficient role | Says so; never invents a plausible order or policy |

### Tools the model can call

All read-only. Each declares the scopes it needs, and the orchestrator filters
the list before the model ever sees it.

| Tool | Required scopes |
|---|---|
| `get_order` | `orders:read` |
| `get_payment_status` | `payments:read` |
| `get_customer` | `customers:read` (contact details additionally need `customers:pii`) |
| `get_vehicle` | `vehicles:read` |
| `get_order_timeline` | `events:read` |
| `get_delivery_status` | `delivery:read` |
| `get_order_documents` | `orders:read` |
| `get_finance_status` | `finance:read` |
| `get_order_summary` | `orders:read` + `payments:read` + `customers:read` |
| `diagnose_order_blockers` | `diagnostics:read` |
| `find_orders` | `orders:read` |
| `search_knowledge_base` | `knowledge:read` |

`GET /api/ai/tools/` returns this list for the calling role, including which
tools are **withheld** and which scope is missing — so "why wouldn't it answer
that?" is answerable from the API rather than from the source.

---

## API

All endpoints require `Authorization: Bearer <access token>` except the
probes and `/api/auth/login/`.

### Assistant

| Method | Path | Notes |
|---|---|---|
| `POST` | `/api/ai/query/` | The endpoint operators use. Throttled at `THROTTLE_AI_QUERY` (30/min) |
| `GET` | `/api/ai/tools/` | Tools available to my role, and those withheld |
| `GET` | `/api/ai/conversations/` | My conversations |
| `GET` | `/api/ai/conversations/{id}/` | Full transcript with token usage and latency |
| `DELETE` | `/api/ai/conversations/{id}/` | Close it; history is retained as an audit record |
| `GET` | `/api/ai/audit/` | Who asked what, and which tools ran — needs `audit:read` |

**Request**

```json
{"question": "Why is order #2325 stuck?", "conversation_id": null}
```

`conversation_id` is optional; omit it to start a new conversation, pass it to
make follow-ups resolve against context.

**Response**

```json
{
  "answer": "Order #2325 is blocked on document verification...",
  "conversation_id": "0f5c...",
  "trace_id": "9b21...",
  "tools_used": [
    {"tool": "diagnose_order_blockers", "ok": true, "duration_ms": 41.2,
     "cache_hit": false, "error_code": null}
  ],
  "iterations": 2,
  "usage": {"input_tokens": 4120, "output_tokens": 310, "cache_read_tokens": 3900},
  "provider": "stub",
  "duration_ms": 812.4,
  "order_ids": [2325],
  "warnings": []
}
```

`warnings` is the honest channel. Possible values include
`ungrounded_answer:money_without_tool_result` (the answer quotes rupee amounts
though no tool succeeded — flagged, never silently rewritten),
`tool_iteration_budget_exhausted`, `answer_truncated_at_max_tokens`,
`model_refusal:<category>` and `empty_model_answer`.

### Operational read API

Same selectors the tools use. All gated by scope, all throttled at
`THROTTLE_READ_API` (600/min).

| Method | Path | Scope |
|---|---|---|
| `GET` | `/api/orders/?phone=&registration_number=&customer_name=&status=&hub=&limit=` | `orders:read` |
| `GET` | `/api/orders/{id}/` | `orders:read` |
| `GET` | `/api/orders/{id}/summary/` | `orders:read` + `payments:read` + `customers:read` |
| `GET` | `/api/orders/{id}/payments/` | `payments:read` |
| `GET` | `/api/orders/{id}/customer/` | `customers:read` (+ `customers:pii` to unmask) |
| `GET` | `/api/orders/{id}/vehicle/` | `vehicles:read` |
| `GET` | `/api/orders/{id}/timeline/` | `events:read` |
| `GET` | `/api/orders/{id}/delivery/` | `delivery:read` |
| `GET` | `/api/orders/{id}/documents/` | `orders:read` |
| `GET` | `/api/orders/{id}/finance/` | `finance:read` |
| `GET` | `/api/orders/{id}/diagnosis/` | `diagnostics:read` |

### Knowledge base

| Method | Path | Scope |
|---|---|---|
| `GET` | `/api/knowledge/search/?q=&category=&top_k=` | `knowledge:read` |
| `GET` | `/api/knowledge/documents/?category=` | `knowledge:read` |

### Auth and probes

| Method | Path | Notes |
|---|---|---|
| `POST` | `/api/auth/login/` | Returns `access` + `refresh` (JWT) |
| `POST` | `/api/auth/refresh/` | Rotate the access token |
| `POST` | `/api/auth/verify/` | Validate a token |
| `GET` | `/api/auth/me/` | Current user, role and scopes |
| `GET` | `/healthz` | Liveness. Checks no dependency, by design |
| `GET` | `/readyz` | Readiness. Database is fatal, cache is reported but degrades gracefully |

### Errors

Every failure — expected or not — uses one envelope:

```json
{"error": {"code": "not_found",
           "message": "No such order.",
           "details": {"order_id": "987654"}},
 "trace_id": "9b21..."}
```

Codes: `validation_error`, `unauthenticated`, `forbidden`, `not_found`,
`method_not_allowed`, `rate_limited`, `upstream_error` (the model provider
failed — 502, not a generic 500), `internal_error`. The trace id is also
returned as the `X-Trace-Id` header and recorded on the audit row.

---