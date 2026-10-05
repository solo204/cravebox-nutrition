#!/usr/bin/env python3
"""
build_usda_compact.py
---------------------
One-time script: reads the large USDA SR Legacy CSVs (locally downloaded)
and extracts only the fields the enricher needs into a compact JSON file
that can be committed to GitHub.

Input files (download once from https://fdc.nal.usda.gov/download-datasets.html):
  autotune/data/usda_sr_legacy.csv   — food names + fdc_id
  autotune/data/nutrient.csv         — nutrient_id → nutrient_nbr
  autotune/data/food_nutrient.csv    — fdc_id + nutrient_id → amount (1.7 GB)

Output:
  autotune/data/usda_compact.json    — {ingredient_name: {kcal, p, c, f, fb, sg, na}}

Usage:
  python autotune/build_usda_compact.py

Run this once locally whenever you re-download the USDA dataset,
then commit autotune/data/usda_compact.json to GitHub.
"""

import csv
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
DATA_DIR   = SCRIPT_DIR / "data"

USDA_FOOD_CSV     = DATA_DIR / "usda_sr_legacy.csv"
USDA_NUTRIENT_CSV = DATA_DIR / "nutrient.csv"
USDA_FN_CSV       = DATA_DIR / "food_nutrient.csv"
OUT_FILE          = DATA_DIR / "usda_compact.json"

# nutrient_nbr → our key
NUTRIENT_NBR_MAP = {
    "208": "kcal",
    "203": "p",
    "205": "c",
    "204": "f",
    "291": "fb",
    "269": "sg",
    "307": "na",
}


def main():
    # ── Step 1: Load food names (fdc_id → name) ───────────────────────────────
    print("Loading food names...")
    if not USDA_FOOD_CSV.exists():
        print(f"ERROR: {USDA_FOOD_CSV} not found. Download SR Legacy CSV from:")
        print("  https://fdc.nal.usda.gov/download-datasets.html")
        sys.exit(1)

    fdc_to_name = {}
    with open(USDA_FOOD_CSV, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            fdc_id = row.get("fdc_id") or row.get("id", "")
            name   = row.get("description", "").strip().lower()
            if fdc_id and name:
                fdc_to_name[fdc_id] = name
    print(f"  {len(fdc_to_name):,} foods loaded")

    # ── Step 2: Build nutrient_id → nutrient_nbr map ─────────────────────────
    print("Loading nutrient definitions...")
    nutrient_id_to_nbr = {}
    if USDA_NUTRIENT_CSV.exists():
        with open(USDA_NUTRIENT_CSV, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                nid = row.get("id", "")
                nbr = row.get("nutrient_nbr", "")
                if nid and nbr:
                    nutrient_id_to_nbr[nid] = nbr
    print(f"  {len(nutrient_id_to_nbr):,} nutrient definitions loaded")

    # ── Step 3: Stream food_nutrient.csv and collect relevant values ──────────
    print(f"Streaming {USDA_FN_CSV.name} (this may take a minute)...")
    if not USDA_FN_CSV.exists():
        print(f"ERROR: {USDA_FN_CSV} not found.")
        sys.exit(1)

    # name → {kcal, p, c, f, fb, sg, na}
    data: dict[str, dict] = {}

    with open(USDA_FN_CSV, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader):
            if i % 5_000_000 == 0 and i > 0:
                print(f"  ...{i:,} rows processed")

            fdc_id = row.get("fdc_id", "")
            nid    = row.get("nutrient_id", "")
            amount = row.get("amount", "")

            name = fdc_to_name.get(fdc_id)
            if not name:
                continue

            nbr = nutrient_id_to_nbr.get(nid)
            if not nbr:
                continue

            key = NUTRIENT_NBR_MAP.get(nbr)
            if not key:
                continue

            try:
                val = round(float(amount), 2)
            except (ValueError, TypeError):
                continue

            if name not in data:
                data[name] = {}
            data[name][key] = val

    # ── Step 4: Filter — keep only entries that have at least kcal ────────────
    before = len(data)
    data = {k: v for k, v in data.items() if "kcal" in v}
    print(f"  Kept {len(data):,} entries with kcal (filtered {before - len(data):,})")

    # ── Step 5: Write compact JSON ────────────────────────────────────────────
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    size_mb = OUT_FILE.stat().st_size / 1_048_576
    print(f"\nDone! Written to {OUT_FILE}")
    print(f"  Entries : {len(data):,}")
    print(f"  Size    : {size_mb:.1f} MB")
    print(f"\nCommit this file to GitHub:")
    print(f"  git add autotune/data/usda_compact.json")
    print(f"  git commit -m 'autotune: add USDA compact lookup'")
    print(f"  git push")


if __name__ == "__main__":
    main()
