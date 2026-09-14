"""Local development settings."""

from .base import *
from .base import env

DEBUG = True
ALLOWED_HOSTS = ["*"]

# Human-readable logs are easier to scan while developing.
LOGGING["handlers"]["stdout"]["formatter"] = env("LOG_FORMAT", default="console")

INSTALLED_APPS += []
