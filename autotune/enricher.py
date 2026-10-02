#!/usr/bin/env python3
"""
enricher.py
-----------
For each UNKNOWN gap in parse_report.json:
  1. Fuzzy-match the cleaned name against USDA SR Legacy bulk data (local CSV)
  2. Fuzzy-match against GroceryDB Harvard CSV
  3. Fall back to live USDA FDC API (requires USDA_API_KEY env var)

If a match is found with confidence ≥ 0.85, emits a proposal:
  - New nutrition_db.json entry        (if ingredient is truly new)
  - New synonyms.json entry            (if ingredient maps to an existing key)

Proposals land in proposals.json for autotune.py to apply.

Usage:
  python autotune/enricher.py
  python autotune/enricher.py --report parse_report.json --out proposals.json
  python autotune/enricher.py --max-gaps 500

Data files (download once, checked in or cached locally):
  autotune/data/usda_sr_legacy.csv    (USDA SR Legacy, ~9k entries)
  autotune/data/grocerydb.csv         (GroceryDB Harvard, ~3k entries)

USDA SR Legacy download:
  https://fdc.nal.usda.gov/download-datasets.html  → SR Legacy, CSV
GroceryDB download:
  https://github.com/GLambard/GroceryDB  → data/GroceryDB_foods.csv
"""

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

import requests
from rapidfuzz import fuzz, process

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
REPO_ROOT  = SCRIPT_DIR.parent        # cravebox-nutrition/
DATA_DIR_ROOT = REPO_ROOT / "data"   # cravebox-nutrition/data/

DB_FILE = DATA_DIR_ROOT / "nutrition_db.json"
SYNONYMS_FILE = DATA_DIR_ROOT / "synonyms.json"
DEFAULT_REPORT = SCRIPT_DIR / "parse_report.json"
DEFAULT_OUT = SCRIPT_DIR / "proposals.json"

DATA_DIR = SCRIPT_DIR / "data"
# FDC multi-file format (what USDA distributes)
USDA_FOOD_CSV     = DATA_DIR / "usda_sr_legacy.csv"   # food names + fdc_ids
USDA_NUTRIENT_CSV = DATA_DIR / "nutrient.csv"          # nutrient_id → nutrient_nbr
USDA_FN_CSV       = DATA_DIR / "food_nutrient.csv"     # fdc_id + nutrient_id → amount
GROCERYDB_CSV     = DATA_DIR / "grocerydb.csv"

USDA_FDC_API = "https://api.nal.usda.gov/fdc/v1"
CONFIDENCE_THRESHOLD = 0.85

# nutrient_nbr values we want (from nutrient.csv nutrient_nbr column)
NUTRIENT_NBR_MAP = {
    "kcal": "208",   # Energy (kcal)
    "p":    "203",   # Protein
    "c":    "205",   # Carbohydrate, by difference
    "f":    "204",   # Total lipid (fat)
    "fb":   "291",   # Fiber, total dietary
    "sg":   "269",   # Sugars, total
    "na":   "307",   # Sodium, Na
}
# Keep for FDC API fallback
NUTRIENT_MAP = NUTRIENT_NBR_MAP


# ── Data loading ──────────────────────────────────────────────────────────────

def load_db() -> dict:
    return json.loads(DB_FILE.read_text(encoding="utf-8"))


def load_synonyms() -> dict:
    if SYNONYMS_FILE.exists():
        return json.loads(SYNONYMS_FILE.read_text(encoding="utf-8"))
    return {}


