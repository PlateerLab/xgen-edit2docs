# Structural operations — design plan

**Status:** proposal (for review before implementation)
**Author:** 2026-08-06
**Target:** xgen_contextifier `0.5.0` (raw primitives) → xgen_edit2docs `0.15.0` (agent verb) → geny propagation

---

## 1. Problem & philosophy

xgen_edit2docs is *addressable and deterministic for **content*** — `set_doc_text`
edits text / table cells / chart data at `analyze_doc` addresses, byte-preserving
everything it doesn't touch. But it has **no deterministic verb for *structure***:
treating a whole slide or sheet as a movable, copyable, reorderable object.

### Empirically verified — what actually works TODAY (2026-08-06, executed, not grepped)

An earlier grep-based report was wrong. Running the real code against a 3-slide deck:

| Op | Implemented today? | Via | Keyless / deterministic? | Reachable by an agent (a verb)? |
|---|---|---|---|---|
| **delete slide** | ✅ **YES** | `xgen_contextifier … remove_slide(i)` (orphan-swept) | ✅ | ❌ **not exposed** in any xgen_edit2docs verb |
| **move / reorder slide** | ✅ **YES** | pure `p:sldIdLst` reorder (**only `presentation.xml` changes**, byte-preserving) — also `recompose_pptx` permuted `KeepSlide` | ✅ | ❌ **not exposed** |
| **duplicate slide** | ❌ **NO** | `recompose_pptx` *explicitly refuses* (`ValueError: duplicate KeepSlide index`); no `duplicate_slide` anywhere | — | ❌ |
| add slide | ⚠️ partial | `recompose_pptx` `NewSlide` (needs a rendered SVG) or the 4-call XML recipe | — | ❌ |
| xlsx copy / move / rename / delete **sheet** | ❌ **NO** | only `add_sheet` exists | — | ❌ |

**So the real situation is not "nothing exists."** Slide **delete** and **move**
already work at the library level, keyless and byte-preserving — they are simply
**not surfaced as an agent verb**, so a Geny/MCP model cannot invoke them. The
*only genuine implementation gap* is **duplicate** (slides and sheets) and the
xlsx sheet ops. The gap is therefore mostly **exposure**, plus one real new
capability (duplicate).

The philosophy inconsistency remains real: charts earned a clean deterministic
verb, but slide/sheet structure is unreachable. This plan closes it with the
*same* philosophy — **a deterministic, keyless, byte-preserving verb over
`analyze_doc` addresses** — while being honest that most of the machinery already
exists and needs wiring, not building.

### Design tenets

1. **Primitives in xgen_contextifier, verb in xgen_edit2docs.** Structural mutation is a
   *document-manipulation* capability → it belongs in the raw layer next to
   `PptxRawDocument.remove_slide`. xgen_edit2docs consumes it, exactly as it already
   consumes `open_raw` for lossless edits. (`xlsx_engine._add_raw_sheet` even
   carries a `# The raw layer has no add_sheet` apology — this plan pays that debt.)
2. **Byte-preservation is non-negotiable.** Any part not structurally involved
   round-trips byte-identically (OPC contract). Acceptance = reopen with
   python-pptx / openpyxl **and** a zip-level "only expected parts changed" assert.
3. **Deterministic & keyless.** No Anthropic key, no rendering, no LLM. Pure
   package surgery. Fast, reproducible, unit-testable without network.
4. **One op vocabulary across formats**, applicability documented per format.

---

## 2. Scope

**In scope (v1):**
- **PPTX slides:** `duplicate`, `move`, `delete`. *(insert-blank: optional, see §8.)*
- **XLSX sheets:** `duplicate` (copy), `move` (reorder), `rename`, `delete`.

**Deferred (documented as follow-ups, not built now):**
- **DOCX** structural ops. DOCX is a *flow* document — there is no part-per-page;
  "move/copy a section" means relocating a body block-range (paragraphs/tables
  between headings) inside `word/document.xml`, a different model. Deferred to a
  Phase 2 (`arrange` over heading-delimited block ranges). See §8.
- **XLSX rename deep-rewrite** of formula / defined-name references that mention
  the old tab name. v1 renames the **tab only** and emits a warning if formulas or
  defined names reference the old name; a ref-rewriting pass is a follow-up.
