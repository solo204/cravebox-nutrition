#!/usr/bin/env python3
"""
autotune.py
-----------
Master orchestrator for the NutritionParser autotune pipeline.

Pipeline:
  1. Crawler   — scrape recipe sites → crawled_ingredients.json
  2. ParserSim — simulate 4-stage resolution → parse_report.json
  3. Enricher  — fill gaps via USDA/GroceryDB → proposals.json
  4. Apply     — write updated nutrition_db.json, synonyms.json, modifiers.json
  5. Version   — bump version.json manifest
  6. Report    — write autotune_summary.json

The final committed files (nutrition_db.json, synonyms.json, modifiers.json,
version.json) are what the Android app NutritionDbUpdater.kt downloads OTA.

Usage:
  python autotune/autotune.py                    # full pipeline
  python autotune/autotune.py --skip-crawl       # reuse existing crawled_ingredients.json
  python autotune/autotune.py --dry-run          # run everything but don't write DB files
  python autotune/autotune.py --max-per-site 50  # limit crawl size for testing
  python autotune/autotune.py --sites "https://example.com"  # crawl extra sites

Environment variables:
  USDA_API_KEY   — USDA FDC API key (free at https://fdc.nal.usda.gov/api-guide.html)
"""

import argparse
import hashlib
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
REPO_ROOT  = SCRIPT_DIR.parent        # cravebox-nutrition/
DATA_DIR_ROOT = REPO_ROOT / "data"   # cravebox-nutrition/data/

DB_FILE = DATA_DIR_ROOT / "nutrition_db.json"
SYNONYMS_FILE = DATA_DIR_ROOT / "synonyms.json"
MODIFIERS_FILE = DATA_DIR_ROOT / "modifiers.json"
VERSION_FILE = DATA_DIR_ROOT / "nutrition_version.json"

CRAWLED_FILE = SCRIPT_DIR / "crawled_ingredients.json"
REPORT_FILE = SCRIPT_DIR / "parse_report.json"
PROPOSALS_FILE = SCRIPT_DIR / "proposals.json"
SUMMARY_FILE = SCRIPT_DIR / "autotune_summary.json"

CRAWLER_SCRIPT = SCRIPT_DIR / "crawler.py"
PARSER_SIM_SCRIPT = SCRIPT_DIR / "parser_sim.py"
ENRICHER_SCRIPT = SCRIPT_DIR / "enricher.py"


# ── Subprocess helpers ────────────────────────────────────────────────────────

def run_step(label: str, cmd: list, check: bool = True) -> int:
    print(f"\n{'='*60}")
    print(f"  STEP: {label}")
    print(f"{'='*60}")
    result = subprocess.run([sys.executable] + cmd, check=check)
    return result.returncode


# ── Apply proposals ───────────────────────────────────────────────────────────

def apply_proposals(proposals_path: Path, db_path: Path,
                    synonyms_path: Path, dry_run: bool) -> dict:
    """Apply proposals.json to nutrition_db.json and synonyms.json."""
    if not proposals_path.exists():
        print("  No proposals file found — skipping apply step.")
        return {"new_entries_applied": 0, "new_synonyms_applied": 0}

    proposals = json.loads(proposals_path.read_text(encoding="utf-8"))
    all_proposals = proposals.get("proposals", [])

    db = json.loads(db_path.read_text(encoding="utf-8"))
    synonyms = json.loads(synonyms_path.read_text(encoding="utf-8")) if synonyms_path.exists() else {}

    entries_added = 0
    synonyms_added = 0
    skipped_existing = 0

    for p in all_proposals:
        ptype = p.get("type")
        conf = p.get("confidence", 0)

        if ptype == "new_entry":
            key = p["key"]
            if key in db:
                skipped_existing += 1
                continue
            nutrition = p["nutrition"]
            # Validate: must have kcal > 0 to be useful
            if nutrition.get("kcal", 0) <= 0:
                continue
            if not dry_run:
                db[key] = nutrition
            entries_added += 1

        elif ptype == "synonym":
            alias = p["alias"]
            canonical = p["canonical"]
            if alias in synonyms:
                skipped_existing += 1
                continue
            if canonical not in db:
                continue  # canonical must exist in DB
            if not dry_run:
                synonyms[alias] = canonical
            synonyms_added += 1

    if not dry_run:
        # Sort DB alphabetically for clean diffs
        db_sorted = dict(sorted(db.items()))
        db_path.write_text(json.dumps(db_sorted, indent=2, ensure_ascii=False), encoding="utf-8")
        synonyms_path.write_text(json.dumps(synonyms, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"  Applied: {entries_added} new DB entries, {synonyms_added} new synonyms")
    else:
        print(f"  [DRY RUN] Would apply: {entries_added} new DB entries, {synonyms_added} new synonyms")

    return {
        "new_entries_applied": entries_added,
        "new_synonyms_applied": synonyms_added,
        "skipped_existing": skipped_existing,
    }


# ── Version manifest ──────────────────────────────────────────────────────────

def bump_version(db_path: Path, synonyms_path: Path, version_path: Path, dry_run: bool) -> dict:
    """Compute SHA-256 hashes and bump the version manifest."""
    # Load existing version
    if version_path.exists():
        version_data = json.loads(version_path.read_text(encoding="utf-8"))
        old_version = version_data.get("version", "1.0.0")
        parts = old_version.split(".")
        try:
            major, minor, patch = int(parts[0]), int(parts[1]), int(parts[2])
            patch += 1
            new_version = f"{major}.{minor}.{patch}"
        except Exception:
            new_version = "1.0.1"
    else:
        new_version = "1.0.0"

    db_content = db_path.read_text(encoding="utf-8")
    db = json.loads(db_content)
    db_sha256 = hashlib.sha256(db_content.encode()).hexdigest()

    synonyms_sha256 = ""
    if synonyms_path.exists():
        syn_content = synonyms_path.read_text(encoding="utf-8")
        synonyms_sha256 = hashlib.sha256(syn_content.encode()).hexdigest()

    manifest = {
        "version": new_version,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "entry_count": len(db),
        "db_sha256": db_sha256,
        "synonyms_sha256": synonyms_sha256,
        "min_app_version": "3.4",
    }

    if not dry_run:
        version_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"  Version bumped: {old_version if version_path.exists() else '(new)'} → {new_version}")
        print(f"  DB entries: {len(db):,}  |  SHA-256: {db_sha256[:16]}...")
    else:
        print(f"  [DRY RUN] Would bump version to {new_version}, {len(db):,} entries")

    return manifest


