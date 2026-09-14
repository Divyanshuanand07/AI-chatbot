---
title: Stuck Order Triage SOP
category: SOP
version: 1.4
owner_team: Order Operations
effective_from: 2026-03-01
---

## When to use this

Use this procedure when an order has not progressed as expected, when a
customer complains about a delay, or when an order appears on the daily
stalled-orders report.

## Definition of a stuck order

An order is treated as stuck when any of the following is true:

- no event has been recorded against it for more than three days while it sits
  in a non-final status;
- it has breached the SLA of the stage it is in;
- it carries a blocker that requires action from a named team.

Orders that are delivered, refunded, or cancelled with nothing outstanding are
not stuck, regardless of their history.

## Triage order

Work the causes from the most upstream to the least, because fixing a
downstream symptom without clearing the upstream cause simply re-blocks the
order.

1. **Explicit hold.** If the order is on hold, read the hold reason first.
   Nothing else can progress until the hold is released or the order cancelled.
2. **Compliance.** KYC rejected or pending blocks delivery outright.
3. **Documents.** Rejected documents block everything downstream. Missing
   documents inside the three-day collection window are normal and need no
   action yet.
4. **Finance.** A rejected loan means the funding plan is dead; an application
   under review beyond three days needs escalation to the lender's relationship
   manager.
5. **Payments.** A failed attempt with no retry, a transaction stuck awaiting
   bank confirmation, or a shortfall against the order total.
6. **RC transfer.** Rejections need re-filing; anything past twenty-one days
   needs an RTO follow-up.
7. **Logistics.** Failed attempts and overdue promised dates.

## Distinguishing causes from symptoms

An overdue delivery date and a long silence on the order are almost always
**symptoms**. Reporting them as the cause sends the operator to the logistics
team when the real fix sits with documentation or finance. Always name the
upstream cause first, then mention the overdue date as a consequence.

## SLA reference

| Stage | SLA |
|---|---|
| Document collection from customer | 3 days |
| Internal document verification | 2 days |
| Payment follow-up after link sent | 2 days |
| Finance decision from lender | 3 days |
| RC transfer at RTO | 21 days |
| Refund completion | 7 days |
| No activity on a live order | 3 days |

## Escalation

- Blocker owned by another team and unresolved for two days: escalate to that
  team's queue owner.
- Order stuck for more than ten days in total: escalate to the Order
  Operations manager and add it to the weekly review.
- Customer has complained twice about the same blocker: escalate immediately,
  regardless of elapsed time.

## Closing the loop

Whenever you act on a stuck order, record an event against it describing what
you did and what you are waiting for. An order with no recorded next action
will resurface on the stalled report and be triaged again from scratch, which
wastes the next agent's time.
