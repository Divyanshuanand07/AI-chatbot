"""
Live Anthropic driver.

Implementation notes that matter:

* **Adaptive thinking.** On Claude Opus 5 thinking is on by default and
  `budget_tokens` is rejected outright. We send `{"type": "adaptive"}` and
  control depth with `output_config.effort` instead.
* **Refusal fallbacks.** Enabled by default. If a safety classifier declines
  a request, the API re-runs it on a fallback model inside the same call, so
  an operator asking a legitimate question never gets a dead end. This needs
  the beta messages endpoint plus the `server-side-fallback-2026-07-01` flag.
* **Prompt caching.** The caller hands us `system` as blocks with the cache
  breakpoint already placed. We never inject a timestamp or request id into
  the prefix, because a single varying byte invalidates the whole cache.
* **Typed error chain.** Caught most-specific first, so a 404 (bad model id)
  is not retried like a 429. The SDK already retries 429/5xx internally.
"""

from __future__ import annotations

import logging

import anthropic
from django.conf import settings

from .base import LLMError, LLMProvider, LLMResponse, ToolCall, Usage

logger = logging.getLogger(__name__)

REFUSAL_FALLBACK_BETA = "server-side-fallback-2026-07-01"

#: Effort levels that forbid explicitly disabling thinking on Opus 5.
_EFFORT_REQUIRING_THINKING = {"xhigh", "max"}


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self):
        config = settings.LLM
        api_key = config["API_KEY"]

        # An unset key is not automatically fatal — the SDK also resolves
        # ANTHROPIC_AUTH_TOKEN and an `ant auth login` profile on disk.
        self.client = anthropic.Anthropic(
            **({"api_key": api_key} if api_key else {}),
            timeout=config["TIMEOUT_SECONDS"],
            max_retries=config["MAX_RETRIES"],
        )
        self.model = config["MODEL"]
        self.max_tokens = config["MAX_TOKENS"]
        self.effort = config["EFFORT"]
        self.thinking_mode = config["THINKING"]
        self.refusal_fallback = config["REFUSAL_FALLBACK"]

        if (
            self.thinking_mode == "disabled"
            and self.effort in _EFFORT_REQUIRING_THINKING
        ):
            # The API rejects this combination; failing here would be a 400 on
            # every request, so correct it once at startup and say so.
            logger.warning(
                "thinking_disabled_incompatible_with_effort",
                extra={"effort": self.effort, "corrected_to": "adaptive"},
            )
            self.thinking_mode = "adaptive"

    # ------------------------------------------------------------------
    def describe(self) -> dict:
        return {
            "provider": self.name,
            "model": self.model,
            "effort": self.effort,
            "thinking": self.thinking_mode,
            "refusal_fallback": self.refusal_fallback,
        }

    def complete(
        self,
        *,
        system: list[dict],
        messages: list[dict],
        tools: list[dict],
    ) -> LLMResponse:
        request: dict = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": messages,
            "output_config": {"effort": self.effort},
        }
        if tools:
            request["tools"] = tools
        if self.thinking_mode == "adaptive":
            request["thinking"] = {"type": "adaptive"}
        else:
            request["thinking"] = {"type": "disabled"}

        try:
            if self.refusal_fallback:
                response = self.client.beta.messages.create(
                    betas=[REFUSAL_FALLBACK_BETA],
                    fallbacks="default",
                    **request,
                )
            else:
                response = self.client.messages.create(**request)
        except anthropic.AuthenticationError as exc:
            raise LLMError(
                "The model provider rejected our credentials.",
                code="llm_unauthenticated",
            ) from exc
        except anthropic.BadRequestError as exc:
            # Our fault: malformed request, bad tool schema, unsupported param.
            logger.error("llm_bad_request", extra={"error": str(exc)})
            raise LLMError(
                f"The assistant built an invalid model request: {exc}",
                code="llm_bad_request",
            ) from exc
        except anthropic.NotFoundError as exc:
            raise LLMError(
                f"Model '{self.model}' is not available to this account.",
                code="llm_model_not_found",
            ) from exc
        except anthropic.RateLimitError as exc:
            raise LLMError(
                "The model provider is rate limiting us. Try again shortly.",
                retryable=True,
                code="llm_rate_limited",
            ) from exc
        except anthropic.APITimeoutError as exc:
            raise LLMError(
                "The model did not respond in time.",
                retryable=True,
                code="llm_timeout",
            ) from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError(
                "Could not reach the model provider.",
                retryable=True,
                code="llm_unreachable",
            ) from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(
                f"Model provider error ({exc.status_code}).",
                retryable=exc.status_code >= 500,
                code="llm_provider_error",
            ) from exc

        return self._parse(response)

    # ------------------------------------------------------------------
    def _parse(self, response) -> LLMResponse:
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []

        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                # block.input is already parsed by the SDK; never string-match
                # the serialised form, escaping differs between models.
                tool_calls.append(
                    ToolCall(
                        id=block.id,
                        name=block.name,
                        arguments=dict(block.input or {}),
                    )
                )

        raw_usage = getattr(response, "usage", None)
        usage = Usage(
            input_tokens=getattr(raw_usage, "input_tokens", 0) or 0,
            output_tokens=getattr(raw_usage, "output_tokens", 0) or 0,
            cache_read_input_tokens=getattr(
                raw_usage, "cache_read_input_tokens", 0
            ) or 0,
            cache_creation_input_tokens=getattr(
                raw_usage, "cache_creation_input_tokens", 0
            ) or 0,
        )

        # stop_details is populated only for refusals — always guard.
        refusal_category = None
        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            refusal_category = getattr(details, "category", None)
            logger.warning(
                "llm_refusal",
                extra={"category": refusal_category, "model": response.model},
            )

        logger.info(
            "llm_turn",
            extra={
                "model": response.model,
                "stop_reason": response.stop_reason,
                "tool_calls": len(tool_calls),
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cache_read_tokens": usage.cache_read_input_tokens,
                "request_id": getattr(response, "_request_id", None),
            },
        )

        return LLMResponse(
            text="\n".join(p for p in text_parts if p).strip(),
            tool_calls=tool_calls,
            stop_reason=response.stop_reason or "end_turn",
            raw_content=list(response.content),
            usage=usage,
            model=response.model,
            request_id=getattr(response, "_request_id", None),
            refusal_category=refusal_category,
        )
