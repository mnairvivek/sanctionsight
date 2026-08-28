"""
Sanctions List Downloader
--------------------------
Downloads the latest sanctions lists from OFAC, EU, UN, OFSI, BIS/State (via CSL),
and OpenSanctions.
Run this script periodically (weekly/monthly) to keep lists current.

Usage:
    python update_lists.py
"""

import json
import os
import sys
import requests
from datetime import datetime

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

LISTS = {
    # OFAC (US Treasury)
    "ofac_sdn.csv": "https://www.treasury.gov/ofac/downloads/sdn.csv",
    "ofac_sdn_alt.csv": "https://www.treasury.gov/ofac/downloads/alt.csv",
    "ofac_consolidated.csv": "https://www.treasury.gov/ofac/downloads/consolidated/cons_prim.csv",

    # UN Security Council
    "un_consolidated.xml": "https://scsanctions.un.org/resources/xml/en/consolidated.xml",

    # OFSI (UK)
    "uk_sanctions.csv": "https://assets.publishing.service.gov.uk/media/65ca03e11bb4370013bb8aa8/UK_Sanctions_List.csv",
}

# EU list requires a different approach — the token URL changes.
# We'll try the known endpoint; if it fails, skip it.
EU_URL = "https://webgate.ec.europa.eu/fsd/fsf/public/files/csvFullSanctionsList/content?token=dG9rZW4tMjAxNw"

# ---------------------------------------------------------------------------
# US Consolidated Screening List (CSL) — trade.gov
# Covers: BIS Entity List, Denied Persons, Unverified, MEU, State Nonproliferation,
#         State AECA Debarred, OFAC FSE, SSI, CAPTA, NS-MBS, NS-CCMC, PLC List
# Completely free, no API key needed, updated daily.
# ---------------------------------------------------------------------------
CSL_JSON_URL = "https://api.trade.gov/v1/consolidated_screening_list/search.json?size=10000&api_key=DEMO_KEY"
CSL_CSV_URL = "https://www.trade.gov/sites/default/files/2024-04/CSL.CSV"

# ---------------------------------------------------------------------------
# OpenSanctions — consolidated sanctions dataset
# Free for non-commercial use (CC BY-NC 4.0). 97K+ sanctioned entities from 328 sources.
# Covers lists not in the tool: Australia, Canada, Switzerland, Japan, INTERPOL, PEPs, etc.
# ---------------------------------------------------------------------------
OPENSANCTIONS_URL = "https://data.opensanctions.org/datasets/latest/sanctions/entities.ftm.json"
OPENSANCTIONS_SIMPLE_URL = "https://data.opensanctions.org/datasets/latest/default/entities.ftm.json"


