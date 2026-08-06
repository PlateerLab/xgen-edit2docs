"""LLM client + prompt loader for xgen_edit2docs server-side orchestration."""

from .anthropic_client import AnthropicClient, DEFAULT_MODEL, LLMResult, LLMUsage
from .prompt_loader import (
    KNOWN_ROLES,
    build_output_lang_directive,
    list_available_locales,
    list_available_prompts,
    load_prompt,
)

__all__ = [
    "AnthropicClient",
    "DEFAULT_MODEL",
    "LLMResult",
    "LLMUsage",
    "KNOWN_ROLES",
    "build_output_lang_directive",
    "list_available_locales",
    "list_available_prompts",
    "load_prompt",
]
