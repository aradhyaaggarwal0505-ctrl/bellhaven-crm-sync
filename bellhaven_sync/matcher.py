"""Match website locations to CRM accounts and turn the differences into proposals.

A proposal is a *fully specified* set of API actions plus the evidence that justifies
it. Nothing here touches the CRM; apply.py executes approved proposals.

Proposal kinds
  create_account     location has no CRM record -> create one under the Bellhaven parent
  reparent           matched account sits under the wrong parent, no billing lock -> move it
  chow_new_account   matched account has revenue AND outstanding AR -> SOP: leave it, create a
                     new account under Bellhaven and point old.chow_current_account at it
  chow_link          a second record for the same building has revenue AND AR -> it must be
                     preserved, so link it to the surviving record via chow_current_account
  mark_duplicate     a second record for the same building -> duplicate_of + Inactive
  rename             matched account's name differs from the website name
  fix_address        street / city / state / zip differs from the website
  fix_care_type      CRM care_type is not one of the website's offerings
  fix_phone          CRM phone differs from the website phone
  reactivate         matched account is Inactive/Needs Review but the facility is live
  chow_outbound      Bellhaven account not on the website, and another parent now has a
                     record at the same address -> the facility was sold; link it
  flag_missing       Bellhaven account not on the website, nowhere else to point -> Needs Review
  add_contact        website names an administrator the account has no contact for -> create
  replace_admin      account's active Administrator differs from the website -> create new, retire old
  fix_contact        a contact with the administrator's name exists but is inactive / wrong title
  move_contact       an active contact is stranded on a retired duplicate record -> re-point it
Account creations (create_account, chow_new_account) also create the Administrator contact.
"""
import hashlib
import json
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple

from . import config
from .normalize import (norm_street, street_number, is_po_box, name_similarity, norm_city,
                        norm_zip, norm_phone, names_equivalent)
from .scraper import Location


@dataclass
class Proposal:
    kind: str
    subject: str            # account_id, or "site:<slug>" for creates
    title: str
    summary: str
    confidence: str         # high | medium | low
    actions: List[Dict]     # ordered API operations (see apply.py)
    evidence: Dict
    fingerprint: str = ""
    key: Dict = field(default_factory=dict)   # what the fingerprint is computed from

    def finalize(self):
        raw = json.dumps({"kind": self.kind, "subject": self.subject, "key": self.key}, sort_keys=True)
        self.fingerprint = hashlib.sha1(raw.encode()).hexdigest()[:16]
        return self

    def to_dict(self):
        return asdict(self)


# --------------------------------------------------------------------------- helpers

def is_parent_account(a: Dict) -> bool:
    return a["name"].endswith("(Parent Account)") or (not a["billing_city"] and not a["billing_street"])


def care_types_for(loc: Location) -> List[str]:
    out = []
    for c in loc.care_offerings:
        v = config.CARE_TYPE_MAP.get(c.strip().lower())
        if v and v not in out:
            out.append(v)
    return out


def account_brief(a: Dict) -> Dict:
    keys = ["account_id", "name", "parent_id", "parent_name", "billing_street", "billing_city",
            "billing_state", "billing_zip", "care_type", "status", "phone", "lifetime_revenue",
            "outstanding_ar", "chow_current_account", "duplicate_of_account", "note"]
    return {k: a.get(k) for k in keys}


