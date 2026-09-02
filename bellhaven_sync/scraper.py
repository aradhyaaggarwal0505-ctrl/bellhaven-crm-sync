"""Scrape every Bellhaven community from the public website.

The directory is paginated. The homepage also links to communities that are not yet in
the directory (e.g. a freshly acquired one), so we crawl every /communities/<slug> link
found on the homepage and on all directory pages, then fetch each detail page.
"""
import html
import json
import re
import time
from dataclasses import dataclass, asdict, field
from typing import List, Dict

import requests

from . import config


@dataclass
class Location:
    slug: str
    name: str
    street: str
    city: str
    state: str
    zip: str
    care_offerings: List[str]
    phone: str = ""
    administrator: str = ""
    url: str = ""
    sources: List[str] = field(default_factory=list)  # where the link was found

    def to_dict(self) -> Dict:
        return asdict(self)


_SLUG_RE = re.compile(r'href="/communities/([a-z0-9\-]+)"')
_H1_RE = re.compile(r"<h1>(.*?)</h1>", re.S)
_ADDR_RE = re.compile(r"<dt>Address</dt>\s*<dd>(.*?)</dd>", re.S)
_BADGE_RE = re.compile(r'<span class="badge">(.*?)</span>', re.S)
_ADMIN_RE = re.compile(r"<dt>Administrator</dt>\s*<dd>(.*?)</dd>", re.S)
_PHONE_RE = re.compile(r"<dt>Phone</dt>\s*<dd>(.*?)</dd>", re.S)
_PAGER_RE = re.compile(r"Page (\d+) of (\d+)")
_CITYLINE_RE = re.compile(r"^(.*?),\s*([A-Z]{2})\s+(\d{5})(?:-\d{4})?$")


def _get(session: requests.Session, path: str) -> str:
    url = config.SITE_BASE + path
    for attempt in range(3):
        r = session.get(url, timeout=20)
        if r.status_code == 200:
            return r.text
        time.sleep(1 + attempt)
    r.raise_for_status()
    return r.text


def _clean(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", s)).strip()


def parse_detail(slug: str, page: str) -> Location:
    name = _clean(_H1_RE.search(page).group(1))
    addr_html = _ADDR_RE.search(page).group(1)
    lines = [_clean(x) for x in re.split(r"<br\s*/?>", addr_html) if _clean(x)]
    street = lines[0] if lines else ""
    city = state = zip_ = ""
    if len(lines) > 1:
        m = _CITYLINE_RE.match(lines[-1])
        if m:
            city, state, zip_ = m.group(1).strip(), m.group(2), m.group(3)
        else:
            city = lines[-1]
    care = [_clean(b) for b in _BADGE_RE.findall(page)]
    admin = _ADMIN_RE.search(page)
    phone = _PHONE_RE.search(page)
    return Location(
        slug=slug, name=name, street=street, city=city, state=state, zip=zip_,
        care_offerings=care,
        phone=_clean(phone.group(1)) if phone else "",
        administrator=_clean(admin.group(1)) if admin else "",
        url=f"{config.SITE_BASE}/communities/{slug}",
    )


def scrape(verbose: bool = True) -> List[Location]:
    s = requests.Session()
    s.headers["User-Agent"] = "bellhaven-sync/1.0 (+crm hygiene bot)"
    found: Dict[str, set] = {}

    def add(slugs, source):
        for sl in slugs:
            found.setdefault(sl, set()).add(source)

    add(_SLUG_RE.findall(_get(s, "/")), "homepage")
    first = _get(s, "/communities")
    add(_SLUG_RE.findall(first), "directory p1")
    m = _PAGER_RE.search(first)
    total_pages = int(m.group(2)) if m else 1
    for p in range(2, total_pages + 1):
        add(_SLUG_RE.findall(_get(s, f"/communities?page={p}")), f"directory p{p}")

    listed = re.search(r"(\d+) communities listed", first)
    claimed = re.search(r"serve (\d+) communities", _get(s, "/"))
    if verbose:
        print(f"[scrape] {len(found)} unique community links "
              f"(directory says {listed.group(1) if listed else '?'} listed, "
              f"homepage says {claimed.group(1) if claimed else '?'} served)")

    locations = []
    for slug in sorted(found):
        loc = parse_detail(slug, _get(s, f"/communities/{slug}"))
        loc.sources = sorted(found[slug])
        locations.append(loc)
    return locations


def save(locations: List[Location], path=None) -> None:
    path = path or (config.DATA_DIR / "site_locations.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([l.to_dict() for l in locations], indent=1))


if __name__ == "__main__":
    locs = scrape()
    save(locs)
    for l in locs:
        print(f"{l.name:50} {l.street:28} {l.city}, {l.state} {l.zip}  {l.care_offerings}  {l.sources}")
