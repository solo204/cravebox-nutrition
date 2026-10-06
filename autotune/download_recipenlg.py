#!/usr/bin/env python3
"""
Download RecipeNLG from HuggingFace and save as a Parquet file
ready for the autotune --dataset flag.

Usage:
    pip install huggingface_hub datasets pyarrow pandas
    python autotune/download_recipenlg.py

Output:
    autotune/data/recipenlg_gathered.parquet   (~900 MB; 1.6M Gathered recipes)

Then run autotune with:
    python autotune/autotune.py \\
        --dataset autotune/data/recipenlg_gathered.parquet \\
        --max-dataset-rows 1600000 \\
        --no-sites
"""

import pathlib
import sys

OUTDIR  = pathlib.Path(__file__).parent / "data"
OUTFILE = OUTDIR / "recipenlg_gathered.parquet"

HF_REPO = "recipe_nlg"
# Mirrors with pre-converted parquet (no loading script required).
# Ordered by preference: most recent full-dataset mirrors first.
HF_MIRRORS = [
    "Mahimas/recipenlg",                  # 2.23M rows, Sep 2025
    "SandhyaKilari/RecipeNLG_dataset",    # 2.23M rows, Apr 2025
    "mbien/recipe_nlg",                   # parquet mirror, Jan 2024
    "Zappandy/recipe_nlg",                # 500k subset
]

def _try_load_mirror(mirrors: list[str]):
    """Try mirrors that don't need a deprecated loading script."""
    from datasets import load_dataset  # type: ignore
    for repo in mirrors:
        try:
            print(f"  Trying mirror: {repo} ...")
            # Some mirrors are CSV-backed; trust_remote_code not needed for standard formats
            ds = load_dataset(repo, split="train", streaming=False, trust_remote_code=False)
            print(f"  ✓ Loaded from {repo} ({len(ds):,} rows)")
            return ds
        except Exception as e:
            print(f"  ✗ {repo}: {e}")
    return None


def main():
    try:
        from datasets import load_dataset  # type: ignore
    except ImportError:
        sys.exit("ERROR: Install 'datasets': pip install datasets pyarrow pandas")

    print("Downloading RecipeNLG from HuggingFace...")
    print("This may take several minutes — the dataset is ~2 GB.")

    ds = None

    # Try the official repo first (works if HF has converted it to parquet)
    try:
        ds = load_dataset(HF_REPO, split="train", streaming=False)
        print(f"✓ Loaded from official {HF_REPO}")
    except RuntimeError as e:
        if "Dataset scripts are no longer supported" in str(e):
            print(f"Official repo uses a deprecated loading script — trying mirrors...")
            ds = _try_load_mirror(HF_MIRRORS)
        else:
            raise

    if ds is None:
        print()
        print("All HuggingFace sources failed.")
        print("Download manually from: https://recipenlg.cs.put.poznan.pl/")
        print("Then place the CSV at:  autotune/data/full_dataset.csv")
        print("And run:")
        print("  python autotune/autotune.py \\")
        print("      --dataset autotune/data/full_dataset.csv \\")
        print("      --max-dataset-rows 1600000 --skip-crawl")
        sys.exit(1)

    print(f"Total recipes: {len(ds):,}")

    # Column name may vary by mirror — normalise to lowercase
    cols = {c.lower(): c for c in ds.column_names}
    src_col = cols.get("source")

    # Filter to Gathered — the high-quality 1.6M subset.
    # source values vary by mirror: int 0, string "0", or string "Gathered".
    if src_col:
        gathered = ds.filter(
            lambda row: row[src_col] in (0, "0", "Gathered", "gathered")
        )
        print(f"Gathered recipes: {len(gathered):,}")
        if len(gathered) == 0:
            # Show what values are actually present
            sample_vals = list({row[src_col] for row in ds.select(range(min(100, len(ds))))})
            print(f"  WARNING: filter returned 0 rows. Sample source values: {sample_vals}")
            print(f"  Using all {len(ds):,} rows instead.")
            gathered = ds
    else:
        print("WARNING: no 'source' column — using all recipes")
        gathered = ds

    OUTDIR.mkdir(exist_ok=True)
    print(f"Saving to {OUTFILE} ...")
    gathered.to_parquet(str(OUTFILE))
    print(f"Done! File size: {OUTFILE.stat().st_size / 1e6:.0f} MB")
    print()
    print("Run autotune now with:")
    print(f"  python autotune/autotune.py \\")
    print(f"      --dataset {OUTFILE} \\")
    print(f"      --max-dataset-rows 1600000 \\")
    print(f"      --no-sites")


if __name__ == "__main__":
    main()