def score_pair(loc: Location, a: Dict) -> Tuple[float, Dict]:
    """Return (score 0..1+, breakdown)."""
    st_l, st_a = norm_street(loc.street), norm_street(a["billing_street"])
    num_l, num_a = street_number(loc.street), street_number(a["billing_street"])
    state_match = loc.state.upper() == (a["billing_state"] or "").upper()
    zip_match = norm_zip(loc.zip) == norm_zip(a["billing_zip"]) and bool(loc.zip)
    city_match = norm_city(loc.city) == norm_city(a["billing_city"]) and bool(loc.city)
    street_match = bool(st_l) and st_l == st_a
    number_match = bool(num_l) and num_l == num_a
    nsim = name_similarity(loc.name, a["name"])
    exact_name = names_equivalent(loc.name, a["name"])
    phone_match = bool(norm_phone(loc.phone)) and norm_phone(loc.phone) == norm_phone(a["phone"])

    score = 0.0
    if street_match and (zip_match or city_match):
        score += 0.65
    elif number_match and city_match:
        score += 0.35
    if zip_match:
        score += 0.10
    if city_match and state_match:
        score += 0.10
    score += 0.30 * nsim
    if phone_match:
        score += 0.10
    if not state_match:
        score *= 0.3   # a same-named building in another state is a different building
    return round(score, 3), {
        "street_match": street_match, "number_match": number_match, "zip_match": zip_match,
        "city_match": city_match, "state_match": state_match, "name_similarity": round(nsim, 2),
        "name_equivalent": exact_name, "phone_match": phone_match,
    }


def is_confident(score: float, b: Dict) -> bool:
    if not b["state_match"]:
        return False
    if b["street_match"] and (b["city_match"] or b["zip_match"]):
        return True
    if b["name_equivalent"] and (b["city_match"] or b["zip_match"]):
        return True
    if b["name_similarity"] >= 0.9 and b["city_match"] and b["zip_match"]:
        return True
    return score >= config.CONFIDENT_SCORE


def has_billing_lock(a: Dict) -> bool:
    """SOP: revenue history AND outstanding AR > 0 -> the record must not be re-parented."""
    return (a.get("lifetime_revenue") or 0) > 0 and (a.get("outstanding_ar") or 0) > 0


def survivor_rank(a: Dict, loc: Location, parent_id: str, contacts_by_acct: Dict) -> tuple:
    """Lower sorts first. Which of several records for one building should survive."""
    status_rank = {"Active": 0, "Needs Review": 1, "Inactive": 2}.get(a["status"], 3)
    under_bellhaven = 0 if a["parent_id"] == parent_id else 1
    owner_rank = 99
    for i, p in enumerate(config.PRIOR_OWNERS_NEWEST_FIRST):
        if (a.get("parent_name") or "").startswith(p):
            owner_rank = i
    if not a["parent_id"]:
        owner_rank = 50   # an orphan record is less trustworthy than one held by a known prior owner
    billing = 0 if ((a.get("lifetime_revenue") or 0) > 0 or (a.get("outstanding_ar") or 0) > 0) else 1
    phone = 0 if norm_phone(a["phone"]) == norm_phone(loc.phone) and loc.phone else 1
    ncontacts = -len(contacts_by_acct.get(a["account_id"], []))
    nsim = -name_similarity(loc.name, a["name"])
    return (status_rank, under_bellhaven, billing, owner_rank, phone, ncontacts, nsim, a["account_id"])


def new_account_body(loc: Location, parent_id: str, note: str) -> Dict:
    ct = care_types_for(loc)
    return {
        "name": loc.name,
        "parent_id": parent_id,
        "billing_street": loc.street,
        "billing_city": loc.city,
        "billing_state": loc.state,
        "billing_zip": loc.zip,
        "care_type": ct[0] if ct else "",
        "phone": loc.phone,
        "status": "Active",
        "note": note,
    }


def norm_person(s: str) -> str:
    return " ".join((s or "").lower().replace(".", "").split())


def admin_contact_action(loc: Location, account_ref: str) -> Dict:
    """Action that creates the website-listed administrator as a contact on account_ref
    (an account_id, or "$created" for an account made earlier in the same proposal)."""
    return {"op": "create_contact", "body": {"name": loc.administrator, "title": "Administrator",
                                             "account_id": account_ref, "email": "", "phone": ""}}


def contact_brief(c: Dict) -> Dict:
    return {k: c.get(k) for k in ("contact_id", "name", "title", "email", "phone", "is_active", "account_id")}


