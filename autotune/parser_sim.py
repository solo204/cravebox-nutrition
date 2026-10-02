#!/usr/bin/env python3
"""
parser_sim.py
-------------
Python port of NutritionParser.kt's 4-stage ingredient resolution waterfall.
Runs against crawled_ingredients.json (or any list of raw ingredient strings)
and produces parse_report.json identifying UNKNOWN and ESTIMATED gaps
that the enricher can fill.

Stages (mirrors NutritionParser.kt):
  1. Exact DB match          → OK
  2. SYNONYM → DB match      → OK
  3. Fuzzy word-boundary     → ESTIMATED (longest key that fully matches)
  4. First-word partial      → ESTIMATED
  X. No match                → UNKNOWN

Usage:
  python autotune/parser_sim.py
  python autotune/parser_sim.py --input crawled_ingredients.json
  python autotune/parser_sim.py --input my_list.json --out report.json
"""

import argparse
import json
import re
import sys
from pathlib import Path
from collections import Counter

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
REPO_ROOT  = SCRIPT_DIR.parent        # cravebox-nutrition/
DATA_DIR_ROOT = REPO_ROOT / "data"   # cravebox-nutrition/data/

DB_FILE = DATA_DIR_ROOT / "nutrition_db.json"
SYNONYMS_FILE = DATA_DIR_ROOT / "synonyms.json"
MODIFIERS_FILE = DATA_DIR_ROOT / "modifiers.json"
DEFAULT_INPUT = SCRIPT_DIR / "crawled_ingredients.json"
DEFAULT_OUT = SCRIPT_DIR / "parse_report.json"


# ── Data loading ──────────────────────────────────────────────────────────────

def load_db() -> dict:
    if not DB_FILE.exists():
        sys.exit(f"ERROR: nutrition_db.json not found at {DB_FILE}")
    return json.loads(DB_FILE.read_text(encoding="utf-8"))


def load_synonyms() -> dict:
    if SYNONYMS_FILE.exists():
        return json.loads(SYNONYMS_FILE.read_text(encoding="utf-8"))
    print(f"  WARNING: synonyms.json not found — run export_from_kotlin.py first. Proceeding with empty synonyms.")
    return {}


def load_modifiers() -> list:
    if MODIFIERS_FILE.exists():
        return json.loads(MODIFIERS_FILE.read_text(encoding="utf-8"))
    print(f"  WARNING: modifiers.json not found — run export_from_kotlin.py first. Using empty modifiers.")
    return []


# ── cleanName — mirrors NutritionParser.kt cleanName() ───────────────────────

# Patterns to strip (mirrors NutritionParser.kt)
_PAREN_RE = re.compile(r'\([^)]*\)')
_DIGITS_LEAD_RE = re.compile(r'^\d+\s*')
_MULTI_SPACE_RE = re.compile(r'\s+')
_FRACTION_RE = re.compile(r'^\d+[/⁄]\d+\s*')
_QTY_UNIT_RE = re.compile(
    r'^\s*(\d+[\d./⁄]*\s*)?(cup|cups|tbsp|tbs|tablespoon|tablespoons|tsp|teaspoon|teaspoons|'
    r'oz|ounce|ounces|lb|lbs|pound|pounds|g|gram|grams|kg|kilogram|kilograms|'
    r'ml|milliliter|milliliters|l|liter|liters|fl\.?\s*oz|'
    r'pinch|dash|handful|bunch|clove|cloves|slice|slices|piece|pieces|'
    r'can|cans|package|packages|pkg|bag|bags|box|boxes|jar|jars|'
    r'sprig|sprigs|stalk|stalks|head|heads|ear|ears|sheet|sheets|'
    r'large|medium|small|whole)\b\s*',
    re.IGNORECASE
)


def clean_name(raw: str, modifiers: set) -> str:
    """Mirror of NutritionParser.kt cleanName()."""
    # Lowercase
    s = raw.lower()
    # Strip parenthetical notes
    s = _PAREN_RE.sub('', s)
    # Split on comma, take first part
    s = s.split(',')[0]
    # Strip leading quantity + unit
    s = _QTY_UNIT_RE.sub('', s)
    # Strip leading fractions / digits
    s = _FRACTION_RE.sub('', s)
    s = _DIGITS_LEAD_RE.sub('', s)
    # Tokenize, remove modifiers and duplicates
    tokens = _MULTI_SPACE_RE.split(s.strip())
    seen: set = set()
    result: list = []
    for t in tokens:
        t = t.strip('.,;:')
        if not t:
            continue
        if t in modifiers:
            continue
        if t not in seen:
            seen.add(t)
            result.append(t)
    return ' '.join(result).strip()


