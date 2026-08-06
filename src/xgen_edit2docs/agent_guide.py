"""Hierarchical skill guide for the xgen_edit2docs tool set (progressive disclosure).

The tool set is organized like a Claude Skill: an ultra-compact tool list is
always in the model's context (the frontmatter), the family map loads on the
first ``doc_guide()`` call (the body), and deep per-task guides load on demand
by topic (the resources). The hierarchy splits **Generate vs Edit** first,
then by mechanism — so a multi-turn agent pays tokens only for the branch it
actually walks.

Topics form a tree by dotted prefix (``edit`` → ``edit.text`` /
``edit.chart`` / ``edit.xml``; ``recipes`` → ``recipes.slides`` /
``recipes.colors``). ``doc_guide(topic)`` resolves exact topics, parent
prefixes (returns the children joined), and unknown topics gracefully (returns
the root map — never a dead end for the model).

Hosts that rename tools (e.g. geny-executor's ``DocAnalyze`` for
``analyze_doc``) pass ``names={canonical: hosted}`` and every guide renders
with the names the model actually sees.
"""

from __future__ import annotations

__all__ = ["ROOT", "GUIDES", "TOPICS", "CANONICAL_TOOL_NAMES", "doc_guide"]

# Canonical (library-surface) tool names. Hosts remap via `names=`.
CANONICAL_TOOL_NAMES = [
    "doc_guide",
    "analyze_doc",
    "render_doc",
    "set_doc_text",
    "read_doc_xml",
    "set_doc_xml",
    "build_doc",
    "generate_doc",
    "edit_doc",
]

ROOT = """\
xgen_edit2docs — office documents (.docx/.xlsx/.pptx) as deterministic tools.
Multi-turn flow: pick a family below → doc_guide(topic) when you need the
detailed shapes/recipes → act. [det] = deterministic, instant, NO API key.
[LLM] = uses the built-in LLM (needs an Anthropic key). Prefer [det]: you do
the thinking, the tools do the work.

FIRST DECISION — CREATE a new file, or CHANGE an existing one?

GENERATE (new file)
  build_doc     [det]  you write the spec, instant render      → topic: build
  generate_doc  [LLM]  one-line intent → designed document     → topic: generate

EDIT (existing file) — run analyze_doc FIRST (addresses + charts list)
  set_doc_text  [det]  text/table/cell values + chart title/data
                                                               → topic: edit.text, edit.chart
  read_doc_xml  [det]  part map, or one part's exact XML       → topic: edit.xml
  set_doc_xml   [det]  patch/create/delete a part's XML — colors, fonts,
                       geometry, add/remove slides, anything   → topic: edit.xml, recipes
  edit_doc      [LLM]  one natural-language edit turn          → topic: edit

INSPECT (either family)
  analyze_doc   [det]  outline + edit addresses + charts list
  render_doc    [det]  to=md|svg|png|pdf (md = read the content) → topic: render

topics: build · generate · edit · edit.text · edit.chart · edit.xml · render ·
recipes.slides · recipes.colors"""

