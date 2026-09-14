"""
Templates that turn tool results into prose, for the offline driver.

Kept separate from routing so `stub_provider` stays about *decisions* and this
file stays about *wording*. Every renderer reads only pre-formatted values
(`money["display"]`, `*_label`, `humanize_age` strings) — it never does
arithmetic or number formatting, exactly as the real model is instructed not
to. If a figure is wrong here, the bug is in the selector, not the phrasing.
"""

from __future__ import annotations

import json


def _money(value) -> str:
    if isinstance(value, dict):
        return value.get("display") or "—"
    return str(value) if value is not None else "—"


def _bullet(lines: list[str]) -> str:
    return "\n".join(f"- {line}" for line in lines if line)


def render_tool_result(block: dict) -> str:
    """Render one tool_result content block."""
    raw = block.get("content")
    if isinstance(raw, list):  # content may be a list of text blocks
        raw = "".join(
            part.get("text", "") for part in raw if isinstance(part, dict)
        )
    try:
        payload = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except (TypeError, ValueError):
        return "A lookup returned data I could not read."

    tool = payload.get("tool", "")

    if not payload.get("ok", False):
        return _render_error(tool, payload.get("error") or {})

    data = payload.get("data") or {}
    renderer = _RENDERERS.get(tool, _render_generic)
    return renderer(data)


# ---------------------------------------------------------------------------
def _render_error(tool: str, error: dict) -> str:
    code = error.get("code", "ERROR")
    message = error.get("message", "The lookup failed.")

    if code == "ORDER_NOT_FOUND":
        return (
            f"{message} I have not found any record of it, so there is nothing "
            f"I can report. Please double-check the order number."
        )
    if code == "INVALID_ORDER_ID":
        return f"{message} Please confirm the order number you meant."
    if code == "FORBIDDEN":
        return (
            "Your role does not have access to that information, so I cannot "
            "retrieve it. Ask an operations manager if you need it."
        )
    if code in ("TIMEOUT", "INTERNAL_ERROR"):
        return (
            f"{message} I would rather tell you the lookup failed than guess "
            f"at the answer."
        )
    if code == "INVALID_SEARCH":
        return message
    return f"The {tool or 'lookup'} failed: {message}"


# ---------------------------------------------------------------------------
def _render_order(d: dict) -> str:
    lines = [
        f"Status: {d.get('status_label')} ({d.get('status')})",
        f"Lifecycle: {d.get('lifecycle_step')}",
        f"Total: {_money(d.get('total_amount'))} | "
        f"Paid: {_money(d.get('amount_paid'))} | "
        f"Due: {_money(d.get('amount_due'))}",
        f"In this status for {d.get('days_in_current_status')} day(s) "
        f"(last changed {d.get('status_last_changed')})",
        f"Hub: {d.get('hub')} | Agent: {d.get('assigned_agent') or 'unassigned'}",
    ]
    if d.get("expected_delivery_date"):
        lines.append(f"Promised delivery: {d['expected_delivery_date']}")
    if d.get("hold_reason"):
        lines.append(f"On hold: {d['hold_reason']}")
    if d.get("cancellation_reason"):
        lines.append(f"Cancelled: {d['cancellation_reason']}")

    return f"**Order #{d.get('order_id')}**\n{_bullet(lines)}"


def _render_payment(d: dict) -> str:
    counts = d.get("counts") or {}
    lines = [
        f"Total order value: {_money(d.get('total_amount'))}",
        f"Received: {_money(d.get('amount_paid'))}",
        f"Still due: {_money(d.get('amount_due'))}",
    ]
    if (d.get("amount_refunded") or {}).get("value"):
        lines.append(f"Refunded: {_money(d.get('amount_refunded'))}")
    lines.append(
        f"Transactions: {counts.get('total', 0)} "
        f"({counts.get('settled', 0)} settled, {counts.get('failed', 0)} failed, "
        f"{counts.get('in_flight', 0)} awaiting confirmation)"
    )

    text = (
        f"**Payment status — order #{d.get('order_id')}**\n"
        f"{d.get('summary')}\n{_bullet(lines)}"
    )

    latest = d.get("latest_payment")
    if latest:
        detail = (
            f"\n\nMost recent transaction: {latest.get('reference')} for "
            f"{_money(latest.get('amount'))} via {latest.get('method_label')} — "
            f"{latest.get('status_label')} ({latest.get('initiated')})"
        )
        if latest.get("failure_reason"):
            detail += f". Failure reason: {latest['failure_reason']}"
        text += detail + "."
    return text


