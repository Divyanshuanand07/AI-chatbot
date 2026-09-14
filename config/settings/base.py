"""
Base Django settings for the AI Operations Assistant.

Environment-driven (12-factor). See `.env.example` for the full list of knobs.
"""

from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env(
    DEBUG=(bool, False),
    ALLOWED_HOSTS=(list, ["*"]),
)

# Read .env if present. Real deployments inject env vars directly.
env_file = BASE_DIR / ".env"
if env_file.exists():
    env.read_env(str(env_file))

# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------
# Long enough to satisfy HS256's recommended key length even in dev, so the
# JWT library does not warn. settings/prod.py removes the default entirely.
SECRET_KEY = env(
    "DJANGO_SECRET_KEY",
    default="dev-only-insecure-key-change-me-before-any-real-deployment",
)
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env("ALLOWED_HOSTS")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third party
    "rest_framework",
    "rest_framework_simplejwt",
    "drf_spectacular",
    # Local
    "apps.accounts",
    "apps.operations",
    "apps.assistant",
    "apps.knowledge",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # Local: assigns a trace id to every request for correlated logs/audit.
    "apps.common.middleware.RequestTraceMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# ---------------------------------------------------------------------------
# Database — PostgreSQL only. pgvector is required for the knowledge base.
# ---------------------------------------------------------------------------
DATABASES = {
    "default": env.db_url(
        "DATABASE_URL",
        default="postgres://ops_ai:ops_ai_pw@localhost:5432/ops_ai",
    )
}
DATABASES["default"]["CONN_MAX_AGE"] = env.int("DB_CONN_MAX_AGE", default=60)
DATABASES["default"]["OPTIONS"] = {
    "connect_timeout": env.int("DB_CONNECT_TIMEOUT", default=5),
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# ---------------------------------------------------------------------------
# Cache — Redis when available, in-memory otherwise (keeps dev/test frictionless)
# ---------------------------------------------------------------------------
REDIS_URL = env("REDIS_URL", default="")
if REDIS_URL:
    CACHES = {
        "default": {
            "BACKEND": "django_redis.cache.RedisCache",
            "LOCATION": REDIS_URL,
            "OPTIONS": {
                "CLIENT_CLASS": "django_redis.client.DefaultClient",
                "SOCKET_CONNECT_TIMEOUT": 2,
                "SOCKET_TIMEOUT": 2,
                "IGNORE_EXCEPTIONS": True,  # cache outage must not break the API
            },
            "KEY_PREFIX": "opsai",
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "opsai-local",
        }
    }

# ---------------------------------------------------------------------------
# i18n / static
# ---------------------------------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = env("TIME_ZONE", default="Asia/Kolkata")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

# ---------------------------------------------------------------------------
# DRF / auth
# ---------------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 25,
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.ScopedRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        # The AI endpoint is the expensive one; throttle it separately.
        "ai_query": env("THROTTLE_AI_QUERY", default="30/min"),
        "read_api": env("THROTTLE_READ_API", default="600/min"),
    },
    "EXCEPTION_HANDLER": "apps.common.exceptions.api_exception_handler",
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": __import__("datetime").timedelta(
        minutes=env.int("JWT_ACCESS_MINUTES", default=60)
    ),
    "REFRESH_TOKEN_LIFETIME": __import__("datetime").timedelta(
        days=env.int("JWT_REFRESH_DAYS", default=7)
    ),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": False,
    "UPDATE_LAST_LOGIN": True,
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Cars24 AI Operations Assistant",
    "DESCRIPTION": (
        "Operational read APIs plus a natural-language assistant. "
        "The LLM selects tools; all operational facts come from PostgreSQL."
    ),
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
}

# ---------------------------------------------------------------------------
# LLM / orchestrator
# ---------------------------------------------------------------------------
LLM = {
    # "anthropic" for live calls, "stub" for deterministic offline/test runs.
    "PROVIDER": env("LLM_PROVIDER", default="anthropic"),
    "MODEL": env("LLM_MODEL", default="claude-opus-5"),
    "MAX_TOKENS": env.int("LLM_MAX_TOKENS", default=16000),
    # low | medium | high | xhigh | max
    "EFFORT": env("LLM_EFFORT", default="high"),
    "THINKING": env("LLM_THINKING", default="adaptive"),  # adaptive | disabled
    # Server-side refusal fallback: on a policy decline the API retries the
    # same request on a fallback model inside the same call.
    "REFUSAL_FALLBACK": env.bool("LLM_REFUSAL_FALLBACK", default=True),
    "TIMEOUT_SECONDS": env.float("LLM_TIMEOUT_SECONDS", default=120.0),
    "MAX_RETRIES": env.int("LLM_MAX_RETRIES", default=2),
    # Hard ceiling on tool-calling round trips per user query.
    "MAX_TOOL_ITERATIONS": env.int("LLM_MAX_TOOL_ITERATIONS", default=6),
    # Conversation turns replayed from the database for follow-up context.
    "HISTORY_TURNS": env.int("LLM_HISTORY_TURNS", default=8),
    "API_KEY": env("ANTHROPIC_API_KEY", default=""),
}