GUIDES: dict[str, str] = {
    # ── GENERATE family ────────────────────────────────────────────────
    "build": """\
build_doc(spec, output) — deterministic generation, no LLM, no key. The
OUTPUT extension picks the engine and the required spec shape:

.docx ← spec is a MARKDOWN string. Subset: # headings, paragraphs,
  - / 1. lists, **bold** / *italic*, | tables |, ``` code blocks.
.xlsx ← spec is {"sheets": [{"name", "headers": [...], "rows": [[...]]}]}
  (styled header row, frozen panes, auto column widths).
.pptx ← spec is {"slides": [...], "theme": {...}?}
  layout ∈ title|content|section|title_only|blank plus DESIGN layouts:
    stat        {"title","value","label"?,"sublabel"?}   — big accent number
    quote       {"quote","attribution"?}                 — pull-quote
    comparison  {"title","left":{"heading","bullets"},"right":{...}} — 2 panels
  bullets: ["str", {"text": ..., "level": 0-8}].

THEMED DECKS (deterministic design — no LLM): add
  "theme": {"bg":"0B1424","accent":"EA580C","ink":"F4F6FB","muted":"94A1B8",
            "panel":"132339","rail":true,"page_numbers":true,"font":"Noto Sans KR"?}
→ 16:9 slides with background fill, left accent rail, NN/NN page numbers,
type hierarchy, panels — a designed deck (e.g. deep-navy + orange) in ONE
call. All theme keys optional (shown values are the defaults). Without
"theme" the legacy built-in template layouts are used.

Returns {path, page_count}. A spec that doesn't match the extension raises a
bilingual ValueError — fix the shape and retry. Use build_doc + theme for
design-spec decks; generate_doc (LLM) only when you want the model to invent
the content AND layout for you.""",
    "generate": """\
generate_doc(intent, output, sources?, template?, deck_mode?, pages?, lang?)
— LLM pipeline (needs an Anthropic key). Output extension picks the engine:

.docx/.xlsx — one writer call → deterministic render (fast).
.pptx — full deck pipeline (strategize→layout→render→quality). SLOW (minutes).
  template: existing .pptx to inherit design from.
  deck_mode: new | template_restyle | template_extend.  pages: [min, max].
sources: local files (PDF/DOCX/PPTX/XLSX/HTML) to ground the content in.

No key available? build_doc renders YOUR spec instantly with no LLM.""",
    # ── EDIT family ────────────────────────────────────────────────────
    "edit": """\
Editing an existing document — decision order:

1. analyze_doc(doc) — ALWAYS first: outline, edit addresses, charts list.
2. Text/table/cell values, or chart title/data?
     → set_doc_text                       (topics: edit.text, edit.chart)
3. Anything else — colors, fonts, fills, geometry, chart styling,
   add/remove slides?
     → read_doc_xml + set_doc_xml         (topics: edit.xml, recipes.*)
4. Vague natural-language instruction AND an Anthropic key available?
     → edit_doc (one instruction per call; questions answered in `reply`)

Verify with render_doc(to=md) or a fresh analyze_doc. Every deterministic
edit byte-preserves untouched content (charts, images, styles, formulas).""",
    "edit.text": """\
set_doc_text(doc, edits, output?) — deterministic structured edits at
addresses from analyze_doc. Edit shapes by extension:

DOCX  {"action":"replace","para":i,"new_text":...}
      {"action":"replace","table":t,"row":r,"col":c,"new_text":...}
      {"action":"insert_after","para":i,"markdown":...}   (para=-1 prepends)
      {"action":"delete","para":i}
XLSX  {"action":"set_cell","sheet":name,"cell":"B3","value":...}
      {"action":"append_rows","sheet":name,"rows":[[...]]}
      {"action":"add_sheet","sheet":name,"headers":[...],"rows":[[...]]}
PPTX  {"slide":i,"shape_id":id,"para":p,"new_text":...}  (+"row"/"col" in tables)

Optional "old_text"/"old_value" guards reject stale edits. Per-edit statuses:
applied | stale | not_found | invalid — fix and resend ONLY the failed ones.
Chart edits mix into the same edits list — see topic edit.chart.""",
    "edit.chart": """\
Chart edits ride set_doc_text: any edit dict with a `chart` index (from
analyze_doc's "charts" list) routes to the chart engine:

  {"chart":0,"title":"Q3 Sales"}                                  — retitle
  {"chart":0,"categories":[...],"series":[{"name","values":[...]}]} — set data

Setting data rewrites the chart caches AND its embedded workbook, so Office's
double-click-edit shows the same numbers. Same shape for docx/xlsx/pptx.
Chart COLORS/fonts/styling are NOT here — that is XML: topic recipes.colors.""",
    "edit.xml": """\
Documents ARE zips of XML — read_doc_xml + set_doc_xml express every edit
OOXML can. Workflow:

1. read_doc_xml(doc)        → part map (slides, charts, styles, sheets, rels)
2. read_doc_xml(doc, part)  → that part's EXACT XML text
3. set_doc_xml(doc, part, edits=[{"find","replace","count"(0=all)}])
   — `find` must match the read text EXACTLY (copy-paste substrings).

Other modes (exactly one per call):
  xml="..."      replace the whole part — CREATES it if missing (pass
                 content_type to register the new part's Override).
  delete=true    remove the part (also patch the rels that reference it).

Safety: the result must stay well-formed XML or NOTHING is written; untouched
parts stay byte-identical. Common parts: ppt/slides/slideN.xml,
ppt/charts/chartN.xml, word/document.xml, xl/worksheets/sheetN.xml,
theme1.xml, and each part's _rels/*.rels. Recipes: recipes.slides,
recipes.colors.""",
    # ── Recipes (proven multi-call sequences) ──────────────────────────
    "recipes.slides": """\
ADD A SLIDE — 4 tool calls (proven end-to-end):
1. xml  = read_doc_xml(doc, "ppt/slides/slide1.xml")   # template; edit texts
   rels = read_doc_xml(doc, "ppt/slides/_rels/slide1.xml.rels")
2. set_doc_xml(doc, "ppt/slides/slide2.xml", xml=xml, content_type=
   "application/vnd.openxmlformats-officedocument.presentationml.slide+xml")
3. set_doc_xml(doc, "ppt/slides/_rels/slide2.xml.rels", xml=rels)
4. set_doc_xml(doc, "ppt/_rels/presentation.xml.rels", edits=[{"find":
     "</Relationships>", "replace": "<Relationship Id=\\"rIdNew\\"
     Type=\\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide\\"
     Target=\\"slides/slide2.xml\\"/></Relationships>"}])
   set_doc_xml(doc, "ppt/presentation.xml", edits=[{"find": "</p:sldIdLst>",
     "replace": "<p:sldId id=\\"9999\\" r:id=\\"rIdNew\\"/></p:sldIdLst>"}])
   (sldId id: any unused number ≥ 256; rIds must be unique in their rels file)

REMOVE A SLIDE: set_doc_xml(delete=true) on slideN.xml and its .rels, then
remove its <p:sldId/> from presentation.xml and its <Relationship/> from
ppt/_rels/presentation.xml.rels via find/replace edits.""",
    "recipes.colors": """\
RECOLOR A CHART SERIES (proven end-to-end):
1. read_doc_xml(doc, "ppt/charts/chart1.xml")
2. Explicit fill lives in c:ser → c:spPr. If the series has NO c:spPr yet,
   insert one right after its </c:tx>:
   set_doc_xml(..., edits=[{"find": "</c:tx>", "replace": "</c:tx><c:spPr>
   <a:solidFill><a:srgbClr val=\\"E74C3C\\"/></a:solidFill></c:spPr>",
   "count": 1}])   (count=1 → first series; repeat per series)
   If c:spPr already has <a:srgbClr val="...">, just replace that value.

SHAPE fills / TEXT colors (slide XML): <a:solidFill><a:srgbClr
val=\\"RRGGBB\\"/></a:solidFill> inside p:spPr (shape) or a:rPr (text run).
THEME colors: <a:schemeClr val=\\"accent1\\"/> etc.; the palette lives in
ppt/theme/theme1.xml (docx/xlsx: word|xl/theme/theme1.xml).""",
    # ── INSPECT ────────────────────────────────────────────────────────
    "render": """\
render_doc(doc, to=png|pdf|svg|md, out_dir?, dpi?) — deterministic, no
LibreOffice, no key.

md  → READ the content: preview.md (docx/xlsx) or per-slide SVGs (pptx).
png → page-1.png…page-N.png (dpi, default 144).
pdf → one <stem>.pdf.   svg → the vector pages.

Use to=md to verify your edits cheaply; png/pdf for human-facing output.""",
}

