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

### Demo accounts

Seeding creates four users, one per role, all with password `opsai12345`:

| Username | Role | For |
|---|---|---|
| `viewer` | `viewer` | Showing what a restricted role cannot reach |
| `agent` | `ops_agent` | The main operator account |
| `manager` | `ops_manager` | Adds the audit trail |
| `admin` | `admin` | Everything |

Orders **#1243** (partial payment with a failed transaction) and **#2325**
(multi-blocker stuck order) are pinned by the seeder, so the demo questions
always land on meaningful data.

### Ask it something

```bash
TOKEN=$(curl -s localhost:8000/api/auth/login/ \
  -H 'Content-Type: application/json' \
  -d '{"username":"agent","password":"opsai12345"}' | jq -r .access)

curl -s localhost:8000/api/ai/query/ \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"question":"Why is order #2325 stuck?"}' | jq
```

Interactive API docs: **http://localhost:8000/api/docs/** (OpenAPI schema at
`/api/schema/`).

---

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

## Design decisions that matter

Full reasoning in [ARCHITECTURE.md](ARCHITECTURE.md). The short version:

- **The LLM never supplies a fact.** It picks tools and writes prose. Every
  number, date, name and status originates from a PostgreSQL row.
- **Tools and the REST API share one read layer**
  ([selectors.py](apps/operations/selectors.py)). There is no query path open
  to the assistant that is not also open to a human with the same role.
- **Blocker analysis is a rule engine, not inference**
  ([diagnostics.py](apps/operations/diagnostics.py)). ~15 rules, each with
  severity, evidence, an owning team and a suggested action. Same order in,
  same findings out. Causes are ranked above their symptoms, so an operator is
  sent to Documentation rather than Logistics for a delivery delay caused by a
  rejected PAN.
- **Citations come from execution records**, not from the model's narration —
  `tools_used` lists what actually ran, with latency and cache-hit per call.
- **Withholding beats refusing.** A viewer's model is never shown
  `diagnose_order_blockers`, so there is nothing to jailbreak it into calling.
  The executor re-checks scopes anyway.
- **Money and dates are pre-formatted** as `{"value", "display"}` before they
  reach the model, so it never does arithmetic or mangles lakh/crore grouping.
- **RAG refuses on undocumented topics** via two independent guards: a
  per-backend similarity floor and a vocabulary gate. Calibrated by
  `make calibrate` — recall 19/19, off-topic leakage 0/11.
- **Follow-ups re-read the database.** Conversation context carries the last
  resolved order id, never cached tool results; serving a payment status
  fetched four turns ago is a correctness bug, not an optimisation.
- **The prompt is split for cache stability**: a byte-identical rule block
  first, volatile role/date/context second, tools sorted by name. Tests assert
  both properties.

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

## Roles and permissions

Roles expand to scopes; scopes are the single authorization vocabulary for the
REST API, the tool layer and field-level PII masking. Each role is a superset
of the one above.

| Scope | `viewer` | `ops_agent` | `ops_manager` | `admin` |
|---|:--:|:--:|:--:|:--:|
| `orders:read` | ✅ | ✅ | ✅ | ✅ |
| `payments:read` | ✅ | ✅ | ✅ | ✅ |
| `customers:read` | ✅ | ✅ | ✅ | ✅ |
| `vehicles:read` | ✅ | ✅ | ✅ | ✅ |
| `events:read` | ✅ | ✅ | ✅ | ✅ |
| `delivery:read` | ✅ | ✅ | ✅ | ✅ |
| `knowledge:read` | ✅ | ✅ | ✅ | ✅ |
| `customers:pii` | — | ✅ | ✅ | ✅ |
| `finance:read` | — | ✅ | ✅ | ✅ |
| `diagnostics:read` | — | ✅ | ✅ | ✅ |
| `audit:read` | — | — | ✅ | ✅ |

Masking is decided by the caller's scopes, not by the request, and the payload
says why — so the assistant can explain the restriction instead of pretending
the data does not exist:

```json
{"phone": "******9056", "pii_visible": false,
 "pii_note": "Contact details are masked because the requesting role lacks
              the 'customers:pii' scope. Do not guess the hidden digits."}
```

---

## Configuration

Everything is environment-driven; `cp .env.example .env` works as-is. Full
annotated list in [.env.example](.env.example). The settings that change
behaviour most:

