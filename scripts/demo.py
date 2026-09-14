#!/usr/bin/env python
"""
Scripted end-to-end demo of the six core use cases.

    python scripts/demo.py                       # against localhost:8000
    python scripts/demo.py --base-url http://localhost:8009

Drives the real HTTP API exactly as an operator's client would: logs in,
asks questions, and prints the answer along with the tools that actually ran.
The tool list is the point — it shows every figure in the answer came from a
database lookup rather than from the model.

Uses only the standard library so it runs anywhere, including inside the
container.
"""

from __future__ import annotations

import argparse
import json
import sys
import textwrap
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "http://localhost:8000"
PASSWORD = "opsai12345"

BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
RED = "\033[31m"
RESET = "\033[0m"


def request(url: str, *, token: str | None = None, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        try:
            return {"_http_error": exc.code, **json.loads(body)}
        except ValueError:
            return {"_http_error": exc.code, "raw": body[:400]}
    except urllib.error.URLError as exc:
        print(f"{RED}Cannot reach {url}: {exc.reason}{RESET}")
        print("Is the server running?  make run   (or: docker compose up)")
        sys.exit(1)


def login(base_url: str, username: str) -> str:
    body = request(
        f"{base_url}/api/auth/login/",
        payload={"username": username, "password": PASSWORD},
    )
    if "access" not in body:
        print(f"{RED}Login failed for {username}: {body}{RESET}")
        print("Has the demo data been seeded?  make seed")
        sys.exit(1)
    return body["access"]


def ask(base_url: str, token: str, question: str, conversation_id=None) -> dict:
    payload = {"question": question}
    if conversation_id:
        payload["conversation_id"] = conversation_id
    return request(f"{base_url}/api/ai/query/", token=token, payload=payload)


def show(question: str, result: dict, *, note: str = "") -> None:
    print(f"\n{BOLD}{CYAN}Q: {question}{RESET}")
    if note:
        print(f"{DIM}   ({note}){RESET}")

    if "_http_error" in result:
        print(f"{RED}   HTTP {result['_http_error']}: {result.get('error')}{RESET}")
        return

    tools = result.get("tools_used", [])
    if tools:
        rendered = ", ".join(
            f"{t['tool']}"
            + ("" if t["ok"] else f"{RED}[{t['error_code']}]{RESET}")
            + f"{DIM}({t['duration_ms']:.0f}ms){RESET}"
            for t in tools
        )
    else:
        rendered = f"{YELLOW}none — answered without touching the database{RESET}"
    print(f"{DIM}   tools:{RESET} {rendered}")

    warnings = result.get("warnings") or []
    if warnings:
        print(f"{YELLOW}   warnings: {', '.join(warnings)}{RESET}")

    print()
    for line in result.get("answer", "").splitlines():
        print(textwrap.indent(line, "   "))


def section(title: str) -> None:
    print(f"\n\n{BOLD}{'=' * 72}{RESET}")
    print(f"{BOLD}{title}{RESET}")
    print(f"{BOLD}{'=' * 72}{RESET}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--order-id", type=int, default=2325)
    parser.add_argument("--payment-order-id", type=int, default=1243)
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    stuck = args.order_id
    paid = args.payment_order_id

    health = request(f"{base}/readyz")
    print(f"{DIM}Service: {base} -> {health}{RESET}")

    agent_token = login(base, "agent")
    viewer_token = login(base, "viewer")
    manager_token = login(base, "manager")

    # ------------------------------------------------------------------
    section("1. Simple lookup — payment status")
    show(
        f"What is the payment status of order #{paid}?",
        ask(base, agent_token, f"What is the payment status of order #{paid}?"),
    )

    # ------------------------------------------------------------------
    section("2. Multi-tool — complete order summary")
    show(
        f"Give me a complete summary of order #{stuck}",
        ask(base, agent_token, f"Give me a complete summary of order #{stuck}"),
        note="one composite tool call instead of six separate lookups",
    )

    # ------------------------------------------------------------------
    section("3. Reasoning — why is the order stuck?")
    show(
        f"Why is order #{stuck} stuck?",
        ask(base, agent_token, f"Why is order #{stuck} stuck?"),
        note="root cause from a deterministic rule engine, not model inference",
    )

    # ------------------------------------------------------------------
    section("4. Narrative — what happened, then a follow-up")
    first = ask(base, agent_token, f"What happened with order #{stuck}?")
    show(f"What happened with order #{stuck}?", first)

    conversation_id = first.get("conversation_id")
    show(
        "and what about its payment?",
        ask(base, agent_token, "and what about its payment?", conversation_id),
        note="no order number in the question — resolved from conversation context",
    )

    # ------------------------------------------------------------------
    section("5. RAG — policy and terminology questions")
    show(
        "What does a PENDING payment status mean?",
        ask(base, agent_token, "What does a PENDING payment status mean?"),
        note="answered from the indexed SOP corpus, with the source cited",
    )
    show(
        "What is our policy on flying drones over the hub?",
        ask(base, agent_token, "What is our policy on flying drones over the hub?"),
        note="undocumented topic — must refuse rather than invent a policy",
    )

    # ------------------------------------------------------------------
    section("6. Guardrails — missing data and authorization")
    show(
        "What is the payment status of order #987654?",
        ask(base, agent_token, "What is the payment status of order #987654?"),
        note="order does not exist — must say so, not describe a plausible one",
    )
    show(
        "What is the payment status?",
        ask(base, agent_token, "What is the payment status?"),
        note="no order id anywhere — must ask instead of guessing",
    )
    show(
        f"Why is order #{stuck} stuck?",
        ask(base, viewer_token, f"Why is order #{stuck} stuck?"),
        note="asked as 'viewer', whose role cannot run diagnostics",
    )

    # ------------------------------------------------------------------
    section("7. Audit trail")
    audit = request(f"{base}/api/ai/audit/?limit=5", token=manager_token)
    if "_http_error" in audit:
        print(f"{RED}   {audit}{RESET}")
    else:
        print(f"\n   {audit['count']} audit entries recorded. Most recent:\n")
        for entry in audit["results"][:5]:
            tools = ", ".join(t["tool_name"] for t in entry["tools_called"]) or "—"
            print(
                f"   {DIM}{entry['created_at'][:19]}{RESET} "
                f"{entry['username']:<8} {entry['role']:<12} "
                f"{GREEN if entry['succeeded'] else RED}"
                f"{'ok' if entry['succeeded'] else 'fail'}{RESET}  "
                f"orders={entry['order_ids_touched']}  tools={tools}"
            )
        print(
            f"\n   {DIM}As 'agent' this endpoint returns 403 — audit:read is "
            f"restricted to managers.{RESET}"
        )

    print(f"\n{GREEN}{BOLD}Demo complete.{RESET}\n")


if __name__ == "__main__":
    main()
