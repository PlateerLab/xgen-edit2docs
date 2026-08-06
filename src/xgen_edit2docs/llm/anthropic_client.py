"""Anthropic SDK wrapper with BYOK + prompt caching + retry + streaming.

Used by the Strategist, Executor and chat editors. BYOK = the caller passes
their own Anthropic API key per request; we never hold a server-wide key.

Prompt caching (see docs/prompt-caching): caching is a **prefix match** and a
breakpoint below the model's minimum cacheable size (4096 tokens on Opus,
2048 on Sonnet/Fable, 1024 on older Sonnet) silently does nothing. Two levers
this client exposes make it actually pay off:

* ``system_suffix`` — a second, separately-cached system block. Per-page
  Executor calls share one big cached prefix (the executor system prompt +
  the spec_lock/brief suffix), so the spec_lock is written to cache once and
  read back on every subsequent page instead of re-sent in each user message.
* ``user_suffix`` — the volatile tail of a user message. The stable
  ``user_message`` prefix carries a cache breakpoint; a retry re-sends the
  identical prefix (cache read) and only the changed reminder in
  ``user_suffix`` is uncached. Retries stop re-paying for the whole prompt.

Streaming is used automatically for large ``max_output_tokens`` so long
generations don't hit the SDK's ~10-minute non-streaming HTTP timeout.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Default model. Opus 4.7 (1M context). Callers pass their own model per
# request (BYOK); role-tiered overrides live in xgen_edit2docs.config.
DEFAULT_MODEL = "claude-opus-4-7"

# Retries on transient failures (429, 5xx). Backoff handled by the SDK.
DEFAULT_MAX_RETRIES = 3

# Above this output-token request size, stream to avoid the SDK's
# non-streaming timeout guard (it refuses long non-streaming requests).
_STREAM_THRESHOLD = 16000

# Cache-cost weights (relative to a full-price input token): a cache write
# costs ~1.25x, a cache read ~0.1x. Used only for the honest cost estimate.
_CACHE_WRITE_WEIGHT = 1.25
_CACHE_READ_WEIGHT = 0.10


@dataclass
class LLMUsage:
    """Per-call token accounting, mirrored from anthropic.types.Usage."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total_prompt_tokens(self) -> int:
        """Every prompt token, however billed (uncached + written + read)."""
        return self.input_tokens + self.cache_write_tokens + self.cache_read_tokens

    @property
    def cost_input_equiv_tokens(self) -> float:
        """Input-side cost in full-price-token equivalents.

        Cache reads bill at ~0.1x and writes at ~1.25x, so this reflects the
        real input cost — unlike a plain ``input + output`` sum, which
        ignores cache tokens entirely (they are billed, just at a discount).
        """
        return (
            self.input_tokens
            + self.cache_write_tokens * _CACHE_WRITE_WEIGHT
            + self.cache_read_tokens * _CACHE_READ_WEIGHT
        )

    def __add__(self, other: LLMUsage) -> LLMUsage:
        return LLMUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
        )


@dataclass
class LLMResult:
    """One completion (streamed or not)."""

    text: str
    usage: LLMUsage
    model: str
    stop_reason: str | None


def build_create_kwargs(
    *,
    system_prompt: str,
    user_message: str,
    model: str,
    max_output_tokens: int,
    temperature: float | None,
    cache_system: bool,
    system_suffix: str | None,
    user_suffix: str,
) -> dict[str, Any]:
    """Assemble ``messages.create`` kwargs — a pure function, so the cache
    breakpoint placement is unit-testable without an API call.

    Breakpoint budget is 4 (API max). We place at most:
    * one on the main system block (when ``cache_system``),
    * one on ``system_suffix`` (the shared per-page tail),
    * one on the stable ``user_message`` prefix (so retries read it).
    """
    system_blocks: list[dict[str, Any]] = []
    main_block: dict[str, Any] = {"type": "text", "text": system_prompt}
    # When there's a suffix, the breakpoint that caches "everything so far"
    # belongs on the LAST block; otherwise on the main block.
    if cache_system and not system_suffix:
        main_block["cache_control"] = {"type": "ephemeral"}
    system_blocks.append(main_block)
    if system_suffix:
        suffix_block: dict[str, Any] = {"type": "text", "text": system_suffix}
        if cache_system:
            suffix_block["cache_control"] = {"type": "ephemeral"}
        system_blocks.append(suffix_block)

    # User content: cache the stable prefix so a retry (same prefix + a new
    # reminder in user_suffix) reads it instead of re-paying full price.
    if user_suffix:
        user_content: Any = [
            {"type": "text", "text": user_message, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": user_suffix},
        ]
    else:
        user_content = user_message

    create_kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_output_tokens,
        "system": system_blocks,
        "messages": [{"role": "user", "content": user_content}],
    }
    if temperature is not None:
        create_kwargs["temperature"] = temperature
    return create_kwargs


