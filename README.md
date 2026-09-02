# Bellhaven → CRM ownership sync

Keeps the CRM's picture of **which facilities Bellhaven Senior Living owns** in step with
Bellhaven's public website. Scrapes every community, matches each one to the right CRM
account, proposes fixes with evidence, and writes approved fixes back through the API.

**Time spent:** ~2 hours (build, review, and writeup), AI-assisted. *(adjust to your actual)*

## Layout

```
bellhaven_sync/
  scraper.py     crawl homepage + paginated directory, fetch every detail page
  normalize.py   name / street / phone normalisation used for matching
  matcher.py     match locations to accounts, classify, emit Proposals (no writes)
  store.py       SQLite queue of proposals + decisions (state/proposals.db)
  apply.py       execute an approved proposal via the API, with live preconditions
  pipeline.py    scrape -> fetch CRM -> match -> queue   (python run_pipeline.py)
review_app/app.py  local Flask reviewer UI (approve = apply now, reject = never re-ask)
.github/workflows/daily-sync.yml, crontab.example   daily schedule
tests/            24 unit tests: normalisation, matching, SOP branch, contacts, apply preconditions, queue idempotency
```

## Run it

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env            # put your CRM token in CRM_TOKEN
.venv/bin/python run_pipeline.py --dry-run   # see proposals, touch nothing
.venv/bin/python run_pipeline.py             # queue proposals in state/proposals.db
.venv/bin/python review_app/app.py           # http://127.0.0.1:5055
.venv/bin/python -m unittest discover tests  # tests
```

Nothing writes to the CRM until a reviewer clicks **Approve & apply** in the app.

## How matching works

The website is the source of truth for *which buildings Bellhaven operates today*; the CRM
is the source of truth for billing. For every scraped location the matcher scores every
non-parent CRM account on:

| signal | weight | note |
|---|---|---|
| normalised street + (zip or city) match | 0.65 | `1125 Logan Blvd` == `1125 Logan Boulevard`, `NW` == `Northwest` |
| street number + city | 0.35 | fallback when the street name is mangled |
| zip / city+state | 0.10 each | |
| name similarity (token Jaccard vs. sequence ratio, after canonicalising rehab/center/centre, dropping of/at/the) | 0.30 | |
| phone match | 0.10 | |
| **different state** | ×0.3 | a same-named building in another state is a different building |

A match is *confident* when the street matches with city or zip, or the names are
equivalent in the same city, or the score is ≥ 0.75. Every account is claimed by at most
one location (highest score wins). Address dominates name on purpose: names are what
change during acquisitions; buildings don't move.

**Several confident records for one building** are the duplicate case. A survivor is
chosen by, in order: Active status → already under Bellhaven → has billing history → held
by the most recent prior owner (Cedar Trail 2026 > Harborview 2025 > orphan) → phone
matches website → more contacts → closer name → lowest id. Every other record becomes
`duplicate_of_account = survivor`, `status = Inactive` — unless it has revenue **and** AR,
in which case it is left alone and linked via `chow_current_account` instead (`chow_link`).

**Then the survivor is diffed** against the website: parent, name, address, care type,
phone, status. Each difference is its own proposal so a reviewer can accept the re-parent
but reject a cosmetic rename.

**Bellhaven accounts that are not on the website** get a second look: if another parent
has an Active record at the same street address, the facility was sold → `chow_outbound`
(link `chow_current_account` to the successor; mark Inactive only if there is no billing
lock). Otherwise → `Needs Review` with a note. I chose Needs Review over Inactive because
"not on the website" does not prove closure, and a rep should not silently lose the
account.

## Contacts

The website names an administrator for every community; the CRM has contacts. After the
account match, the survivor's contacts are diffed against the website administrator:

| situation | proposal |
|---|---|
| no contact with that name | `add_contact` (title Administrator, no invented email) |
| a different *active* Administrator | `replace_admin`: create the new one, set the old one `is_active=false` (kept, not deleted) |
| same name, but inactive or wrong title | `fix_contact` |
| active contact stranded on a retired duplicate record | `move_contact`: re-point `account_id` to the survivor (or deactivate if the survivor already has that person) |

Account creations (`create_account`, `chow_new_account`) create the administrator contact
in the same proposal, so a brand-new account never lands without a person on it.
Contacts on records preserved for billing (old Tiffin/Marietta, Sandusky) are left alone.

## The billing SOP

`has_billing_lock(a) = lifetime_revenue > 0 and outstanding_ar > 0`

Whenever the matcher wants to move an account to a different parent it checks the lock:

* **locked** → `chow_new_account`: create a new account under Bellhaven from the website
  data, then set `chow_current_account` on the old one. The old record's parent, name,
  address, status, revenue and AR are never touched (only a note is appended). No other
  edits are proposed for the old record.
* **not locked** → `reparent`: PATCH `parent_id` directly.

`apply.py` re-reads the live account immediately before every PATCH and re-checks the
lock and the expected old parent, so a proposal queued yesterday cannot do the wrong thing
if billing posted an invoice overnight; it fails closed and asks for a re-run.

Outcome in this sandbox: **Tiffin** and **Marietta** (Cedar Trail, both with AR) got new
accounts + CHOW pointers; **Lima** (Harborview, AR = 0) and **Findlay** (orphan, AR = 0)
were re-parented directly; **Sandusky** (Bellhaven, AR = $5,200) was found under Millstone
at the same address, so it kept its parent and status and only gained the CHOW pointer.

## Safe re-runs

Each proposal has a fingerprint = hash(kind, subject, target values), deliberately
*excluding* dates and note text. The store never re-inserts a fingerprint that is
`approved / applied / rejected / failed`; pending ones are refreshed in place; pending ones
that stop being generated are marked `stale` (and revived if they come back). On top of
that, the matcher only proposes changes for differences that still exist in the live CRM,
and it skips records that are already merged (`Inactive` + `duplicate_of_account`) or
CHOW'd (`chow_current_account` set). Running the pipeline right after the review produced
`{'new': 0, ...}` and `[match] {}`.

**Fail-closed guards.** A website outage or layout change would otherwise make the matcher
propose flagging every Bellhaven facility as missing, so the pipeline aborts before touching
the queue if the scrape returns fewer than half as many locations as the CRM has Active
Bellhaven facilities, or if any location is missing name/address fields. At apply time every
PATCH first re-reads the live account and re-checks the billing lock and the expected parent
(`tests/test_safety.py` covers both).

## Schedule

`.github/workflows/daily-sync.yml` runs at 06:00 ET daily (and on demand), with the token
in a repo secret and a concurrency group so two runs can't write the queue at once. It
commits `state/proposals.db` back so the decisions travel with the repo. `crontab.example`
is the single-machine equivalent. The review app stays local; the pipeline never writes to
the CRM.

## Notifications

A queue nobody reads is worth nothing. Every run writes `state/last_run.json`; if it queued
anything new, the pipeline POSTs a summary to `NOTIFY_WEBHOOK_URL` (Slack-compatible,
optional) and the GitHub Actions job opens an issue listing the new proposals.

## What the run found (62 proposals, all approved)

| kind | n | examples |
|---|---|---|
| create_account | 4 | Batavia, Carlisle (no record); **Amberly Manor, Hudson OH** — the existing "Amberly Manor" is in Colorado Springs under Juniper Point, a name collision, not a match; **Bellhaven at Union Square** — "Union Square Senior Living" is at a different street (240 Market St vs 118 Union Square Dr) so it is left under Juniper Point and flagged as related |
| chow_new_account | 2 | Tiffin, Marietta (billing lock) |
| reparent | 4 | Lima (Harborview), Findlay (orphan, linked only from the homepage — the directory says 34, the homepage says 35), Zanesville and Kettering (Cedar Trail, AR = 0) |
| mark_duplicate | 7 | 3 records at 750 Stewart Rd Monroe, 3 at 3313 Wilmington Pike Kettering, Harborview copies of Port Clinton and Erie, a second Owosso row |
| rename | 9 | Riverbend Manor → Bellhaven of Chagrin Falls, Sunny Acres → Bellhaven Willow Creek, Chesterton Senior Commons → Bellhaven of Chesterton, Cedar Trail of Zanesville → Bellhaven of Zanesville, plus spelling variants |
| fix_address | 2 | Ashtabula (PO Box → street), Portsmouth zip 45626 → 45662 |
| fix_phone | 15 | CRM phone ≠ website phone |
| chow_outbound | 1 | Sandusky → Millstone Care of Sandusky |
| flag_missing | 2 | Alliance, Coldwater → Needs Review |
| add_contact | 11 | administrators for the 4 created accounts, the 2 CHOW accounts, and 5 existing accounts with no Administrator contact |
| replace_admin | 4 | Lima, Sycamore Ridge, Saline, Wooster: website names a different administrator; old contact deactivated |
| move_contact | 1 | Tricia Lindqvist re-pointed from the retired Owosso duplicate to the survivor |

Care offerings: the CRM has one `care_type`; the website can list several (Erie, Findlay).
The matcher accepts the CRM value if it is *any* of the website's offerings and records the
full list in the note, so no care-type change was needed here.

The contact proposals were generated by the *second* run: the first run created the
accounts, and once they existed the next run saw they had no administrator. That is the
daily loop working as intended.

## Things I would do next

* A `chow_link` / duplicate with revenue on the *loser* is handled in code but never
  occurred in this data — worth a fixture test with real billing rows.
* Notify (Slack/email) when the daily run queues anything new.
