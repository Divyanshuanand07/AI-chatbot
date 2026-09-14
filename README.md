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