def contact_proposals(loc: Location, acct: Dict, contacts: List[Dict], today: str) -> List["Proposal"]:
    """Sync the website's administrator onto a matched account's contacts."""
    out: List[Proposal] = []
    if not loc.administrator:
        return out
    want = norm_person(loc.administrator)
    same = [c for c in contacts if norm_person(c["name"]) == want]
    active_admins = [c for c in contacts if c["is_active"] and c["title"] == "Administrator"]
    ev = {"location": loc.to_dict(), "matched_account": account_brief(acct),
          "site_administrator": loc.administrator, "crm_contacts": [contact_brief(c) for c in contacts]}
    if same:
        c = same[0]
        body = {}
        if not c["is_active"]:
            body["is_active"] = True
        if c["title"] != "Administrator":
            body["title"] = "Administrator"
        if body:
            out.append(Proposal(
                kind="fix_contact", subject=acct["account_id"],
                title=f"Fix contact: {c['name']} at {acct['name']}",
                summary=(f"Website lists {loc.administrator} as Administrator; CRM has them as "
                         f"'{c['title']}'{'' if c['is_active'] else ' (inactive)'}."),
                confidence="high",
                actions=[{"op": "patch_contact", "contact_id": c["contact_id"], "body": body,
                          "preconditions": {"account_id": acct["account_id"]}}],
                evidence={**ev, "contact": contact_brief(c), "reasons": ["Name matches the website administrator."]},
                key={"contact": c["contact_id"], **body},
            ).finalize())
        return out
    stale = [c for c in active_admins]
    actions = [admin_contact_action(loc, acct["account_id"])]
    for c in stale:
        actions.append({"op": "patch_contact", "contact_id": c["contact_id"], "body": {"is_active": False},
                        "preconditions": {"account_id": acct["account_id"], "is_active": True}})
    if stale:
        out.append(Proposal(
            kind="replace_admin", subject=acct["account_id"],
            title=f"Administrator changed: {acct['name']}",
            summary=(f"Website lists {loc.administrator} as Administrator; CRM has "
                     + ", ".join(c["name"] for c in stale) + ". Create the new contact and mark the old one inactive."),
            confidence="medium",
            actions=actions,
            evidence={**ev, "reasons": ["Website administrator differs from the CRM's active Administrator contact.",
                                        "Old contact is deactivated, not deleted, so history and email stay visible.",
                                        "No email is invented for the new contact; the facility phone is on the account."]},
            key={"admin": loc.administrator, "retire": sorted(c["contact_id"] for c in stale)},
        ).finalize())
    else:
        out.append(Proposal(
            kind="add_contact", subject=acct["account_id"],
            title=f"Add administrator: {loc.administrator} at {acct['name']}",
            summary=f"Website lists {loc.administrator} as Administrator; the account has no contact by that name.",
            confidence="high",
            actions=actions,
            evidence={**ev, "reasons": ["Account has no active Administrator contact." if not active_admins
                                        else "No contact with this name on the account."]},
            key={"admin": loc.administrator},
        ).finalize())
    return out


def offerings_note(loc: Location) -> str:
    return "Care offerings per website: " + ", ".join(loc.care_offerings) if loc.care_offerings else ""


# --------------------------------------------------------------------------- main

