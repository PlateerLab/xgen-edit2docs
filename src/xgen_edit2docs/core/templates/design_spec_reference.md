# {project_name} - Design Spec

> Human-readable design narrative — rationale, audience, style, color choices, content outline. Read once by downstream roles for context.
>
> Machine-readable execution contract: `spec_lock.md` (color / typography / icon / image short form). Executor re-reads `spec_lock.md` before every SVG page to resist context-compression drift. Keep both in sync; on divergence, `spec_lock.md` wins.

## I. Project Information

| Item | Value |
| ---- | ----- |
| **Project Name** | {project_name} |
| **Canvas Format** | {canvas_info['name']} ({canvas_info['dimensions']}) |
| **Page Count** | [Filled by Strategist] |
| **Design Style** | {design_style} |
| **Target Audience** | [Filled by Strategist] |
| **Use Case** | [Filled by Strategist] |
| **Created Date** | {date_str} |

---

## II. Canvas Specification

| Property | Value |
| -------- | ----- |
| **Format** | {canvas_info['name']} |
| **Dimensions** | {canvas_info['dimensions']} |
| **viewBox** | `{canvas_info['viewbox']}` |
| **Margins** | [Recommended by Strategist, e.g., left/right 60px, top/bottom 50px] |
| **Content Area** | [Calculated from canvas] |

---

## III. Visual Theme

### Theme Style

- **Style**: {design_style}
- **Theme**: [Light theme / Dark theme]
- **Tone**: [Filled by Strategist, e.g., tech, professional, modern, innovative]

### Color Scheme

> Strategist: determine values from project content, industry, brand colors.

| Role | HEX | Purpose |
| ---- | --- | ------- |
| **Background** | `#......` | Page background (light theme typically white; dark theme dark gray/navy) |
| **Secondary bg** | `#......` | Card background, section background |
| **Primary** | `#......` | Title decorations, key sections, icons |
| **Accent** | `#......` | Data highlights, key information, links |
| **Secondary accent** | `#......` | Secondary emphasis, gradient transitions |
| **Body text** | `#......` | Main body text (dark theme uses light text) |
| **Secondary text** | `#......` | Captions, annotations |
| **Tertiary text** | `#......` | Supplementary info, footers |
| **Border/divider** | `#......` | Card borders, divider lines |
| **Success** | `#......` | Positive indicators (green family) |
| **Warning** | `#......` | Issue markers (red family) |

> **Reference**: Industry colors in `references/strategist.md` or `scripts/config.py` under `INDUSTRY_COLORS`

### Gradient Scheme (if needed, using SVG syntax)

```xml
<!-- Title gradient -->
<linearGradient id="titleGradient" x1="0%" y1="0%" x2="100%" y2="100%">
  <stop offset="0%" stop-color="#[primary]"/>
  <stop offset="100%" stop-color="#[secondary accent]"/>
</linearGradient>

<!-- Background decorative gradient (note: rgba forbidden, use stop-opacity) -->
<radialGradient id="bgDecor" cx="80%" cy="20%" r="50%">
  <stop offset="0%" stop-color="#[primary]" stop-opacity="0.15"/>
  <stop offset="100%" stop-color="#[primary]" stop-opacity="0"/>
</radialGradient>
```

---

## IV. Typography System

### Font Plan

> **Per-role families are expected, not optional.** Title / Body / Emphasis / Code may each use a different family (e.g., display serif title + geometric sans body). One family throughout is not required. See [strategist.md §g — Font Combinations](../references/strategist.md) for starting directions; you may propose a combination not listed.
>
> **⚠️ PPT-safe stack discipline (HARD rule).** PPTX stores a single `typeface` per run — no runtime fallback. Every stack MUST end with a cross-platform pre-installed font: `"Microsoft YaHei", sans-serif` / `SimSun, serif` / `Arial, sans-serif` / `"Times New Roman", serif` / `Consolas, "Courier New", monospace`. Stacks led by a non-preinstalled font (Inter / Google Fonts / brand typefaces) are allowed only when this spec notes the font-install or embedding requirement.

**Typography direction**: [Fill in one phrase, e.g., "modern CJK sans" / "academic serif" / "brand-specific: McKinsey Bower (requires font install)"]

Two views on the same font decisions — fill both, keep them consistent:

