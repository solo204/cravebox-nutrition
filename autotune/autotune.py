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
import os
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
REPO_ROOT  = SCRIPT_DIR.parent        # cravebox-nutrition/
DATA_DIR_ROOT = REPO_ROOT / "data"   # cravebox-nutrition/data/

DB_FILE = DATA_DIR_ROOT / "nutrition_db.json"
SYNONYMS_FILE = DATA_DIR_ROOT / "synonyms.json"
MODIFIERS_FILE = DATA_DIR_ROOT / "modifiers.json"
VERSION_FILE = DATA_DIR_ROOT / "nutrition_version.json"

SYNONYM_PROPOSALS_FILE = DATA_DIR_ROOT / "synonym_proposals.json"

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

    # Load protected keys from enricher so autotune honours the same set
    try:
        import importlib.util, sys as _sys
        _spec = importlib.util.spec_from_file_location("enricher", ENRICHER_SCRIPT)
        _enricher = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_enricher)
        _PROTECTED = _enricher._PROTECTED_KEYS
    except Exception:
        _PROTECTED = set()

    for p in all_proposals:
        ptype = p.get("type")
        conf = p.get("confidence", 0)

        if ptype == "new_entry":
            key = p["key"]
            if key in db:
                skipped_existing += 1
                continue
            if key in _PROTECTED:
                print(f"  SKIP protected key: {key}")
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
            # Prevent synonym chains: canonical must be a direct DB key, not itself a synonym
            if canonical in synonyms:
                print(f"  SKIP chain synonym: {alias} → {canonical} (target is also a synonym)")
                skipped_existing += 1
                continue
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


# ── Real-user ingredients from backend ───────────────────────────────────────

def fetch_render_ingredients(crawled_path: Path, dry_run: bool) -> dict:
    """
    Fetch raw ingredient strings logged by the backend during real user extractions,
    inject them into crawled_ingredients.json so the parser sim + enricher handle them.
    Clears the backend file after successful processing.
    """
    backend_url = os.environ.get("BACKEND_URL", "https://cravebox-backend.onrender.com")
    ingredients_url = f"{backend_url}/ingredients"

    print(f"  Fetching real-user ingredients from {ingredients_url}...")
    try:
        with urlopen(Request(ingredients_url), timeout=15) as resp:
            raw = resp.read().decode("utf-8").strip()
    except URLError as e:
        print(f"  WARNING: Could not reach backend — skipping real-user ingredients: {e}")
        return {"fetched": 0, "injected": 0}

    if not raw:
        print("  No ingredients on backend — nothing to inject.")
        return {"fetched": 0, "injected": 0}

    # Parse JSONL
    fetched = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line).get("ingredient", "").strip().lower()
            if item:
                fetched.append(item)
        except Exception:
            continue

    print(f"  Fetched {len(fetched)} ingredient strings from backend.")

    # Load existing crawled list and deduplicate
    existing = set()
    crawled = []
    if crawled_path.exists():
        crawled = json.loads(crawled_path.read_text(encoding="utf-8"))
        existing = set(i.strip().lower() for i in crawled if isinstance(i, str))

    new_items = [i for i in fetched if i not in existing]
    print(f"  Injecting {len(new_items)} new ingredients into crawled_ingredients.json "
          f"({len(fetched) - len(new_items)} already present).")

    if not dry_run and new_items:
        crawled.extend(new_items)
        crawled_path.write_text(json.dumps(crawled, indent=2, ensure_ascii=False), encoding="utf-8")

    # Clear backend file after processing
    if not dry_run:
        try:
            req = Request(ingredients_url, method="DELETE")
            with urlopen(req, timeout=10):
                pass
            print("  Backend ingredients cleared.")
        except Exception as e:
            print(f"  WARNING: Could not clear backend ingredients: {e}")

    return {"fetched": len(fetched), "injected": len(new_items)}


# ── Synonym candidates from backend ──────────────────────────────────────────