def build_proposals(locations: List[Location], accounts: List[Dict], contacts: List[Dict],
                    today: str) -> Tuple[List[Proposal], Dict]:
    parent = next((a for a in accounts if a["name"] == config.PARENT_ACCOUNT_NAME), None)
    if not parent:
        raise RuntimeError(f"Parent account {config.PARENT_ACCOUNT_NAME!r} not found in CRM")
    parent_id = parent["account_id"]
    by_id = {a["account_id"]: a for a in accounts}
    contacts_by_acct: Dict[str, list] = {}
    for c in contacts:
        contacts_by_acct.setdefault(c["account_id"], []).append(c)

    # Candidates: real facility records. Records already merged away (Inactive + duplicate_of)
    # or already CHOW'd away (chow_current_account set) are not candidates.
    candidates = [a for a in accounts if not is_parent_account(a)
                  and not (a["status"] == "Inactive" and a["duplicate_of_account"])
                  and not a["chow_current_account"]]

    proposals: List[Proposal] = []
    claimed: Dict[str, str] = {}      # account_id -> slug
    match_report = []

    # ---- pass 1: for every location, find all records that describe this building
    loc_matches: Dict[str, List[Tuple[float, Dict, Dict]]] = {}
    for loc in locations:
        scored = []
        for a in candidates:
            sc, b = score_pair(loc, a)
            if sc >= config.POSSIBLE_SCORE * 0.5:
                scored.append((sc, b, a))
        scored.sort(key=lambda t: -t[0])
        loc_matches[loc.slug] = scored

    # resolve conflicts: an account belongs to the location that scores it highest
    best_for_acct: Dict[str, Tuple[float, str]] = {}
    for loc in locations:
        for sc, b, a in loc_matches[loc.slug]:
            if is_confident(sc, b):
                cur = best_for_acct.get(a["account_id"])
                if not cur or sc > cur[0]:
                    best_for_acct[a["account_id"]] = (sc, loc.slug)

    for loc in locations:
        scored = loc_matches[loc.slug]
        confident = [(sc, b, a) for sc, b, a in scored
                     if is_confident(sc, b) and best_for_acct.get(a["account_id"], (0, None))[1] == loc.slug]
        possible = [(sc, b, a) for sc, b, a in scored if not is_confident(sc, b) and sc >= config.POSSIBLE_SCORE]
        loc_ev = {"location": loc.to_dict()}

        if not confident:
            # ---------------- no CRM record: create
            related = [{"score": sc, **account_brief(a), "breakdown": b} for sc, b, a in possible[:3]]
            body = new_account_body(loc, parent_id, f"{config.NOTE_TAG} created {today} from website listing {loc.url}. "
                                    + offerings_note(loc))
            reasons = ["No CRM account matches this website location by address or name."]
            if related:
                reasons.append("Nearby / similarly named records exist but do not match on address, "
                               "so they are treated as different buildings (see related records).")
            if "homepage" in loc.sources and not any(s.startswith("directory") for s in loc.sources):
                reasons.append("This community is linked only from the homepage (not yet in the directory).")
            create_actions = [{"op": "create", "body": body}]
            if loc.administrator:
                create_actions.append(admin_contact_action(loc, "$created"))
            proposals.append(Proposal(
                kind="create_account", subject=f"site:{loc.slug}",
                title=f"Create account: {loc.name}",
                summary=(f"Create '{loc.name}' ({loc.city}, {loc.state}) under {config.PARENT_ACCOUNT_NAME}"
                         + (f" with administrator contact {loc.administrator}." if loc.administrator else ".")),
                confidence="high" if not related else "medium",
                actions=create_actions,
                evidence={**loc_ev, "related_records": related, "reasons": reasons},
                key={"name": loc.name, "street": loc.street, "zip": loc.zip},
            ).finalize())
            match_report.append({"slug": loc.slug, "outcome": "create", "account_id": None})
            continue

        # ---------------- one or more records describe this building
        confident.sort(key=lambda t: survivor_rank(t[2], loc, parent_id, contacts_by_acct))
        sc, b, surv = confident[0]
        claimed[surv["account_id"]] = loc.slug
        others = confident[1:]
        surv_ev = {**loc_ev, "matched_account": account_brief(surv), "score": sc, "breakdown": b,
                   "contacts": [c["name"] + " (" + c["title"] + ")" for c in contacts_by_acct.get(surv["account_id"], [])]}
        match_report.append({"slug": loc.slug, "outcome": "matched", "account_id": surv["account_id"], "score": sc})

        # duplicates / chow links for the other records
        for osc, ob, o in others:
            claimed[o["account_id"]] = loc.slug
            ev = {**loc_ev, "this_record": account_brief(o), "surviving_record": account_brief(surv),
                  "score": osc, "breakdown": ob,
                  "contacts": [c["name"] + " (" + c["title"] + ")" for c in contacts_by_acct.get(o["account_id"], [])],
                  "why_this_survivor": "Survivor chosen by: Active status, already under Bellhaven, billing history, "
                                       "most recent prior owner, phone matches website, contact count, name similarity."}
            if has_billing_lock(o):
                proposals.append(Proposal(
                    kind="chow_link", subject=o["account_id"],
                    title=f"CHOW link (billing lock): {o['name']} -> {surv['name']}",
                    summary=(f"'{o['name']}' is a second record for {loc.name} but has revenue and outstanding AR, "
                             f"so it is preserved unchanged and linked via chow_current_account to {surv['account_id']}."),
                    confidence="high",
                    actions=[{"op": "patch", "account_id": o["account_id"],
                              "body": {"chow_current_account": surv["account_id"]},
                              "append_note": f"{config.NOTE_TAG} {today}: same building as {surv['account_id']} ({loc.name}); "
                                             f"kept for billing (AR>0), current account is {surv['account_id']}.",
                              "preconditions": {"billing_lock": True}}],
                    evidence={**ev, "reasons": ["Same street address as the surviving record.",
                                                "Has lifetime revenue and outstanding AR, so SOP forbids altering it."]},
                    key={"survivor": surv["account_id"]},
                ).finalize())
            else:
                proposals.append(Proposal(
                    kind="mark_duplicate", subject=o["account_id"],
                    title=f"Duplicate: {o['name']} -> {surv['name']}",
                    summary=(f"'{o['name']}' ({o['parent_name'] or 'no parent'}) is the same building as "
                             f"'{surv['name']}' [{surv['account_id']}]. Mark Inactive, duplicate_of = survivor."),
                    confidence="high" if ob["street_match"] else "medium",
                    actions=[{"op": "patch", "account_id": o["account_id"],
                              "body": {"duplicate_of_account": surv["account_id"], "status": "Inactive"},
                              "append_note": f"{config.NOTE_TAG} {today}: duplicate of {surv['account_id']} ({loc.name}), "
                                             f"same address {loc.street}, {loc.city}. Merged; use the surviving account."}],
                    evidence={**ev, "reasons": [
                        "Same street address as the surviving record." if ob["street_match"] else "Same name and city as the surviving record.",
                        "No revenue/AR lock, so it can be retired as a duplicate.",
                        f"Website lists this facility as '{loc.name}' under Bellhaven."]},
                    key={"survivor": surv["account_id"]},
                ).finalize())

        # ---- survivor field fixes
        wrong_parent = surv["parent_id"] != parent_id
        if wrong_parent and has_billing_lock(surv):
            body = new_account_body(loc, parent_id,
                                    f"{config.NOTE_TAG} created {today}: change of ownership. Predecessor account "
                                    f"{surv['account_id']} ('{surv['name']}', {surv['parent_name'] or 'no parent'}) "
                                    f"kept for billing. " + offerings_note(loc))
            proposals.append(Proposal(
                kind="chow_new_account", subject=surv["account_id"],
                title=f"CHOW (billing lock): new account for {loc.name}",
                summary=(f"'{surv['name']}' is under {surv['parent_name'] or 'no parent'} but the website lists it as a "
                         f"Bellhaven community. It has revenue ${surv['lifetime_revenue']:,} and outstanding AR "
                         f"${surv['outstanding_ar']:,}, so per SOP: leave it, create a new account under Bellhaven, "
                         f"and set chow_current_account on the old one."),
                confidence="high",
                actions=[
                    {"op": "create", "body": body},
                    *([admin_contact_action(loc, "$created")] if loc.administrator else []),
                    {"op": "patch", "account_id": surv["account_id"],
                     "body": {"chow_current_account": "$created"},
                     "append_note": f"{config.NOTE_TAG} {today}: ownership changed to Bellhaven Senior Living; "
                                    f"account preserved for billing (AR>0). Current account: $created.",
                     "preconditions": {"billing_lock": True, "parent_id": surv["parent_id"]}},
                ],
                evidence={**surv_ev, "reasons": [
                    f"Website address {loc.street}, {loc.city} matches this record.",
                    f"CRM parent is '{surv['parent_name'] or 'none'}', website says Bellhaven.",
                    f"lifetime_revenue={surv['lifetime_revenue']}, outstanding_ar={surv['outstanding_ar']} -> billing lock (SOP)."]},
                key={"new_name": loc.name, "old_parent": surv["parent_id"]},
            ).finalize())
            # do NOT propose any other edits to the old record
            continue

        if wrong_parent:
            proposals.append(Proposal(
                kind="reparent", subject=surv["account_id"],
                title=f"Re-parent: {surv['name']} -> Bellhaven",
                summary=(f"'{surv['name']}' is under {surv['parent_name'] or 'no parent'}; the website lists it as a "
                         f"Bellhaven community. No billing lock (revenue={surv['lifetime_revenue']}, "
                         f"AR={surv['outstanding_ar']}), so move it directly."),
                confidence="high",
                actions=[{"op": "patch", "account_id": surv["account_id"], "body": {"parent_id": parent_id},
                          "append_note": f"{config.NOTE_TAG} {today}: re-parented from "
                                         f"{surv['parent_name'] or 'no parent'} to Bellhaven per website listing {loc.url}.",
                          "preconditions": {"billing_lock": False, "parent_id": surv["parent_id"]}}],
                evidence={**surv_ev, "reasons": [
                    f"Website address {loc.street}, {loc.city} matches this record.",
                    f"CRM parent is '{surv['parent_name'] or 'none'}', website says Bellhaven.",
                    "No revenue+AR lock, so SOP allows a direct parent change."]},
                key={"new_parent": parent_id, "old_parent": surv["parent_id"]},
            ).finalize())

        if surv["name"] != loc.name:
            cosmetic = names_equivalent(surv["name"], loc.name)
            proposals.append(Proposal(
                kind="rename", subject=surv["account_id"],
                title=f"Rename: {surv['name']} -> {loc.name}",
                summary=(f"Website name is '{loc.name}'; CRM has '{surv['name']}'. "
                         + ("Difference is punctuation/abbreviation only." if cosmetic else
                            "The facility appears to have been rebranded.")),
                confidence="high" if (b["street_match"] or cosmetic) else "medium",
                actions=[{"op": "patch", "account_id": surv["account_id"], "body": {"name": loc.name},
                          "append_note": f"{config.NOTE_TAG} {today}: renamed from '{surv['name']}' per website."}],
                evidence={**surv_ev, "reasons": [
                    "Same street address on website and CRM." if b["street_match"] else "Matched by name and city.",
                    "cosmetic difference" if cosmetic else "material rename"]},
                key={"new_name": loc.name},
            ).finalize())

        addr_changes = {}
        if norm_street(surv["billing_street"]) != norm_street(loc.street):
            addr_changes["billing_street"] = loc.street
        if norm_city(surv["billing_city"]) != norm_city(loc.city):
            addr_changes["billing_city"] = loc.city
        if (surv["billing_state"] or "").upper() != loc.state.upper():
            addr_changes["billing_state"] = loc.state
        if norm_zip(surv["billing_zip"]) != norm_zip(loc.zip):
            addr_changes["billing_zip"] = loc.zip
        if addr_changes:
            reasons = [f"CRM: {surv['billing_street']}, {surv['billing_city']}, {surv['billing_state']} {surv['billing_zip']}",
                       f"Website: {loc.street}, {loc.city}, {loc.state} {loc.zip}"]
            if is_po_box(surv["billing_street"]):
                reasons.append("CRM street is a PO Box; website gives the physical street address.")
            proposals.append(Proposal(
                kind="fix_address", subject=surv["account_id"],
                title=f"Fix address: {surv['name']}",
                summary="Update " + ", ".join(f"{k}: '{surv[k]}' -> '{v}'" for k, v in addr_changes.items()),
                confidence="high",
                actions=[{"op": "patch", "account_id": surv["account_id"], "body": addr_changes,
                          "append_note": f"{config.NOTE_TAG} {today}: address corrected per website."}],
                evidence={**surv_ev, "reasons": reasons},
                key=addr_changes,
            ).finalize())

        site_ct = care_types_for(loc)
        if site_ct and surv["care_type"] not in site_ct:
            proposals.append(Proposal(
                kind="fix_care_type", subject=surv["account_id"],
                title=f"Fix care type: {surv['name']}",
                summary=f"CRM care_type '{surv['care_type']}' is not among website offerings {loc.care_offerings}; set to '{site_ct[0]}'.",
                confidence="medium",
                actions=[{"op": "patch", "account_id": surv["account_id"], "body": {"care_type": site_ct[0]},
                          "append_note": f"{config.NOTE_TAG} {today}: care_type set from website. {offerings_note(loc)}"}],
                evidence={**surv_ev, "reasons": ["Website care offerings: " + ", ".join(loc.care_offerings)]},
                key={"care_type": site_ct[0]},
            ).finalize())

        if loc.phone and norm_phone(surv["phone"]) != norm_phone(loc.phone):
            proposals.append(Proposal(
                kind="fix_phone", subject=surv["account_id"],
                title=f"Fix phone: {surv['name']}",
                summary=f"CRM phone '{surv['phone']}' differs from website phone '{loc.phone}'.",
                confidence="medium",
                actions=[{"op": "patch", "account_id": surv["account_id"], "body": {"phone": loc.phone},
                          "append_note": f"{config.NOTE_TAG} {today}: phone updated from website (was '{surv['phone']}')."}],
                evidence={**surv_ev, "reasons": ["Website is the operator's own published number."]},
                key={"phone": loc.phone},
            ).finalize())

        if surv["status"] != "Active":
            proposals.append(Proposal(
                kind="reactivate", subject=surv["account_id"],
                title=f"Reactivate: {surv['name']}",
                summary=f"Account is '{surv['status']}' but the facility is listed live on the website.",
                confidence="medium",
                actions=[{"op": "patch", "account_id": surv["account_id"], "body": {"status": "Active"},
                          "append_note": f"{config.NOTE_TAG} {today}: listed on website {loc.url}; set Active."}],
                evidence={**surv_ev, "reasons": ["Listed on the website."]},
                key={"status": "Active"},
            ).finalize())

        proposals.extend(contact_proposals(loc, surv, contacts_by_acct.get(surv["account_id"], []), today))

    # ---- pass 1b: active contacts stranded on retired duplicate records -> move to the survivor
    for a in accounts:
        if not (a["status"] == "Inactive" and a["duplicate_of_account"]):
            continue
        surv, hops = by_id.get(a["duplicate_of_account"]), 0
        while surv and surv["duplicate_of_account"] and hops < 5:
            surv, hops = by_id.get(surv["duplicate_of_account"]), hops + 1
        if not surv or surv["parent_id"] != parent_id or surv["status"] != "Active":
            continue
        surv_contacts = contacts_by_acct.get(surv["account_id"], [])
        for c in contacts_by_acct.get(a["account_id"], []):
            if not c["is_active"]:
                continue
            already = any(norm_person(x["name"]) == norm_person(c["name"]) for x in surv_contacts)
            proposals.append(Proposal(
                kind="move_contact", subject=surv["account_id"],
                title=f"Move contact: {c['name']} ({c['title']}) -> {surv['name']}",
                summary=(f"'{c['name']}' is an active contact on retired duplicate '{a['name']}' [{a['account_id']}]. "
                         + ("A contact with the same name already exists on the survivor, so deactivate this one."
                            if already else f"Re-point it to the surviving account {surv['account_id']}.")),
                confidence="high",
                actions=[{"op": "patch_contact", "contact_id": c["contact_id"],
                          "body": ({"is_active": False} if already else {"account_id": surv["account_id"]}),
                          "preconditions": {"account_id": a["account_id"], "is_active": True}}],
                evidence={"retired_record": account_brief(a), "surviving_record": account_brief(surv),
                          "contact": contact_brief(c), "crm_contacts": [contact_brief(x) for x in surv_contacts],
                          "reasons": ["Contacts on an Inactive duplicate are invisible to reps working the survivor."]},
                key={"contact": c["contact_id"], "to": surv["account_id"], "dedupe": already},
            ).finalize())

    # ---- pass 2: Bellhaven-parented records that are not on the website
    site_streets = {}
    for a in accounts:
        if is_parent_account(a):
            continue
        site_streets.setdefault((norm_street(a["billing_street"]), norm_city(a["billing_city"])), []).append(a)

    for a in candidates:
        if a["parent_id"] != parent_id or a["account_id"] in claimed:
            continue
        if a["status"] != "Active":
            continue   # already flagged / retired; nothing new to say
        key = (norm_street(a["billing_street"]), norm_city(a["billing_city"]))
        successors = [o for o in site_streets.get(key, []) if o["account_id"] != a["account_id"]
                      and o["parent_id"] and o["parent_id"] != parent_id and o["status"] == "Active"]
        ev = {"account": account_brief(a),
              "contacts": [c["name"] + " (" + c["title"] + ")" for c in contacts_by_acct.get(a["account_id"], [])],
              "website_check": f"No community at '{a['billing_street']}, {a['billing_city']}' or named "
                               f"'{a['name']}' appears on {config.SITE_BASE}/communities as of {today}."}
        if successors:
            succ = successors[0]
            lock = has_billing_lock(a)
            body = {"chow_current_account": succ["account_id"]}
            if not lock:
                body["status"] = "Inactive"
            proposals.append(Proposal(
                kind="chow_outbound", subject=a["account_id"],
                title=f"Sold: {a['name']} -> {succ['parent_name']}",
                summary=(f"'{a['name']}' is no longer on the Bellhaven website and '{succ['name']}' "
                         f"({succ['parent_name']}) exists at the same address. Link chow_current_account to it"
                         + (" and leave the account untouched (revenue+AR billing lock)." if lock
                            else " and mark Inactive (no billing lock).")),
                confidence="high",
                actions=[{"op": "patch", "account_id": a["account_id"], "body": body,
                          "append_note": f"{config.NOTE_TAG} {today}: facility no longer listed by Bellhaven; "
                                         f"now '{succ['name']}' under {succ['parent_name']} ({succ['account_id']}).",
                          "preconditions": {"billing_lock": lock}}],
                evidence={**ev, "successor_record": account_brief(succ), "reasons": [
                    "Not on the Bellhaven website.",
                    f"Another parent ({succ['parent_name']}) has an Active record at the same street address.",
                    f"lifetime_revenue={a['lifetime_revenue']}, outstanding_ar={a['outstanding_ar']} -> "
                    + ("billing lock: parent and status left as-is." if lock else "no lock: mark Inactive.")]},
                key={"successor": succ["account_id"], "lock": lock},
            ).finalize())
        else:
            proposals.append(Proposal(
                kind="flag_missing", subject=a["account_id"],
                title=f"Not on website: {a['name']}",
                summary=(f"'{a['name']}' ({a['billing_city']}, {a['billing_state']}) is under Bellhaven in the CRM but "
                         f"is not listed on the website. No other record explains where it went, so set Needs Review."),
                confidence="medium",
                actions=[{"op": "patch", "account_id": a["account_id"], "body": {"status": "Needs Review"},
                          "append_note": f"{config.NOTE_TAG} {today}: not listed on Bellhaven website; "
                                         f"verify whether closed or sold before further outreach."}],
                evidence={**ev, "reasons": ["Not on the Bellhaven website.",
                                             "No record under another parent at this address.",
                                             "Status set to Needs Review rather than Inactive because closure is unconfirmed."]},
                key={"status": "Needs Review"},
            ).finalize())

    report = {"parent_id": parent_id, "locations": len(locations), "accounts": len(accounts),
              "matches": match_report, "proposals_by_kind": {}}
    for p in proposals:
        report["proposals_by_kind"][p.kind] = report["proposals_by_kind"].get(p.kind, 0) + 1
    return proposals, report
