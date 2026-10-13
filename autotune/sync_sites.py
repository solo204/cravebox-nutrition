"""Sync autotune/sites.json from cravebox-world-sites master_sites.json."""
import json
import urllib.request
from pathlib import Path

MASTER_URL = (
    "https://raw.githubusercontent.com/solo204/cravebox-world-sites"
    "/main/data/master_sites.json"
)
SITES_FILE = Path(__file__).parent / "sites.json"


def main():
    # Preserve existing crawler config block
    with open(SITES_FILE, encoding="utf-8") as f:
        existing = json.load(f)
    crawler_config = existing.get("crawler", {})

    # Fetch master list
    with urllib.request.urlopen(MASTER_URL, timeout=15) as r:
        master = json.loads(r.read().decode())

    # Only active sites; map to autotune format
    sites = [
        {
            "name": s["name"],
            "url": s["url"],
            "sitemap": "",   # crawler auto-discovers sitemaps
            "enabled": True,
        }
        for s in master
        if s.get("active", True)
    ]

    out = {
        "_comment": "Auto-synced from cravebox-world-sites/data/master_sites.json",
        "sites": sites,
        "crawler": crawler_config,
    }
    with open(SITES_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"Synced {len(sites)} active sites → {SITES_FILE}")


if __name__ == "__main__":
    main()
