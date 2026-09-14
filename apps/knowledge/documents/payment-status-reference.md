---
title: Payment Status Reference
category: REFERENCE
version: 3.2
owner_team: Payments
effective_from: 2026-04-01
---

## Purpose

This reference explains every payment status that appears against an order,
what it means operationally, and what an agent should do about it. Use it when
a customer or colleague asks what a payment status means, or before you tell a
customer to pay again.

## INITIATED

The payment request has been created in our system and a payment link or
collect request has been generated, but the customer has not yet acted on it.
No money has moved. An order sitting in INITIATED for more than a few hours
usually means the customer never opened the link.

Action: resend the payment link. It is safe to create a new attempt because
nothing has been debited.

## PENDING

The customer has acted and the transaction is now with the bank or the UPI
network, but no final confirmation has come back to us. This is the one status
where the customer's money may already have left their account while our
system still shows nothing received.

Action: do **not** ask the customer to pay again. Run a gateway status
reconciliation against the payment reference first. Asking for a second
payment against a PENDING transaction is the most common cause of duplicate
collection complaints, and refunding a duplicate takes five to seven working
days.

## AUTHORIZED

The bank has approved and held the funds, but we have not captured them yet.
This appears on card transactions. The hold typically expires in seven days if
not captured.

Action: raise a capture request with the Payments team the same day. If the
hold expires the customer must pay again from scratch.

## SUCCESS

Money has been received and settled to Cars24. This is the only status that
counts towards the amount paid on an order. A payment in any other status must
never be treated as received, and must never be counted when telling a
customer how much is outstanding.

## FAILED

The transaction was rejected. The failure reason recorded against the payment
tells you what to do next:

- INSUFFICIENT_FUNDS — the account did not have the balance. Ask the customer
  to retry after funding the account, or offer a different method.
- LIMIT_EXCEEDED — the amount is above the customer's per-transaction or
  daily limit. Suggest net banking or splitting the payment, or ask them to
  raise the limit in their banking app.
- CARD_DECLINED — the issuing bank refused the card. The customer must contact
  their bank; we cannot resolve this from our side.
- UPI_TIMEOUT — the collect request expired unanswered. Simply resend it.
- MANDATE_MISSING — no active e-mandate. Required for EMI collections; the
  mandate has to be set up before the instalment can be taken.

Action: a FAILED payment is safe to retry. Always quote the failure reason to
the customer, because "your payment failed" without a reason generates a
support call.

## CANCELLED

The payment attempt was abandoned or cancelled before completion, either by
the customer or by an agent. No money moved. Treat the same as INITIATED.

## REFUNDED

Money previously received has been returned to the customer. A refunded
payment does not count towards the amount paid. See the Refund and
Cancellation Policy for timelines.

## How amounts are calculated

The amount paid on an order is the sum of SUCCESS payments excluding refunds.
The amount due is the order total minus that figure. These are calculated by
the system; agents should never compute them by hand from the transaction list,
because a PENDING or AUTHORIZED transaction in the list is easy to count by
mistake.

## When an order status and the payment ledger disagree

If an order shows a status that implies full payment (payment complete, ready
for delivery, out for delivery) while an amount is still due, treat it as a
reconciliation incident. Do not release the vehicle. Raise it with Finance the
same day and record the discrepancy against the order.