- **Role breakdown** (table below) — lists the *pieces* per role: CJK font, Latin font, CSS generic fallback. Human-readable design language.
- **Per-role font stacks** (after the table) — the *ordered* CSS `font-family` strings that actually go into SVG `font-family=""` and `spec_lock.md`'s `*_family` lines. Order controls browser rendering (Latin-led vs. CJK-led), so this is the **actual data** — not derivable from the table alone.

| Role | Chinese | English | Fallback tail |
| ---- | ------- | ------- | ------------- |
| **Title** | [e.g., `"Microsoft YaHei"`, or `"Microsoft YaHei", "PingFang SC"` for macOS preview nicety] | [e.g., `Georgia`] | [e.g., `serif`] |
| **Body** | [e.g., `"Microsoft YaHei", "PingFang SC"`] | [e.g., `Arial`] | [e.g., `sans-serif`] |
| **Emphasis** | [e.g., `SimSun`, or `—` for Latin-only] | [e.g., `Georgia`] | [e.g., `serif`] |
| **Code** | — | [e.g., `Consolas, "Courier New"`] | [e.g., `monospace`] |

**Per-role font stacks** (CSS `font-family` strings, one per role — arrange the table's pieces in the order your design intends):

- Title: `[Fill in stack, e.g. Georgia, "Microsoft YaHei", serif for Latin-led; or "Microsoft YaHei", "PingFang SC", Georgia, serif for CJK-led]`
- Body: `[Fill in stack — may be same as Title]`
- Emphasis: `[Fill in stack, or write "same as Body" to omit the override]`
- Code: `[Fill in monospace stack, e.g. Consolas, "Courier New", monospace]`

> **Stack ordering — why it matters**: CSS `font-family` falls back font-by-font (not char-by-char) — the browser uses the **first installed** font for everything it can render, skipping to the next only when a glyph is missing. So:
> - `Georgia, "Microsoft YaHei", serif` → Latin in Georgia (elegant serif), CJK falls through to Microsoft YaHei. **Use when Latin typography is the primary design statement** (academic / editorial / Latin-heavy covers).
> - `"Microsoft YaHei", Georgia, serif` → Everything in Microsoft YaHei (Latin uses YaHei's Latin glyphs — a different design tone). **Use when the deck is CJK-primary and Latin is incidental**.
>
> The converter (`drawingml_utils.py parse_font_family`) maps these to PPTX `<a:latin>` / `<a:ea>` regardless of order — but browser preview and SVG native rendering reflect stack order. Pick the order matching your design intent.

> **Why two views**: the breakdown shows role assignment at a glance; stacks carry the ordering info the breakdown can't encode. Keep both consistent — table cells should be exactly the fonts in the stacks (any order).

### Font Size Hierarchy

> **Ramp discipline, not a fixed menu.** `body` is the single anchor; every other size is a ratio of it. Each row below gives the role's allowed ratio band — Executor may pick any px value inside the band (e.g., 40px hero number, 13px chart annotation, 72px cover headline) without pre-declaring intermediates in `spec_lock.md`.
> **Unit**: px uniformly (SVG native) to avoid pt/px conversion errors.
> **Baseline selection**: drive by **content density**, not design style.

**Baseline**: Body font size = [fill in]px (any reasonable integer — `18` and `24` are most common; `16` for chart-heavy, `20`/`22` for medium density, `28-32` for poster / cover decks are all valid. Drive by content density.)

| Purpose | Ratio to body | Example @ body=24 (relaxed) | Example @ body=18 (dense) | Weight |
| ------- | ------------- | --------------------------- | ------------------------- | ------ |
| Cover title (hero headline) | 2.5-5x | 60-120px | 45-90px | Bold / Heavy |
| Chapter / section opener | 2-2.5x | 48-60px | 36-45px | Bold |
| Page title | 1.5-2x | 36-48px | 27-36px | Bold |
| Hero number (consulting KPIs) | 1.5-2x | 36-48px | 27-36px | Bold |
| Subtitle | 1.2-1.5x | 29-36px | 22-27px | SemiBold |
| **Body content** | **1x** | **24px** | **18px** | Regular |
| Annotation / caption | 0.7-0.85x | 17-20px | 13-15px | Regular |
| Page number / footnote | 0.5-0.65x | 12-16px | 9-12px | Regular |

> The two px columns are illustrations for common baselines. For any other `body` value, multiply by each row's ratio — the checker (`svg_quality_checker._check_spec_lock_drift`) reads the live `body` from `spec_lock.md` and applies the bands, so no code change is needed for a different baseline.

> Sizes outside **every** band remain forbidden — surface the need and extend `spec_lock.md typography` (e.g., `cover_title: 96`) rather than invent a one-off value.

---

## V. Layout Principles

### Page Structure

- **Header area**: [Height and content description]
- **Content area**: [Height and content description]
- **Footer area**: [Height and content description]

### Layout Pattern Library (combine or break as content demands)

> **Principle — proportion follows information weight, not preset ratios.** The table below is a pattern library, not a menu. Combine two patterns on one page, break the grid entirely for a `breathing` page, or propose a pattern not listed when content calls for it. Defaulting every page to a symmetric grid produces the "AI-generated" look — vary intentionally.

| Pattern | Suitable Scenarios |
| ------- | ----------------- |
| **Single column centered** | Covers, conclusions, key points |
| **Symmetric split (5:5)** | Comparisons where two sides carry equal weight |
| **Asymmetric split (3:7 / 2:8)** | One side dominates — data chart vs. brief takeaway, image vs. caption |
| **Top-bottom split** | Processes, timelines, ultra-wide image + text |
| **Three/four column cards** | Feature lists, parallel points, team intros |
| **Matrix grid (2×2)** | Two-axis classifications, strategic quadrants |
| **Z-pattern / waterfall** | Storytelling, case studies — content blocks alternate left/right guiding the eye |
| **Center-radiating** | Core concept + surrounding nodes, ecosystem / stakeholder maps |
| **Full-bleed + floating text** | `breathing` / feature pages — image fills canvas, text floats with opacity overlay |
| **Figure-text overlap** | Hero moments — headline / big number sits over or against an image edge instead of beside it |
| **Negative-space-driven** | A single element in 40-60% whitespace — lets one idea land with weight |

### Spacing Specification

> Spacing defaults depend on **container type**. Cards are one option, not the universal default. Tables below split by container type; a page may consult only one set (e.g., a `breathing` page with no cards uses only universal + non-card entries).

**Universal** (any container type):

| Element | Recommended Range | Current Project |
| ------- | ---------------- | --------------- |
| Safe margin from canvas edge | 40-60px | [fill in] |
| Content block gap | 24-40px | [fill in] |
| Icon-text gap | 8-16px | [fill in] |

**Card-based layouts** (consult only when the page uses cards — typically `dense` pages with parallel containers):

| Element | Recommended Range | Current Project |
| ------- | ---------------- | --------------- |
| Card gap | 20-32px | [fill in] |
| Card padding | 20-32px | [fill in] |
| Card border radius | 8-16px | [fill in] |
| Single-row card height | 530-600px | [fill in] |
| Double-row card height | 265-295px each | [fill in] |
| Three-column card width | 360-380px each | [fill in] |

**Non-card containers** (naked text blocks / full-bleed imagery / divider-separated content — typical for `breathing` pages or minimalist designs):

- Vertical rhythm carried by **whitespace**, not gutters — block gaps run wider than card gaps since there's no container edge to separate content.
- **Line-height**: 1.4-1.6× body font size.
- **Full-bleed text placement**: inset text away from the image's focal points; legibility over photographic backgrounds typically needs a gradient or opacity overlay.
- **Content width** is driven by reading comfort and image composition, not a card grid slot — don't back-compute "column width" when there's no column.

---

## VI. Icon Usage Specification

### Source

- **Built-in icon library**: `templates/icons/` (11,600+ icons across five libraries; see `templates/icons/README.md`)
- **Usage method**: SVG placeholder `<use data-icon="library/icon-name" .../>`; Design Spec should list approved `library/icon-name` entries for Executor.

### Recommended Icon List (fill as needed)

| Purpose | Icon Path | Page |
| ------- | --------- | ---- |
| [example] | `chunk-filled/circle-checkmark` | Slide XX |

---

## VII. Visualization Reference List (if needed)

> When the deck includes data visualization or infographic-style structured information, Strategist selects types from `templates/charts/charts_index.json` and lists them here for Executor reference. Path stays under `templates/charts/` for backward compatibility.

**Read-audit** (mandatory):

```
Catalog read: <N> templates / <M> categories
Runners-up considered: <key_A> (rejected: <reason>), <key_B> (rejected: <reason>), <key_C> (rejected: <reason>)
```

Runners-up must be genuine second-best matches for a page in this deck. If fewer than 3 viz pages exist, list what exists and note "fewer than 3 viz pages".

| Visualization Type | Reference Template | Used In |
| ------------------ | ------------------ | ------- |
| [e.g. grouped_bar_chart] | `templates/charts/grouped_bar_chart.svg` | Slide 05 |

---

## VIII. Image Resource List (if needed)

| Filename | Dimensions | Ratio | Purpose | Type | Status | Generation Description |
| -------- | --------- | ----- | ------- | ---- | ------ | --------------------- |
| cover_bg.png | {canvas_info['dimensions']} | [ratio] | Cover background | [Background/Photography/Illustration/Diagram/Decorative] | [Pending/Existing/Placeholder] | [AI generation prompt] |

**Status**:

- **Pending** — needs AI generation, provide description
- **Existing** — user-supplied, place in `images/`
- **Placeholder** — not yet processed, use dashed border in SVG

**Type** (used by Image_Generator for prompt strategy):

- **Background** — full-page (covers / chapters); reserve text area
- **Photography** — real scenes, people, products, architecture
- **Illustration** — flat / vector / cartoon / concept diagrams
- **Diagram** — flowcharts, architecture diagrams, concept maps
- **Decorative** — partial decorations, textures, borders, dividers

---

## IX. Content Outline

### Part 1: [Chapter Name]

#### Slide 01 - Cover

- **Layout**: Full-screen background image + centered title
- **Title**: [Main title]
- **Subtitle**: [Subtitle]
- **Info**: [Author / Date / Organization]

#### Slide 02 - [Page Name]

- **Layout**: [Choose a pattern from §V, combine two, or break the grid as the content demands]
- **Title**: [Page title]
- **Visualization**: [visualization_type] (see VII. Visualization Reference List)
- **Content**:
  - [Point 1]
  - [Point 2]
  - [Point 3]

> **Visualization field**: add only when the page has data visualization or structured infographic elements. Type must be listed in §VII.

---

[Strategist continues adding more pages based on source document content and page count planning...]

---

## X. Speaker Notes Requirements

One speaker note file per page, saved to `notes/`:

- **Filename**: match SVG name (e.g., `01_cover.md`)
- **Content**: script key points, timing cues, transition phrases

---

## XI. Technical Constraints Reminder

### SVG Generation Must Follow:

1. viewBox: `{canvas_info['viewbox']}`
2. Background uses `<rect>` elements
3. Text wrapping uses `<tspan>` (`<foreignObject>` FORBIDDEN)
4. Transparency uses `fill-opacity` / `stroke-opacity`; `rgba()` FORBIDDEN
5. FORBIDDEN: `mask`, `<style>`, `class`, `foreignObject`
6. FORBIDDEN: `textPath`, `animate*`, `script`
7. Text characters: write typography & symbols as raw Unicode (em dash `—`, en dash `–`, `©`, `®`, `→`, NBSP, etc.); HTML named entities (`&nbsp;`, `&mdash;`, `&copy;`, `&reg;` …) are FORBIDDEN. XML reserved chars in text MUST be escaped as `&amp;` `&lt;` `&gt;` `&quot;` `&apos;` (e.g. `R&amp;D`, `error &lt; 5%`). See shared-standards.md §1.0
7. `marker-start` / `marker-end` conditionally allowed: `<marker>` must be in `<defs>`, `orient="auto"`, shape must be triangle / diamond / circle (see shared-standards.md §1.1)
8. `clipPath` conditionally allowed **only on `<image>` elements**: `<clipPath>` in `<defs>`, single shape child (circle / ellipse / rect with rx,ry / path / polygon). Do NOT apply to shapes / groups / text — draw the target geometry directly with the matching native element (`<circle>` / `<ellipse>` / `<rect rx>` / `<polygon>` / `<path>`). See shared-standards.md §1.2

### PPT Compatibility Rules:

- `<g opacity="...">` FORBIDDEN (group opacity); set on each child element individually
- Image transparency uses overlay mask layer (`<rect fill="bg-color" opacity="0.x"/>`)
- Inline styles only; external CSS and `@font-face` FORBIDDEN

---

## Appendix K. Korean (ko-KR) Reference Examples

These are paste-ready building blocks for Korean decks. Use them as
starting points in §IV Typography and §III Visual Theme. They line up
with the Korean appendices in [strategist.en.md §K](../prompts/strategist.en.md)
and [executor-base.en.md §K](../prompts/executor-base.en.md).

### K.1 Korean font stack reference

| Use | Stack |
|---|---|
| Body (modern startup / B2B default) | `"Pretendard", "Pretendard Variable", "Apple SD Gothic Neo", "Malgun Gothic", "Noto Sans KR", sans-serif` |
| Body (warm humanist) | `"Spoqa Han Sans Neo", "Noto Sans KR", "Malgun Gothic", sans-serif` |
| Body (information-dense) | `"Pretendard", "Apple SD Gothic Neo", "Malgun Gothic", sans-serif`, `letter-spacing: -0.02em`, weight 400 |
| Title (impact) | `"Pretendard 900", "Apple SD Gothic Neo", "Malgun Gothic", sans-serif` |
| Title (editorial serif) | `"Nanum Myeongjo", "Batang", "Times New Roman", serif` |
| Mono / code | `"D2 Coding", Consolas, "Courier New", monospace` |
| Number-heavy display (statistics) | `"Pretendard 900", "Inter", "Helvetica Neue", "Apple SD Gothic Neo", sans-serif` |

Every Korean stack MUST end with `"Malgun Gothic"` (the only Hangul font
shipped with every recent Windows). Pretendard is OFL-licensed and safe
to embed in PPTX when you want to guarantee fidelity.

### K.2 Korean color tokens — drop-in palettes

Paste into §III Visual Theme. Pair one primary with neutrals + 1 accent.

```yaml
# K-startup minimal (default for unspecified Korean decks)
colors:
  primary: "#0064FF"      # Toss-blue (or substitute "#161616" near-black)
  secondary: "#F5F6F7"    # light surface
  accent: "#FF0066"       # warm action highlight (use sparingly)
  text: "#161616"
  text_muted: "#525252"
  background: "#FFFFFF"

# Korean government / public sector
colors:
  primary: "#003478"      # Korean navy
  secondary: "#5C7BAE"
  accent: "#C8102E"       # Korean red for emphasis only
  text: "#1A1A1A"
  background: "#FFFFFF"

# Korean consulting (베인 한국 / 삼정KPMG tone)
colors:
  primary: "#0F2A47"      # slate-navy
  secondary: "#7A8AA0"
  accent: "#C8102E"       # consulting red
  text: "#1A1A1A"
  background: "#FFFFFF"

# Korean media / publishing (editorial serif)
colors:
  primary: "#1F1F1F"
  secondary: "#A9A9A9"
  accent: "#B23A48"       # warm magazine red
  text: "#1A1A1A"
  background: "#F8F4EC"   # warm paper
```

### K.3 Korean typography ramp (px)

| Slot | Size | Weight | Notes |
|---|---:|---:|---|
| Cover title | 72 | 900 | ≤18 Hangul |
| Cover subtitle | 28 | 400 | ≤30 Hangul |
| Section heading | 60 | 700 | ≤16 Hangul |
| Page title | 40 | 700 | ≤20 Hangul |
| Page subtitle | 20 | 500 | ≤30 Hangul (single line) |
| Body | 20 | 400 | ≤50 Hangul / line |
| Caption | 13 | 400 | ≤35 Hangul |
| Source attribution | 11 | 400 | bottom-left |

All Korean body uses `letter-spacing: -0.02em`, headlines `-0.03em` to
`-0.06em`. No italic anywhere.

### K.4 Korean speaker-notes register reference

```markdown
# 표지

이번 발표에서는 2026년 3분기 영업 결과를 임원진께 공유드리겠습니다.
[화면 강조] 매출은 전년 동기 대비 12% 증가한 3,420억 원을 기록했습니다.
```

Use `-입니다 / -습니다` polite-formal endings. `[화면 강조]` and
`[잠시 멈춤]` cues are read by humans but stripped by TTS, so the
narration sounds natural.
