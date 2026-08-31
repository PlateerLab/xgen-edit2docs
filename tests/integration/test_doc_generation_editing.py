"""Stub-LLM tests for generate_document / edit_document + unified facade."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from xgen_edit2docs.documents.docx_engine import docx_from_markdown, docx_outline
from xgen_edit2docs.documents.xlsx_engine import xlsx_from_spec, xlsx_outline
from xgen_edit2docs.llm.anthropic_client import LLMResult, LLMUsage
from xgen_edit2docs.tools.edit_doc import EditDocRequest, edit_document
from xgen_edit2docs.tools.generate_doc import GenerateDocRequest, generate_document


@dataclass
class _ScriptedLLM:
    outputs: list[str]
    calls: list[dict] = field(default_factory=list)

    async def complete(self, system_prompt, user_message, **kwargs):
        # Capture the volatile tail (user_suffix) + resolved model so tests
        # can assert the retry-cache shape and model tiering.
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


def _wire(monkeypatch, module_name: str, llm) -> None:
    module = sys.modules[module_name]
    monkeypatch.setattr(module, "AnthropicClient", lambda **kw: llm)


DOC_MD = "# 주간 보고\n\n## 진행 사항\n- 스튜디오 배포 완료\n"
SHEET_YAML = (
    "sheets:\n"
    "  - name: \"진척\"\n"
    "    headers: [\"항목\", \"상태\"]\n"
    "    rows:\n"
    "      - [\"배포\", \"완료\"]\n"
)


class TestGenerateDocument:
    @pytest.mark.asyncio
    async def test_docx_generation(self, monkeypatch):
        llm = _ScriptedLLM(outputs=[f"```document\n{DOC_MD}```"])
        _wire(monkeypatch, "xgen_edit2docs.tools.generate_doc", llm)
        resp = await generate_document(
            GenerateDocRequest(intent="주간 보고서", fmt="docx",
                               anthropic_api_key="sk-stub")
        )
        texts = [e["text"] for e in docx_outline(resp.content)]
        assert "주간 보고" in texts and "스튜디오 배포 완료" in texts

    @pytest.mark.asyncio
    async def test_xlsx_generation_with_repair_retry(self, monkeypatch):
        llm = _ScriptedLLM(
            outputs=["말로만 하는 대답 (스펙 블록 없음)", f"```sheet_spec\n{SHEET_YAML}```"]
        )
        _wire(monkeypatch, "xgen_edit2docs.tools.generate_doc", llm)
        resp = await generate_document(
            GenerateDocRequest(intent="진척 시트", fmt="xlsx",
                               anthropic_api_key="sk-stub")
        )
        assert len(llm.calls) == 2
        assert "REMINDER" in llm.calls[1]["user"]
        outline = xlsx_outline(resp.content)
        assert outline["sheets"][0]["name"] == "진척"
        assert any(w.code == "generate_doc_retried" for w in resp.warnings)

    @pytest.mark.asyncio
    async def test_double_failure_raises_bilingual(self, monkeypatch):
        llm = _ScriptedLLM(outputs=["no block", "still no block"])
        _wire(monkeypatch, "xgen_edit2docs.tools.generate_doc", llm)
        with pytest.raises(ValueError, match="문서 생성에 실패"):
            await generate_document(
                GenerateDocRequest(intent="x", fmt="xlsx",
                                   anthropic_api_key="sk-stub")
            )


class TestEditDocument:
    @pytest.mark.asyncio
    async def test_docx_edit_turn(self, monkeypatch):
        content = docx_from_markdown(DOC_MD)
        target = next(e for e in docx_outline(content) if "배포 완료" in e["text"])
        plan = (
            "```reply\n진행 사항을 갱신합니다.\n```\n"
            "```edit_plan\noperations:\n"
            f"  - action: replace\n    para: {target['para']}\n"
            "    new_text: \"스튜디오 v2 배포 완료\"\n"
            "  - action: insert_after\n"
            f"    para: {target['para']}\n"
            "    markdown: \"- 다음 주: PyPI 발행\"\n"
            "```"
        )
        llm = _ScriptedLLM(outputs=[plan])
        _wire(monkeypatch, "xgen_edit2docs.tools.edit_doc", llm)
        resp = await edit_document(
            EditDocRequest(content=content, fmt="docx", instruction="진행사항 갱신",
                           anthropic_api_key="sk-stub")
        )
        assert resp.changed is True
        assert len(resp.operations) == 2
        texts = [e["text"] for e in docx_outline(resp.content)]
        assert any("v2 배포 완료" in t for t in texts)
        assert any("PyPI 발행" in t for t in texts)
        # planner saw the paragraph outline
        assert "# Document outline" in llm.calls[0]["user"]

    @pytest.mark.asyncio
    async def test_xlsx_edit_turn(self, monkeypatch):
        content = xlsx_from_spec({"sheets": [{"name": "진척", "headers": ["항목", "상태"],
                                              "rows": [["배포", "진행중"]]}]})
        plan = (
            "```reply\n상태를 갱신합니다.\n```\n"
            "```edit_plan\noperations:\n"
            "  - action: set_cell\n    sheet: \"진척\"\n    cell: \"B2\"\n"
            "    value: \"완료\"\n```"
        )
        llm = _ScriptedLLM(outputs=[plan])
        _wire(monkeypatch, "xgen_edit2docs.tools.edit_doc", llm)
        resp = await edit_document(
            EditDocRequest(content=content, fmt="xlsx", instruction="배포 상태 완료로",
                           anthropic_api_key="sk-stub")
        )
        assert resp.changed is True
        assert xlsx_outline(resp.content)["sheets"][0]["sample"][1][1] == "완료"

    @pytest.mark.asyncio
    async def test_question_only_turn_no_change(self, monkeypatch):
        content = docx_from_markdown(DOC_MD)
        llm = _ScriptedLLM(
            outputs=["```reply\n이 문서는 주간 보고서입니다.\n```\n```edit_plan\noperations: []\n```"]
        )
        _wire(monkeypatch, "xgen_edit2docs.tools.edit_doc", llm)
        resp = await edit_document(
            EditDocRequest(content=content, fmt="docx", instruction="이 문서 뭐야?",
                           anthropic_api_key="sk-stub")
        )
        assert resp.changed is False and resp.content == content
        assert "주간 보고서" in resp.reply

    @pytest.mark.asyncio
    async def test_plan_missing_retries_then_admits(self, monkeypatch):
        content = docx_from_markdown(DOC_MD)
        llm = _ScriptedLLM(outputs=["고치겠습니다.", "이번에도 계획 없음."])
        _wire(monkeypatch, "xgen_edit2docs.tools.edit_doc", llm)
        resp = await edit_document(
            EditDocRequest(content=content, fmt="docx", instruction="다 고쳐줘",
                           anthropic_api_key="sk-stub")
        )
        assert len(llm.calls) == 2
        assert resp.changed is False
        # English-first default: the engine's honesty notice is English…
        assert "no changes were applied" in resp.reply

    @pytest.mark.asyncio
    async def test_plan_missing_notice_localizes_to_korean(self, monkeypatch):
        content = docx_from_markdown(DOC_MD)
        llm = _ScriptedLLM(outputs=["고치겠습니다.", "이번에도 계획 없음."])
        _wire(monkeypatch, "xgen_edit2docs.tools.edit_doc", llm)
        resp = await edit_document(
            EditDocRequest(content=content, fmt="docx", instruction="다 고쳐줘",
                           lang="ko-KR", anthropic_api_key="sk-stub")
        )
        assert resp.changed is False
        assert "적용되지 않았습니다" in resp.reply


class TestUnifiedFacade:
    def test_extension_dispatch_and_deterministic_verbs(self, tmp_path: Path):
        from xgen_edit2docs import analyze_doc, preview_doc, set_doc_text

        docx_path = tmp_path / "r.docx"
        docx_path.write_bytes(docx_from_markdown(DOC_MD))
        xlsx_path = tmp_path / "s.xlsx"
        xlsx_path.write_bytes(
            xlsx_from_spec({"sheets": [{"name": "S", "headers": ["a"], "rows": [[1]]}]})
        )

        info = analyze_doc(docx_path)
        assert info["format"] == "docx"
        target = next(e for e in info["outline"] if "배포" in e["text"])
        out = tmp_path / "r2.docx"
        result = set_doc_text(
            docx_path,
            [{"para": target["para"], "new_text": "수정됨", "old_text": target["text"]}],
            output=out,
        )
        assert result.applied == 1
        assert "수정됨" in preview_doc(out)

        info2 = analyze_doc(xlsx_path)
        assert info2["format"] == "xlsx"
        r2 = set_doc_text(xlsx_path, [{"sheet": "S", "cell": "A2", "value": 42}])
        assert r2.applied == 1

        md_preview = preview_doc(xlsx_path, out_dir=tmp_path / "p")
        assert Path(md_preview).name == "preview.md"

    def test_unsupported_extension_raises(self, tmp_path: Path):
        from xgen_edit2docs import analyze_doc

        bad = tmp_path / "file.xyz"
        bad.write_bytes(b"x")
        with pytest.raises(ValueError, match="Unsupported document format"):
            analyze_doc(bad)

    def test_legacy_extension_contract(self, tmp_path: Path):
        """레거시(.hwp)는 읽기 계열에서 정규화 대상 — 깨진 파일이면
        LegacyConvertError, 편집 계열이면 정규화 전에 read-only 거부."""
        from xgen_edit2docs import analyze_doc, set_doc_text
        from xgen_edit2docs.documents.legacy import LegacyConvertError

        junk = tmp_path / "file.hwp"
        junk.write_bytes(b"x")
        with pytest.raises(LegacyConvertError):
            analyze_doc(junk)  # v0.19.0 회귀: xlsx 분기로 흘러 엉뚱한 에러가 났다
        with pytest.raises(ValueError, match="read-only"):
            set_doc_text(junk, [])

    def test_agent_tools_dispatch(self, tmp_path: Path):
        from xgen_edit2docs.agent_tools import TOOL_NAMES, run_tool

        assert TOOL_NAMES == [
            "doc_guide", "analyze_doc", "render_doc", "set_doc_text",
            "arrange_doc", "read_doc_xml", "set_doc_xml", "build_doc",
            "generate_doc", "edit_doc",
        ]
        docx_path = tmp_path / "r.docx"
        docx_path.write_bytes(docx_from_markdown(DOC_MD))
        info = run_tool("analyze_doc", {"doc": str(docx_path)})
        assert info["format"] == "docx"
        res = run_tool(
            "render_doc",
            {"doc": str(docx_path), "to": "md", "out_dir": str(tmp_path / "p")},
        )
        assert res["to"] == "md" and res["paths"][0].endswith("preview.md")


class TestEditStreaming:
    """Per-operation live-edit events for the studio."""

    @pytest.mark.asyncio
    async def test_docx_edit_emits_plan_and_op_events(self, monkeypatch):
        content = docx_from_markdown("첫 문단\n\n둘째 문단")
        plan = (
            "```reply\n두 문단을 수정합니다.\n```\n"
            "```edit_plan\noperations:\n"
            "  - action: replace\n    para: 0\n    new_text: \"수정1\"\n"
            "  - action: replace\n    para: 1\n    new_text: \"수정2\"\n```"
        )
        llm = _ScriptedLLM(outputs=[plan])
        _wire(monkeypatch, "xgen_edit2docs.tools.edit_doc", llm)

        events: list = []

        async def on_event(e):
            events.append(e)

        resp = await edit_document(
            EditDocRequest(content=content, fmt="docx", instruction="고쳐줘",
                           anthropic_api_key="sk-stub"),
            on_event=on_event,
        )
        assert resp.changed is True

        plan_evs = [e for e in events if "plan" in e.message_vars]
        assert len(plan_evs) == 1
        planned = plan_evs[0].message_vars["plan"]
        assert len(planned) == 2
        assert planned[0]["target"] == {"kind": "paragraph", "para": 0}
        assert "Replace paragraph 0" in planned[0]["label"]

        op_evs = [e for e in events if "op" in e.message_vars]
        done = [e.message_vars["op"] for e in op_evs if e.message_vars["op"]["phase"] == "done"]
        assert len(done) == 2
        assert all(o["status"] == "applied" for o in done)
        assert {o["target"]["para"] for o in done} == {0, 1}
        # stage bookends present
        stages = [e.stage for e in events]
        assert stages[0] == "planning_edits" and stages[-1] == "done"

    @pytest.mark.asyncio
    async def test_xlsx_edit_op_targets(self, monkeypatch):
        content = xlsx_from_spec({"sheets": [{"name": "S", "headers": ["a"], "rows": [[1]]}]})
        plan = (
            "```reply\n셀을 수정합니다.\n```\n"
            "```edit_plan\noperations:\n"
            "  - action: set_cell\n    sheet: \"S\"\n    cell: \"A2\"\n    value: 9\n```"
        )
        llm = _ScriptedLLM(outputs=[plan])
        _wire(monkeypatch, "xgen_edit2docs.tools.edit_doc", llm)
        events: list = []

        async def on_event(e):
            events.append(e)

        await edit_document(
            EditDocRequest(content=content, fmt="xlsx", instruction="A2를 9로",
                           anthropic_api_key="sk-stub"),
            on_event=on_event,
        )
        op_done = [
            e.message_vars["op"] for e in events
            if "op" in e.message_vars and e.message_vars["op"]["phase"] == "done"
        ]
        assert len(op_done) == 1
        assert op_done[0]["target"] == {"kind": "cell", "sheet": "S", "cell": "A2"}