def apply_synonym_candidates(db_path: Path, synonyms_path: Path,
                              dry_run: bool, min_seen: int = 3) -> dict:
    """
    Fetch ingredient_mappings candidates collected by the backend, aggregate
    by seen count, validate against the DB, and merge into synonyms.json.
    Clears the backend file after successful processing.
    """
    backend_url = os.environ.get("BACKEND_URL", "https://cravebox-backend.onrender.com")
    candidates_url = f"{backend_url}/synonym-candidates"

    print(f"  Fetching candidates from {candidates_url}...")
    try:
        with urlopen(Request(candidates_url), timeout=15) as resp:
            raw = resp.read().decode("utf-8").strip()
    except URLError as e:
        print(f"  WARNING: Could not reach backend — skipping synonym candidates: {e}")
        return {"candidates_fetched": 0, "synonyms_added": 0, "skipped": 0}

    if not raw:
        print("  No candidates on backend — nothing to process.")
        return {"candidates_fetched": 0, "synonyms_added": 0, "skipped": 0}

    # Parse JSONL
    pairs = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            m = json.loads(line)
            original = m.get("original", "").strip().lower()
            canonical = m.get("canonical", "").strip().lower()
            if original and canonical:
                pairs.append((original, canonical))
        except Exception:
            continue

    print(f"  Parsed {len(pairs)} candidate lines.")

    # Aggregate: count (original, canonical) pairs
    counts = Counter(pairs)

    db = json.loads(db_path.read_text(encoding="utf-8"))
    synonyms = json.loads(synonyms_path.read_text(encoding="utf-8")) if synonyms_path.exists() else {}

    proposals = []
    skipped = 0

    for (original, canonical), seen in counts.most_common():
        if seen < min_seen:
            break  # most_common is sorted descending — everything below is too rare
        if original == canonical:
            print(f"    SKIP '{original}' → '{canonical}' (self-mapping)")
            skipped += 1
            continue
        if original in synonyms:
            print(f"    SKIP '{original}' → '{canonical}' (original already mapped to '{synonyms[original]}')")
            skipped += 1
            continue
        if canonical in synonyms:
            print(f"    SKIP '{original}' → '{canonical}' (canonical is itself a synonym — use its target instead)")
            skipped += 1
            continue
        if canonical not in db:
            print(f"    SKIP '{original}' → '{canonical}' (canonical not in DB)")
            skipped += 1
            continue
        proposals.append({"original": original, "canonical": canonical, "seen": seen})
        print(f"    PROPOSE: '{original}' → '{canonical}' (seen {seen}x)")

    # Write proposals file for human review — never auto-merges into synonyms.json
    if not dry_run:
        SYNONYM_PROPOSALS_FILE.write_text(
            json.dumps({"proposals": proposals, "generated_at": datetime.now(timezone.utc).isoformat()},
                       indent=2, ensure_ascii=False),
            encoding="utf-8"
        )
        print(f"  Wrote {len(proposals)} proposals to {SYNONYM_PROPOSALS_FILE.name} — review before applying.")

    # Clear backend candidates file after processing
    if not dry_run:
        try:
            req = Request(candidates_url, method="DELETE")
            with urlopen(req, timeout=10):
                pass
            print("  Backend candidates cleared.")
        except Exception as e:
            print(f"  WARNING: Could not clear backend candidates: {e}")

    return {
        "candidates_fetched": len(pairs),
        "proposals_written": len(proposals),
        "skipped": skipped,
    }


# ── Apply approved synonym proposals ─────────────────────────────────────────