def _render_diagnosis(d: dict) -> str:
    if not d.get("is_stuck"):
        text = f"**Order #{d.get('order_id')} is not stuck.** {d.get('verdict')}"
        if d.get("healthy_signals"):
            text += "\n\nWhat is in order:\n" + _bullet(d["healthy_signals"])
        return text

    primary = d.get("primary_blocker") or {}
    parts = [
        f"**Order #{d.get('order_id')} is stuck.**",
        f"\n**Root cause — {primary.get('title')}** "
        f"(severity: {primary.get('severity')}, owner: {primary.get('owner_team')})",
        primary.get("detail", ""),
        f"\nNext action: {primary.get('suggested_action')}",
    ]

    others = [
        b
        for b in d.get("blockers", [])
        if b.get("code") != primary.get("code") and not b.get("is_symptom")
    ]
    if others:
        parts.append("\nOther open findings:")
        parts.append(
            _bullet(
                [
                    f"{b['title']} — {b['detail']} (owner: {b['owner_team']})"
                    for b in others
                ]
            )
        )

    consequences = d.get("consequences") or []
    if consequences:
        parts.append("\nKnock-on effects:")
        parts.append(_bullet([f"{c['title']} — {c['detail']}" for c in consequences]))

    if d.get("healthy_signals"):
        parts.append("\nNot a problem:")
        parts.append(_bullet(d["healthy_signals"]))

    parts.append(
        f"\nLast activity on this order: {d.get('last_activity')}. "
        f"Assigned agent: {d.get('assigned_agent') or 'unassigned'}."
    )
    return "\n".join(p for p in parts if p)


def _render_timeline(d: dict) -> str:
    events = d.get("events") or []
    if not events:
        return f"No events are recorded against order #{d.get('order_id')}."

    lines = [
        f"{e['occurred_at'][:16].replace('T', ' ')} — {e['description']} "
        f"[{e['event_type']}, {e['actor']}]"
        for e in events
    ]
    header = (
        f"**What happened — order #{d.get('order_id')}** "
        f"(showing {d.get('event_count_returned')} of "
        f"{d.get('event_count_total')} events; current status "
        f"{d.get('current_status')})"
    )
    footer = f"\nLast activity: {d.get('last_activity')}."
    return f"{header}\n{_bullet(lines)}{footer}"


def _render_customer(d: dict) -> str:
    lines = [
        f"Name: {d.get('full_name')} ({d.get('customer_code')})",
        f"Location: {d.get('city')}, {d.get('state')}",
        f"Phone: {d.get('phone')}",
        f"Email: {d.get('email') or '—'}",
        f"KYC: {d.get('kyc_status')}",
        f"Customer since {d.get('customer_since')}, "
        f"{d.get('total_orders')} order(s) in total",
    ]
    text = f"**Customer on order #{d.get('order_id')}**\n{_bullet(lines)}"
    if not d.get("pii_visible"):
        text += (
            "\n\nNote: contact details are masked because your role does not "
            "include permission to view customer contact information."
        )
    return text


def _render_vehicle(d: dict) -> str:
    lines = [
        f"Vehicle: {d.get('display_name')}",
        f"Registration: {d.get('registration_number')}",
        f"Fuel/Transmission: {d.get('fuel_type')} / {d.get('transmission')}",
        f"Odometer: {d.get('km_driven'):,} km" if d.get("km_driven") else None,
        f"Owners: {d.get('owner_count')} | Colour: {d.get('colour')}",
        f"Inspection score: {d.get('inspection_score')}",
        f"Listing price: {_money(d.get('listing_price'))}",
        f"RC transfer: {d.get('rc_transfer_status_label')}",
        f"Held at: {d.get('hub')}",
    ]
    return f"**Vehicle on order #{d.get('order_id')}**\n{_bullet(lines)}"