# ── 4-stage waterfall ─────────────────────────────────────────────────────────

def resolve(name: str, db: dict, synonyms: dict, modifiers: set) -> tuple[str, str, str]:
    """
    Returns (status, matched_key, matched_via).
    status: 'OK' | 'ESTIMATED' | 'UNKNOWN'
    """
    cleaned = clean_name(name, modifiers)
    if not cleaned:
        return 'UNKNOWN', '', 'empty_after_clean'

    # Stage 1: exact DB match
    if cleaned in db:
        return 'OK', cleaned, 'exact'

    # Stage 2: synonym map → DB
    if cleaned in synonyms:
        canonical = synonyms[cleaned]
        if canonical in db:
            return 'OK', canonical, f'synonym→{canonical}'

    # Stage 3: fuzzy — longest DB key that word-boundary-matches the cleaned name
    best_key = None
    best_len = 0
    for k in db:
        if len(k) > best_len:
            # word-boundary check: cleaned name starts with k as a whole word
            pattern = r'\b' + re.escape(k) + r'\b'
            if re.search(pattern, cleaned) or cleaned.startswith(k + ' ') or cleaned == k:
                best_key = k
                best_len = len(k)

    if best_key:
        return 'ESTIMATED', best_key, f'fuzzy→{best_key}'

    # Stage 4: first-word partial
    first = cleaned.split()[0] if cleaned.split() else ''
    if first and first in db:
        return 'ESTIMATED', first, f'first_word→{first}'

    return 'UNKNOWN', cleaned, 'no_match'


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="CraveBox NutritionParser simulation")
    parser.add_argument("--input", default=str(DEFAULT_INPUT), help="Input ingredients JSON (list of strings)")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="Output parse report JSON")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        sys.exit(f"ERROR: input file not found: {input_path}")

    print("=== CraveBox NutritionParser Simulation ===")
    print("Loading DB, synonyms, modifiers ...")

    db = load_db()
    synonyms = load_synonyms()
    modifiers_list = load_modifiers()
    modifiers = set(modifiers_list)

    ingredients: list[str] = json.loads(input_path.read_text(encoding="utf-8"))
    print(f"Loaded {len(db):,} DB entries | {len(synonyms):,} synonyms | {len(modifiers):,} modifiers")
    print(f"Running simulation on {len(ingredients):,} ingredient strings ...")

    results: list[dict] = []
    status_counts: Counter = Counter()

    for raw in ingredients:
        status, key, via = resolve(raw, db, synonyms, modifiers)
        status_counts[status] += 1
        results.append({
            "raw": raw,
            "cleaned": clean_name(raw, modifiers),
            "status": status,
            "matched_key": key,
            "matched_via": via,
        })

    total = len(results)
    ok = status_counts['OK']
    est = status_counts['ESTIMATED']
    unk = status_counts['UNKNOWN']

    print(f"\nResults:")
    print(f"  OK:        {ok:>6,}  ({100*ok/total:.1f}%)")
    print(f"  ESTIMATED: {est:>6,}  ({100*est/total:.1f}%)")
    print(f"  UNKNOWN:   {unk:>6,}  ({100*unk/total:.1f}%)")

    # Group UNKNOWNs by cleaned name for enricher
    unknown_groups: dict[str, list[str]] = {}
    for r in results:
        if r['status'] == 'UNKNOWN':
            cname = r['cleaned']
            unknown_groups.setdefault(cname, []).append(r['raw'])

    # Top UNKNOWNs by frequency
    top_unknowns = sorted(unknown_groups.items(), key=lambda x: -len(x[1]))

    report = {
        "summary": {
            "total": total,
            "ok": ok,
            "estimated": est,
            "unknown": unk,
            "ok_pct": round(100 * ok / total, 1) if total else 0,
        },
        "unknown_gaps": [
            {"cleaned_name": k, "count": len(v), "examples": v[:3]}
            for k, v in top_unknowns
        ],
        "estimated_details": [
            r for r in results if r['status'] == 'ESTIMATED'
        ],
        "all_results": results,
    }

    out_path = Path(args.out)
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n  Report → {out_path}")
    print(f"  Top unknown gaps: {', '.join(k for k, _ in top_unknowns[:10])}")
    print("=== Done ===")


if __name__ == "__main__":
    main()
