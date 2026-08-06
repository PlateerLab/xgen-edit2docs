"""Prompt loader + output-language directive builder.

xgen_edit2docs prompts live in a single English source of truth under
`src/xgen_edit2docs/core/prompts/<role>.en.md`. We do NOT maintain `<role>.ko.md`
variants — every prompt is English with explicit "Multilingual Output"
sections that tell the model what language to produce.

Why: modern LLMs (Claude, GPT) follow "Output in {lang}" instructions
reliably even when the system prompt is English. One source of truth
avoids translation drift across ko/en/zh/ja copies and keeps the deep
domain knowledge (typography, OOXML constraints, layout grids) in the
language the engineers actually write in.

For tools that call the LLM, prepend `build_output_lang_directive(lang)` to
the system prompt so the model knows whether to emit Korean, English,
Chinese, or Japanese in its outputs. The directive is short, ASCII, and
unambiguous; it does not duplicate the prompt's body.
"""

from __future__ import annotations

import functools
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "core" / "prompts"

# Roles whose prompt files must exist as <role>.en.md.
KNOWN_ROLES: set[str] = {
    "strategist",
    "document-writer",
    "sheet-designer",
    "doc-editor-planner",
    "sheet-editor-planner",
    "editor-planner",
    "editor-slide",
    "executor-base",
    "executor-consultant",
    "executor-consultant-top",
    "executor-general",
    "image-base",
    "image-generator",
    "image-searcher",
    "image-layout-spec",
    "template-designer",
    "shared-standards",
    "svg-image-embedding",
    "canvas-formats",
    "animations",
}


# Human-readable language names used in the output directive. Locales not
# listed here fall back to "the same language as the user-supplied content"
# which lets the LLM make a reasonable inference.
_LANG_LABELS: dict[str, str] = {
    "ko-KR": "Korean (한국어)",
    "en-US": "English",
    "en-GB": "English",
    "zh-CN": "Simplified Chinese (简体中文)",
    "zh-TW": "Traditional Chinese (繁體中文)",
    "ja-JP": "Japanese (日本語)",
}


@functools.lru_cache(maxsize=64)
def load_prompt(role: str) -> str:
    """Return the markdown content of `<role>.en.md` from PROMPTS_DIR.

    No language fallback chain — we maintain a single English source per role
    and inject the runtime language via `build_output_lang_directive(lang)`.

    Args:
        role: Prompt role (see KNOWN_ROLES).

    Raises:
        FileNotFoundError: if `<role>.en.md` is missing.
    """
    path = PROMPTS_DIR / f"{role}.en.md"
    if not path.exists():
        raise FileNotFoundError(
            f"No prompt found for role={role!r}. Expected: {path}"
        )
    return path.read_text(encoding="utf-8")


def build_output_lang_directive(lang: str) -> str:
    """Build the runtime "Output Language" instruction prepended to system prompts.

    The directive is deliberately short and explicit:
    - Tells the model WHAT language to use for user-facing strings
      (titles, body text, speaker notes, design_spec, etc.)
    - Spells out that *structural* fields (YAML keys, slot names, asset ids)
      stay English regardless of the deck language
    - Names the locale code so the OOXML `lang` attribute downstream stays
      consistent

    Args:
        lang: BCP-47 locale code (e.g. "ko-KR"). Unknown codes are echoed
            verbatim into the directive.

    Returns:
        Multi-paragraph English directive suitable for placement at the top
        of an LLM system message.
    """
    label = _LANG_LABELS.get(lang, lang)
    return (
        f"# Output Language\n\n"
        f"Produce all user-facing content in {label} ({lang}). This applies to:\n"
        f"- Slide titles, subtitles, body copy, captions, callouts\n"
        f"- Speaker notes\n"
        f"- The natural-language portions of `design_spec.md`\n"
        f"- The string values inside `spec_lock.yaml` (keep keys in English)\n"
        f"- Chart labels, table headers, button text\n\n"
        f"Keep the following in **English** regardless of the output language:\n"
        f"- YAML / JSON keys and field names (e.g. `pages:`, `title:`, `subtitle:`)\n"
        f"- Slot identifiers, asset filenames, layout names\n"
        f"- Token names in the design system (e.g. `--primary`, `body-large`)\n"
        f"- OOXML attribute names and CSS-style property names\n\n"
        f"When the user's source material is already in {label}, preserve its "
        f"voice and terminology — do not paraphrase technical terms into "
        f"unfamiliar translations.\n"
    )


def list_available_prompts() -> list[str]:
    """Return the role names backed by a `<role>.en.md` file."""
    if not PROMPTS_DIR.exists():
        return []
    return sorted(p.stem.removesuffix(".en") for p in PROMPTS_DIR.glob("*.en.md"))


# Backwards-compat aliases (M2-era callers passed a `lang` arg). The loader
# now ignores `lang`; callers that want language steering use
# `build_output_lang_directive` and concat it to the system prompt.

def list_available_locales(role: str) -> list[str]:  # pragma: no cover - legacy shim
    """Returns `['en']` if `<role>.en.md` exists, else `[]`."""
    return ["en"] if (PROMPTS_DIR / f"{role}.en.md").exists() else []