def apply_approved_synonym_proposals(db_path: Path, synonyms_path: Path,
                                      proposals_path: Path, dry_run: bool) -> dict:
    """
    Merge synonym_proposals.json into synonyms.json.
    Called only when --apply-synonym-proposals flag is passed (after human review).
    """
    if not proposals_path.exists():
        print("  No synonym_proposals.json found — nothing to apply.")
        return {"synonyms_applied": 0}

    data = json.loads(proposals_path.read_text(encoding="utf-8"))
    proposals = data.get("proposals", [])

    db = json.loads(db_path.read_text(encoding="utf-8"))
    synonyms = json.loads(synonyms_path.read_text(encoding="utf-8")) if synonyms_path.exists() else {}

    applied = 0
    for p in proposals:
        original = p.get("original", "").strip().lower()
        canonical = p.get("canonical", "").strip().lower()
        if not original or not canonical:
            continue
        if original in synonyms or canonical not in db:
            continue
        if not dry_run:
            synonyms[original] = canonical
        print(f"    {'[DRY] ' if dry_run else ''}APPLY: '{original}' → '{canonical}'")
        applied += 1

    if applied > 0 and not dry_run:
        synonyms_path.write_text(json.dumps(synonyms, indent=2, ensure_ascii=False), encoding="utf-8")
        # Clear proposals after applying
        proposals_path.write_text(json.dumps({"proposals": [], "applied_at": datetime.now(timezone.utc).isoformat()},
                                              indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"  Applied {applied} synonyms. synonym_proposals.json cleared.")

    return {"synonyms_applied": applied}


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

    db_bytes = db_path.read_bytes()
    db = json.loads(db_bytes)
    # Hash raw bytes so the stored SHA matches what any tool reading the file will see,
    # regardless of platform line-ending normalisation (avoids \r\n vs \n mismatch on Windows).
    db_sha256 = hashlib.sha256(db_bytes).hexdigest()

    synonyms_sha256 = ""
    if synonyms_path.exists():
        syn_bytes = synonyms_path.read_bytes()
        synonyms_sha256 = hashlib.sha256(syn_bytes).hexdigest()

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
    parser.add_argument("--no-sites", action="store_true",
                        help="Pass --no-sites to crawler: skip sites.json, read dataset only")
    parser.add_argument("--apply-synonym-proposals", action="store_true",
                        help="Merge synonym_proposals.json into synonyms.json (after human review)")
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

    # ── Step 0: Inject real-user ingredients from backend ─────────────────────
    print(f"\n{'='*60}")
    print(f"  STEP: 0/5  Real-user ingredients (backend → crawled_ingredients.json)")
    print(f"{'='*60}")
    render_result = fetch_render_ingredients(CRAWLED_FILE, dry_run=args.dry_run)
    summary["render_ingredients"] = render_result

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
        if args.no_sites:
            crawler_args += ["--no-sites"]
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

    # ── Step 4a: Apply approved synonym proposals (if flag set) ───────────────
    if args.apply_synonym_proposals:
        print(f"\n{'='*60}")
        print(f"  STEP: 4a/5  Apply approved synonym proposals")
        print(f"{'='*60}")
        approved_result = apply_approved_synonym_proposals(
            DB_FILE, SYNONYMS_FILE, SYNONYM_PROPOSALS_FILE, dry_run=args.dry_run)
        summary["synonym_proposals_applied"] = approved_result
    else:
        summary["synonym_proposals_applied"] = {"synonyms_applied": 0}

    # ── Step 4b: Synonym candidates from backend ───────────────────────────────
    print(f"\n{'='*60}")
    print(f"  STEP: 4b/5  Synonym candidates (backend → synonyms.json)")
    print(f"{'='*60}")
    candidates_result = apply_synonym_candidates(DB_FILE, SYNONYMS_FILE, dry_run=args.dry_run)
    summary["synonym_candidates"] = candidates_result

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
    approved = summary.get("synonym_proposals_applied", {}).get("synonyms_applied", 0)
    total_synonyms = apply_result.get('new_synonyms_applied', 0) + approved
    proposals_pending = candidates_result.get('proposals_written', 0)
    print(f"║   New synonyms:  {total_synonyms:,}  ({approved} user-approved)")
    if proposals_pending:
        print(f"║   Pending review:{proposals_pending:,} synonym proposals in data/synonym_proposals.json")
    print(f"║   Version:       {version_manifest.get('version', '?')}")
    print(f"╚══════════════════════════════════════════════════╝")


if __name__ == "__main__":
    main()
