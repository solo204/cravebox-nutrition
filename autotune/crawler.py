#!/usr/bin/env python3
"""
crawler.py
----------
Crawls recipe websites and/or reads open recipe datasets to extract
raw ingredient strings, saved to crawled_ingredients.json.

Usage:
  # Crawl configured sites (sites.json) with bot-bypass headers
  python autotune/crawler.py

  # Add extra sites on the fly
  python autotune/crawler.py --sites "https://example.com,https://other.com"

  # Scrape specific recipe URLs directly
  python autotune/crawler.py --urls "https://example.com/pasta,https://example.com/soup"

  # Read from a local RecipeNLG dataset (CSV with 'NER' column)
  python autotune/crawler.py --dataset /path/to/full_dataset.csv

  # Combine dataset + live crawl
  python autotune/crawler.py --dataset /path/to/full_dataset.csv --sites "..."

  # Limit recipes per site / dataset rows
  python autotune/crawler.py --max-per-site 50 --max-dataset-rows 100000

Output:
  autotune/crawled_ingredients.json  — deduplicated list of raw ingredient strings
"""

import argparse
import csv
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from tqdm import tqdm

# Try curl_cffi first (impersonates real Chrome, bypasses Cloudflare).
# Fall back to requests if not installed.
try:
    from curl_cffi import requests as cffi_requests
    _USE_CFFI = True
except ImportError:
    import requests as std_requests
    _USE_CFFI = False

# Scrapling StealthyFetcher — Playwright-based, handles JS-rendered sites.
# Only imported when --stealth is used.
try:
    from scrapling import StealthyFetcher as _StealthyFetcher
    _USE_SCRAPLING = True
except ImportError:
    _USE_SCRAPLING = False

_STEALTH_MODE = False  # set to True by --stealth flag

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
SITES_FILE = SCRIPT_DIR / "sites.json"
OUT_FILE = SCRIPT_DIR / "crawled_ingredients.json"

# ── Default config ─────────────────────────────────────────────────────────────
DEFAULT_MAX_PER_SITE = 200
DEFAULT_MAX_DATASET_ROWS = 500_000
DEFAULT_DELAY = 1.5
DEFAULT_TIMEOUT = 20
DEFAULT_WORKERS = 4

# Realistic Chrome UA
CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

CHROME_HEADERS = {
    "User-Agent": CHROME_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}


# ── HTTP session ───────────────────────────────────────────────────────────────

def make_session():
    """
    Returns a session that impersonates Chrome at the TLS level (curl_cffi)
    or falls back to a requests.Session with realistic headers.
    In --stealth mode returns None (StealthyFetcher is used per-request instead).
    """
    if _STEALTH_MODE:
        if not _USE_SCRAPLING:
            print("  [crawler] ERROR: --stealth requires scrapling. Run: pip install scrapling && playwright install chromium")
            sys.exit(1)
        print("  [crawler] Using Scrapling StealthyFetcher (Playwright, JS-rendered pages) ✓")
        return None  # stealth fetches are done per-request
    if _USE_CFFI:
        session = cffi_requests.Session(impersonate="chrome120")
        session.headers.update(CHROME_HEADERS)
        print("  [crawler] Using curl_cffi (Chrome TLS impersonation) ✓")
    else:
        session = std_requests.Session()
        session.headers.update(CHROME_HEADERS)
        print("  [crawler] WARNING: curl_cffi not installed — using requests (may be blocked by Cloudflare)")
        print("  [crawler] Install with: pip install curl_cffi")
    return session


def safe_get(session, url: str, timeout: int = DEFAULT_TIMEOUT):
    if _STEALTH_MODE:
        try:
            fetcher = _StealthyFetcher()
            page = fetcher.fetch(url, timeout=timeout * 1000)  # ms
            # Return a simple wrapper with .text and .content attributes
            class _Resp:
                def __init__(self, html): self.text = html; self.content = html.encode()
                def raise_for_status(self): pass
            return _Resp(page.html_content)
        except Exception as e:
            return None
    try:
        r = session.get(url, timeout=timeout, allow_redirects=True)
        r.raise_for_status()
        return r
    except Exception:
        return None


# ── Sitemap parsing ────────────────────────────────────────────────────────────

def get_recipe_urls_from_sitemap(session, sitemap_url: str,
                                  max_urls: int, timeout: int) -> list[str]:
    """Recursively walks sitemap XML. Returns up to max_urls recipe-looking URLs."""
    urls: list[str] = []
    queue = [sitemap_url]
    visited: set[str] = set()

    while queue and len(urls) < max_urls:
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)

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