- **Column ops** on native tables (already out of scope in the raw layer).

---

## 3. Architecture (two layers)

```
xgen_edit2docs.arrange_doc(doc, ops, output=)          ← NEW agent verb (deterministic, keyless)
        │  dispatch by extension
        ▼
xgen_contextifier.open_raw(bytes)                       ← existing entrypoint
        │
        ├── PptxRawDocument.duplicate_slide / move_slide / (remove_slide ✓existing)
        └── XlsxRawDocument.copy_sheet / move_sheet / rename_sheet / delete_sheet / (add_sheet)
                │  all built on:
                ▼
        OpcPackage  (add_part / remove_part / rels_for / set_content_type_override / to_bytes)
        + NEW OpcPackage.clone_part_graph(...)     ← format-agnostic part-subtree copy helper
```

The only genuinely new engine code is **`clone_part_graph`** (copy a part plus its
transitively-referenced rels subtree under fresh names, with a share/clone policy) —
the inverse of the orphan-sweep `remove_slide` already implements. Everything else is
element reordering in `presentation.xml` / `workbook.xml`.

---

## 4. xgen_contextifier raw layer — new API (`0.5.0`)

### 4.1 `OpcPackage.clone_part_graph` (new, format-agnostic)

```python
def clone_part_graph(
    self,
    src_part: str,
    *,
    rename: Callable[[str], str],       # src part name -> fresh dest name
    share_types: Iterable[str] = (),    # rel Type suffixes to REFERENCE, not copy
    stop_types: Iterable[str] = (),     # rel Type suffixes to skip entirely
) -> tuple[str, dict[str, str]]:
    """Deep-copy *src_part* and everything it transitively references into
    new parts, returning (new_src_name, {old_part: new_part}).

    For each rel of a copied part:
      * Type endswith a `share_types` suffix  -> new part's rels points at the
        SAME (shared) target; the target is NOT copied. (images, layouts)
      * Type endswith a `stop_types` suffix    -> rel dropped from the copy.
      * otherwise                              -> target is cloned recursively
        under rename(), and the copied rels entry retargets to the clone.
    Content-type Overrides are duplicated for every cloned part. rId tokens
    inside copied part XML are preserved (the rebuilt .rels reuses the same
    Ids, only their targets differ), so copied bodies need no XML rewrite.
    """
```

This is the heart of `duplicate`/`copy`. It reuses the existing `Relationships`,
`_rels_name_for`, `content_type_of`, `set_content_type_override`, `add_part`.

### 4.2 `PptxRawDocument` — new methods

```python
def duplicate_slide(self, index: int, *, at: int | None = None) -> int:
    """Insert an independent copy of slide *index* at position *at*
    (default: right after the source). Returns the new slide's index.

    Clone policy (via clone_part_graph on the slide part):
      * notesSlide  -> CLONED (its back-ref /slide rel is retargeted to the
                       NEW slide part so the copy's notes attach to the copy).
      * chart / chartEx (+ embedded workbook, colors, style, quickStyle)
                    -> CLONED  (so editing the copy's chart data via edit_chart
                       does NOT mutate the original — faithful independence).
      * image / media / audio / video -> SHARED (referenced, not copied;
                       read-only content, matches PowerPoint's duplicate).
      * slideLayout -> SHARED (never copy a layout).
    Then: new content-type override, new Relationship in
    ppt/_rels/presentation.xml.rels (Type .../slide), new <p:sldId id=N r:id=..>
    inserted into p:sldIdLst at *at* (N = max sldId + 1, >= 256)."""

def move_slide(self, index: int, to: int) -> None:
    """Reorder slides: move the p:sldId at *index* to position *to*.
    Pure reorder of p:sldIdLst in ppt/presentation.xml — NO part is copied,
    renamed or deleted; every slide part stays byte-identical."""
```

`remove_slide(index)` already exists (orphan-reference-counted delete) — reused as-is.

### 4.3 `XlsxRawDocument` — new methods

