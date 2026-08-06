"""Unit tests confirming strategize + execute prepend the output-lang directive
to the LLM system prompt."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from xgen_edit2docs.llm.anthropic_client import LLMResult, LLMUsage
from xgen_edit2docs.tools import (
    ExecuteBatchRequest,
    ExecutePageRequest,
    StrategizeRequest,
    execute_batch,
    strategize,
)


@dataclass
class _Capture:
    """Records the system_prompt passed into the stub LLM, then returns a
    deterministic response so the caller can finish parsing."""

    response: str = ""
    calls: list[dict] = field(default_factory=list)

    async def complete(self, system_prompt, user_message, **kwargs):
        self.calls.append({"system": system_prompt, "user": user_message, **kwargs})
        return LLMResult(
            text=self.response,
            usage=LLMUsage(input_tokens=10, output_tokens=10),
            model="stub",
            stop_reason="end_turn",
        )


class TestStrategizeLangDirective:
    @pytest.mark.asyncio
    async def test_korean_lang_prepends_directive(self):
        llm = _Capture(
            response="```design_spec\n# Korean\n## Page 1\nfoo\n```\n```spec_lock\nlang: ko-KR\n```",
        )
        await strategize(
            StrategizeRequest(
                sources_markdown=["# x"],
                user_intent="한국어 자료",
                lang="ko-KR",
                anthropic_api_key="stub",
            ),
            client=llm,
        )
        system_prompt = llm.calls[0]["system"]
        # The directive sits at the top of the system prompt.
        assert system_prompt.startswith("# Output Language")
        assert "Korean" in system_prompt.split("---")[0]
        assert "ko-KR" in system_prompt.split("---")[0]
        # And then the actual Strategist body follows.
        assert "Role: Strategist" in system_prompt

    @pytest.mark.asyncio
    async def test_english_lang_prepends_english_directive(self):
        llm = _Capture(
            response="```design_spec\n# x\n## Page 1\n.\n```\n```spec_lock\nlang: en-US\n```",
        )
        await strategize(
            StrategizeRequest(
                sources_markdown=["# x"],
                user_intent="English source",
                lang="en-US",
                anthropic_api_key="stub",
            ),
            client=llm,
        )
        directive_head = llm.calls[0]["system"].split("---")[0]
        assert "English" in directive_head
        assert "en-US" in directive_head


class TestExecuteLangDirective:
    @pytest.mark.asyncio
    async def test_executor_korean_lang_threads_directive(self):
        llm = _Capture(response="```svg\n<svg></svg>\n```\n```notes\n.\n```")
        await execute_batch(
            ExecuteBatchRequest(
                spec_lock="lang: ko-KR",
                pages=[
                    ExecutePageRequest(
                        spec_lock="lang: ko-KR",
                        page_index=0,
                        page_summary="...",
                        lang="ko-KR",
                        anthropic_api_key="stub",
                        style="general",
                    )
                ],
            ),
            client=llm,
        )
        system_prompt = llm.calls[0]["system"]
        assert system_prompt.startswith("# Output Language")
        # Korean label, the executor-base prompt body, AND the style variant
        # all show up.
        first_section, _, rest = system_prompt.partition("---")
        assert "Korean" in first_section and "ko-KR" in first_section
        assert "executor" in rest.lower() or "Executor" in rest
