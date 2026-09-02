"""Fail-closed behaviour: preconditions at apply time, scrape sanity guard, and queue idempotency."""
import os
import tempfile
import unittest
from datetime import date

from bellhaven_sync import config
from bellhaven_sync.apply import apply_actions, PreconditionFailed
from bellhaven_sync.pipeline import guard_scrape, ScrapeSanityError
from bellhaven_sync.store import Store
from bellhaven_sync.matcher import build_proposals
from tests.test_matcher import PARENT, acct, loc


class FakeCRM:
    def __init__(self, accounts):
        self.accounts = {a["account_id"]: dict(a) for a in accounts}
        self.writes = []

    def get_account(self, aid):
        return dict(self.accounts[aid])

    def update_account(self, aid, body):
        self.writes.append(("patch", aid, body))
        self.accounts[aid].update(body)
        return {"account_id": aid, "message": "updated"}

    def create_account(self, body):
        self.writes.append(("create", body))
        new = dict(PARENT, **body, account_id="NEW1")
        self.accounts["NEW1"] = new
        return {"account_id": "NEW1"}


class ApplyTests(unittest.TestCase):
    def test_chow_creates_then_links_and_appends_note(self):
        old = acct(parent_id="X", parent_name="Other", lifetime_revenue=10, outstanding_ar=5, note="keep me")
        crm = FakeCRM([PARENT, old])
        actions = [{"op": "create", "body": {"name": "New"}},
                   {"op": "patch", "account_id": "A1", "body": {"chow_current_account": "$created"},
                    "append_note": "linked to $created", "preconditions": {"billing_lock": True, "parent_id": "X"}}]
        res = apply_actions(crm, actions)
        self.assertEqual(res["created_account_id"], "NEW1")
        self.assertEqual(crm.accounts["A1"]["chow_current_account"], "NEW1")
        self.assertEqual(crm.accounts["A1"]["note"], "keep me | linked to NEW1")
        self.assertEqual(crm.accounts["A1"]["parent_id"], "X")   # untouched

    def test_billing_lock_appearing_later_blocks_reparent(self):
        # proposal said "no lock"; billing posted AR since -> must not re-parent
        live = acct(parent_id="X", lifetime_revenue=10, outstanding_ar=99)
        crm = FakeCRM([PARENT, live])
        actions = [{"op": "patch", "account_id": "A1", "body": {"parent_id": "P1"},
                    "preconditions": {"billing_lock": False, "parent_id": "X"}}]
        with self.assertRaises(PreconditionFailed):
            apply_actions(crm, actions)
        self.assertEqual(crm.writes, [])

    def test_parent_changed_since_proposal_blocks(self):
        live = acct(parent_id="SOMEONE_ELSE")
        crm = FakeCRM([PARENT, live])
        actions = [{"op": "patch", "account_id": "A1", "body": {"parent_id": "P1"},
                    "preconditions": {"billing_lock": False, "parent_id": "X"}}]
        with self.assertRaises(PreconditionFailed):
            apply_actions(crm, actions)
        self.assertEqual(crm.writes, [])


class GuardTests(unittest.TestCase):
    def test_empty_scrape_aborts(self):
        accounts = [PARENT] + [acct(account_id=f"A{i}", billing_street=f"{i} Main St") for i in range(10)]
        with self.assertRaises(ScrapeSanityError):
            guard_scrape([], accounts)
        with self.assertRaises(ScrapeSanityError):
            guard_scrape([loc()] * 3, accounts)      # 3 < 50% of 10
        guard_scrape([loc()] * 6, accounts)          # fine

    def test_missing_fields_abort(self):
        with self.assertRaises(ScrapeSanityError):
            guard_scrape([loc(zip="")], [PARENT])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = Store(self.tmp.name)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _props(self):
        a = acct(parent_id="X", parent_name="Other (Parent Account)")
        props, _ = build_proposals([loc()], [PARENT, a], [], date.today().isoformat())
        return props

    def test_rejected_is_never_requeued_and_pending_is_not_duplicated(self):
        p = self._props()
        self.assertEqual(self.store.upsert_proposals(p, 1)["new"], 1)
        self.assertEqual(self.store.upsert_proposals(p, 2)["still_pending"], 1)
        self.store.set_status(p[0].fingerprint, "rejected")
        c = self.store.upsert_proposals(p, 3)
        self.assertEqual((c["new"], c["already_decided"]), (0, 1))
        self.assertEqual(self.store.counts(), {"rejected": 1})

    def test_disappearing_proposal_goes_stale_then_revives(self):
        p = self._props()
        self.store.upsert_proposals(p, 1)
        self.assertEqual(self.store.upsert_proposals([], 2)["stale"], 1)
        self.assertEqual(self.store.upsert_proposals(p, 3)["revived"], 1)

    def test_fingerprint_ignores_date_in_note(self):
        a = acct(parent_id="X", parent_name="Other (Parent Account)")
        p1, _ = build_proposals([loc()], [PARENT, a], [], "2026-01-01")
        p2, _ = build_proposals([loc()], [PARENT, a], [], "2026-06-30")
        self.assertEqual(p1[0].fingerprint, p2[0].fingerprint)


if __name__ == "__main__":
    unittest.main()