```python
def copy_sheet(self, key: int | str, new_name: str, *, at: int | None = None) -> RawSheet:
    """Insert an independent copy of a sheet. Clones the worksheet part and its
    drawing/chart subtree (clone_part_graph, sharing images), adds a <sheet>
    (fresh sheetId, r:id, name=new_name) to xl/workbook.xml at *at*, plus the
    workbook rels entry and content-type override. new_name must be unique and
    <=31 chars (Excel limit). Copies the sheet's own defined names? -> NO (v1:
    tab + cells + drawings only; warn if global defined names reference it)."""

def move_sheet(self, key: int | str, to: int) -> None:
    """Reorder the <sheet> element in xl/workbook.xml/<sheets>. Tab order only;
    no part touched. (bookViews/activeTab index is left as-is.)"""

def rename_sheet(self, key: int | str, new_name: str) -> None:
    """Set the <sheet name=..> attribute (<=31 chars, unique). v1 does NOT
    rewrite formula/defined-name references to the old name — emits nothing at
    this layer; the xgen_edit2docs verb surfaces a warning when such references exist."""

def delete_sheet(self, key: int | str) -> None:
    """Remove the <sheet>, its workbook rel, the worksheet part and its
    orphan subtree (drawings/charts/embeddings no surviving sheet uses) — the
    same reference-counted sweep as PptxRawDocument.remove_slide, generalized.
    Refuses to delete the last remaining sheet (a workbook needs >=1)."""

def add_sheet(self, name: str, *, at: int | None = None) -> RawSheet:
    """Empty worksheet (folds xgen_edit2docs' _add_raw_sheet 4-touchpoint code back
    into the raw layer where it belongs)."""
```

The reference-counted delete/orphan-sweep helpers (`_delete_part`,
`_referenced_parts`, `_drop_content_type_overrides`) currently live on
`PptxRawDocument`; **lift them to `RawDocumentBase`** so both formats share one
tested implementation.

### 4.4 Byte-preservation contract (tests)

For every op: reopen the result with the host Office lib (python-pptx / openpyxl)
**and** assert the changed-parts set at the zip level:

| op | parts allowed to change |
|---|---|
| `move_slide` / `move_sheet` | only `ppt/presentation.xml` / `xl/workbook.xml` |
| `delete_*` | the removed part(s) + their orphans + `presentation.xml`/`workbook.xml` + rels + `[Content_Types].xml` |
| `duplicate`/`copy` | **added** parts only; **no existing part changes** except `presentation.xml`/`workbook.xml` (+ their rels, `[Content_Types].xml`) |

The duplicate "no existing part changes" assert is what proves independence — the
original slide/sheet and its charts are untouched.

---

## 5. xgen_edit2docs verb — `arrange_doc` (`0.15.0`)

### 5.1 The surface decision (needs your sign-off — see §9-A)

**Recommendation: a new, 9th deterministic verb `arrange_doc`.** Structural ops have
a different *address model* (a whole slide/sheet by index/name, not a text address)
and a different *op vocabulary* (duplicate/move/delete/rename) from `set_doc_text`
(within-element content). Folding them into `set_doc_text` keeps the count at 8 but
makes an already-broad verb ("text" that also does charts and now structure)
incoherent. The philosophy is cleaner as **content vs. structure** verbs.

### 5.2 Library function (`simple.py`)

```python
def arrange_doc(doc, ops: list[dict], *, output=None) -> ArrangeResult:
    """Deterministic structural edits: duplicate / move / delete slides
    (.pptx) or sheets (.xlsx); rename sheets. No Anthropic key. Byte-
    preserving. ops apply in sequence, each resolving `target` against the
    CURRENT state (after prior ops). Reuses _fmt_of / _read_pptx / _default_output."""
```

Returns a new dataclass mirroring `TextEditsResult`:
`ArrangeResult(path: Path, applied: int, results: list[dict], warnings: list[dict])`
— each result `{op, target, to?, name?, status: applied|invalid|not_found|refused, message?}`.

### 5.3 Op schema (one vocabulary, per-format applicability)