def load_usda_sr(food_csv: Path = USDA_FOOD_CSV,
                  nutrient_csv: Path = USDA_NUTRIENT_CSV,
                  fn_csv: Path = USDA_FN_CSV) -> list[dict]:
    """
    Load USDA FDC SR Legacy data from three CSVs:
      usda_sr_legacy.csv  — fdc_id, description  (food names)
      nutrient.csv        — id, nutrient_nbr      (nutrient definitions)
      food_nutrient.csv   — fdc_id, nutrient_id, amount (per 100g values)
    """
    if not food_csv.exists():
        return []

    # Step 1: load SR Legacy food names → {fdc_id: name}
    print("    Loading SR Legacy food names ...")
    sr_foods: dict[str, str] = {}
    with open(food_csv, encoding="utf-8", errors="replace") as f:
        for row in csv.DictReader(f):
            if row.get("data_type", "").strip('"') == "sr_legacy_food":
                fdc_id = row.get("fdc_id", "").strip('"')
                desc   = row.get("description", "").strip('"').lower()
                if fdc_id and desc:
                    sr_foods[fdc_id] = desc
    print(f"    {len(sr_foods):,} SR Legacy foods found")

    if not sr_foods:
        return []

    # Step 2: build nutrient_id → schema_key map from nutrient.csv
    # nutrient.id (used in food_nutrient) → our key (kcal/p/c/f/fb/sg/na)
    nbr_to_key = {v: k for k, v in NUTRIENT_NBR_MAP.items()}  # "208" → "kcal" etc.
    nutrient_id_to_key: dict[str, str] = {}
    if nutrient_csv.exists():
        with open(nutrient_csv, encoding="utf-8", errors="replace") as f:
            for row in csv.DictReader(f):
                nid  = row.get("id", "").strip('"')
                nbrr = row.get("nutrient_nbr", "").strip('"').strip(".0")
                key  = nbr_to_key.get(nbrr)
                if key:
                    nutrient_id_to_key[nid] = key

    # Step 3: stream food_nutrient.csv, collect values for SR Legacy fdc_ids only
    print("    Streaming food_nutrient.csv (this may take a moment) ...")
    food_nutrients: dict[str, dict] = {fid: {} for fid in sr_foods}

    with open(fn_csv, encoding="utf-8", errors="replace") as f:
        for row in csv.DictReader(f):
            fdc_id     = row.get("fdc_id", "").strip('"')
            nutrient_id = row.get("nutrient_id", "").strip('"')
            amount_str  = row.get("amount", "").strip('"')

            if fdc_id not in food_nutrients:
                continue
            key = nutrient_id_to_key.get(nutrient_id)
            if key:
                food_nutrients[fdc_id][key] = _safe_float(amount_str)

    # Step 4: assemble final corpus
    entries: list[dict] = []
    for fdc_id, name in sr_foods.items():
        nutrients = food_nutrients.get(fdc_id, {})
        entries.append({
            "name":   name,
            "kcal":   nutrients.get("kcal", 0.0),
            "p":      nutrients.get("p",    0.0),
            "c":      nutrients.get("c",    0.0),
            "f":      nutrients.get("f",    0.0),
            "fb":     nutrients.get("fb",   0.0),
            "sg":     nutrients.get("sg",   0.0),
            "na":     nutrients.get("na",   0.0),
            "source": "usda_sr",
        })

    return entries


