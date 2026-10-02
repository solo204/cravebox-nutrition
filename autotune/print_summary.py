#!/usr/bin/env python3
"""Prints a human-readable autotune run summary. Called by autotune.yml."""
import json, sys
from pathlib import Path

p = Path(__file__).parent / "autotune_summary.json"
if not p.exists():
    print("No summary file found.")
    sys.exit(0)

d  = json.loads(p.read_text())
pr = d.get("parse_report", {})
ap = d.get("apply", {})
v  = d.get("version", {})

print(f"Crawled:       {d.get('crawled_ingredients', '?'):,}" if isinstance(d.get('crawled_ingredients'), int) else f"Crawled:       {d.get('crawled_ingredients', '?')}")
print(f"Parse OK:      {pr.get('ok', '?')}  ({pr.get('ok_pct', '?')}%)")
print(f"UNKNOWN gaps:  {pr.get('unknown', '?')}")
print(f"New entries:   {ap.get('new_entries_applied', '?')}")
print(f"New synonyms:  {ap.get('new_synonyms_applied', '?')}")
print(f"DB version:    {v.get('version', '?')}  ({v.get('entry_count', '?')} entries)")
print(f"Elapsed:       {d.get('elapsed_seconds', '?')}s")