TOOLS = {
    # Per-tool wall-clock budget. A slow tool must not hang the request.
    "TIMEOUT_SECONDS": env.float("TOOL_TIMEOUT_SECONDS", default=8.0),
    "MAX_PARALLEL": env.int("TOOL_MAX_PARALLEL", default=6),
    "CACHE_ENABLED": env.bool("TOOL_CACHE_ENABLED", default=True),
}

KNOWLEDGE = {
    # "hashing" needs no network and is deterministic; "voyage" calls Voyage AI.
    "EMBEDDING_PROVIDER": env("EMBEDDING_PROVIDER", default="hashing"),
    "EMBEDDING_DIM": env.int("EMBEDDING_DIM", default=512),
    "VOYAGE_API_KEY": env("VOYAGE_API_KEY", default=""),
    "VOYAGE_MODEL": env("VOYAGE_MODEL", default="voyage-3.5"),
    "TOP_K": env.int("KNOWLEDGE_TOP_K", default=4),
    # Below this cosine similarity we report "no relevant policy found"
    # instead of handing the model weak context it might over-trust.
    #
    # The floor is per-backend, and that is not a detail. Cosine scores are
    # only comparable within one embedding space: the lexical hashing backend
    # scores a good match around 0.2-0.5, while a semantic model scores the
    # same match around 0.6-0.8. A single shared threshold either rejects
    # valid matches on one backend or admits noise on the other. These values
    # were calibrated against the on-topic/off-topic query set in
    # `manage.py calibrate_knowledge`.
    "MIN_SIMILARITY_BY_PROVIDER": {
        "hashing": env.float("KNOWLEDGE_MIN_SIMILARITY_HASHING", default=0.17),
        "voyage": env.float("KNOWLEDGE_MIN_SIMILARITY_VOYAGE", default=0.50),
    },
    # Fallback for an unrecognised backend.
    "MIN_SIMILARITY": env.float("KNOWLEDGE_MIN_SIMILARITY", default=0.25),
    # Lexical backends only: reject a query when more than this fraction of
    # its content words appear nowhere in the corpus. Stops "policy on flying
    # drones over the hub" matching a real policy on the words it happens to
    # share. See apps/knowledge/vocabulary.py.
    # Calibrated: on-topic queries peak at 0.20 OOV, off-topic bottom out at
    # 0.25, so 0.22 separates them. Re-run `calibrate_knowledge` after any
    # corpus change — adding a document moves both distributions.
    "MAX_OOV_RATIO": env.float("KNOWLEDGE_MAX_OOV_RATIO", default=0.22),
}

# Operational SLA thresholds used by the deterministic blocker diagnosis.
# These live in settings so ops can tune them without a code change.
OPS_SLA = {
    "STALE_EVENT_DAYS": env.int("SLA_STALE_EVENT_DAYS", default=3),
    # How long a customer is allowed to upload documents before it counts as
    # a blocker. Without this, every freshly created order looks "stuck"
    # simply because nothing has been uploaded yet.
    "DOCS_COLLECTION_DAYS": env.int("SLA_DOCS_COLLECTION_DAYS", default=3),
    "DOCS_VERIFICATION_DAYS": env.int("SLA_DOCS_VERIFICATION_DAYS", default=2),
    "PAYMENT_PENDING_DAYS": env.int("SLA_PAYMENT_PENDING_DAYS", default=2),
    "FINANCE_REVIEW_DAYS": env.int("SLA_FINANCE_REVIEW_DAYS", default=3),
    "RC_TRANSFER_DAYS": env.int("SLA_RC_TRANSFER_DAYS", default=21),
    "DELIVERY_ATTEMPT_LIMIT": env.int("SLA_DELIVERY_ATTEMPT_LIMIT", default=2),
    "REFUND_DAYS": env.int("SLA_REFUND_DAYS", default=7),
}

# ---------------------------------------------------------------------------
# Logging — single-line JSON so log shippers can parse it directly.
# ---------------------------------------------------------------------------
LOG_LEVEL = env("LOG_LEVEL", default="INFO")
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {"()": "apps.common.logging.JsonFormatter"},
        "console": {
            "format": "%(asctime)s %(levelname)-7s %(name)s %(message)s",
        },
    },
    "handlers": {
        "stdout": {
            "class": "logging.StreamHandler",
            "formatter": env("LOG_FORMAT", default="json"),
        },
    },
    "root": {"handlers": ["stdout"], "level": LOG_LEVEL},
    "loggers": {
        "django.db.backends": {"level": "WARNING", "propagate": True},
        "apps": {"level": LOG_LEVEL, "propagate": True},
    },
}

# ---------------------------------------------------------------------------
# Security (tightened further in settings/prod.py)
# ---------------------------------------------------------------------------
X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = False
