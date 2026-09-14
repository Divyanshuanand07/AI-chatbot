---
title: Finance Rejection Playbook
category: PLAYBOOK
version: 1.2
owner_team: Finance
effective_from: 2026-05-05
---

## Purpose

What to do when a lender rejects a customer's loan application. A rejection
does not automatically mean the order dies, and handled well a meaningful
share of these orders still convert.

## First, understand the rejection

Ask the lender's relationship manager for the rejection category. The four
common ones and what they allow:

- **Low credit score.** Another lender with a different risk appetite may
  still approve. Worth a second application.
- **Insufficient or unstable income.** A co-applicant or a larger down payment
  reduces the loan amount and often clears the threshold.
- **Document mismatch.** Not a credit decision at all. Fix the document and
  re-apply to the same lender.
- **Existing obligations or recent default.** Unlikely to be approved anywhere
  in the near term. Be honest with the customer rather than running them
  through three more rejections.

Never tell the customer their credit score or the lender's internal reason
verbatim. Say the application was not approved by that lender and move to
options.

## Options to offer, in order

1. **Alternate lender.** We work with multiple partners with different risk
   appetites. One rejection is not the market's answer.
2. **Higher down payment.** A smaller loan is easier to approve. Model the
   revised EMI before offering it so the number is real.
3. **Co-applicant.** A spouse or parent with income can change the outcome.
4. **Self-funded purchase.** Some customers can fund the balance and were
   simply defaulting to finance.
5. **Cancellation and refund.** If none of the above works, cancel cleanly.
   Where the order is cancelled because the loan was rejected, the token
   amount is refunded in full with no deduction.

## Timeline

A rejection must be communicated to the customer within **one working day**.
Sitting on a rejection while trying a second lender is the wrong order of
operations — the customer will hear about it from the lender's own SMS and lose
trust. Tell them, then present options.

Do not let an order sit in a finance-in-progress state after a rejection. The
status must move to reflect reality, otherwise the order silently ages on the
stalled report and nobody owns it.

## Applications stuck under review

An application under review for more than **three days** has breached SLA.
Escalate to the lender's relationship manager for a decision — do not simply
wait, and do not tell the customer "it is being processed" for a second week.

## Recording the outcome

Record the rejection, the options presented, and the customer's decision as
events against the order. This is both the audit trail and what lets the next
agent pick the conversation up without starting over.
