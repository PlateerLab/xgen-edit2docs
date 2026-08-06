"""Token-optimization guardrails for the EDIT/CHAT doc pipelines.

Structural/assembly tests — no API key, no ``anthropic`` package. A recording
fake ``AnthropicClient`` captures every ``complete()`` call's user_message /
user_suffix / model so we can assert:

* the plan-missing RETRY re-sends the SAME ``user_message`` (cache read) and
  carries the reminder in ``user_suffix`` instead of re-paying the whole
  prompt (edit_doc; edit_deck is covered in test_edit_robustness),
* a large docx outline is WINDOWED under the char budget while preserving the
  ``para N`` address format, and a small one is sent verbatim,
* an edit op targeting a windowed-in paragraph still resolves + applies,
* planner model tiering honours EDIT2DOCS_MODEL_PLANNER.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field

import pytest

from xgen_edit2docs.config import reset_settings_cache
from xgen_edit2docs.documents.docx_engine import docx_from_markdown, docx_outline
from xgen_edit2docs.llm import DEFAULT_MODEL
from xgen_edit2docs.llm.anthropic_client import LLMResult, LLMUsage
from xgen_edit2docs.tools.edit_doc import EditDocRequest, _outline_context, edit_document


@dataclass
class _RecordingLLM:
    """Records each complete() call's user_message + volatile kwargs."""

    outputs: list[str]
    calls: list[dict] = field(default_factory=list)

    async def complete(self, system_prompt, user_message, **kwargs):
        self.calls.append(
            {
                "system": system_prompt,
                "user": user_message,
                "user_suffix": kwargs.get("user_suffix", ""),
                "model": kwargs.get("model"),
            }
        )
        text = self.outputs[min(len(self.calls) - 1, len(self.outputs) - 1)]
        return LLMResult(
            text=text,
            usage=LLMUsage(input_tokens=1, output_tokens=1),
            model="stub",
            stop_reason="end_turn",
        )


def _wire(monkeypatch, llm) -> None:
    module = sys.modules["xgen_edit2docs.tools.edit_doc"]
    monkeypatch.setattr(module, "AnthropicClient", lambda **kw: llm)


def _big_docx(n_paras: int = 400) -> bytes:
    """A docx whose full outline exceeds the windowing budget.

    One Heading 1 (para 0) then n_paras Normal paragraphs (para 1..n), each
    long enough that the outline blows past ~40k chars.
    """
    md = "\n\n".join(
        ["# Big Report"] + [f"P{i} " + "내용 " * 40 for i in range(n_paras)]
    )
    return docx_from_markdown(md)


def _small_docx(n_paras: int = 20) -> bytes:
    md = "\n\n".join(["# Small"] + [f"P{i} short body text" for i in range(n_paras)])
    return docx_from_markdown(md)


_NO_PLAN = "고치겠습니다 — 좋은 방향으로 정리할게요."  # no edit_plan fenced block
_GOOD_PLAN = (
    "```reply\n반영했습니다.\n```\n"
    "```edit_plan\noperations:\n"
    "  - action: replace\n    para: 1\n    new_text: \"수정된 첫 문단\"\n```"
)


# ---------------------------------------------------------------------------
# Retry cache reuse
# ---------------------------------------------------------------------------


class TestRetryCacheReuse:
    @pytest.mark.asyncio
    async def test_edit_doc_retry_reuses_user_message(self, monkeypatch):
        content = docx_from_markdown("첫 문단\n\n둘째 문단")
        llm = _RecordingLLM(outputs=[_NO_PLAN, _GOOD_PLAN])
        _wire(monkeypatch, llm)

        resp = await edit_document(
            EditDocRequest(
                content=content, fmt="docx", instruction="문단 정리해줘",
                anthropic_api_key="sk-stub",
            )
        )
        assert len(llm.calls) == 2
        # Byte-identical stable prefix → the retry reads the cache write.
        assert llm.calls[0]["user"] == llm.calls[1]["user"]
        # Reminder rode in the volatile suffix, NOT concatenated into user.
        assert llm.calls[0]["user_suffix"] == ""
        assert "REMINDER" in llm.calls[1]["user_suffix"]
        assert "REMINDER" not in llm.calls[1]["user"]
        # And the retry's plan actually applied.
        assert resp.changed is True


# ---------------------------------------------------------------------------
# Outline windowing
# ---------------------------------------------------------------------------


