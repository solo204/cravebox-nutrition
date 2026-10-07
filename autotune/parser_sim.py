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
  3. Fuzzy substring         → ESTIMATED (longest key found in / containing cleaned name)
  4. First-word partial      → ESTIMATED
  X. No match                → UNKNOWN

Usage:
  python autotune/parser_sim.py
  python autotune/parser_sim.py --input crawled_ingredients.json
  python autotune/parser_sim.py --input my_list.json --out report.json
  python autotune/parser_sim.py --min-match-rate 0.99   # fail if below threshold
"""

import argparse
import json
import re
import sys
from pathlib import Path
from collections import Counter, defaultdict

# ── ingredient-parser-nlp (optional, graceful fallback to regex if missing) ───
try:
    from ingredient_parser import parse_ingredient as _nlp_parse
    _NLP_AVAILABLE = True
except ImportError:
    _NLP_AVAILABLE = False
    print("  INFO: ingredient-parser-nlp not installed — using regex name extraction only.")

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


# ── To-taste stripping — mirrors NutritionParser.kt ─────────────────────────

_TO_TASTE_RES = [
    re.compile(r'^~?to\s+taste\s+', re.I),
    re.compile(r'\s+to\s+taste$', re.I),
    re.compile(r'^~?a\s+little\s+bit\s+(of\s+)?', re.I),
    re.compile(r'^~?a\s+pinch\s+(of\s+)?', re.I),
    re.compile(r'^~?a?\s*splash\s+(of\s+)?', re.I),
]
_SKIP_RES = [
    re.compile(r'as\s+needed', re.I),
    re.compile(r'^water$', re.I),
    re.compile(r'^salt\s+and\s+pepper$', re.I),
    re.compile(r'^optional$', re.I),
]

def _strip_to_taste(s: str) -> str:
    for r in _TO_TASTE_RES:
        s = r.sub('', s)
    return s.strip()

def _is_to_taste(s: str) -> bool:
    return any(r.search(s) for r in _TO_TASTE_RES)

def _should_skip(s: str) -> bool:
    return any(r.search(s) for r in _SKIP_RES)


# ── Fast resolver — build word-index once, then O(words) per query ───────────

def build_index(db: dict) -> dict[str, list[str]]:
    """Map every word in every DB key → list of keys containing that word."""
    idx: dict[str, list[str]] = defaultdict(list)
    for k in db:
        for w in k.split():
            idx[w].append(k)
    return idx


def nlp_extract_name(raw: str) -> str | None:
    """
    Use ingredient-parser-nlp CRF model to extract the ingredient NAME field.
    Returns the name string (lowercased), or None if unavailable/failed.
    Falls back gracefully if the library isn't installed or parsing fails.
    """
    if not _NLP_AVAILABLE:
        return None
    try:
        result = _nlp_parse(raw)
        name = result.name
        if name and hasattr(name, 'text') and name.text:
            return name.text.lower().strip()
        if isinstance(name, list) and name:
            return ' '.join(n.text for n in name if hasattr(n, 'text')).lower().strip()
    except Exception:
        pass
    return None


def resolve(name: str, db: dict, synonyms: dict, modifiers: set,
            db_index: dict) -> tuple[str, str, str]:
    """
    Returns (status, matched_key, matched_via).
    status: 'OK' | 'ESTIMATED' | 'UNKNOWN'
    """
    # Strip to-taste qualifiers before cleaning
    raw = name.strip().lstrip('~-•').strip()
    if _is_to_taste(raw):
        raw = _strip_to_taste(raw)
    if not raw or _should_skip(raw):
        return 'SKIP', '', 'skip'

    # Stage 0: NLP name extraction (ingredient-parser-nlp CRF, 95.86% accuracy)
    # Handles fractions, ranges, complex modifiers better than regex.
    # Falls back to regex clean_name() if NLP unavailable or returns empty.
    nlp_name = nlp_extract_name(raw)
    if nlp_name:
        # Strip any remaining modifiers from NLP output
        tokens = _MULTI_SPACE_RE.split(nlp_name.strip())
        cleaned = ' '.join(t for t in tokens if t and t not in modifiers).strip()
    else:
        cleaned = clean_name(raw, modifiers)

    if not cleaned:
        return 'SKIP', '', 'empty_after_clean'

    # Stage 1: exact DB match
    if cleaned in db:
        return 'OK', cleaned, 'exact'

    # Stage 2: synonym → DB
    canonical = synonyms.get(cleaned)
    if canonical and canonical in db:
        return 'OK', canonical, f'synonym→{canonical}'

    # Stage 3: fast substring fuzzy — candidates via word index, no regex
    words = cleaned.split()
    candidates: set[str] = set()
    for w in words:
        candidates.update(db_index.get(w, []))
    # also check if cleaned is a substring of any candidate or vice versa
    best_key, best_len = None, 0
    for k in candidates:
        if len(k) >= 3 and (k in cleaned or cleaned in k or
                             cleaned.startswith(k) or k.startswith(cleaned)):
            if len(k) > best_len:
                best_key, best_len = k, len(k)

    if best_key:
        return 'ESTIMATED', best_key, f'fuzzy→{best_key}'

    # Stage 4: first meaningful word in DB
    first = next((w for w in words if len(w) > 3), '')
    if first and first in db:
        return 'ESTIMATED', first, f'first_word→{first}'

    return 'UNKNOWN', cleaned, 'no_match'


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="CraveBox NutritionParser simulation")
    parser.add_argument("--input", default=str(DEFAULT_INPUT), help="Input ingredients JSON (list of strings)")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="Output parse report JSON")
    parser.add_argument("--min-match-rate", type=float, default=0.0,
                        help="Exit non-zero if match rate (OK+ESTIMATED / countable) falls below this. "
                             "Use 0.99 in CI to gate on regressions.")
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

    # Build word index once for fast fuzzy matching
    db_index = build_index(db)

    results: list[dict] = []
    status_counts: Counter = Counter()
    estimated_pairs: Counter = Counter()   # (cleaned, matched_key) → count

    for raw in ingredients:
        status, key, via = resolve(raw, db, synonyms, modifiers, db_index)
        status_counts[status] += 1
        cleaned = clean_name(_strip_to_taste(raw.strip().lstrip('~-•').strip()), modifiers)
        results.append({
            "raw": raw,
            "cleaned": cleaned,
            "status": status,
            "matched_key": key,
            "matched_via": via,
        })
        if status == 'ESTIMATED' and cleaned and key and cleaned != key:
            estimated_pairs[(cleaned, key)] += 1

    total = len(results)
    skipped = status_counts['SKIP']
    countable = total - skipped
    ok  = status_counts['OK']
    est = status_counts['ESTIMATED']
    unk = status_counts['UNKNOWN']
    match_rate = round((ok + est) / countable, 4) if countable else 0

    print(f"\nResults (excl. {skipped:,} skipped):")
    print(f"  OK:        {ok:>6,}  ({100*ok/countable:.1f}%)")
    print(f"  ESTIMATED: {est:>6,}  ({100*est/countable:.1f}%)")
    print(f"  UNKNOWN:   {unk:>6,}  ({100*unk/countable:.1f}%)")
    print(f"  Match rate: {100*match_rate:.2f}%")

    # Group UNKNOWNs by cleaned name for enricher
    unknown_groups: dict[str, list[str]] = defaultdict(list)
    for r in results:
        if r['status'] == 'UNKNOWN':
            unknown_groups[r['cleaned']].append(r['raw'])
    top_unknowns = sorted(unknown_groups.items(), key=lambda x: -len(x[1]))

    # Top estimated pairs — candidates for auto-promotion to synonyms
    top_estimated = [
        {"cleaned_name": name, "fuzzy_matched_as": key, "count": cnt}
        for (name, key), cnt in estimated_pairs.most_common(200)
        if cnt >= 2  # only repeat occurrences are worth promoting
    ]
    print(f"  Top estimated pairs (candidates for synonym promotion): {len(top_estimated)}")

    report = {
        "summary": {
            "total": total,
            "skipped": skipped,
            "countable": countable,
            "ok": ok,
            "estimated": est,
            "unknown": unk,
            "match_rate": match_rate,
            "match_rate_pct": round(100 * match_rate, 2),
        },
        "unknown_gaps": [
            {"cleaned_name": k, "count": len(v), "examples": v[:3]}
            for k, v in top_unknowns
        ],
        "top_estimated": top_estimated,
        "all_results": results,
    }

    out_path = Path(args.out)
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n  Report → {out_path}")
    if top_unknowns:
        print(f"  Top unknown gaps: {', '.join(k for k, _ in top_unknowns[:10])}")
    if top_estimated:
        print(f"  Top estimated (synonym candidates):")
        for e in top_estimated[:10]:
            print(f"    [{e['count']}x] {e['cleaned_name']} → {e['fuzzy_matched_as']}")
    print("=== Done ===")

    # Match-rate gate — fail CI if below threshold
    if args.min_match_rate > 0 and match_rate < args.min_match_rate:
        print(f"\n❌ MATCH RATE {100*match_rate:.2f}% is below required {100*args.min_match_rate:.2f}%")
        sys.exit(1)


if __name__ == "__main__":
    main()