def load_grocerydb(csv_path: Path) -> list[dict]:
    """
    Load GroceryDB Harvard CSV.
    Expected columns: food_name, energy_kcal, protein_g, fat_g, carb_g,
                      fiber_g, sugar_g, sodium_mg  (per 100g)
    """
    if not csv_path.exists():
        return []

    entries = []
    with open(csv_path, encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                entries.append({
                    "name": row.get("food_name", "").lower().strip(),
                    "kcal": _safe_float(row.get("energy_kcal")),
                    "p":    _safe_float(row.get("protein_g")),
                    "c":    _safe_float(row.get("carb_g")),
                    "f":    _safe_float(row.get("fat_g")),
                    "fb":   _safe_float(row.get("fiber_g")),
                    "sg":   _safe_float(row.get("sugar_g")),
                    "na":   _safe_float(row.get("sodium_mg")),
                    "source": "grocerydb",
                })
            except Exception:
                continue
    return entries


def _safe_float(val) -> float:
    try:
        return round(float(str(val).replace(",", "").strip()), 1)
    except (TypeError, ValueError):
        return 0.0


# ── USDA FDC API fallback ─────────────────────────────────────────────────────

def query_fdc_api(query: str, api_key: str, timeout: int = 10) -> Optional[dict]:
    """Query USDA FDC API for a food item. Returns our nutrition schema dict or None."""
    if not api_key:
        return None

    try:
        r = requests.get(
            f"{USDA_FDC_API}/foods/search",
            params={"query": query, "api_key": api_key, "pageSize": 3, "dataType": "SR Legacy,Foundation"},
            timeout=timeout,
        )
        r.raise_for_status()
        foods = r.json().get("foods", [])
    except Exception:
        return None

    if not foods:
        return None

    food = foods[0]
    nutrients = {str(n["nutrientNumber"]): n.get("value", 0) for n in food.get("foodNutrients", [])}

    return {
        "kcal": round(float(nutrients.get(NUTRIENT_MAP["kcal"], 0)), 1),
        "p":    round(float(nutrients.get(NUTRIENT_MAP["p"], 0)), 1),
        "c":    round(float(nutrients.get(NUTRIENT_MAP["c"], 0)), 1),
        "f":    round(float(nutrients.get(NUTRIENT_MAP["f"], 0)), 1),
        "fb":   round(float(nutrients.get(NUTRIENT_MAP["fb"], 0)), 1),
        "sg":   round(float(nutrients.get(NUTRIENT_MAP["sg"], 0)), 1),
        "na":   round(float(nutrients.get(NUTRIENT_MAP["na"], 0)), 1),
    }


# ── Fuzzy matching ────────────────────────────────────────────────────────────

def fuzzy_match(query: str, corpus: list[dict], threshold: float = CONFIDENCE_THRESHOLD) -> Optional[tuple[dict, float]]:
    """
    Match query against corpus entries by name.
    Returns (entry, confidence) or None.
    """
    if not corpus:
        return None

    names = [e["name"] for e in corpus]
    result = process.extractOne(query, names, scorer=fuzz.token_sort_ratio)
    if not result:
        return None

    best_name, score, idx = result
    confidence = score / 100.0

    if confidence >= threshold:
        return corpus[idx], confidence
    return None


def find_existing_key(query: str, db: dict, threshold: float = CONFIDENCE_THRESHOLD) -> Optional[tuple[str, float]]:
    """Check if query maps to an existing DB key with high confidence."""
    result = process.extractOne(query, list(db.keys()), scorer=fuzz.token_sort_ratio)
    if not result:
        return None
    best_key, score, _ = result
    confidence = score / 100.0
    if confidence >= threshold:
        return best_key, confidence
    return None


# ── Proposal generation ───────────────────────────────────────────────────────

def enrich_gap(cleaned_name: str, examples: list[str], db: dict, synonyms: dict,
               usda_corpus: list[dict], grocery_corpus: list[dict],
               api_key: str) -> Optional[dict]:
    """
    Try to enrich one gap. Returns a proposal dict or None.
    """
    query = cleaned_name

    # First check: does this map to an existing DB key? → synonym proposal
    existing = find_existing_key(query, db, threshold=0.90)
    if existing:
        existing_key, conf = existing
        if existing_key != query:  # avoid self-referential synonyms
            return {
                "type": "synonym",
                "alias": cleaned_name,
                "canonical": existing_key,
                "confidence": round(conf, 3),
                "source": "fuzzy_existing_key",
            }

    # Try USDA SR Legacy
    match = fuzzy_match(query, usda_corpus)
    if match:
        entry, conf = match
        nutrition = {k: entry[k] for k in ("kcal", "p", "c", "f", "fb", "sg", "na")}
        return {
            "type": "new_entry",
            "key": cleaned_name,
            "nutrition": nutrition,
            "confidence": round(conf, 3),
            "source": f"usda_sr:{entry['name']}",
        }

    # Try GroceryDB
    match = fuzzy_match(query, grocery_corpus)
    if match:
        entry, conf = match
        nutrition = {k: entry[k] for k in ("kcal", "p", "c", "f", "fb", "sg", "na")}
        return {
            "type": "new_entry",
            "key": cleaned_name,
            "nutrition": nutrition,
            "confidence": round(conf, 3),
            "source": f"grocerydb:{entry['name']}",
        }

    # Fallback: USDA FDC API
    if api_key:
        nutrition = query_fdc_api(query, api_key)
        if nutrition and nutrition.get("kcal", 0) > 0:
            return {
                "type": "new_entry",
                "key": cleaned_name,
                "nutrition": nutrition,
                "confidence": 0.85,  # API results assumed min threshold
                "source": "usda_fdc_api",
            }
        time.sleep(0.5)  # rate limit courtesy

    return None


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="CraveBox nutrition enricher")
    parser.add_argument("--report", default=str(DEFAULT_REPORT), help="parse_report.json path")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="Output proposals.json path")
    parser.add_argument("--max-gaps", type=int, default=1000, help="Max gaps to process (default 1000)")
    parser.add_argument("--threshold", type=float, default=CONFIDENCE_THRESHOLD,
                        help=f"Min confidence to emit proposal (default {CONFIDENCE_THRESHOLD})")
    args = parser.parse_args()

    report_path = Path(args.report)
    if not report_path.exists():
        sys.exit(f"ERROR: report not found: {report_path}")

    print("=== CraveBox Nutrition Enricher ===")

    db = load_db()
    synonyms = load_synonyms()
    api_key = os.environ.get("USDA_API_KEY", "")

    print(f"Loading USDA SR Legacy from {DATA_DIR} ...")
    usda_corpus = load_usda_sr()
    print(f"  {len(usda_corpus):,} entries loaded" if usda_corpus else "  WARNING: files not found — download from https://fdc.nal.usda.gov/download-datasets.html")

    print(f"Loading GroceryDB from {GROCERYDB_CSV} ...")
    grocery_corpus = load_grocerydb(GROCERYDB_CSV)
    print(f"  {len(grocery_corpus):,} entries loaded" if grocery_corpus else "  WARNING: not found — download from https://github.com/GLambard/GroceryDB")

    if not usda_corpus and not grocery_corpus and not api_key:
        print("  WARNING: No local data files and no USDA_API_KEY. Set USDA_API_KEY env var for API fallback.")

    report = json.loads(report_path.read_text(encoding="utf-8"))
    gaps = report.get("unknown_gaps", [])
    gaps = gaps[: args.max_gaps]
    print(f"Processing {len(gaps):,} unknown gaps ...")

    proposals: list[dict] = []
    skipped = 0

    for gap in gaps:
        cleaned_name = gap["cleaned_name"]
        if not cleaned_name or len(cleaned_name) < 2:
            skipped += 1
            continue

        proposal = enrich_gap(
            cleaned_name=cleaned_name,
            examples=gap.get("examples", []),
            db=db,
            synonyms=synonyms,
            usda_corpus=usda_corpus,
            grocery_corpus=grocery_corpus,
            api_key=api_key,
        )

        if proposal and proposal["confidence"] >= args.threshold:
            proposal["gap_count"] = gap.get("count", 1)
            proposals.append(proposal)
        else:
            skipped += 1

    proposals.sort(key=lambda x: -x.get("gap_count", 1))

    new_entries = sum(1 for p in proposals if p["type"] == "new_entry")
    new_synonyms = sum(1 for p in proposals if p["type"] == "synonym")

    print(f"\nProposals generated:")
    print(f"  New DB entries: {new_entries:,}")
    print(f"  New synonyms:   {new_synonyms:,}")
    print(f"  Skipped:        {skipped:,}")

    out = {
        "summary": {
            "gaps_processed": len(gaps),
            "proposals": len(proposals),
            "new_entries": new_entries,
            "new_synonyms": new_synonyms,
            "skipped": skipped,
        },
        "proposals": proposals,
    }

    out_path = Path(args.out)
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n  Proposals → {out_path}")
    print("=== Done ===")


if __name__ == "__main__":
    main()
