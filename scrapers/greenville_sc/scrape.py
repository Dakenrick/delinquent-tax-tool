"""
Greenville County, SC — Delinquent Tax Sale scraper.

Unlike Dorchester, Greenville publishes ONE combined list (no separate
real-property vs mobile-home split) at a single URL, and each row's Map #
links directly to a public, unprotected property detail page — no login,
no bot protection, no separate lookup API to fight. That detail page
(Details.aspx) carries owner, address, acreage, subdivision, deed info and
assessed value, which is a natural follow-up enrichment step later — this
script only parses the list page itself.

Writes data/greenville_sc/tax_sale.json.

Run standalone:
    python scrape.py
Requires: requests, beautifulsoup4, lxml
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

LIST_URL = "https://www.greenvillecounty.org/appsas400/taxsale/"
DETAILS_BASE_URL = "https://www.greenvillecounty.org/appsas400/RealProperty/Details.aspx"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data" / "greenville_sc"
OUT_PATH = DATA_DIR / "tax_sale.json"
CHANGES_FILE = DATA_DIR / "changes.json"
MAX_HISTORY_ENTRIES = 200


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


@dataclass
class TaxSaleRecord:
    item_number: str
    parcel_id: str | None  # Greenville calls this "Map #". Item numbers in
    # the ~91000-96113 range are personal property / mobile home entries
    # with no Map # and no detail page — parcel_id and detail_url are None
    # for those rows rather than being silently dropped.
    name: str
    total_balance_due: float | None
    detail_url: str | None
    tax_year: str


def fetch_list_page() -> str:
    resp = requests.get(LIST_URL, headers=HEADERS, timeout=60)
    resp.raise_for_status()
    return resp.text


def parse_records(html: str, tax_year: str) -> tuple[list[TaxSaleRecord], dict]:
    """
    Returns (records, footer_totals). footer_totals is best-effort — this
    page hasn't shown a confirmed total/checksum row the way Dorchester's
    does, so it may come back empty; that's fine, just means no built-in
    validation check for this county.
    """
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table")
    if table is None:
        raise RuntimeError("No <table> found on the tax sale list page.")

    records: list[TaxSaleRecord] = []
    footer_totals = {"total_accounts": None, "total_balance_due": None}

    for tr in table.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if len(cells) < 4:
            continue

        texts = [_clean(c.get_text()) for c in cells]
        item_number, map_cell_text, name, amount_text = texts[0], texts[1], texts[2], texts[3]

        # Header row
        if item_number.lower().startswith("item"):
            continue

        # Possible footer/total row — best-effort, format unconfirmed
        joined_upper = " ".join(texts).upper()
        if "TOTAL" in joined_upper:
            nums = re.findall(r"[\d,]+\.?\d*", " ".join(texts))
            if len(nums) >= 2:
                try:
                    footer_totals["total_accounts"] = int(nums[0].replace(",", ""))
                    footer_totals["total_balance_due"] = float(nums[-1].replace(",", ""))
                except ValueError:
                    pass
            continue

        if not item_number.isdigit():
            continue  # not a real data row (header/footer/stray markup)

        # Map # comes from the link's href (authoritative) when present,
        # falling back to the cell text (should always match, but hrefs
        # are the actual source of truth for what the detail page expects).
        # Some rows (confirmed: item #s ~91000-96113, personal property /
        # mobile home entries) have a genuinely blank Map # cell and no
        # detail link — those are kept, just with parcel_id/detail_url
        # set to None instead of being dropped.
        link = cells[1].find("a")
        map_number = None
        if link and link.get("href"):
            match = re.search(r"MapNumber=([A-Za-z0-9]+)", link["href"])
            if match:
                map_number = match.group(1)
        if not map_number and map_cell_text:
            map_number = map_cell_text
        map_number = map_number or None

        balance = _parse_money(amount_text)
        detail_url = (
            f"{DETAILS_BASE_URL}?TaxYear={tax_year}&MapNumber={map_number}"
            if map_number else None
        )

        records.append(
            TaxSaleRecord(
                item_number=item_number,
                parcel_id=map_number,
                name=name,
                total_balance_due=balance,
                detail_url=detail_url,
                tax_year=tax_year,
            )
        )

    return records, footer_totals


def _compact(record: dict) -> dict:
    return {
        "item_number": record.get("item_number"),
        "name": record.get("name"),
        "parcel_id": record.get("parcel_id"),
        "total_balance_due": record.get("total_balance_due"),
    }


def _dedup_key(record: dict) -> str:
    # Map # is the stable identifier when present. Rows with no Map # (the
    # personal property / mobile home block, item #s ~91000-96113) have
    # nothing else stable to key on, so fall back to the item number —
    # imperfect if the county ever renumbers that block, but far better
    # than dropping the rows entirely.
    return record.get("parcel_id") or f"item-{record.get('item_number')}"


def compute_diff(previous_records: list[dict], new_records: list[dict]) -> tuple[list[dict], list[dict]]:
    prev_by_id = {_dedup_key(r): r for r in previous_records}
    new_by_id = {_dedup_key(r): r for r in new_records}
    added_ids = new_by_id.keys() - prev_by_id.keys()
    removed_ids = prev_by_id.keys() - new_by_id.keys()
    added = [_compact(new_by_id[i]) for i in added_ids]
    removed = [_compact(prev_by_id[i]) for i in removed_ids]
    return added, removed


def append_change_log(added: list[dict], removed: list[dict]) -> None:
    if not added and not removed:
        return
    history = []
    if CHANGES_FILE.exists():
        try:
            history = json.loads(CHANGES_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            history = []
    history.append(
        {
            "date": datetime.now(timezone.utc).date().isoformat(),
            "list_type": "tax_sale",
            "added_count": len(added),
            "removed_count": len(removed),
            "added": added,
            "removed": removed,
        }
    )
    history = history[-MAX_HISTORY_ENTRIES:]
    CHANGES_FILE.write_text(json.dumps(history, indent=2), encoding="utf-8")


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Greenville's list is keyed to a specific tax year (2025 in what we've
    # seen so far, for the 2026 sale). Kept as a constant here rather than
    # derived, since the county controls when this rolls over, not a fixed
    # calendar rule.
    tax_year = "2025"

    try:
        html = fetch_list_page()
        records, footer_totals = parse_records(html, tax_year)
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] Failed to scrape Greenville tax sale list: {exc}", file=sys.stderr)
        return 1

    if not records:
        print("[ERROR] Parsed zero records — the page structure may have changed.", file=sys.stderr)
        return 1

    previous_records: list[dict] = []
    had_previous_file = OUT_PATH.exists()
    if had_previous_file:
        try:
            previous_data = json.loads(OUT_PATH.read_text(encoding="utf-8"))
            previous_records = previous_data.get("records", [])
        except json.JSONDecodeError:
            previous_records = []

    new_record_dicts = [asdict(r) for r in records]

    if had_previous_file:
        added, removed = compute_diff(previous_records, new_record_dicts)
        if added or removed:
            append_change_log(added, removed)
            print(f"[CHANGES] +{len(added)} new, -{len(removed)} removed since last run")

    parsed_total = round(sum(r.total_balance_due or 0 for r in records), 2)
    site_total = footer_totals.get("total_balance_due")

    result = {
        "county": "Greenville",
        "state": "SC",
        "list_type": "tax_sale",
        "source_url": LIST_URL,
        "tax_year": tax_year,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "record_count": len(records),
        "site_reported_total_accounts": footer_totals.get("total_accounts"),
        "site_reported_total_balance_due": site_total,
        "parsed_total_balance_due": parsed_total,
        "totals_match": (
            None if site_total is None else abs(site_total - parsed_total) < 0.01
        ),
        "records": new_record_dicts,
    }

    OUT_PATH.write_text(json.dumps(result, indent=2), encoding="utf-8")

    if result["totals_match"] is None:
        print(f"[OK] {result['record_count']} records, ${parsed_total:,.2f} total (no site checksum available to verify against)")
    else:
        status = "OK" if result["totals_match"] else "MISMATCH"
        print(f"[{status}] {result['record_count']} records, parsed total ${parsed_total:,.2f} vs site total ${site_total}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