| Variable | Default | What it controls |
|---|---|---|
| `DATABASE_URL` | `postgres://ops_ai:ops_ai_pw@localhost:5432/ops_ai` | PostgreSQL 17 + pgvector. Required |
| `REDIS_URL` | `redis://localhost:6379/0` | Tool cache. Blank falls back to in-process memory |
| `LLM_PROVIDER` | `stub` | `stub` = deterministic, offline, free. `anthropic` = live calls |
| `ANTHROPIC_API_KEY` | — | Required only for `LLM_PROVIDER=anthropic` |
| `LLM_MODEL` | `claude-opus-5` | Model id |
| `LLM_MAX_TOOL_ITERATIONS` | `6` | Hard ceiling on the tool-calling loop |
| `LLM_HISTORY_TURNS` | `8` | Conversation turns replayed into the prompt |
| `LLM_MAX_RETRIES` / `LLM_TIMEOUT_SECONDS` | `2` / `120` | Provider retry and timeout budget |
| `TOOL_TIMEOUT_SECONDS` | `8` | Per-tool wall clock; returns a `TIMEOUT` envelope |
| `TOOL_MAX_PARALLEL` | `6` | Concurrent tool execution within one turn |
| `TOOL_CACHE_ENABLED` | `True` | Per-tool result caching |
| `EMBEDDING_PROVIDER` | `hashing` | `hashing` = local and free; `voyage` needs `VOYAGE_API_KEY` |
| `EMBEDDING_DIM` | `512` | Must match the indexed corpus — re-run `make knowledge` if changed |
| `KNOWLEDGE_TOP_K` | `4` | Passages returned per search |
| `KNOWLEDGE_MIN_SIMILARITY` | `0.25` | Floor below which retrieval refuses |
| `THROTTLE_AI_QUERY` / `THROTTLE_READ_API` | `30/min` / `600/min` | Rate limits |
| `LOG_FORMAT` | `console` | Use `json` in production |

**SLA thresholds** drive the blocker rule engine and are tunable without
touching code: `SLA_STALE_EVENT_DAYS` (3), `SLA_DOCS_VERIFICATION_DAYS` (2),
`SLA_DOCS_COLLECTION_DAYS` (3), `SLA_PAYMENT_PENDING_DAYS` (2),
`SLA_FINANCE_REVIEW_DAYS` (3), `SLA_RC_TRANSFER_DAYS` (21),
`SLA_DELIVERY_ATTEMPT_LIMIT` (2), `SLA_REFUND_DAYS` (7).

---

## Testing

**220 tests, all passing.** The suite is hermetic — no network, no API key, no
cost — because the stub model provider and hashing embedder are real
implementations rather than mocks.

```bash
make test     # pytest
make cov      # with a term-missing coverage report
make lint     # ruff
make fmt      # ruff --fix + format
```

| File | Tests | Covers |
|---|--:|---|
| [tests/test_selectors.py](tests/test_selectors.py) | 42 | The read layer — the single source of operational truth |
| [tests/test_orchestrator.py](tests/test_orchestrator.py) | 38 | Tool loop, budgets, grounding checks, prompt-cache stability |
| [tests/test_api.py](tests/test_api.py) | 36 | HTTP contract, auth, scope enforcement, error envelope |
| [tests/test_knowledge.py](tests/test_knowledge.py) | 34 | Chunking, embedding signatures, retrieval and refusal guards |
| [tests/test_tools.py](tests/test_tools.py) | 27 | Registry, scope filtering, caching, timeouts |
| [tests/test_diagnostics.py](tests/test_diagnostics.py) | 25 | The rule engine — roughly half assert an order is **not** stuck |
| [tests/test_seed.py](tests/test_seed.py) | 18 | Seed integrity: the timeline can never contradict the ledger |

Two things worth knowing about how these are written:

- **Not crying wolf is tested as hard as detection.** An early rule engine
  flagged every new order because "mandatory documents not uploaded" was
  unconditionally high severity — which is the normal state of a one-day-old
  order. Age-grading took flagged orders from 66% to 52%.
- **Retrieval quality is measured, not asserted by eye.** `make calibrate`
  runs `manage.py calibrate_knowledge` over a labelled query set and reports
  recall and off-topic leakage (currently 19/19 and 0/11).

The local (non-Docker) path needs `vector` available in `template1`, because
Django builds the test database from it — see [Quick start](#quick-start).
