#!/usr/bin/env python3
"""
export_from_kotlin.py
---------------------
One-time (and as-needed) utility.
Reads NutritionParser.kt and extracts:
  - SYNONYMS mapOf(...)  → synonyms.json
  - MODIFIERS setOf(...) → modifiers.json

Run from repo root or autotune/ directory:
  python autotune/export_from_kotlin.py

Outputs land next to this script:
  autotune/synonyms.json
  autotune/modifiers.json
"""

import re
import json
import sys
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent


def _find_repo() -> Path:
    """Locate RecipeExtractor-clean/ by searching upward then sideways from this script."""
    # Walk upward looking for the repo marker
    for parent in [SCRIPT_DIR] + list(SCRIPT_DIR.parents):
        candidate = parent / "RecipeExtractor-clean"
        if (candidate / "settings.gradle.kts").exists():
            return candidate
        # Maybe we're already inside the repo
        if (parent / "settings.gradle.kts").exists():
            return parent
    sys.exit("ERROR: could not locate RecipeExtractor-clean/ repo. "
             "Place autotune/ anywhere inside or alongside the repo folder.")


REPO_ROOT = _find_repo()
KT_FILE = REPO_ROOT / "app/src/main/java/com/cravebox/app/util/NutritionParser.kt"

OUT_SYNONYMS  = REPO_ROOT / "data" / "synonyms.json"
OUT_MODIFIERS = REPO_ROOT / "data" / "modifiers.json"


def extract_synonyms(text: str) -> dict:
    """Extract mapOf("alias" to "canonical", ...) block labelled SYNONYMS.
    Scans line-by-line from the mapOf opening to avoid early termination
    on ')' characters inside comments or values.
    """
    lines = text.splitlines()

    # Find the opening line
    start = None
    for i, line in enumerate(lines):
        if re.search(r'SYNONYMS\s*=\s*mapOf\s*\(', line):
            start = i
            break

    if start is None:
        sys.exit("ERROR: could not find SYNONYMS mapOf block in NutritionParser.kt")

    pair_re = re.compile(r'"([^"]+)"\s+to\s+"([^"]+)"')
    # Marks end of mapOf: a line that is just ')' or '),' with optional whitespace
    end_re = re.compile(r'^\s*\)\s*,?\s*$')
    # Also stop on next val/fun/private declaration at same/lower indent level
    decl_re = re.compile(r'^\s*(private\s+)?(val|fun|var|companion|class|object)\s+')

    synonyms = {}
    in_block = True
    for line in lines[start + 1:]:
        if end_re.match(line):
            break
        if decl_re.match(line):
            break
        m = pair_re.search(line)
        if m:
            synonyms[m.group(1).strip()] = m.group(2).strip()

    return synonyms


def extract_modifiers(text: str) -> list:
    """Extract setOf(...) block labelled MODIFIERS."""
    m = re.search(r'MODIFIERS\s*=\s*setOf\s*\((.+?)\)', text, re.DOTALL)
    if not m:
        sys.exit("ERROR: could not find MODIFIERS setOf block in NutritionParser.kt")

    block = m.group(1)
    words = re.findall(r'"([^"]+)"', block)
    return sorted(set(w.strip() for w in words))


def main():
    if not KT_FILE.exists():
        sys.exit(f"ERROR: NutritionParser.kt not found at {KT_FILE}")

    print(f"Reading {KT_FILE} ...")
    text = KT_FILE.read_text(encoding="utf-8")

    synonyms = extract_synonyms(text)
    modifiers = extract_modifiers(text)

    OUT_SYNONYMS.write_text(json.dumps(synonyms, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  synonyms.json  — {len(synonyms):,} entries → {OUT_SYNONYMS}")

    OUT_MODIFIERS.write_text(json.dumps(modifiers, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  modifiers.json — {len(modifiers):,} entries → {OUT_MODIFIERS}")


if __name__ == "__main__":
    main()
