"""Daily pipeline: scrape -> fetch CRM -> match -> queue proposals for review.

Safe to re-run: decided proposals are never re-queued (see store.upsert_proposals), and the
matcher only proposes changes for differences that still exist in the live CRM.
"""
import argparse
import json
import sys
from datetime import date

from . import config
from .crm import CRM
from .matcher import build_proposals
from .scraper import scrape, save
from .store import Store


class ScrapeSanityError(RuntimeError):
    pass


def guard_scrape(locations, accounts) -> None:
    """Fail closed if the scrape looks broken. Without this, a website outage or a layout
    change would make the matcher propose flagging every Bellhaven facility as missing."""
    bellhaven_active = [a for a in accounts if a.get("parent_name") == config.PARENT_ACCOUNT_NAME
                        and a.get("status") == "Active" and not a.get("chow_current_account")]
    floor = max(1, int(config.MIN_SCRAPE_RATIO * len(bellhaven_active)))
    if len(locations) < floor:
        raise ScrapeSanityError(
            f"scraped only {len(locations)} locations but CRM has {len(bellhaven_active)} active Bellhaven "
            f"facilities (floor {floor}); refusing to generate proposals from a suspect scrape")
    bad = [l.slug for l in locations if not (l.name and l.street and l.city and l.state and l.zip)]
    if bad:
        raise ScrapeSanityError(f"locations with missing name/address fields (layout change?): {bad}")


def run(dry_run: bool = False, verbose: bool = True) -> dict:
    today = date.today().isoformat()
    crm = CRM()
    me = crm.me()
    if verbose:
        print(f"[crm] {me.get('message')} (sandbox of {me.get('candidate')})")

    locations = scrape(verbose=verbose)
    save(locations)
    accounts = crm.list_accounts()
    contacts = crm.list_contacts()
    (config.DATA_DIR / "crm_accounts_snapshot.json").write_text(json.dumps(accounts, indent=1))
    if verbose:
        print(f"[crm] {len(accounts)} accounts, {len(contacts)} contacts")

    guard_scrape(locations, accounts)
    proposals, report = build_proposals(locations, accounts, contacts, today)
    if verbose:
        print(f"[match] {report['proposals_by_kind']}")

    if dry_run:
        for p in proposals:
            print(f"  {p.kind:16} {p.confidence:6} {p.fingerprint}  {p.title}")
        return {"dry_run": True, **report}

    store = Store()
    run_id = store.start_run()
    counts = store.upsert_proposals(proposals, run_id)
    summary = {**report, "queue": counts, "date": today}
    store.finish_run(run_id, summary)
    if verbose:
        print(f"[queue] {counts}")
        print(f"[queue] totals by status: {store.counts()}")
    return summary


def main(argv=None):
    ap = argparse.ArgumentParser(description="Bellhaven website -> CRM sync")
    ap.add_argument("--dry-run", action="store_true", help="print proposals, do not touch the queue")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)
    run(dry_run=a.dry_run, verbose=not a.quiet)


if __name__ == "__main__":
    sys.exit(main())