def download_file(name: str, url: str, headers: dict = None) -> bool:
    """Download a file and save it to the data directory."""
    filepath = os.path.join(DATA_DIR, name)
    try:
        print(f"  Downloading {name}...")
        r = requests.get(
            url, timeout=120,
            headers=headers or {"User-Agent": "SanctionSight/2.3"},
            stream=True,
        )
        if r.status_code == 200:
            with open(filepath, "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    f.write(chunk)
            size_kb = os.path.getsize(filepath) / 1024
            if size_kb < 0.1:
                print(f"    ✗ File too small ({size_kb:.1f} KB) — likely empty/error response")
                return False
            print(f"    ✓ Saved ({size_kb:.1f} KB)")
            return True
        else:
            print(f"    ✗ Failed (status {r.status_code})")
            return False
    except Exception as exc:
        print(f"    ✗ Error: {exc}")
        return False


def download_csl() -> bool:
    """
    Download the US Consolidated Screening List in JSON format.
    Falls back to CSV if JSON download fails.
    trade.gov updates this daily at 5AM EST.
    """
    print("  Downloading US Consolidated Screening List (CSL)...")
    filepath_json = os.path.join(DATA_DIR, "us_csl.json")

    # Try the paged JSON API — fetch up to 10 pages of 1000 records each
    try:
        all_results = []
        page = 1
        while True:
            url = (
                f"https://api.trade.gov/v1/consolidated_screening_list/search"
                f"?size=1000&offset={(page - 1) * 1000}&api_key=DEMO_KEY"
            )
            r = requests.get(url, timeout=30, headers={"User-Agent": "SanctionSight/2.3"})
            if r.status_code != 200:
                break
            data = r.json()
            results = data.get("results", [])
            all_results.extend(results)
            total = data.get("total", 0)
            print(f"    Page {page}: {len(results)} records (total: {total})")
            if len(all_results) >= total or not results:
                break
            page += 1
            if page > 20:  # Safety cap
                break

        if all_results:
            with open(filepath_json, "w", encoding="utf-8") as f:
                json.dump({"results": all_results, "total": len(all_results)}, f)
            size_kb = os.path.getsize(filepath_json) / 1024
            print(f"    ✓ CSL JSON saved: {len(all_results)} records ({size_kb:.1f} KB)")
            return True
    except Exception as exc:
        print(f"    ✗ CSL JSON API failed: {exc} — trying CSV fallback")

    # CSV fallback
    return download_file("us_csl.csv", CSL_CSV_URL)


def download_opensanctions() -> bool:
    """
    Download OpenSanctions consolidated sanctions dataset.
    Free for non-commercial use (CC BY-NC 4.0).
    Format: line-delimited JSON (one entity per line, Follow-The-Money schema).
    ~97K sanctioned entities, covers 328 sources not otherwise in the tool.
    """
    print("  Downloading OpenSanctions consolidated sanctions dataset...")
    filepath = os.path.join(DATA_DIR, "opensanctions_sanctions.jsonl")
    try:
        r = requests.get(
            OPENSANCTIONS_URL, timeout=120,
            headers={"User-Agent": "SanctionSight/2.3"},
            stream=True,
        )
        if r.status_code == 200:
            count = 0
            with open(filepath, "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    f.write(chunk)
                    count += chunk.count(b"\n")
            size_kb = os.path.getsize(filepath) / 1024
            print(f"    ✓ OpenSanctions saved: ~{count} entities ({size_kb:.1f} KB)")
            return True
        else:
            print(f"    ✗ OpenSanctions failed (status {r.status_code})")
            return False
    except Exception as exc:
        print(f"    ✗ OpenSanctions error: {exc}")
        return False


def main():
    print("=" * 60)
    print("  SANCTIONS LIST DOWNLOADER v2.3")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    os.makedirs(DATA_DIR, exist_ok=True)

    success = 0
    failed = 0

    # Standard lists
    for name, url in LISTS.items():
        if download_file(name, url):
            success += 1
        else:
            failed += 1

    # EU list separately
    if download_file("eu_sanctions.csv", EU_URL):
        success += 1
    else:
        print("    (EU list may require updated token — check manually)")
        failed += 1

    print()
    print("--- US Consolidated Screening List ---")
    if download_csl():
        success += 1
    else:
        failed += 1

    print()
    print("--- OpenSanctions ---")
    if download_opensanctions():
        success += 1
    else:
        failed += 1
        print("    (OpenSanctions is free for non-commercial use — CC BY-NC 4.0)")

    print()
    print("=" * 60)
    print(f"  Done. {success} downloaded, {failed} failed.")
    print(f"  Files saved to: {DATA_DIR}")
    print("=" * 60)

    # Write a timestamp file
    with open(os.path.join(DATA_DIR, "_last_updated.txt"), "w") as f:
        f.write(datetime.now().isoformat())

    # Phase 1: record a fresh ListSnapshot row per file (provenance).
    # Import lazily so this script still runs in environments where the
    # DB deps haven't been installed.
    try:
        from sanctions_list_screener import SanctionsListScreener  # noqa: F401
        # Instantiating the screener triggers loading and snapshot recording.
        SanctionsListScreener(DATA_DIR)
    except Exception as exc:
        print(f"    (ListSnapshot recording skipped: {exc})")


if __name__ == "__main__":
    main()
