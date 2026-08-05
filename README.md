# edit2docs

**AI-agent-native document engine — DOCX · XLSX · PPTX. English-first, with first-class Korean support.**

[![PyPI](https://img.shields.io/pypi/v/edit2docs)](https://pypi.org/project/edit2docs/)
[![Python](https://img.shields.io/badge/python-3.12%2B-blue)](https://pypi.org/project/edit2docs/)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-green)](./LICENSE)

[한국어 README](./README.ko.md)

`edit2docs` generates complete Office documents from a one-line intent and
chat-edits existing files — Word reports, Excel workbooks, PowerPoint decks —
always producing **natively editable OOXML** (real paragraphs, real cells,
real charts — never screenshots of them). One engine, four surfaces: import
it, hand it to an agent, plug it into an MCP client, or run it as a service.

```bash
pip install edit2docs              # library + agent tools + local MCP
pip install "edit2docs[server]"    # + the hosted multi-tenant service
```

```python
from edit2docs import generate_doc, edit_doc

generate_doc("Executive briefing on Q3 sales", output="deck.pptx")
r = edit_doc("deck.pptx", "Make slide 3's title more assertive")
print(r.reply)          # the editor explains what it changed
```

---

## Ecosystem

| Repo | What it is |
|---|---|
| **[edit2docs](https://github.com/CocoRoF/edit2docs)** (this repo) | The engine: library · agent tools · MCP · hosted FastAPI service |
| **[edit2docs-web](https://github.com/CocoRoF/edit2docs-web)** | Web studio for the hosted service — upload, generate, chat-edit with a live addressable preview, per-op edit highlighting, EN/KO UI. Next.js 15 / React 19 / Tailwind |
| [ppt-master](https://github.com/hugohe3/ppt-master) | Upstream project (MIT) the PPTX core is derived from — synced through v3.1 |
| [edit2ppt](https://github.com/CocoRoF/edit2ppt) | Sister project; the deck pipeline and hosted service originate there |

A production deployment of engine + studio runs behind
[hr_blog2.0](https://github.com/CocoRoF/hr_blog2.0)'s compose stack — its
`edit2docs-server/` and `edit2docs-web/` service dirs are a working reference
for wiring both containers behind nginx.

---

## The seven verbs

Every surface exposes the same seven **format-dispatched** verbs — the file
extension picks the engine. Deterministic verbs need no API key; generative
ones are BYOK (`api_key=...` or `ANTHROPIC_API_KEY`).

| verb | what it does | LLM? |
|---|---|---|
| `generate_doc` | intent (+ optional sources / PPTX template) → complete document | ✳ |
| `edit_doc` | one natural-language edit turn; untouched content stays **byte-identical** | ✳ |
| `preview_doc` | .pptx → per-slide SVG · .docx/.xlsx → markdown | — |
| `render_doc` | any format → page **PNGs / PDF / SVGs** — no LibreOffice, no subprocess | — |
| `analyze_doc` | structure outline **with the exact addresses `set_doc_text` / `edit_chart` need** (incl. a `charts` list) | — |
| `set_doc_text` | deterministic targeted edits — **lossless** (charts / images / styles / formulas survive) | — |
| `edit_chart` | deterministically edit a native chart's **data or title** — rewrites the chart *and* its embedded workbook | — |

**Lossless editing.** The deterministic edit verbs run on
[contextifier](https://github.com/CocoRoF/Contextifier)'s raw OOXML layer:
edits are surgical and untouched package parts stay byte-identical, so a
chart, pivot table, sparkline, inline image or cached formula is never
collateral damage of a text edit. The PPTX chat editor (`edit_doc` on a
deck) likewise **preserves native charts and tables** on slides it
regenerates, instead of flattening them into pictures.

---

## 1 · Python library

### Generate — the output extension picks the engine

```python
from edit2docs import generate_doc

generate_doc("Q3 performance report", output="report.docx")
generate_doc("Quarterly sales summary", output="sales.xlsx", sources=["raw.pdf"])
generate_doc("Executive briefing on Q3 sales", output="deck.pptx",
             template="brand.pptx",          # optional user PPTX template
             deck_mode="template_restyle",   # "new" | "template_restyle" | "template_extend"
             pages=(8, 12))                  # target page range (pptx)
generate_doc("3분기 실적 보고서", output="report.docx", lang="ko-KR")  # any language, same call
```

Full signature: `generate_doc(intent, *, output, api_key=None, sources=None,
template=None, deck_mode="new", pages=(8, 12), lang="en-US", model=...)` →
`GenerateResult(path, page_count, design_spec, warnings)`.

`sources` accepts PDF / DOCX / DOC / PPTX / XLSX / HTML / EPUB / IPYNB paths —
each is converted to markdown and given to the writer as reference material.

### Edit — one chat turn, everything else byte-identical

```python
from edit2docs import edit_doc

r = edit_doc("report.docx", "Add a 'deployment complete' item to the progress section")
print(r.reply)        # what the editor did, in your language
print(r.operations)   # the applied ops, e.g. [{"action": "insert_after", ...}]

r = edit_doc("deck.pptx", "이 문서 내용을 반영해서 3번 슬라이드를 고쳐줘",
             sources=["notes.pdf"], lang="ko-KR",
             chat_history=[{"role": "user", "content": "..."},
                           {"role": "assistant", "content": "..."}])
```

The planner sees a numbered outline of your document, plans the **minimal**
operations, and the deterministic engine applies them — untouched paragraphs,
cells and slides survive byte-for-byte. If planning fails, the reply says so
honestly instead of pretending (no silent no-ops).

### Inspect & edit deterministically (no LLM, no key)

```python
from edit2docs import analyze_doc, set_doc_text, preview_doc, render_doc

info = analyze_doc("report.docx")
# {"format": "docx", "outline": [
#    {"para": 0, "style": "Heading 1", "text": "Q3 Report"},
#    {"table": 0, "row": 1, "col": 2, "text": "142"}, ...]}   ← addresses

set_doc_text("report.docx", [
    {"para": 0, "new_text": "Q3 Final Report"},               # docx: replace / insert_after / delete
])
set_doc_text("sales.xlsx", [
    {"sheet": "Sales", "cell": "B3", "value": 142},           # xlsx: set_cell / append_rows / add_sheet
])
set_doc_text("deck.pptx", [
    {"slide": 0, "shape_id": 2, "para": 0, "new_text": "New title"},  # pptx
])

# Edit a native chart's data or title — the chart stays a real, editable
# PowerPoint/Excel chart (its embedded workbook is rewritten too).
from edit2docs import edit_chart, list_charts

list_charts("deck.pptx")   # [{"chart": 0, "kind": "bar", "title": ..., "series": [...]}]
edit_chart("deck.pptx", [
    {"chart": 0, "title": "Q3 Sales"},
    {"chart": 0, "categories": ["Q1", "Q2", "Q3"],
     "series": [{"name": "Sales", "values": [120, 135, 150]}]},
])

preview_doc("deck.pptx", out_dir="previews")   # per-slide self-contained SVGs
render_doc("report.docx", to="pdf")            # page PNGs / a PDF / raw SVGs
render_doc("deck.pptx", to="png", dpi=200)     # resvg raster — no LibreOffice
```

Async variants exist for the generative verbs: `async_generate_doc`,
`async_edit_doc` (use inside an existing event loop).

---

## 2 · Agent tools (function calling)

The same seven verbs as Anthropic tool-use schemas plus a dispatcher:

```python
import anthropic
from edit2docs.agent_tools import ANTHROPIC_TOOLS, run_tool

client = anthropic.Anthropic()
msg = client.messages.create(
    model="claude-sonnet-5",
    max_tokens=2048,
    tools=ANTHROPIC_TOOLS,
    messages=[{"role": "user", "content": "Fix the title of slide 3 in deck.pptx"}],
)
for block in msg.content:
    if block.type == "tool_use":
        result = run_tool(block.name, block.input)   # sync; run_tool_async also exists
```

---

## 3 · Local MCP server (zero infra)

`pip install edit2docs` ships an `edit2docs-mcp` stdio server exposing all six
verbs over local files:

```jsonc
// Claude Desktop / Claude Code / Cursor
{
  "mcpServers": {
    "edit2docs": {
      "command": "edit2docs-mcp",
      "env": { "ANTHROPIC_API_KEY": "sk-ant-..." }   // only generative tools need it
    }
  }
}
```

Then just talk: *"generate a 10-page deck about our roadmap as
~/decks/roadmap.pptx, then render it to PDF"*.

---

## 4 · Hosted service

```bash
pip install "edit2docs[server]"
edit2docs serve                    # FastAPI on :8000 — standalone mode
```

Standalone mode needs **zero external infra**: SQLite + local-fs storage + an
inline job queue, auto-bootstrapped on first start. Add Postgres / Redis / S3
via env vars when you outgrow it.

| REST endpoint | purpose |
|---|---|
| `POST /v1/assets` · `GET /v1/assets/{id}` | upload / fetch documents (200 MB cap) |
| `POST /v1/jobs/generate-deck` · `/v1/jobs/edit-deck` | queue generative jobs (any of the 3 formats) |
| `GET /v1/jobs/{id}` · `GET /v1/jobs/{id}/events` | job status · **SSE progress stream** (stages + per-operation live-edit events with addressable targets) |
| `POST /v1/preview` | pptx → per-slide SVGs · docx/xlsx → **addressable HTML** |
| `POST /v1/text-edits` | deterministic targeted edits |
| `GET /health` | liveness + mode report |
| `/mcp` · `/mcp-sse` | the same verbs over MCP (Streamable HTTP / SSE) |

Anthropic keys are **BYOK per request** (`X-Anthropic-API-Key` header) — never
persisted. Errors come back bilingual: `message` follows the request's
`Accept-Language`, with `message_en` / `message_ko` always present.

Key env vars (prefix `EDIT2DOCS_`):

| var | default | notes |
|---|---|---|
| `EDIT2DOCS_DEFAULT_LANG` | `en-US` | set `ko-KR` to make a deployment Korean-by-default |
| `EDIT2DOCS_DATA_DIR` | `/data/edit2docs` | standalone SQLite + file storage root |
| `EDIT2DOCS_DATABASE_URL` | (sqlite) | e.g. `postgresql+asyncpg://...` |
| `EDIT2DOCS_REDIS_URL` | (inline queue) | enables the arq worker queue |
| `EDIT2DOCS_S3_*` | (local fs) | endpoint / bucket / keys for S3-compatible storage |
| `EDIT2DOCS_AUTH_DEV_API_KEY` | (anonymous) | single bearer token for small deployments |
| `EDIT2DOCS_MAX_UPLOAD_SIZE_BYTES` | 200 MB | match your reverse proxy |
| `EDIT2DOCS_MODEL_{PLANNER,WRITER,STRATEGIST,EXECUTOR}` | (request model) | per-role model override — run cheap planner/writer turns on a smaller model without touching callers |
| `EDIT2DOCS_STRATEGIST_SOURCE_CHAR_CAP` | 60000 | cap per source doc fed to the deck strategist (0 = uncapped) |

### The web studio

[**edit2docs-web**](https://github.com/CocoRoF/edit2docs-web) is the official
frontend for this service: drag-and-drop upload, generation with staged SSE
progress, and a co-editing studio where the chat edits your document while the
canvas **highlights the exact paragraph / cell / slide each operation
touches** (the preview HTML carries `data-e2d-*` addresses; PPTX slides carry
`data-e2p-*`). English-first UI with a KO/EN toggle. Point it at the engine
with `EDIT2DOCS_SERVER_INTERNAL_URL` + `EDIT2DOCS_SERVER_API_KEY`.

---

## How each format works

* **DOCX** — the writer LLM emits a constrained markdown document; a
  deterministic renderer (python-docx) turns it into styled Word. Edits are
  paragraph-addressed operations (`replace` / `insert_after` / `delete`,
  table cells by `table`/`row`/`col`). The hosted preview is a native,
  *addressable* HTML rendering — every paragraph carries `data-e2d-para`,
  every cell `data-e2d-cell`, the same addresses the outline and the
  live-edit op stream use — with real merged cells, alignment, colors,
  images, footnotes and page breaks.
* **XLSX** — the designer LLM emits a YAML *sheet spec* (sheets / headers /
  rows / number formats, formulas allowed); openpyxl renders styled sheets.
  Edits are `set_cell` / `append_rows` / `add_sheet` with staleness guards.
  The hosted preview is a spreadsheet-style grid (column letters, row
  numbers, merged ranges, cached formula results), every cell stamped
  `data-e2d-cell="B3"` — exactly the address `set_cell` takes.
* **PPTX** — the full multi-stage pipeline: strategist → per-page SVG →
  native DrawingML, user-PPTX templates (restyle / extend), chat-edit with
  slide recompose, per-paragraph text edits incl. table cells, optional
  Edge-TTS narration. Exported text is **paragraph-merged** (edits as real
  paragraphs, not per-line boxes).

### Native charts & tables (PPTX)

SVG groups marked `data-pptx-native="chart|table"` export as **real,
editable PowerPoint objects** — a chart XML part with an embedded Excel
workbook (double-click it in PowerPoint and edit the data), or a native
`<a:tbl>` table — instead of drawn shapes:

```xml
<g id="sales_chart" data-pptx-native="chart">
  <metadata data-pptx-native="chart">
    { "name": "sales_chart",
      "x": 125, "y": 141, "width": 1000, "height": 440,
      "type": "bar",
      "categories": ["Q1", "Q2", "Q3"],
      "series": [{ "name": "Sales", "values": [120, 135, 150] }] }
  </metadata>
  <!-- fallback shapes, used when native export is off -->
</g>
```

Opt in with `ExportRequest(native_objects=True)` (`tools/export.py`).
Supported chart types: bar / column / line / area / pie / doughnut /
of-pie / radar (classic), scatter / bubble (XY), and box-whisker / funnel /
histogram / pareto / sunburst / treemap / waterfall (chartEx). The quality
checker validates marker payloads before export.

Every LLM planner follows the same contract: fenced `reply` + `edit_plan`
blocks, one retry with a format reminder, and an honest reply (instead of a
silent no-op) when planning fails.

---

## Languages

English is the default (`lang="en-US"`); **Korean is a first-class citizen,
not an afterthought** — Hangul-aware text widths, per-run OOXML `lang`
attributes detected from the actual script, Korean font stacks
(Pretendard / Malgun), a complete Korean message catalog, localized chat
replies and live-edit labels. Flip any call with `lang="ko-KR"`, any request
with `Accept-Language: ko-KR`, or a whole deployment with
`EDIT2DOCS_DEFAULT_LANG=ko-KR`. zh-CN / zh-TW / ja-JP get the same script
detection and font-stack treatment.

---

## Development

```bash
git clone https://github.com/CocoRoF/edit2docs && cd edit2docs
uv venv .venv && uv pip install -e ".[server,dev]"
.venv/bin/python -m pytest tests/          # 769 tests
.venv/bin/python -m ruff check src/edit2docs --exclude src/edit2docs/core
```

## Version history

| version | highlights |
|---|---|
| **v0.14.1** | dependency fix — cap `mcp < 2.0` (the 2.0 major dropped `mcp.server.fastmcp`; an uncapped floor crash-looped the hosted server) |
| v0.10–0.14 | **`build_doc`** (deterministic generation, no LLM) · **`read_doc_xml`/`set_doc_xml`** (direct OOXML editing, create/delete parts) · agent surface consolidated to **8 verbs** · **hierarchical `doc_guide`** (progressive-disclosure skill map) · **themed decks** (deterministic design in one `build_doc` call) |
| **v0.9.0** | **token optimization** — prompt-cache restructuring (edit retries read the cached prefix ~10× cheaper; per-page executor spec_lock cached once, not re-sent), fan-out cache warm-up, unbounded-input caps (strategist sources, edit outline windowing), retry-severity tiering, per-role model tiering, streaming, honest cache accounting + per-stage cost |
| v0.8.0 | **lossless editing** on [contextifier](https://github.com/CocoRoF/Contextifier)'s raw OOXML layer — set_doc_text/edit_doc no longer destroy charts, images, sparklines, styles or cached formulas; PPTX chat-edit preserves native charts/tables; new **`edit_chart`** verb (data + title, embedded workbook synced) |
| v0.7.0 | upstream sync (ppt-master v2.7 → v3.1, 3 waves): **native chart/table export**, paragraph-merge editability, PowerPoint repair-prompt fixes, checker hardening · **English-first flip** with full Korean support |
| v0.5–0.6 | `render_doc` — native page rendering to PNG/PDF/SVG for all 3 formats (resvg + PyMuPDF, no LibreOffice) |
| v0.4.0 | addressable native previews (`data-e2d-*`) — preview, outline and editor share one address space |
| v0.3.0 | live edit streaming — per-operation SSE events with addressable targets |
| v0.2.x | multi-format hosted API + full-format hardening |
| v0.1.0 | multi-format engine: the core verbs across DOCX/XLSX/PPTX |

## License

[Apache-2.0](./LICENSE). The PPTX core under `src/edit2docs/core/` is derived
from [ppt-master](https://github.com/hugohe3/ppt-master) (MIT, © Hugo He) via
[edit2ppt](https://github.com/CocoRoF/edit2ppt) and kept in sync (currently
through upstream v3.1); the original MIT terms for those portions are
preserved in [NOTICE](./NOTICE) and
[LICENSE.ppt-master.MIT](./LICENSE.ppt-master.MIT).