def _render_delivery(d: dict) -> str:
    if not d.get("has_delivery_record"):
        return (
            f"**Delivery — order #{d.get('order_id')}**\n"
            f"{d.get('summary')} Promised date: "
            f"{d.get('expected_delivery_date') or 'not set'}."
        )
    lines = [
        f"Status: {d.get('status_label')}",
        f"Scheduled: {d.get('scheduled_for') or 'not scheduled'}"
        + (f" ({d['slot']})" if d.get("slot") else ""),
        f"Promised date: {d.get('expected_delivery_date') or 'not set'}",
        f"Attempts made: {d.get('attempts')}",
        f"Partner: {d.get('logistics_partner') or '—'} | "
        f"Tracking: {d.get('tracking_reference') or '—'}",
    ]
    if d.get("failure_reason"):
        lines.append(f"Last failure: {d['failure_reason']}")
    if d.get("delivered_at"):
        lines.append(f"Delivered at: {d['delivered_at']}")
    if d.get("is_overdue"):
        lines.append(f"**Overdue by {d.get('days_overdue')} day(s).**")
    return f"**Delivery — order #{d.get('order_id')}**\n{_bullet(lines)}"


def _render_documents(d: dict) -> str:
    docs = d.get("documents") or []
    lines = [
        f"{doc['doc_type_label']}: {doc['status_label']}"
        + (f" — {doc['rejection_reason']}" if doc.get("rejection_reason") else "")
        for doc in docs
    ]
    status = (
        "All mandatory documents are verified."
        if d.get("all_mandatory_verified")
        else f"{d.get('pending_count')} document(s) still outstanding."
    )
    return (
        f"**Documents — order #{d.get('order_id')}**\n{status}\n{_bullet(lines)}"
    )


def _render_finance(d: dict) -> str:
    if not d.get("is_financed"):
        return (
            f"Order #{d.get('order_id')} is self-funded — there is no loan "
            f"on this order."
        )
    lines = [
        f"Status: {d.get('finance_status_label')}",
        f"Lender: {d.get('finance_partner') or '—'}",
        f"Loan amount: {_money(d.get('loan_amount'))}",
        f"Last updated: {d.get('updated')}",
    ]
    return f"**Finance — order #{d.get('order_id')}**\n{_bullet(lines)}"


def _render_summary(d: dict) -> str:
    sections = [
        _render_order(d.get("order") or {}),
        _render_customer(d.get("customer") or {}),
        _render_vehicle(d.get("vehicle") or {}),
        _render_payment(d.get("payment") or {}),
        _render_finance(d.get("finance") or {}),
        _render_delivery(d.get("delivery") or {}),
        _render_documents(d.get("documents") or {}),
        _render_timeline(d.get("timeline") or {}),
    ]
    return "\n\n".join(s for s in sections if s)


def _render_find_orders(d: dict) -> str:
    orders = d.get("orders") or []
    if not orders:
        return (
            "No orders matched that search "
            f"({d.get('filters_applied')}). Nothing to report."
        )
    lines = [
        f"#{o['order_id']} — {o['status_label']} — {o['customer_name']} — "
        f"{o['vehicle']} ({o['registration_number']}) — due "
        f"{_money(o.get('amount_due'))}"
        for o in orders
    ]
    header = (
        f"**{d.get('match_count')} matching order(s)**"
        + (
            f", showing the first {d.get('returned')}"
            if d.get("truncated")
            else ""
        )
    )
    return f"{header}\n{_bullet(lines)}"


def _render_knowledge(d: dict) -> str:
    passages = d.get("results") or []
    if not passages:
        return (
            "I could not find anything in the documented SOPs or policies "
            "that covers that. I would rather say so than invent a policy."
        )
    parts = [f"**From the {passages[0].get('document_title')}**"]
    for passage in passages:
        parts.append(
            f"\n_{passage.get('heading') or passage.get('document_title')}_\n"
            f"{passage.get('content')}"
        )
    parts.append(
        "\nSources: "
        + ", ".join(
            sorted({f"{p.get('document_title')} v{p.get('version')}" for p in passages})
        )
    )
    return "\n".join(parts)


def _render_generic(d: dict) -> str:
    return json.dumps(d, indent=2, default=str)[:1500]


_RENDERERS = {
    "get_order": _render_order,
    "get_payment_status": _render_payment,
    "diagnose_order_blockers": _render_diagnosis,
    "get_order_timeline": _render_timeline,
    "get_customer": _render_customer,
    "get_vehicle": _render_vehicle,
    "get_delivery_status": _render_delivery,
    "get_order_documents": _render_documents,
    "get_finance_status": _render_finance,
    "get_order_summary": _render_summary,
    "find_orders": _render_find_orders,
    "search_knowledge_base": _render_knowledge,
}
