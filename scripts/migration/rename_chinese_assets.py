"""Rename Chinese-named directories/files under src/xgen_edit2docs/core/templates to English.

This is a one-shot migration script for the G13 patch documented in
ppt-master-analysis/03-korean-gaps.md and 06-bilingual-conventions.md.

Strategy:
1. Rename files inside each Chinese-named directory (using git mv).
2. Rename the directories themselves.
3. Walk all text files (.md, .svg, .json, .py, .yaml) under src/ and substitute
   old path/name references with the new English ones.

Usage:
    python3 scripts/migration/rename_chinese_assets.py [--dry-run]

The script is idempotent: running twice does nothing the second time.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TEMPLATES_LAYOUTS = REPO_ROOT / "src" / "xgen_edit2docs" / "core" / "templates" / "layouts"
SEARCH_ROOTS = [REPO_ROOT / "src", REPO_ROOT / "tests"]

# Directory renames: old name (Chinese) -> new name (English).
DIR_RENAMES: dict[str, str] = {
    "中国电建_常规": "china_power_construction_standard",
    "中国电建_现代": "china_power_construction_modern",
    "中汽研_常规": "caar_standard",
    "中汽研_现代": "caar_modern",
    "中汽研_商务": "caar_business",
    "招商银行": "cmb_bank",
    "重庆大学": "chongqing_university",
}

# File renames within Chinese-named directories. Applied AFTER directory rename
# context (the value side has neither old nor new directory prefix).
FILE_RENAMES: dict[str, str] = {
    "重庆大学logo.png": "cqu_logo.png",
    "重庆大学logo2.png": "cqu_logo_alt.png",
    "水电三局logo.png": "hydropower_bureau3_logo.png",
    "电建logo.png": "power_construction_logo.png",
    "中国水务logo.png": "china_water_logo.png",
    "华东院logo.png": "east_china_institute_logo.png",
    "大型 logo.png": "large_logo.png",
    "右上角 logo.png": "top_right_logo.png",
}


def run_git_mv(src: Path, dst: Path, dry_run: bool) -> None:
    """Run `git mv` from REPO_ROOT (relative paths) to preserve history."""
    rel_src = src.relative_to(REPO_ROOT)
    rel_dst = dst.relative_to(REPO_ROOT)
    if dry_run:
        print(f"[dry-run] git mv {rel_src} -> {rel_dst}")
        return
    if not src.exists():
        print(f"[skip]    {rel_src} (does not exist)")
        return
    if dst.exists():
        print(f"[skip]    {rel_dst} already exists")
        return
    result = subprocess.run(
        ["git", "mv", str(rel_src), str(rel_dst)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"[error]   git mv failed: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    print(f"[moved]   {rel_src} -> {rel_dst}")


def rename_files_in_chinese_dirs(dry_run: bool) -> None:
    """Step 1: rename files INSIDE Chinese-named directories (before dir rename)."""
    for chinese_dir_name in DIR_RENAMES:
        dir_path = TEMPLATES_LAYOUTS / chinese_dir_name
        if not dir_path.is_dir():
            continue
        for old_file_name, new_file_name in FILE_RENAMES.items():
            old_path = dir_path / old_file_name
            new_path = dir_path / new_file_name
            if old_path.exists():
                run_git_mv(old_path, new_path, dry_run)


def rename_directories(dry_run: bool) -> None:
    """Step 2: rename Chinese directories themselves."""
    for old_name, new_name in DIR_RENAMES.items():
        old_path = TEMPLATES_LAYOUTS / old_name
        new_path = TEMPLATES_LAYOUTS / new_name
        if old_path.exists():
            run_git_mv(old_path, new_path, dry_run)


def update_text_references(dry_run: bool) -> None:
    """Step 3: substitute old names in text files (md, svg, json, py, yaml, yml)."""
    extensions = {".md", ".svg", ".json", ".py", ".yaml", ".yml"}
    # Combined substitution map; do filename renames first (so directory token
    # is not partially matched). Order matters when one name is a substring of
    # another (none here, but stay defensive).
    substitutions = {**FILE_RENAMES, **DIR_RENAMES}

    files_touched = 0
    for root in SEARCH_ROOTS:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix not in extensions:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            new_text = text
            for old, new in substitutions.items():
                if old in new_text:
                    new_text = new_text.replace(old, new)
            if new_text != text:
                rel = path.relative_to(REPO_ROOT)
                if dry_run:
                    print(f"[dry-run] update refs in {rel}")
                else:
                    path.write_text(new_text, encoding="utf-8")
                    print(f"[updated] {rel}")
                files_touched += 1
    if not dry_run:
        print(f"[summary] {files_touched} files updated with new references")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Show actions without changing anything")
    args = parser.parse_args()

    print(f"REPO_ROOT = {REPO_ROOT}")
    if not TEMPLATES_LAYOUTS.exists():
        print(f"[fatal] templates/layouts not found: {TEMPLATES_LAYOUTS}", file=sys.stderr)
        sys.exit(1)

    print("=== Step 1: rename files inside Chinese directories ===")
    rename_files_in_chinese_dirs(args.dry_run)

    print("=== Step 2: rename Chinese directories ===")
    rename_directories(args.dry_run)

    print("=== Step 3: update text references ===")
    update_text_references(args.dry_run)

    print("=== Done ===")


if __name__ == "__main__":
    main()
