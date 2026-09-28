"""
Dorchester County, SC — Delinquent Tax scraper.

Pulls two live AS400 WebSmart pages and writes structured JSON:
  - Real Property list  -> data/dorchester_sc/real_property.json
  - Mobile Home list     -> data/dorchester_sc/mobile_homes.json

Both source pages share the same table shape:
    NAME | <ID COLUMN> | TOTAL BALANCE DUE | PRIOR YEARS UNPAID
where <ID COLUMN> is "TMS#" for real property and "STICKER" for mobile homes.

Quirks handled:
  - "Formerly Owned By: <name>" rows are continuation rows for the record
    directly above them, not separate properties. They're merged in as
    `formerly_owned_by` on the parent record.
  - The mobile home list is empty for most of the year (its advertising
    window opens later than real property's) — zero rows is a valid,
    expected result, not an error.
  - A trailing "TOTAL ACCOUNTS / TOTAL BALANCE DUE" row is the page's own
    checksum. We parse it and compare it against what we actually parsed,
    so a mismatch (site changed its markup, a row got missed) is loud and
    visible in the run log instead of silently producing bad data.

Run standalone:
    python scrape.py
Requires: requests, beautifulsoup4, lxml (see requirements.txt)
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

HEADERS = {
    # A plain requests default UA gets blocked on some county sites.
    # This one has been reliable for the AS400 pages specifically.
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

REAL_PROPERTY_URL = "https://as400.dorchestercounty.net/webapps/dtx000200.pgm"
MOBILE_HOME_URL = "https://as400.dorchestercounty.net/webapps/dtx000201.pgm"

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data" / "dorchester_sc"

FORMERLY_OWNED_PREFIX = "Formerly Owned By:"


@dataclass
class DelinquentRecord:
    name: str
    parcel_id: str
    id_type: str  # "tms" | "sticker"
    total_balance_due: float | None
    prior_years_unpaid: list[str] = field(default_factory=list)
    formerly_owned_by: list[str] = field(default_factory=list)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _parse_money(text: str) -> float | None:
    text = _clean(text).replace("$", "").replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _parse_prior_years(text: str) -> list[str]:
    text = _clean(text)
    if not text:
        return []
    return text.split()


def fetch_table_rows(url: str) -> list[list[str]]:
    """Fetch the page and return every <tr> as a list of cleaned cell strings."""
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")

    table = soup.find("table")
    if table is None:
        raise RuntimeError(f"No <table> found on page: {url}")

    rows: list[list[str]] = []
    for tr in table.find_all("tr"):
        cells = [_clean(td.get_text()) for td in tr.find_all(["td", "th"])]
        if any(cells):
            rows.append(cells)
    return rows


def parse_records(rows: list[list[str]], id_type: str) -> tuple[list[DelinquentRecord], dict]:
    """
    Turn raw table rows into DelinquentRecord objects.
    Returns (records, footer_totals) where footer_totals is the page's own
    self-reported {total_accounts, total_balance_due}, if we could find it.
    """
    records: list[DelinquentRecord] = []
    footer_totals = {"total_accounts": None, "total_balance_due": None}

    for cells in rows:
        first = cells[0] if cells else ""

        # Header row
        if first.upper() == "NAME":
            continue

        # Footer/checksum row, e.g. ["TOTAL ACCOUNTS:", "703", "TOTAL BALANCE DUE:", "2,512,667.49"]
        joined = " ".join(cells).upper()
        if "TOTAL ACCOUNTS" in joined:
            nums = re.findall(r"[\d,]+\.?\d*", " ".join(cells))
            if len(nums) >= 2:
                try:
                    footer_totals["total_accounts"] = int(nums[0].replace(",", ""))
                    footer_totals["total_balance_due"] = float(nums[-1].replace(",", ""))
                except ValueError:
                    pass
            continue

        # Continuation row: "Formerly Owned By: X" with nothing else on the row
        if first.startswith(FORMERLY_OWNED_PREFIX):
            prior_name = _clean(first[len(FORMERLY_OWNED_PREFIX):])
            if records:
                records[-1].formerly_owned_by.append(prior_name)
            continue

        # Skip the mobile-home placeholder row: blank name/sticker, balance "0.00" or ".00"
        if not first and (_parse_money(cells[-1] if cells else "") in (None, 0.0)):
            continue

        # Normal data row: NAME | ID | BALANCE | PRIOR YEARS
        if len(cells) < 3:
            continue  # malformed row — skip rather than guess

        name = cells[0]
        parcel_id = cells[1] if len(cells) > 1 else ""
        balance = _parse_money(cells[2]) if len(cells) > 2 else None
        prior_years = _parse_prior_years(cells[3]) if len(cells) > 3 else []

        if not name and not parcel_id:
            continue

        records.append(
            DelinquentRecord(
                name=name,
                parcel_id=parcel_id,
                id_type=id_type,
                total_balance_due=balance,
                prior_years_unpaid=prior_years,
            )
        )

    return records, footer_totals


def scrape_list(url: str, id_type: str, list_label: str) -> dict:
    rows = fetch_table_rows(url)
    records, footer_totals = parse_records(rows, id_type)

    parsed_total = round(sum(r.total_balance_due or 0 for r in records), 2)
    site_total = footer_totals.get("total_balance_due")

    result = {
        "county": "Dorchester",
        "state": "SC",
        "list_type": list_label,
        "source_url": url,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "record_count": len(records),
        "site_reported_total_accounts": footer_totals.get("total_accounts"),
        "site_reported_total_balance_due": site_total,
        "parsed_total_balance_due": parsed_total,
        "totals_match": (
            site_total is not None and abs(site_total - parsed_total) < 0.01
        ),
        "records": [asdict(r) for r in records],
    }
    return result


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    exit_code = 0

    jobs = [
        (REAL_PROPERTY_URL, "tms", "real_property", DATA_DIR / "real_property.json"),
        (MOBILE_HOME_URL, "sticker", "mobile_home", DATA_DIR / "mobile_homes.json"),
    ]

    for url, id_type, label, out_path in jobs:
        try:
            result = scrape_list(url, id_type, label)
        except Exception as exc:  # noqa: BLE001 — surface any failure clearly in CI logs
            print(f"[ERROR] Failed to scrape {label} from {url}: {exc}", file=sys.stderr)
            exit_code = 1
            continue

        out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")

        status = "OK" if result["totals_match"] else "MISMATCH"
        print(
            f"[{status}] {label}: {result['record_count']} records, "
            f"parsed total ${result['parsed_total_balance_due']:,.2f} "
            f"vs site total ${result['site_reported_total_balance_due']}"
        )
        if not result["totals_match"]:
            # Don't fail the whole run on this alone — but make it visible.
            print(
                f"[WARN] {label}: parsed total does not match the site's own "
                f"footer total. The page markup may have changed — worth a look.",
                file=sys.stderr,
            )

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
