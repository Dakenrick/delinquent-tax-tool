"""
Dorchester County, SC — CAMA/GIS enrichment.

Takes every TMS# already in data/dorchester_sc/real_property.json and looks
each one up against the county's VisionLive CAMA API to pull:
    account number, owner name (per GIS), subdivision, street address,
    mobile-home decal/serial (if the parcel has one on file)

Writes data/dorchester_sc/parcel_details.json, keyed by TMS# (our existing
dash-formatted parcel_id), so the frontend can join it against the
delinquent tax list without changing anything about that file.

IMPORTANT — this hits a live third-party API (Vision Government Solutions'
"VisionLive" CAMA system) sitting behind Akamai bot protection. This script:
  - is deliberately slow (a delay between requests) rather than fast
  - saves progress incrementally, so a partial run isn't wasted
  - skips parcels already successfully looked up on a rerun
  - aborts early (rather than burning through the whole list) if it gets
    several blocked/failed responses in a row, since that likely means
    every subsequent request will fail the same way

Run standalone:
    python enrich_gis.py [--tax-year 2025] [--limit 20] [--delay 0.6]
Requires: requests (see requirements.txt)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

CAMA_PAGE_URL = (
    "https://www.dorchestercountysc.gov/government/property-tax-services/"
    "assessor/real-estate-mobile-home-search/cama-parcel-lookup-old-page-test-page"
)
API_URL = "https://www.dorchestercountysc.gov/Sys/Handler/ParcelSearch"

DEFAULT_TAX_YEAR = "2025"
DEFAULT_DELAY_SECONDS = 0.6
CONSECUTIVE_FAILURE_LIMIT = 3
SAVE_EVERY = 25

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data" / "dorchester_sc"
REAL_PROPERTY_FILE = DATA_DIR / "real_property.json"
OUTPUT_FILE = DATA_DIR / "parcel_details.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def tms_to_search_format(parcel_id: str) -> str:
    """'055-00-00-050.000-C' -> '0550000050000' (digits only)."""
    return "".join(ch for ch in parcel_id if ch.isdigit())


def build_session() -> requests.Session:
    """Load the CAMA lookup page first to pick up real session cookies,
    the same way a browser would before it's allowed to call the API."""
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    session.get(CAMA_PAGE_URL, timeout=30)
    return session


def lookup_tms(session: requests.Session, search_tms: str, tax_year: str) -> dict:
    """
    Returns a dict describing what happened:
      {"status": "matched", "match": {...}} |
      {"status": "no_match"} |
      {"status": "blocked" | "error", "detail": "..."}
    """
    payload = [
        {"ParameterName": "TAXYEAR", "Value": tax_year},
        {"ParameterName": "TMS", "Value": search_tms},
    ]
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json;charset=UTF-8",
        "Origin": "https://www.dorchestercountysc.gov",
        "Referer": CAMA_PAGE_URL,
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }

    try:
        resp = session.post(
            API_URL, data=json.dumps(payload), headers=headers, timeout=30
        )
    except requests.RequestException as exc:
        return {"status": "error", "detail": f"request failed: {exc}"}

    if resp.status_code != 200:
        return {
            "status": "blocked",
            "detail": f"HTTP {resp.status_code}: {resp.text[:200]!r}",
        }

    try:
        results = resp.json()
    except ValueError:
        # Got a 200 but not JSON — almost certainly an Akamai challenge page.
        return {
            "status": "blocked",
            "detail": f"non-JSON response: {resp.text[:200]!r}",
        }

    if not results:
        return {"status": "no_match"}

    return {"status": "matched", "match": results[0]}


def to_record(parcel_id: str, search_tms: str, result: dict) -> dict:
    base = {
        "parcel_id": parcel_id,
        "search_tms": search_tms,
        "lookup_status": result["status"],
        "looked_up_at": datetime.now(timezone.utc).isoformat(),
    }
    if result["status"] == "matched":
        m = result["match"]
        addr_parts = [
            str(m.get("STREETNO") or ""),
            m.get("STREETNAME") or "",
            m.get("STREETTYPE") or "",
        ]
        base.update(
            {
                "account_no": m.get("ACCOUNTNO"),
                "owner_name_gis": m.get("NAME1"),
                "subdivision": m.get("SUBNAME"),
                "street_number": m.get("STREETNO"),
                "street_name": m.get("STREETNAME"),
                "street_type": m.get("STREETTYPE"),
                "full_address": " ".join(p for p in addr_parts if p).strip() or None,
                "mh_decal_no": m.get("MHDECALNO"),
                "mh_serial_no": m.get("MHSERIALNO"),
                "unit_name": m.get("UNITNAME"),
            }
        )
    else:
        base["error_detail"] = result.get("detail")
    return base


