#!/usr/bin/env python3
"""
crawler.py
----------
Crawls recipe websites listed in sites.json (or supplied via --sites / --urls),
extracts Schema.org recipeIngredient arrays, and saves unique raw ingredient
strings to crawled_ingredients.json.

Usage:
  # Use sites.json (default)
  python autotune/crawler.py

  # Add extra sites on the fly (combined with sites.json)
  python autotune/crawler.py --sites "https://example.com,https://other.com"

  # Scrape specific recipe URLs directly
  python autotune/crawler.py --urls "https://example.com/pasta,https://example.com/soup"

  # Limit recipes per site (overrides sites.json setting)
  python autotune/crawler.py --max-per-site 50

Output:
  autotune/crawled_ingredients.json  — deduplicated list of raw ingredient strings
"""

import argparse
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
SITES_FILE = SCRIPT_DIR / "sites.json"
OUT_FILE = SCRIPT_DIR / "crawled_ingredients.json"

# ── Default config (overridden by sites.json) ─────────────────────────────────
DEFAULT_MAX_PER_SITE = 200
DEFAULT_DELAY = 1.5
DEFAULT_TIMEOUT = 15
DEFAULT_UA = "CraveBox-NutritionAutotune/1.0 (github.com/solo204/recipe-extractor-android)"
DEFAULT_WORKERS = 4


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def make_session(user_agent: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return s


def safe_get(session: requests.Session, url: str, timeout: int = DEFAULT_TIMEOUT) -> Optional[requests.Response]:
    try:
        r = session.get(url, timeout=timeout, allow_redirects=True)
        r.raise_for_status()
        return r
    except Exception:
        return None


# ── Sitemap parsing ───────────────────────────────────────────────────────────

def get_recipe_urls_from_sitemap(session: requests.Session, sitemap_url: str,
                                  max_urls: int, timeout: int) -> list[str]:
    """Recursively walks sitemap XML. Returns up to max_urls recipe-looking URLs."""
    urls: list[str] = []
    queue = [sitemap_url]
    visited_sitemaps: set[str] = set()

    while queue and len(urls) < max_urls:
        url = queue.pop(0)
        if url in visited_sitemaps:
            continue
        visited_sitemaps.add(url)

        r = safe_get(session, url, timeout)
        if not r:
            continue

        try:
            root = ET.fromstring(r.content)
        except ET.ParseError:
            continue

        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        tag = root.tag.split("}")[-1] if "}" in root.tag else root.tag

        if tag == "sitemapindex":
            # Index of sitemaps — recurse into each
            for loc in root.findall(".//sm:loc", ns):
                if loc.text:
                    queue.append(loc.text.strip())
        elif tag == "urlset":
            for loc in root.findall(".//sm:loc", ns):
                if loc.text and _looks_like_recipe_url(loc.text.strip()):
                    urls.append(loc.text.strip())
                    if len(urls) >= max_urls:
                        break

    return urls[:max_urls]


def get_recipe_urls_by_crawling(session: requests.Session, base_url: str,
                                 max_urls: int, timeout: int) -> list[str]:
    """Fallback: crawl homepage links looking for recipe-like URLs."""
    r = safe_get(session, base_url, timeout)
    if not r:
        return []

    soup = BeautifulSoup(r.text, "lxml")
    domain = urlparse(base_url).netloc
    found: list[str] = []

    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"])
        if urlparse(href).netloc == domain and _looks_like_recipe_url(href):
            found.append(href)
            if len(found) >= max_urls:
                break

    return found


def _looks_like_recipe_url(url: str) -> bool:
    """Heuristic: URL path suggests a recipe page."""
    path = urlparse(url).path.lower()
    recipe_signals = ["/recipe", "/recipes/", "/food/", "/dish/", "/cook/"]
    skip_signals = ["/sitemap", "/category/", "/tag/", "/author/", "/page/",
                    "/search", "/login", "/account", ".xml", ".json", ".jpg", ".png"]
    if any(s in path for s in skip_signals):
        return False
    return any(s in path for s in recipe_signals) or (
        len(path.strip("/").split("/")) >= 2 and "-" in path
    )


# ── Schema.org ingredient extraction ─────────────────────────────────────────

def extract_ingredients_from_url(session: requests.Session, url: str,
                                   timeout: int) -> list[str]:
    """Fetch a recipe page and extract Schema.org recipeIngredient values."""
    r = safe_get(session, url, timeout)
    if not r:
        return []

    soup = BeautifulSoup(r.text, "lxml")
    ingredients: list[str] = []

    # 1. JSON-LD blocks (most reliable)
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue

        for obj in _flatten_jsonld(data):
            if isinstance(obj, dict) and obj.get("@type") in ("Recipe", "recipe"):
                raw = obj.get("recipeIngredient", [])
                if isinstance(raw, list):
                    ingredients.extend(str(i).strip() for i in raw if i)

    if ingredients:
        return ingredients

    # 2. Microdata fallback
    for span in soup.find_all(attrs={"itemprop": "recipeIngredient"}):
        text = span.get_text(strip=True)
        if text:
            ingredients.append(text)

    return ingredients


def _flatten_jsonld(obj) -> list:
    """Flatten @graph arrays and nested objects."""
    if isinstance(obj, list):
        result = []
        for item in obj:
            result.extend(_flatten_jsonld(item))
        return result
    if isinstance(obj, dict):
        if "@graph" in obj:
            return _flatten_jsonld(obj["@graph"])
        return [obj]
    return []


# ── Site crawl orchestration ──────────────────────────────────────────────────

def crawl_site(site: dict, config: dict, session: requests.Session) -> list[str]:
    """Crawl one site entry and return ingredient strings."""
    name = site.get("name", site.get("url", "?"))
    url = site.get("url", "")
    sitemap = site.get("sitemap", "")
    max_per = config.get("max_recipes_per_site", DEFAULT_MAX_PER_SITE)
    delay = config.get("request_delay_seconds", DEFAULT_DELAY)
    timeout = config.get("request_timeout_seconds", DEFAULT_TIMEOUT)

    print(f"\n  [{name}] Finding recipe URLs ...")
    if sitemap:
        recipe_urls = get_recipe_urls_from_sitemap(session, sitemap, max_per, timeout)
    else:
        recipe_urls = get_recipe_urls_by_crawling(session, url, max_per, timeout)

    print(f"  [{name}] Found {len(recipe_urls)} recipe URLs — scraping ingredients ...")
    all_ingredients: list[str] = []

    for recipe_url in tqdm(recipe_urls, desc=f"  {name}", leave=False):
        ings = extract_ingredients_from_url(session, recipe_url, timeout)
        all_ingredients.extend(ings)
        time.sleep(delay)

    print(f"  [{name}] Collected {len(all_ingredients):,} ingredient strings")
    return all_ingredients


def crawl_urls_direct(urls: list[str], config: dict, session: requests.Session) -> list[str]:
    """Scrape a list of explicit recipe URLs directly."""
    delay = config.get("request_delay_seconds", DEFAULT_DELAY)
    timeout = config.get("request_timeout_seconds", DEFAULT_TIMEOUT)
    all_ingredients: list[str] = []

    for url in tqdm(urls, desc="  Direct URLs"):
        ings = extract_ingredients_from_url(session, url, timeout)
        all_ingredients.extend(ings)
        time.sleep(delay)

    return all_ingredients


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="CraveBox recipe ingredient crawler")
    parser.add_argument("--sites", help="Comma-separated site URLs to crawl (adds to sites.json)")
    parser.add_argument("--urls", help="Comma-separated specific recipe page URLs to scrape directly")
    parser.add_argument("--max-per-site", type=int, help="Override max recipes per site")
    parser.add_argument("--out", default=str(OUT_FILE), help="Output JSON path")
    args = parser.parse_args()

    # Load sites.json
    config: dict = {}
    sites: list[dict] = []
    if SITES_FILE.exists():
        data = json.loads(SITES_FILE.read_text(encoding="utf-8"))
        sites = [s for s in data.get("sites", []) if s.get("enabled", True)]
        config = data.get("crawler", {})
    else:
        print(f"  WARNING: {SITES_FILE} not found — using CLI args only")

    # Inject extra sites from --sites
    if args.sites:
        for raw_url in args.sites.split(","):
            raw_url = raw_url.strip()
            if raw_url:
                sites.append({"name": raw_url, "url": raw_url, "sitemap": "", "enabled": True})

    # Override max per site
    if args.max_per_site:
        config["max_recipes_per_site"] = args.max_per_site

    ua = config.get("user_agent", DEFAULT_UA)
    session = make_session(ua)

    print(f"=== CraveBox Ingredient Crawler ===")
    print(f"Sites: {len(sites)}  |  Max per site: {config.get('max_recipes_per_site', DEFAULT_MAX_PER_SITE)}")

    all_ingredients: list[str] = []

    # Crawl configured sites
    for site in sites:
        try:
            ings = crawl_site(site, config, session)
            all_ingredients.extend(ings)
        except KeyboardInterrupt:
            print("\nInterrupted — saving partial results ...")
            break
        except Exception as e:
            print(f"  ERROR crawling {site.get('name', '?')}: {e}")

    # Scrape explicit URLs
    if args.urls:
        direct_urls = [u.strip() for u in args.urls.split(",") if u.strip()]
        print(f"\nScraping {len(direct_urls)} direct URL(s) ...")
        ings = crawl_urls_direct(direct_urls, config, session)
        all_ingredients.extend(ings)

    # Deduplicate (preserve order, case-sensitive for now — parser_sim lowercases)
    seen: set[str] = set()
    unique: list[str] = []
    for ing in all_ingredients:
        key = ing.strip()
        if key and key not in seen:
            seen.add(key)
            unique.append(key)

    out_path = Path(args.out)
    out_path.write_text(json.dumps(unique, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n=== Done: {len(unique):,} unique ingredient strings → {out_path} ===")


if __name__ == "__main__":
    main()