```jsonc
// pptx (target = slide index, 0-based, from analyze_doc slides[].index; to = position)
{"op": "duplicate", "target": 0, "to": 3}      // copy slide 0, insert at index 3
{"op": "move",      "target": 5, "to": 1}
{"op": "delete",    "target": 2}
// xlsx (target = sheet name or index; name = new tab name)
{"op": "duplicate", "target": "Sales", "name": "Sales (copy)", "to": 2}
{"op": "move",      "target": "Sales", "to": 0}
{"op": "rename",    "target": "Sheet1", "name": "Summary"}
{"op": "delete",    "target": "Old"}
```

- `target`: int (slide index / sheet index) or str (sheet name). Out of range / no
  such sheet → `status: not_found`, op skipped, others continue.
- `to`: destination index. Omitted on `duplicate` → after source; on `move` required.
- `name`: required for xlsx `duplicate`/`rename`; ignored (with warning) for pptx.
- **Sequential semantics:** ops mutate the doc left-to-right; each `target`/`to`
  is resolved against the state *after* previous ops. Documented loudly in the guide
  (this is the one sharp edge — index shifting). `applied` counts successful ops.
- Refusals: delete last sheet → `status: refused`. Invalid op name / missing
  required field → `status: invalid`. Never raises on a single bad op; the batch
  is best-effort with a per-op result (mirrors `apply_chart_edits`).

### 5.4 Dispatch (`agent_tools.run_tool_async`)

New branch `if name == "arrange_doc":` → `simple.arrange_doc(args["doc"],
args["ops"], output=args.get("output"))` → `{path, applied, results, warnings}`.

---

## 6. Wiring checklist (every surface a verb must satisfy)

Derived from the current `set_doc_xml` wiring. Each item is required for `arrange_doc`:

1. **`simple.py`** — implement `arrange_doc` + `ArrangeResult`; reuse
   `_fmt_of` / `_read_pptx` / `_default_output`; write output only if `applied > 0`.
2. **`__init__.py`** — add `"arrange_doc": ".simple"` and `"ArrangeResult": ".simple"`
   to `_LAZY` (auto-flows into `__all__`).
3. **`agent_tools.py`** — append the tool dict to `ANTHROPIC_TOOLS` (**description
   ≤ 320 chars** — enforced by `test_descriptions_stay_compact`); add the
   `run_tool_async` dispatch branch. `TOOL_NAMES` / `OPENAI_TOOLS` derive automatically.
4. **`mcp/local_server.py`** — add an `@mcp.tool(name="arrange_doc", ...)` wrapper
   delegating to `run_tool_async` (local stdio MCP mirrors the agent tools 1:1).
   *(Hosted `mcp/server.py` is a separate asset-oriented surface and does not carry
   the deterministic verbs — no change there.)*
5. **`agent_guide.py`** — add `GUIDES["arrange"]` (the recipe + the sequential-index
   caveat); reference `arrange_doc` in `ROOT`'s map + the `topics:` footer; add to
   `CANONICAL_TOOL_NAMES`. `TOPICS` derives automatically. Also rewrite
   `recipes.slides` (its 4-call add/remove-slide recipe is superseded by `arrange_doc`).
6. **`api/` (optional, §9-D)** — a hosted HTTP route has NO existing sibling for
   `set_doc_xml`, so only add `api/routes/arrange.py` + `include_router` if the
   studio/web needs a server endpoint. Default: **skip** for v1 (library + MCP +
   agent-tools cover the consumers; xgen_edit2docs-web calls the text-edits route today).
7. **Tests** — update the **three** hard-coded surface assertions:
   `tests/unit/test_toolkit.py:102` (exact `TOOL_NAMES` set),
   `tests/integration/test_doc_generation_editing.py:228` (same),
   `tests/unit/test_toolkit.py:423` (local-MCP registry == `TOOL_NAMES`).

---

## 7. Test plan