class TestOutlineWindowing:
    def test_large_docx_is_windowed_under_budget(self):
        req = EditDocRequest(
            content=_big_docx(400), fmt="docx", instruction="전체 톤 정리",
            anthropic_api_key="sk-stub",
        )
        warnings: list = []
        outline = _outline_context(req, warnings)

        assert len(outline) <= 40000
        assert "WINDOWED" in outline
        # Address format preserved for the shown paragraphs.
        assert "- para 0 [" in outline
        assert "- para 400 [" in outline
        # A mid-document paragraph is omitted (windowed out).
        assert "- para 200 [" not in outline
        assert "paragraphs omitted; ask to see a specific range" in outline
        # Warning emitted with honest accounting.
        w = next(w for w in warnings if w.code == "outline_windowed")
        assert w.detail["total_paragraphs"] == 401
        assert w.detail["shown_paragraphs"] < 401
        assert w.detail["anchored"] is False

    def test_small_docx_is_unchanged_no_warning(self):
        req = EditDocRequest(
            content=_small_docx(20), fmt="docx", instruction="x",
            anthropic_api_key="sk-stub",
        )
        warnings: list = []
        outline = _outline_context(req, warnings)

        assert "WINDOWED" not in outline
        assert warnings == []
        assert outline.startswith("# Document outline (paragraph addresses)\n")
        # Every paragraph is present (nothing omitted).
        assert "paragraphs omitted" not in outline

    def test_anchor_centers_window_on_referenced_paragraph(self):
        req = EditDocRequest(
            content=_big_docx(400), fmt="docx",
            instruction="para 250 문단을 더 명확하게 다시 써줘",
            anthropic_api_key="sk-stub",
        )
        warnings: list = []
        outline = _outline_context(req, warnings)

        assert "- para 250 [" in outline
        # Neighbours within the anchor radius are present too.
        assert "- para 240 [" in outline and "- para 260 [" in outline
        w = next(w for w in warnings if w.code == "outline_windowed")
        assert w.detail["anchored"] is True

    def test_quoted_text_anchors_the_window(self):
        content = _big_docx(400)
        # Quote a phrase that only paragraph 250 (text "P249 …") contains.
        req = EditDocRequest(
            content=content, fmt="docx",
            instruction='"P249" 로 시작하는 문단을 고쳐줘',
            anthropic_api_key="sk-stub",
        )
        warnings: list = []
        outline = _outline_context(req, warnings)
        assert "- para 250 [" in outline
        assert next(
            w for w in warnings if w.code == "outline_windowed"
        ).detail["anchored"] is True


# ---------------------------------------------------------------------------
# Address integrity through a windowed outline
# ---------------------------------------------------------------------------


class TestWindowedAddressIntegrity:
    @pytest.mark.asyncio
    async def test_edit_op_on_windowed_in_paragraph_applies(self, monkeypatch):
        content = _big_docx(400)
        # Confirm para 250 is what the planner would see in the window.
        warnings: list = []
        req = EditDocRequest(
            content=content, fmt="docx",
            instruction="para 250 문단을 고쳐줘", anthropic_api_key="sk-stub",
        )
        outline = _outline_context(req, warnings)
        assert "- para 250 [" in outline

        plan = (
            "```reply\n250번 문단을 갱신했습니다.\n```\n"
            "```edit_plan\noperations:\n"
            "  - action: replace\n    para: 250\n"
            "    new_text: \"윈도우 대상 문단 갱신됨\"\n```"
        )
        llm = _RecordingLLM(outputs=[plan])
        _wire(monkeypatch, llm)
        resp = await edit_document(req)

        assert resp.changed is True
        assert len(resp.operations) == 1
        target = next(e for e in docx_outline(resp.content) if e.get("para") == 250)
        assert target["text"] == "윈도우 대상 문단 갱신됨"


# ---------------------------------------------------------------------------
# Planner model tiering
# ---------------------------------------------------------------------------


class TestPlannerModelTiering:
    @pytest.fixture(autouse=True)
    def _clean_settings(self, monkeypatch):
        monkeypatch.delenv("EDIT2DOCS_MODEL_PLANNER", raising=False)
        reset_settings_cache()
        yield
        monkeypatch.delenv("EDIT2DOCS_MODEL_PLANNER", raising=False)
        reset_settings_cache()

    @pytest.mark.asyncio
    async def test_env_override_selects_planner_model(self, monkeypatch):
        monkeypatch.setenv("EDIT2DOCS_MODEL_PLANNER", "claude-sonnet-5")
        reset_settings_cache()
        llm = _RecordingLLM(outputs=[_GOOD_PLAN])
        _wire(monkeypatch, llm)
        await edit_document(
            EditDocRequest(
                content=docx_from_markdown("첫 문단\n\n둘째 문단"), fmt="docx",
                instruction="고쳐줘", model="claude-opus-4-7",
                anthropic_api_key="sk-stub",
            )
        )
        assert llm.calls[0]["model"] == "claude-sonnet-5"

    @pytest.mark.asyncio
    async def test_unset_uses_request_model(self, monkeypatch):
        llm = _RecordingLLM(outputs=[_GOOD_PLAN])
        _wire(monkeypatch, llm)
        await edit_document(
            EditDocRequest(
                content=docx_from_markdown("첫 문단\n\n둘째 문단"), fmt="docx",
                instruction="고쳐줘", model=DEFAULT_MODEL,
                anthropic_api_key="sk-stub",
            )
        )
        assert llm.calls[0]["model"] == DEFAULT_MODEL