# ── Summary ───────────────────────────────────────────────────────────────────

def write_summary(data: dict, path: Path):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n  Summary → {path}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="CraveBox NutritionParser autotune orchestrator")
    parser.add_argument("--skip-crawl", action="store_true",
                        help="Skip crawler step (reuse existing crawled_ingredients.json)")
    parser.add_argument("--skip-enrich", action="store_true",
                        help="Skip enricher step (reuse existing proposals.json)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run full pipeline but don't write DB/synonym/version files")
    parser.add_argument("--max-per-site", type=int, default=200,
                        help="Max recipes per site (passed to crawler)")
    parser.add_argument("--max-gaps", type=int, default=1000,
                        help="Max gaps for enricher to process")
    parser.add_argument("--sites", default="",
                        help="Extra site URLs to crawl (comma-separated)")
    parser.add_argument("--urls", default="",
                        help="Specific recipe URLs to scrape (comma-separated)")
    parser.add_argument("--dataset", default="",
                        help="Path to local open recipe dataset file (RecipeNLG CSV etc.)")
    parser.add_argument("--max-dataset-rows", type=int, default=500000,
                        help="Max rows to read from dataset (default: 500000)")
    args = parser.parse_args()

    started_at = time.time()
    print("╔══════════════════════════════════════════════════╗")
    print("║   CraveBox NutritionParser Autotune Pipeline     ║")
    print("╚══════════════════════════════════════════════════╝")
    if args.dry_run:
        print("  *** DRY RUN MODE — no files will be written ***")

    summary: dict = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": args.dry_run,
    }

    # ── Step 1: Crawl ──────────────────────────────────────────────────────────
    if args.skip_crawl:
        print("\n[1/5] Crawler — SKIPPED (using existing crawled_ingredients.json)")
        if not CRAWLED_FILE.exists():
            sys.exit(f"ERROR: --skip-crawl set but {CRAWLED_FILE} not found")
    else:
        crawler_args = [str(CRAWLER_SCRIPT), f"--max-per-site={args.max_per_site}"]
        if args.sites:
            crawler_args += ["--sites", args.sites]
        if args.urls:
            crawler_args += ["--urls", args.urls]
        if args.dataset:
            crawler_args += ["--dataset", args.dataset,
                             "--max-dataset-rows", str(args.max_dataset_rows)]
        run_step("1/5  Crawler", crawler_args)

    crawled = json.loads(CRAWLED_FILE.read_text(encoding="utf-8"))
    summary["crawled_ingredients"] = len(crawled)

    # ── Step 2: Parser simulation ──────────────────────────────────────────────
    run_step("2/5  Parser simulation", [str(PARSER_SIM_SCRIPT)])

    report = json.loads(REPORT_FILE.read_text(encoding="utf-8"))
    summary["parse_report"] = report.get("summary", {})

    # ── Step 3: Enricher ───────────────────────────────────────────────────────
    if args.skip_enrich:
        print("\n[3/5] Enricher — SKIPPED (using existing proposals.json)")
        if not PROPOSALS_FILE.exists():
            sys.exit(f"ERROR: --skip-enrich set but {PROPOSALS_FILE} not found")
    else:
        run_step("3/5  Enricher", [str(ENRICHER_SCRIPT), f"--max-gaps={args.max_gaps}"])

    proposals = json.loads(PROPOSALS_FILE.read_text(encoding="utf-8"))
    summary["proposals"] = proposals.get("summary", {})

    # ── Step 4: Apply proposals ────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  STEP: 4/5  Apply proposals")
    print(f"{'='*60}")
    apply_result = apply_proposals(PROPOSALS_FILE, DB_FILE, SYNONYMS_FILE, dry_run=args.dry_run)
    summary["apply"] = apply_result

    # ── Step 5: Bump version ───────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  STEP: 5/5  Bump version manifest")
    print(f"{'='*60}")
    version_manifest = bump_version(DB_FILE, SYNONYMS_FILE, VERSION_FILE, dry_run=args.dry_run)
    summary["version"] = version_manifest

    # ── Final summary ──────────────────────────────────────────────────────────
    elapsed = round(time.time() - started_at, 1)
    summary["elapsed_seconds"] = elapsed
    summary["completed_at"] = datetime.now(timezone.utc).isoformat()

    write_summary(summary, SUMMARY_FILE)

    print(f"\n╔══════════════════════════════════════════════════╗")
    print(f"║   Autotune complete in {elapsed}s")
    print(f"║   DB entries:    {version_manifest.get('entry_count', '?'):,}")
    print(f"║   New entries:   {apply_result.get('new_entries_applied', 0):,}")
    print(f"║   New synonyms:  {apply_result.get('new_synonyms_applied', 0):,}")
    print(f"║   Version:       {version_manifest.get('version', '?')}")
    print(f"╚══════════════════════════════════════════════════╝")


if __name__ == "__main__":
    main()