**Raw layer (xgen_contextifier, `tests/unit/raw/`):**
- `duplicate_slide`: copy reopens in python-pptx; new slide count +1; copy's chart is
  an **independent** part (edit copy's chart → original chart bytes unchanged); shared
  image part count unchanged; notes cloned & re-attached; zip-diff = added parts only.
- `move_slide`: order in `sldIdLst` changes; **only** `presentation.xml` differs.
- `copy_sheet`/`move_sheet`/`rename_sheet`/`delete_sheet`: analogues via openpyxl;
  delete orphan-sweeps drawings/charts; delete-last-sheet refused; rename >31 / dup name rejected.
- `clone_part_graph`: unit test the share/clone/stop policy on a synthetic package.
- Regression: existing `remove_slide` + byte-preservation suites stay green after the
  base-class lift.

**xgen_edit2docs verb (`tests/unit/test_arrange.py`, new):**
- Each op end-to-end via `arrange_doc` and via `run_tool("arrange_doc", ...)`.
- Sequential-index semantics (two ops where the second depends on the first's shift).
- Per-op `status` matrix (applied / not_found / invalid / refused); best-effort batch.
- Cross-format guard (pptx op on xlsx target → clear error).
- `analyze_doc` address round-trip (duplicate slide `i`, then `analyze_doc` shows N+1 slides).

**Acceptance:** every produced file reopens in the host Office library without repair.

---

## 8. Optional / follow-up items

- **insert-blank slide** (`{"op":"insert","layout":"Title and Content","to":k}`): needs
  a blank `<p:sld>` referencing a layout — closer to `build_doc`. Cheap follow-up once
  `duplicate` lands; not required for the two motivating use-cases (copy + move).
- **DOCX arrange** (Phase 2): `move`/`duplicate` over heading-delimited block ranges in
  `word/document.xml` (addresses = `analyze_doc` docx outline sections). Different model,
  separate milestone.
- **XLSX ref-rewrite on rename** (Phase 2): rewrite `Sheet1!A1` / defined names when a
  tab is renamed. v1 warns; this makes rename "safe" for formula-heavy books.

---

## 9. Decisions for your review

- **A. Surface.** New 9th verb `arrange_doc` *(recommended)* vs. fold structural ops
  into `set_doc_text` as extra edit actions (keeps 8 verbs, muddies "text").
- **B. Verb name.** `arrange_doc` *(recommended)* vs. `set_doc_structure` /
  `organize_doc` / `move_doc_parts`.
- **C. Duplicate clone policy.** Faithful-independent — **clone** charts+notes, **share**
  images/layouts *(recommended, matches PowerPoint)* vs. share-everything (lighter but
  editing the copy's chart mutates the original).
- **D. Hosted HTTP route.** Skip for v1 *(recommended)* vs. add `POST /v1/arrange` now.
- **E. Geny exposure.** After xgen_edit2docs `0.15.0`: (i) geny gets `arrange_doc` for free
  as a library import, or (ii) also add a first-class `DocArrange` built-in in
  geny-executor `doc_tools` *(recommended — mirrors DocApplyEdits/DocXmlEdit so the
  model sees it)*, then bump floors.

---

## 10. Milestones & release sequence

1. **M1 — xgen_contextifier raw primitives** (`clone_part_graph`, pptx duplicate/move,
   xlsx copy/move/rename/delete/add; base-class lift of the sweep helpers) + raw tests.
   → release **xgen_contextifier `0.5.0`** (PyPI).
2. **M2 — xgen_edit2docs `arrange_doc` verb** + full wiring (§6) + verb tests; bump
   `xgen_contextifier>=0.5.0`; rewrite `recipes.slides` guide. → release **xgen_edit2docs `0.15.0`**.
3. **M3 — deploy** hrletsgo (2222) from `main` (no-cache rebuild → verify healthy /
   version / studio 200) and **align geny** (xgen_edit2docs floor `>=0.15.0`; optional
   `DocArrange` built-in in geny-executor → release; bump Geny backend pins).

**Effort is small because most of it already works.** slide **delete** exists
(`remove_slide`), slide **move** is a proven ~5-line `p:sldIdLst` reorder. The only
*substantial* new code is **duplicate** — `clone_part_graph` (~120 lines) + the
`duplicate_slide` / `copy_sheet` methods that use it — plus the trivial move/rename
reorders and the xlsx `delete_sheet` (a generalization of the existing `remove_slide`
sweep). Then one verb + wiring. Low risk: built on the proven OPC byte-preservation
contract and the existing orphan-sweep machinery. In short: **~70% exposure of
already-working ops, ~30% one new capability (duplicate).**