def discover_sitemap(session, base_url: str, timeout: int) -> Optional[str]:
    """Try robots.txt first, then common sitemap paths."""
    parsed = urlparse(base_url)
    root = f"{parsed.scheme}://{parsed.netloc}"

    # Check robots.txt
    r = safe_get(session, f"{root}/robots.txt", timeout)
    if r:
        for line in r.text.splitlines():
            if line.lower().startswith("sitemap:"):
                return line.split(":", 1)[1].strip()

    # Try common paths
    for path in ["/sitemap.xml", "/sitemap_index.xml", "/sitemap/recipes.xml",
                 "/recipe-sitemap.xml", "/sitemaps/recipes.xml"]:
        candidate = root + path
        r = safe_get(session, candidate, timeout)
        if r and r.status_code == 200 and b"<urlset" in r.content[:500]:
            return candidate

    return None


def get_recipe_urls_by_crawling(session, base_url: str,
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
    path = urlparse(url).path.lower()
    recipe_signals = ["/recipe", "/recipes/", "/food/", "/dish/", "/cook/"]
    skip_signals = ["/sitemap", "/category/", "/tag/", "/author/", "/page/",
                    "/search", "/login", "/account", ".xml", ".json",
                    ".jpg", ".png", ".gif", ".webp"]
    if any(s in path for s in skip_signals):
        return False
    return any(s in path for s in recipe_signals) or (
        len(path.strip("/").split("/")) >= 2 and "-" in path
    )


# ── Schema.org extraction ──────────────────────────────────────────────────────

def extract_ingredients_from_url(session, url: str, timeout: int) -> list[str]:
    """Fetch a recipe page and extract Schema.org recipeIngredient values."""
    r = safe_get(session, url, timeout)
    if not r:
        return []

    soup = BeautifulSoup(r.text, "lxml")
    ingredients: list[str] = []

    # 1. JSON-LD
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for obj in _flatten_jsonld(data):
            if isinstance(obj, dict) and obj.get("@type") in ("Recipe", "recipe"):
                raw = obj.get("recipeIngredient", [])
                if isinstance(raw, list):
                    for item in raw:
                        s = str(item).strip()
                        if not s:
                            continue
                        # WP Recipe Maker and similar plugins sometimes concatenate
                        # multiple ingredients into one JSON-LD string. Split them.
                        if len(s) > 120:
                            parts = _split_concatenated_ingredients(s)
                            ingredients.extend(parts)
                        else:
                            ingredients.append(s)

    if ingredients:
        return ingredients

    # 2. CSS class fallback — WP Recipe Maker, Tasty Recipes, WPRM, etc.
    css_hits = soup.select(
        "li.wprm-recipe-ingredient, "
        "li.tasty-recipes-ingredient, "
        "li.recipe-ingredient, "
        "[class*='recipe-ingredient']:not(ul):not(ol), "
        ".ingredients li"
    )
    for el in css_hits:
        text = el.get_text(" ", strip=True)
        if text and 3 < len(text) < 300:
            ingredients.append(text)

    if ingredients:
        return ingredients

    # 3. Microdata fallback
    for span in soup.find_all(attrs={"itemprop": "recipeIngredient"}):
        text = span.get_text(strip=True)
        if text:
            ingredients.append(text)

    if ingredients:
        return ingredients

    # 4. Heading-proximity fallback: <ul>/<ol> after an "Ingredients" heading
    _ING_HEADING = re.compile(r'ingredient', re.I)
    for heading in soup.find_all(["h2", "h3", "h4"]):
        if _ING_HEADING.search(heading.get_text()):
            for sib in heading.next_siblings:
                if getattr(sib, "name", None) in ("ul", "ol"):
                    for li in sib.find_all("li"):
                        text = li.get_text(" ", strip=True)
                        if text and 3 < len(text) < 300:
                            ingredients.append(text)
                    break

    return ingredients


# Pattern that signals the START of a new ingredient line (digit/fraction + optional unit)
_INGREDIENT_START = re.compile(
    r'(?<=[^\d])(?=(?:\d[\d\s./]*|[¼½¾⅓⅔⅛⅜⅝⅞]))'
    r'|(?<=\))(?=[A-Z])'  # ends paren, next is capital
)

def _split_concatenated_ingredients(text: str) -> list[str]:
    """Split a blob of concatenated ingredient strings into individual lines.

    WP Recipe Maker (and similar) sometimes returns all ingredients in one
    JSON-LD string without newline separators. We detect boundaries where a
    new ingredient starts (a digit or Unicode fraction following a non-digit)
    and split there.
    """
    # First try: newlines already present
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if len(lines) > 1:
        return [l for l in lines if 3 < len(l) < 300]

    # Second try: split on pattern where a quantity starts after a non-digit
    # e.g. "...3 large onions¼ tsp salt..." → split before "¼"
    FRACTIONS = "¼½¾⅓⅔⅛⅜⅝⅞"
    parts = []
    current = []
    i = 0
    chars = list(text)
    while i < len(chars):
        ch = chars[i]
        # Detect start of new ingredient: fraction character, or digit preceded
        # by a letter/paren (no space boundary)
        if ch in FRACTIONS and current:
            parts.append("".join(current).strip())
            current = [ch]
        elif ch.isdigit() and i > 0 and chars[i - 1].isalpha():
            # e.g. "mushrooms10½ oz" → split before "10"
            parts.append("".join(current).strip())
            current = [ch]
        else:
            current.append(ch)
        i += 1
    if current:
        parts.append("".join(current).strip())

    result = [p for p in parts if 3 < len(p) < 300]
    return result if len(result) > 1 else [text]


def _flatten_jsonld(obj) -> list:
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


# ── Site crawl ─────────────────────────────────────────────────────────────────

def crawl_site(site: dict, config: dict, session) -> list[str]:
    name = site.get("name", site.get("url", "?"))
    url = site.get("url", "")
    sitemap = site.get("sitemap", "")
    max_per = config.get("max_recipes_per_site", DEFAULT_MAX_PER_SITE)
    delay = config.get("request_delay_seconds", DEFAULT_DELAY)
    if _STEALTH_MODE:
        delay = max(delay, 4.0)  # StealthyFetcher is slower; give it breathing room
    timeout = config.get("request_timeout_seconds", DEFAULT_TIMEOUT)

    print(f"\n  [{name}] Finding recipe URLs ...")

    # Use explicit sitemap, or try to discover one, or fall back to crawling
    if sitemap:
        recipe_urls = get_recipe_urls_from_sitemap(session, sitemap, max_per, timeout)
    else:
        discovered = discover_sitemap(session, url, timeout)
        if discovered:
            print(f"  [{name}] Discovered sitemap: {discovered}")
            recipe_urls = get_recipe_urls_from_sitemap(session, discovered, max_per, timeout)
        else:
            print(f"  [{name}] No sitemap found — crawling homepage links ...")
            recipe_urls = get_recipe_urls_by_crawling(session, url, max_per, timeout)

    print(f"  [{name}] Found {len(recipe_urls)} recipe URLs — scraping ingredients ...")
    all_ingredients: list[str] = []

    for recipe_url in tqdm(recipe_urls, desc=f"  {name}", leave=False):
        ings = extract_ingredients_from_url(session, recipe_url, timeout)
        all_ingredients.extend(ings)
        time.sleep(delay)

    print(f"  [{name}] Collected {len(all_ingredients):,} ingredient strings")
    return all_ingredients


def crawl_urls_direct(urls: list[str], config: dict, session) -> list[str]:
    delay = config.get("request_delay_seconds", DEFAULT_DELAY)
    timeout = config.get("request_timeout_seconds", DEFAULT_TIMEOUT)
    all_ingredients: list[str] = []

    for url in tqdm(urls, desc="  Direct URLs"):
        ings = extract_ingredients_from_url(session, url, timeout)
        all_ingredients.extend(ings)
        time.sleep(delay)

    return all_ingredients


# ── Dataset helpers ────────────────────────────────────────────────────────────

def _parse_list_field(value: str) -> list:
    """
    Parse a list field that may be JSON (['a','b']) or Python repr (['a', 'b']).
    Food.com / Kaggle uses Python repr with single quotes.
    """
    if not value or value.strip() in ("", "[]"):
        return []
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        pass
    # Python repr fallback: replace single quotes → double quotes carefully
    try:
        import ast
        result = ast.literal_eval(value)
        if isinstance(result, list):
            return result
    except Exception:
        pass
    return []


# ── Dataset ingestion ──────────────────────────────────────────────────────────

def load_from_dataset(dataset_path: str, max_rows: int) -> list[str]:
    """
    Load ingredient strings from a local open recipe dataset.

    Supports:
      - RecipeNLG CSV/TSV  (columns: 'NER'/'ner' — canonical names; 'source' — filters to Gathered)
      - RecipeNLG Parquet  (HuggingFace export; requires pandas + pyarrow)
      - Generic JSON       (list of objects with 'ingredients' or 'recipeIngredient' key)
      - Plain text         (one ingredient per line)

    Prefers NER canonical names (e.g. "brown sugar") over raw strings ("1 c. brown sugar")
    for cleaner NutritionParser lookup keys. Filters source=0 (Gathered) rows automatically.

    RecipeNLG download: https://recipenlg.cs.put.poznan.pl/
    HuggingFace: huggingface-cli download --repo-type dataset recipe_nlg
    (~2.2M total recipes; 1.6M Gathered subset; official CSV ~2GB)
    """
    path = Path(dataset_path)
    if not path.exists():
        print(f"  [dataset] ERROR: file not found: {dataset_path}")
        return []

    suffix = path.suffix.lower()
    ingredients: list[str] = []

    print(f"  [dataset] Loading {path.name} (max {max_rows:,} rows) ...")

    if suffix in (".csv", ".tsv"):
        sep = "\t" if suffix == ".tsv" else ","
        with open(path, encoding="utf-8", errors="replace", newline="") as f:
            reader = csv.DictReader(f, delimiter=sep)
            fieldnames = [c for c in (reader.fieldnames or [])]
            fieldnames_lower = [c.lower() for c in fieldnames]

            # RecipeNLG official CSV uses 'NER' (uppercase); HuggingFace export uses 'ner' (lowercase)
            # Prefer NER canonical names (e.g. "brown sugar") over raw strings
            # ("1 c. firmly packed brown sugar") — cleaner lookup keys for NutritionParser.
            def _col(name: str) -> str | None:
                """Return the actual column name regardless of case, or None."""
                for orig, low in zip(fieldnames, fieldnames_lower):
                    if low == name.lower():
                        return orig
                return None

            ner_col = _col("ner")
            raw_col = _col("ingredients")
            src_col = _col("source")  # 0=Gathered (high-quality), 1=Recipes1M

            if not ner_col and not raw_col:
                print(f"  [dataset] WARNING: no 'ingredients' or 'NER' column found.")
                print(f"  [dataset] Available columns: {fieldnames}")
                return []

            if ner_col:
                print(f"  [dataset] Using NER canonical names column '{ner_col}' (cleaner keys)")
            else:
                print(f"  [dataset] Using raw ingredients column '{raw_col}'")

            gathered_only = src_col is not None
            skipped_source = 0

            for i, row in enumerate(tqdm(reader, desc="  Reading dataset", total=max_rows)):
                if i >= max_rows:
                    break

                # Filter to source=0 (Gathered) when column present — skips Recipes1M
                if src_col and row.get(src_col, "0") not in ("0", "Gathered", "gathered"):
                    skipped_source += 1
                    continue

                # Prefer NER canonical names (best DB keys); fall back to raw strings
                if ner_col:
                    ner_list = _parse_list_field(row.get(ner_col, "[]"))
                    ingredients.extend(str(s).strip().lower() for s in ner_list if s)
                elif raw_col:
                    raw_list = _parse_list_field(row.get(raw_col, "[]"))
                    ingredients.extend(str(s).strip() for s in raw_list if s)

            if gathered_only and skipped_source:
                print(f"  [dataset] Skipped {skipped_source:,} Recipes1M rows (source≠0)")

    elif suffix == ".parquet":
        try:
            import pandas as pd  # type: ignore
        except ImportError:
            print("  [dataset] ERROR: 'pandas' not installed. Run: pip install pandas pyarrow")
            return []
        df = pd.read_parquet(path)
        # Normalize column names to lowercase for matching
        col_map = {c.lower(): c for c in df.columns}
        ner_col  = col_map.get("ner")
        raw_col  = col_map.get("ingredients")
        src_col  = col_map.get("source")

        if src_col:
            df = df[df[src_col].isin([0, "Gathered", "gathered"])]
            print(f"  [dataset] Filtered to {len(df):,} Gathered rows")

        use_col = ner_col or raw_col
        if not use_col:
            print(f"  [dataset] WARNING: no 'ingredients' or 'ner' column. Available: {list(df.columns)}")
            return []

        print(f"  [dataset] Using column '{use_col}'")
        for val in tqdm(df[use_col].iloc[:max_rows], desc="  Reading dataset"):
            if isinstance(val, list):
                items = val
            else:
                items = _parse_list_field(str(val))
            ingredients.extend(str(s).strip().lower() for s in items if s)

    elif suffix == ".json":
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            for i, obj in enumerate(tqdm(data, desc="  Reading dataset")):
                if i >= max_rows:
                    break
                if isinstance(obj, dict):
                    for key in ("ingredients", "recipeIngredient", "ingredient_lines"):
                        val = obj.get(key, [])
                        if isinstance(val, list):
                            ingredients.extend(str(s).strip() for s in val if s)
                            break

    elif suffix == ".txt":
        with open(path, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= max_rows:
                    break
                line = line.strip()
                if line:
                    ingredients.append(line)

    else:
        print(f"  [dataset] Unsupported format: {suffix}. Use .csv, .tsv, .parquet, .json, or .txt")
        return []

    print(f"  [dataset] Loaded {len(ingredients):,} raw ingredient strings from dataset")
    return ingredients


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="CraveBox recipe ingredient crawler")
    parser.add_argument("--sites", help="Comma-separated site URLs to crawl (adds to sites.json)")
    parser.add_argument("--urls", help="Comma-separated specific recipe page URLs to scrape directly")
    parser.add_argument("--dataset", help="Path to a local open recipe dataset (RecipeNLG CSV/TSV/Parquet, JSON, or TXT)")
    parser.add_argument("--no-sites", action="store_true",
                        help="Skip loading sites.json and do not crawl any websites (dataset-only mode)")
    parser.add_argument("--stealth", action="store_true",
                        help="Use Scrapling StealthyFetcher (Playwright) for JS-rendered / Cloudflare-protected sites. Requires: pip install scrapling && playwright install chromium")
    parser.add_argument("--max-per-site", type=int, help="Override max recipes per site")
    parser.add_argument("--max-dataset-rows", type=int, default=DEFAULT_MAX_DATASET_ROWS,
                        help=f"Max rows to read from dataset (default: {DEFAULT_MAX_DATASET_ROWS:,})")
    parser.add_argument("--out", default=str(OUT_FILE), help="Output JSON path")
    args = parser.parse_args()

    # Enable stealth mode if requested
    if args.stealth:
        global _STEALTH_MODE
        if not _USE_SCRAPLING:
            print("  ERROR: --stealth requires scrapling. Run: pip install scrapling && playwright install chromium")
            return
        _STEALTH_MODE = True
        print("  [stealth] StealthyFetcher (Playwright) enabled — JS-rendered sites supported.")

    # Load sites.json (skipped when --no-sites is set)
    config: dict = {}
    sites: list[dict] = []
    if args.no_sites:
        print("  --no-sites: skipping sites.json and live crawl (dataset-only mode)")
    elif SITES_FILE.exists():
        data = json.loads(SITES_FILE.read_text(encoding="utf-8"))
        sites = [s for s in data.get("sites", []) if s.get("enabled", True)]
        config = data.get("crawler", {})
    else:
        print(f"  WARNING: {SITES_FILE} not found — using CLI args only")

    # Inject extra sites from --sites (--no-sites takes precedence)
    if args.sites and not args.no_sites:
        for raw_url in args.sites.split(","):
            raw_url = raw_url.strip()
            if raw_url:
                sites.append({"name": raw_url, "url": raw_url, "sitemap": "", "enabled": True})

    if args.max_per_site:
        config["max_recipes_per_site"] = args.max_per_site

    all_ingredients: list[str] = []

    # ── Dataset ingestion (no HTTP needed) ──
    if args.dataset:
        print(f"\n=== CraveBox Dataset Ingestion ===")
        dataset_ings = load_from_dataset(args.dataset, args.max_dataset_rows)
        all_ingredients.extend(dataset_ings)

    # ── Live site crawling ──
    if sites or args.urls:
        session = make_session()

        print(f"\n=== CraveBox Ingredient Crawler ===")
        print(f"Sites: {len(sites)}  |  Max per site: {config.get('max_recipes_per_site', DEFAULT_MAX_PER_SITE)}")

        for site in sites:
            try:
                ings = crawl_site(site, config, session)
                all_ingredients.extend(ings)
            except KeyboardInterrupt:
                print("\nInterrupted — saving partial results ...")
                break
            except Exception as e:
                print(f"  ERROR crawling {site.get('name', '?')}: {e}")

        if args.urls:
            direct_urls = [u.strip() for u in args.urls.split(",") if u.strip()]
            print(f"\nScraping {len(direct_urls)} direct URL(s) ...")
            ings = crawl_urls_direct(direct_urls, config, session)
            all_ingredients.extend(ings)

    if not all_ingredients:
        print("\nNo ingredients collected. Use --dataset, --sites, or --urls.")
        sys.exit(0)

    # Deduplicate (preserve order, case-sensitive — parser_sim lowercases)
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
