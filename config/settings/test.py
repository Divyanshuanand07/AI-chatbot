"""
Test settings.

The suite never calls a real model: the orchestrator runs against the
scripted stub provider, so tool selection, RBAC, guardrails and answer
assembly are all asserted deterministically.
"""

from .base import *
from .base import KNOWLEDGE, LLM, TOOLS

DEBUG = False

LLM["PROVIDER"] = "stub"
LLM["MAX_TOOL_ITERATIONS"] = 6

# Deterministic, offline embeddings keep knowledge-base tests hermetic.
KNOWLEDGE["EMBEDDING_PROVIDER"] = "hashing"

TOOLS["CACHE_ENABLED"] = False
# Run tools inline. Django's per-test transaction is invisible to the worker
# thread's own connection, so threaded execution would make every fixture
# disappear. The threaded path is covered separately by tests marked
# `django_db(transaction=True)`, which commit and are therefore visible.
TOOLS["TIMEOUT_SECONDS"] = 0

# Pinned explicitly rather than inherited from the environment. Every blocker
# rule is a function of these numbers, so a developer's local `.env` must not
# be able to change what the assertions mean.
OPS_SLA = {
    "STALE_EVENT_DAYS": 3,
    "DOCS_COLLECTION_DAYS": 3,
    "DOCS_VERIFICATION_DAYS": 2,
    "PAYMENT_PENDING_DAYS": 2,
    "FINANCE_REVIEW_DAYS": 3,
    "RC_TRANSFER_DAYS": 21,
    "DELIVERY_ATTEMPT_LIMIT": 2,
    "REFUND_DAYS": 7,
}

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "opsai-test",
    }
}

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

LOGGING["root"]["level"] = "WARNING"
