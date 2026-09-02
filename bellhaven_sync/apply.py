"""Execute an approved proposal against the CRM.

Every action is re-validated against the *live* account right before writing, so a
proposal that was generated yesterday cannot silently do the wrong thing today:
  - billing_lock precondition: re-checks lifetime_revenue / outstanding_ar (the SOP)
  - parent_id precondition: the account still has the parent we thought it had
Notes are appended to the live note field, never overwritten.
"""
from typing import Dict, List

from . import config
from .crm import CRM, CRMError
from .matcher import has_billing_lock


class PreconditionFailed(RuntimeError):
    pass


def _check_preconditions(acct: Dict, pre: Dict) -> None:
    if "billing_lock" in pre and has_billing_lock(acct) != pre["billing_lock"]:
        raise PreconditionFailed(
            f"billing lock changed on {acct['account_id']} (revenue={acct['lifetime_revenue']}, "
            f"AR={acct['outstanding_ar']}); re-run the pipeline to regenerate this proposal")
    if "parent_id" in pre and (acct.get("parent_id") or "") != (pre["parent_id"] or ""):
        raise PreconditionFailed(
            f"parent of {acct['account_id']} changed since proposal ({acct.get('parent_id')!r}); re-run the pipeline")


def _append_note(existing: str, addition: str) -> str:
    existing = (existing or "").strip()
    if not addition:
        return existing
    return (existing + " | " if existing else "") + addition


def apply_actions(crm: CRM, actions: List[Dict]) -> Dict:
    created_id = None
    log = []
    for act in actions:
        if act["op"] == "create":
            body = dict(act["body"])
            res = crm.create_account(body)
            created_id = res.get("account_id") or (res.get("data") or {}).get("account_id")
            if not created_id:
                raise CRMError(f"create returned no account_id: {res}")
            log.append({"op": "create", "account_id": created_id, "name": body.get("name")})
        elif act["op"] == "patch":
            acct_id = act["account_id"]
            live = crm.get_account(acct_id)
            _check_preconditions(live, act.get("preconditions") or {})
            body = {k: (created_id if v == "$created" else v) for k, v in act["body"].items()}
            if any(v == "$created" for v in act["body"].values()) and not created_id:
                raise CRMError("patch references $created but no account was created")
            note_add = (act.get("append_note") or "").replace("$created", created_id or "?")
            if note_add:
                body["note"] = _append_note(live.get("note", ""), note_add)
            res = crm.update_account(acct_id, body)
            log.append({"op": "patch", "account_id": acct_id, "fields": sorted(body.keys()), "response": res})
        elif act["op"] == "create_contact":
            body = {k: (created_id if v == "$created" else v) for k, v in act["body"].items()}
            if any(v == "$created" for v in act["body"].values()) and not created_id:
                raise CRMError("create_contact references $created but no account was created")
            res = crm.create_contact(body)
            cid = res.get("contact_id") or (res.get("data") or {}).get("contact_id")
            log.append({"op": "create_contact", "contact_id": cid, "name": body.get("name"), "account_id": body.get("account_id")})
        elif act["op"] == "patch_contact":
            live = crm.get_contact(act["contact_id"])
            pre = act.get("preconditions") or {}
            if "account_id" in pre and live.get("account_id") != pre["account_id"]:
                raise PreconditionFailed(f"contact {act['contact_id']} moved since proposal; re-run the pipeline")
            if "is_active" in pre and bool(live.get("is_active")) != pre["is_active"]:
                raise PreconditionFailed(f"contact {act['contact_id']} active flag changed since proposal; re-run the pipeline")
            res = crm.update_contact(act["contact_id"], act["body"])
            log.append({"op": "patch_contact", "contact_id": act["contact_id"], "fields": sorted(act["body"].keys()), "response": res})
        else:
            raise CRMError(f"unknown op {act['op']}")
    return {"ok": True, "created_account_id": created_id, "log": log}


def apply_proposal(store, crm: CRM, fingerprint: str, by: str = "reviewer") -> Dict:
    """Mark approved, then execute. Returns the result dict; status becomes applied/failed."""
    p = store.get(fingerprint)
    if not p:
        return {"ok": False, "error": "unknown proposal"}
    if p["status"] in ("applied",):
        return {"ok": False, "error": "already applied"}
    store.set_status(fingerprint, "approved", by=by)
    try:
        result = apply_actions(crm, p["actions"])
        store.set_status(fingerprint, "applied", result=result)
        return result
    except (CRMError, PreconditionFailed, Exception) as e:  # noqa: BLE001
        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        store.set_status(fingerprint, "failed", result=result)
        return result
