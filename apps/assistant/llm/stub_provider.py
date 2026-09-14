"""
Deterministic offline driver.

This is not a mock that returns canned strings. It is a real (if crude)
implementation of the two jobs the LLM does in this system:

  1. **Intent + entity extraction** -> which tools to call with which
     arguments, using keyword rules and a regex for the order id.
  2. **Answer composition** -> turning tool results into readable prose,
     using templates.

Because it plugs into the same orchestrator as the live provider, the entire
pipeline — tool selection, parallel execution, scope filtering, follow-up
context, missing-order handling, citation assembly — is exercised end to end
with no API key and no spend. Every one of those behaviours is asserted in
the test suite against this driver.

What it deliberately does *not* do is understand language. Paraphrases the
keyword table misses will fall through to `get_order`. That is the honest
boundary: this driver proves the plumbing, the real model provides the
comprehension.
"""

from __future__ import annotations

import logging
import re

from .base import LLMProvider, LLMResponse, ToolCall, Usage

logger = logging.getLogger(__name__)

#: "#1243", "order 1243", "1243" — three or more digits.
ORDER_ID_PATTERN = re.compile(r"#?\b(\d{3,})\b")
PHONE_PATTERN = re.compile(r"\b(\d{10})\b")
REGISTRATION_PATTERN = re.compile(
    r"\b([A-Z]{2}\s?\d{1,2}\s?[A-Z]{1,3}\s?\d{3,4})\b", re.IGNORECASE
)

#: Intent -> (tool name, keywords). Order matters: earlier wins ties.
INTENT_RULES: list[tuple[str, tuple[str, ...]]] = [
    (
        "get_payment_status",
        ("payment", "paid", "pay", "due", "refund", "money", "amount",
         "transaction", "outstanding", "balance", "emi"),
    ),
    (
        "diagnose_order_blockers",
        ("stuck", "blocked", "blocker", "why", "delayed", "delay", "hold",
         "not moving", "not progressing", "problem", "issue", "wrong",
         "holding up", "pending on"),
    ),
    (
        "get_order_timeline",
        ("happened", "timeline", "history", "events", "log", "activity",
         "so far", "progress so far", "journey"),
    ),
    (
        "get_delivery_status",
        ("delivery", "deliver", "delivered", "dispatch", "shipped",
         "when will", "eta", "slot", "logistics"),
    ),
    (
        "get_customer",
        ("customer", "buyer", "contact", "phone", "email", "who is",
         "whose", "kyc"),
    ),
    (
        "get_vehicle",
        ("vehicle", "car", "registration", "reg no", "make", "model",
         "variant", "inspection", "rc ", "rc transfer", "km"),
    ),
    (
        "get_order_documents",
        ("document", "docs", "paperwork", "aadhaar", "pan", "upload",
         "verification", "verified"),
    ),
    (
        "get_finance_status",
        ("finance", "loan", "lender", "disbursed", "disbursement",
         "credit", "financing"),
    ),
]

SUMMARY_KEYWORDS = (
    "summary", "summarise", "summarize", "overview", "everything",
    "complete picture", "full detail", "all details", "brief me", "rundown",
)

#: Phrases that mark a question as being about *policy or terminology*
#: rather than about a specific order.
#:
#: These must be definitional, not merely interrogative. An earlier version
#: included "what is the", which matches "what is the payment status of order
#: #1243?" — routing a straightforward order lookup into the SOP search. The
#: rule of thumb: a phrase belongs here only if it would be odd in a question
#: about one particular order.
KNOWLEDGE_KEYWORDS = (
    "policy", "sop", "standard operating", "procedure", "guideline",
    "what does", "what do we do when", "meaning", " mean",
    "how long should", "how many days should", "who owns",
    "am i allowed", "are we allowed", "supposed to",
)

MAX_TOOLS_PER_TURN = 4