class AnthropicClient:
    """Thin BYOK-friendly wrapper around ``anthropic.AsyncAnthropic``.

    The SDK is imported lazily so test code (which never calls ``complete``)
    doesn't need the ``anthropic`` package installed.
    """

    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_MODEL,
        max_retries: int = DEFAULT_MAX_RETRIES,
        timeout_seconds: float = 600.0,
        base_url: str | None = None,
    ):
        if not api_key:
            raise ValueError("api_key is required (BYOK — caller must pass their own key)")
        self._api_key = api_key
        self._model = model
        self._max_retries = max_retries
        self._timeout_seconds = timeout_seconds
        self._base_url = base_url
        self._client = None  # lazy

    def _ensure_client(self):
        if self._client is None:
            try:
                from anthropic import AsyncAnthropic  # type: ignore[import-not-found]
            except ImportError as exc:
                raise RuntimeError(
                    "`anthropic` package not installed. Install with: "
                    "uv pip install anthropic (or pip install anthropic)"
                ) from exc
            kwargs: dict[str, Any] = {
                "api_key": self._api_key,
                "max_retries": self._max_retries,
                "timeout": httpx.Timeout(self._timeout_seconds),
            }
            if self._base_url:
                kwargs["base_url"] = self._base_url
            self._client = AsyncAnthropic(**kwargs)
        return self._client

    async def complete(
        self,
        system_prompt: str,
        user_message: str,
        *,
        max_output_tokens: int = 8192,
        temperature: float | None = None,
        cache_system: bool = True,
        model: str | None = None,
        system_suffix: str | None = None,
        user_suffix: str = "",
        stream: bool | None = None,
    ) -> LLMResult:
        """One completion with prompt caching + optional streaming.

        Args:
            system_prompt: the primary (large, stable) system prompt.
            user_message: the stable user prefix. When ``user_suffix`` is
                given, this prefix carries a cache breakpoint so retries
                read it back.
            system_suffix: a second cached system block shared across calls
                (e.g. the per-deck spec_lock / layout brief). Written to
                cache once, read on every subsequent page.
            user_suffix: the volatile user tail (e.g. a retry reminder);
                never cached.
            stream: force streaming on/off; ``None`` auto-streams for large
                ``max_output_tokens``.

        ``temperature`` is omitted from the SDK call when None — recent
        Claude models (Opus 4.7+) reject the parameter.
        """
        client = self._ensure_client()
        create_kwargs = build_create_kwargs(
            system_prompt=system_prompt,
            user_message=user_message,
            model=model or self._model,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
            cache_system=cache_system,
            system_suffix=system_suffix,
            user_suffix=user_suffix,
        )

        use_stream = max_output_tokens > _STREAM_THRESHOLD if stream is None else stream
        if use_stream:
            async with client.messages.stream(**create_kwargs) as stream_ctx:
                response = await stream_ctx.get_final_message()
        else:
            response = await client.messages.create(**create_kwargs)

        return LLMResult(
            text=_extract_text(response),
            usage=_extract_usage(response),
            model=create_kwargs["model"],
            stop_reason=getattr(response, "stop_reason", None),
        )


def _extract_text(response: Any) -> str:
    """Concatenate text blocks from a Messages response."""
    parts: list[str] = []
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", ""))
    return "".join(parts)


def _extract_usage(response: Any) -> LLMUsage:
    usage = getattr(response, "usage", None)
    if usage is None:
        return LLMUsage()
    return LLMUsage(
        input_tokens=getattr(usage, "input_tokens", 0) or 0,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
        cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
    )
