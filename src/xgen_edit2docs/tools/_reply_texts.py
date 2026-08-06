"""Localized fallback texts for the chat editors' replies.

The editors answer in the conversation's language (``req.lang``). These are
the deterministic notices the engine itself appends to LLM replies —
plan-generation failure, operation-cap truncation, generic acknowledgement.
English-first; Korean fully supported. Other locales fall back to English
(the LLM-authored part of the reply is already in the right language via
the output-language directive).
"""

from __future__ import annotations

__all__ = ["reply_text"]

_TEXTS: dict[str, dict[str, str]] = {
    "plan_failed": {
        "en": (
            "\n\n[Notice] The edit plan could not be generated, so no changes "
            "were applied. Please split the request into more specific steps "
            "and try again."
        ),
        "ko": (
            "\n\n[주의] 편집 계획 생성에 실패해 변경이 적용되지 않았습니다. "
            "요청을 조금 더 구체적으로 나눠서 다시 보내주세요."
        ),
    },
    "plan_truncated": {
        "en": (
            "\n\n[Notice] {emitted} operations were planned; applying the "
            "first {cap} this turn (operation cap). Send the same request "
            "again to continue with the rest."
        ),
        "ko": (
            "\n\n[안내] 계획된 작업 {emitted}개 중 상한에 따라 앞 {cap}개만 "
            "이번 턴에 적용합니다. 나머지는 같은 요청을 한 번 더 보내면 "
            "이어서 처리됩니다."
        ),
    },
    "request_done": {
        "en": "Done.",
        "ko": "요청을 처리했습니다.",
    },
}


def reply_text(key: str, lang: str, **vars: object) -> str:
    """Fetch a reply snippet in the turn's language (ko -> Korean, else English)."""
    entry = _TEXTS[key]
    text = entry["ko"] if (lang or "").lower().startswith("ko") else entry["en"]
    return text.format(**vars) if vars else text