class StubProvider(LLMProvider):
    name = "stub"

    def describe(self) -> dict:
        return {"provider": self.name, "model": "stub-deterministic-v1"}

    # ------------------------------------------------------------------
    def complete(
        self,
        *,
        system: list[dict],
        messages: list[dict],
        tools: list[dict],
    ) -> LLMResponse:
        available = {tool["name"] for tool in tools}
        pending_results = self._pending_tool_results(messages)

        if pending_results:
            # Second leg of the loop: tool results are in, write the answer.
            text = self._compose_answer(pending_results)
            return LLMResponse(
                text=text,
                stop_reason="end_turn",
                raw_content=[{"type": "text", "text": text}],
                usage=Usage(input_tokens=0, output_tokens=len(text) // 4),
                model="stub-deterministic-v1",
            )

        question = self._last_user_text(messages)
        context_order_id = self._context_order_id(system)
        calls, withheld = self._select_tools(question, available, context_order_id)

        if not calls:
            text = (
                self._explain_restriction(withheld)
                if withheld
                else self._clarify(question, available)
            )
            return LLMResponse(
                text=text,
                stop_reason="end_turn",
                raw_content=[{"type": "text", "text": text}],
                usage=Usage(output_tokens=len(text) // 4),
                model="stub-deterministic-v1",
            )

        return LLMResponse(
            text="",
            tool_calls=calls,
            stop_reason="tool_use",
            raw_content=[
                {
                    "type": "tool_use",
                    "id": call.id,
                    "name": call.name,
                    "input": call.arguments,
                }
                for call in calls
            ],
            usage=Usage(output_tokens=20 * len(calls)),
            model="stub-deterministic-v1",
        )

    # ------------------------------------------------------------------
    # Reading the conversation
    # ------------------------------------------------------------------
    def _last_user_text(self, messages: list[dict]) -> str:
        for message in reversed(messages):
            if message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                texts = [
                    block.get("text", "")
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                ]
                if texts:
                    return "\n".join(texts)
        return ""

    def _pending_tool_results(self, messages: list[dict]) -> list[dict]:
        """Tool results in the final message, if the last turn was results."""
        if not messages:
            return []
        last = messages[-1]
        if last.get("role") != "user" or not isinstance(last.get("content"), list):
            return []
        return [
            block
            for block in last["content"]
            if isinstance(block, dict) and block.get("type") == "tool_result"
        ]

    def _context_order_id(self, system: list[dict]) -> int | None:
        """
        Recover the conversation's current order from the system prompt.

        The orchestrator writes resolved context as plain English into the
        volatile system block ("most recently discussed order is #1243"), so
        the same text serves the real model and this driver. That is how
        follow-ups like "and its payment status?" resolve.
        """
        for block in system:
            text = block.get("text", "") if isinstance(block, dict) else ""
            match = re.search(r"most recently discussed order is #(\d+)", text)
            if match:
                return int(match.group(1))
        return None

    # ------------------------------------------------------------------
    # Intent routing
    # ------------------------------------------------------------------
    def _select_tools(
        self, question: str, available: set[str], context_order_id: int | None
    ) -> tuple[list[ToolCall], list[str]]:
        """
        Returns (tool calls, withheld tool names).

        A tool the caller's role does not permit is simply absent from
        `available`. Tracking which intents we *wanted* but could not use lets
        the answer say "your role does not permit that" instead of quietly
        answering an easier question — silently substituting a different
        lookup is worse than refusing, because the operator cannot tell.
        """
        lowered = question.lower()
        withheld: list[str] = []
        order_id = self._extract_order_id(question) or context_order_id

        def call(name: str, args: dict) -> ToolCall:
            index = len(selected)
            return ToolCall(id=f"stub_{index}_{name}", name=name, arguments=args)

        selected: list[ToolCall] = []

        # 1. Policy / "what does this mean" questions need no order at all.
        looks_like_knowledge = any(k in lowered for k in KNOWLEDGE_KEYWORDS)
        if looks_like_knowledge and "search_knowledge_base" in available:
            selected.append(
                ToolCall(
                    id="stub_0_search_knowledge_base",
                    name="search_knowledge_base",
                    arguments={"query": question.strip()[:400], "category": None},
                )
            )
            # A policy question that also names an order gets the order too,
            # so the answer can be specific rather than generic.
            if order_id and "get_order" in available:
                selected.append(call("get_order", {"order_id": order_id}))
            return selected, withheld

        # 2. No order id anywhere — try to resolve one from other identifiers.
        if order_id is None:
            phone = PHONE_PATTERN.search(question)
            registration = REGISTRATION_PATTERN.search(question)
            if (phone or registration) and "find_orders" in available:
                return [
                    ToolCall(
                        id="stub_0_find_orders",
                        name="find_orders",
                        arguments={
                            "phone": phone.group(1) if phone else None,
                            "registration_number": (
                                registration.group(1) if registration else None
                            ),
                            "customer_name": None,
                            "status": None,
                            "hub": None,
                            "limit": 10,
                        },
                    )
                ], withheld
            return [], withheld  # nothing to work with -> ask for clarification

        # 3. "Everything about this order" is one composite call, not six.
        if any(k in lowered for k in SUMMARY_KEYWORDS):
            if "get_order_summary" in available:
                return [call("get_order_summary", {"order_id": order_id})], withheld

        # 4. Otherwise collect every matching intent (supports "payment and
        #    delivery status of #1243" in a single parallel turn).
        for tool_name, keywords in INTENT_RULES:
            matched = any(keyword in lowered for keyword in keywords)
            if not matched:
                continue
            if tool_name not in available:
                withheld.append(tool_name)
                continue
            selected.append(call(tool_name, {"order_id": order_id}))
            if len(selected) >= MAX_TOOLS_PER_TURN:
                break

        # 5. Fall back to the plain status lookup — but only if nothing was
        #    withheld. If the operator asked for something their role cannot
        #    see, answering a different question instead would be misleading.
        if not selected and not withheld and "get_order" in available:
            selected.append(call("get_order", {"order_id": order_id}))

        return selected, withheld

    def _extract_order_id(self, question: str) -> int | None:
        # Skip 10-digit phone numbers, which would otherwise match.
        candidates = [
            int(m.group(1))
            for m in ORDER_ID_PATTERN.finditer(question)
            if len(m.group(1)) < 10
        ]
        return candidates[0] if candidates else None

    def _explain_restriction(self, withheld: list[str]) -> str:
        readable = {
            "diagnose_order_blockers": "run a blocker diagnosis on an order",
            "get_finance_status": "view loan or finance details",
            "get_customer": "view customer details",
            "get_payment_status": "view payment details",
            "search_knowledge_base": "search internal SOPs and policies",
        }
        wanted = ", ".join(readable.get(name, name) for name in withheld)
        return (
            f"Your role does not have permission to {wanted}, so I cannot "
            f"answer that. I have not substituted a different lookup, because "
            f"that would not be the answer you asked for. Ask an operations "
            f"manager if you need this access."
        )

    def _clarify(self, question: str, available: set[str]) -> str:
        if not question.strip():
            return (
                "I did not receive a question. Ask me about an order, for "
                "example: 'what is the payment status of order #1243?'"
            )
        return (
            "I could not find an order id in that question, and I will not "
            "guess one. Give me the order number (for example 'order #1243'), "
            "or the customer's phone number or the vehicle registration "
            "number, and I will look it up."
        )

    # ------------------------------------------------------------------
    # Answer composition
    # ------------------------------------------------------------------
    def _compose_answer(self, results: list[dict]) -> str:
        from .stub_renderers import render_tool_result

        sections: list[str] = []
        for block in results:
            sections.append(render_tool_result(block))
        return "\n\n".join(s for s in sections if s).strip()