TOPICS = list(GUIDES)


def _rename(text: str, names: dict[str, str] | None) -> str:
    """Render canonical tool names as the host's names (longest-first so no
    partial overlaps)."""
    if not names:
        return text
    for canonical in sorted(names, key=len, reverse=True):
        text = text.replace(canonical, names[canonical])
    return text


def doc_guide(
    topic: str | None = None, *, names: dict[str, str] | None = None
) -> dict:
    """The progressive-disclosure entry point.

    * no topic → the family map (Generate | Edit | Inspect) + topic index.
    * exact topic → that guide (+ its subtopics listed).
    * parent prefix (e.g. ``recipes``) → all child guides joined.
    * unknown topic → the family map with a note (never a dead end).
    """
    if not topic or not str(topic).strip():
        return {"topic": "", "guide": _rename(ROOT, names), "topics": TOPICS}

    t = str(topic).strip().lower().rstrip(".")
    if t in GUIDES:
        guide = GUIDES[t]
        children = [k for k in TOPICS if k.startswith(t + ".")]
        if children:
            guide += "\n\nSubtopics: " + ", ".join(children)
        return {"topic": t, "guide": _rename(guide, names), "topics": TOPICS}

    children = [k for k in TOPICS if k.startswith(t + ".")]
    if children:
        joined = "\n\n────────\n\n".join(GUIDES[k] for k in children)
        return {"topic": t, "guide": _rename(joined, names), "topics": TOPICS}

    return {
        "topic": t,
        "guide": _rename(
            f"(unknown topic {t!r} — showing the family map)\n\n" + ROOT, names
        ),
        "topics": TOPICS,
    }