def load_existing_output() -> dict:
    if OUTPUT_FILE.exists():
        try:
            data = json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))
            return {r["parcel_id"]: r for r in data.get("parcels", [])}
        except (json.JSONDecodeError, KeyError):
            return {}
    return {}


def save_output(records_by_parcel: dict, tax_year: str, complete: bool) -> None:
    matched = sum(1 for r in records_by_parcel.values() if r["lookup_status"] == "matched")
    output = {
        "county": "Dorchester",
        "state": "SC",
        "source": "VisionLive CAMA API",
        "tax_year_queried": tax_year,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_complete": complete,
        "total_parcels": len(records_by_parcel),
        "matched": matched,
        "parcels": list(records_by_parcel.values()),
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(output, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tax-year", default=DEFAULT_TAX_YEAR)
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY_SECONDS)
    parser.add_argument(
        "--limit", type=int, default=None, help="Only process the first N parcels (for testing)."
    )
    args = parser.parse_args()

    if not REAL_PROPERTY_FILE.exists():
        print(f"[ERROR] {REAL_PROPERTY_FILE} not found — run scrape.py first.", file=sys.stderr)
        return 1

    real_property = json.loads(REAL_PROPERTY_FILE.read_text(encoding="utf-8"))
    parcel_ids = [r["parcel_id"] for r in real_property.get("records", []) if r.get("parcel_id")]
    if args.limit:
        parcel_ids = parcel_ids[: args.limit]

    records_by_parcel = load_existing_output()
    already_done = {
        pid for pid, r in records_by_parcel.items() if r["lookup_status"] == "matched"
    }
    todo = [pid for pid in parcel_ids if pid not in already_done]

    print(f"[INFO] {len(parcel_ids)} parcels total, {len(already_done)} already matched, {len(todo)} to look up.")
    if not todo:
        print("[INFO] Nothing to do.")
        save_output(records_by_parcel, args.tax_year, complete=True)
        return 0

    session = build_session()
    consecutive_failures = 0
    processed_since_save = 0
    aborted = False

    for i, parcel_id in enumerate(todo, start=1):
        search_tms = tms_to_search_format(parcel_id)
        result = lookup_tms(session, search_tms, args.tax_year)
        record = to_record(parcel_id, search_tms, result)
        records_by_parcel[parcel_id] = record

        if result["status"] in ("blocked", "error"):
            consecutive_failures += 1
            print(f"[WARN] ({i}/{len(todo)}) {parcel_id}: {result['status']} — {result.get('detail')}")
        else:
            consecutive_failures = 0
            status_str = "matched" if result["status"] == "matched" else "no match"
            print(f"[OK] ({i}/{len(todo)}) {parcel_id}: {status_str}")

        processed_since_save += 1
        if processed_since_save >= SAVE_EVERY:
            save_output(records_by_parcel, args.tax_year, complete=False)
            processed_since_save = 0

        if consecutive_failures >= CONSECUTIVE_FAILURE_LIMIT:
            print(
                f"[ERROR] {consecutive_failures} failures in a row — stopping early. "
                f"This likely means the site is blocking these requests (Akamai bot "
                f"protection). Progress so far has been saved.",
                file=sys.stderr,
            )
            aborted = True
            break

        time.sleep(args.delay)

    save_output(records_by_parcel, args.tax_year, complete=not aborted)

    matched = sum(1 for r in records_by_parcel.values() if r["lookup_status"] == "matched")
    print(f"[DONE] {matched}/{len(parcel_ids)} parcels matched so far.")
    return 1 if aborted else 0


if __name__ == "__main__":
    raise SystemExit(main())
