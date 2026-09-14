"""Production settings. Fails fast on missing critical configuration."""

from .base import *
from .base import LLM, env

DEBUG = False
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS")
SECRET_KEY = env("DJANGO_SECRET_KEY")  # no default — must be provided

# HTTPS / transport security
SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=True)
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=31536000)
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True

CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])

# A production deployment talking to a real model needs a real credential.
if LLM["PROVIDER"] == "anthropic" and not LLM["API_KEY"]:
    raise RuntimeError(
        "ANTHROPIC_API_KEY is required when LLM_PROVIDER=anthropic. "
        "Set LLM_PROVIDER=stub for a deployment without model access."
    )
