import unittest
from datetime import date

from bellhaven_sync.normalize import norm_street, name_similarity, names_equivalent
from bellhaven_sync.matcher import build_proposals, has_billing_lock
from bellhaven_sync.scraper import Location

PARENT = {"account_id": "P1", "name": "Bellhaven Senior Living (Parent Account)", "parent_id": "", "parent_name": "",
          "billing_street": "", "billing_city": "", "billing_state": "", "billing_zip": "", "care_type": "",
          "status": "Active", "phone": "", "lifetime_revenue": 0, "outstanding_ar": 0,
          "chow_current_account": "", "duplicate_of_account": "", "note": ""}


def acct(**kw):
    a = dict(PARENT, account_id="A1", name="Bellhaven of Testville", parent_id="P1",
             parent_name="Bellhaven Senior Living (Parent Account)", billing_street="1 Main St",
             billing_city="Testville", billing_state="OH", billing_zip="44000", care_type="Assisted Living",
             phone="(555) 000-0000")
    a.update(kw)
    return a


def loc(**kw):
    d = dict(slug="bellhaven-of-testville", name="Bellhaven of Testville", street="1 Main Street", city="Testville",
             state="OH", zip="44000", care_offerings=["Assisted Living"], phone="(555) 000-0000", url="u")
    d.update(kw)
    return Location(**d)


def kinds(props):
    return sorted(p.kind for p in props)


class NormalizeTests(unittest.TestCase):
    def test_street_abbreviations(self):
        self.assertEqual(norm_street("1125 Logan Boulevard"), norm_street("1125 Logan Blvd"))
        self.assertEqual(norm_street("1250 Northwest Franklin St"), norm_street("1250 NW Franklin Street"))

    def test_name_equivalence(self):
        self.assertTrue(names_equivalent("Bellhaven Rehab and Nursing of Grove City",
                                         "Bellhaven Rehabilitation & Nursing of Grove City"))
        self.assertTrue(names_equivalent("Bellhaven Health Care Center of Ashland",
                                         "Bellhaven Healthcare Centre of Ashland"))
        self.assertFalse(names_equivalent("Bellhaven at Union Square", "Union Square Senior Living"))
        self.assertLess(name_similarity("Bellhaven at Union Square", "Union Square Senior Living"), 0.9)


class MatcherTests(unittest.TestCase):
    today = date.today().isoformat()

    def test_clean_match_proposes_nothing(self):
        props, _ = build_proposals([loc()], [PARENT, acct()], [], self.today)
        self.assertEqual(kinds(props), [])

    def test_reparent_without_billing_lock(self):
        a = acct(parent_id="X", parent_name="Other (Parent Account)", lifetime_revenue=50000, outstanding_ar=0)
        props, _ = build_proposals([loc()], [PARENT, a], [], self.today)
        self.assertEqual(kinds(props), ["reparent"])
        self.assertEqual(props[0].actions[0]["body"], {"parent_id": "P1"})

    def test_billing_lock_creates_new_account_and_chow(self):
        a = acct(parent_id="X", parent_name="Other (Parent Account)", lifetime_revenue=50000, outstanding_ar=100,
                 phone="(999) 999-9999", name="Old Name")
        self.assertTrue(has_billing_lock(a))
        props, _ = build_proposals([loc()], [PARENT, a], [], self.today)
        self.assertEqual(kinds(props), ["chow_new_account"])   # and no rename/phone edits on the old record
        ops = [x["op"] for x in props[0].actions]
        self.assertEqual(ops, ["create", "patch"])
        self.assertEqual(props[0].actions[1]["body"], {"chow_current_account": "$created"})
        self.assertNotIn("parent_id", props[0].actions[1]["body"])

    def test_same_name_other_state_is_a_different_building(self):
        a = acct(billing_state="CO", billing_city="Denver", billing_zip="80000", billing_street="9 Elm St")
        props, _ = build_proposals([loc()], [PARENT, a], [], self.today)
        self.assertEqual(kinds(props), ["create_account", "flag_missing"])

    def test_duplicates_pick_bellhaven_survivor(self):
        a = acct()
        b = acct(account_id="A2", name="Harborview of Testville", parent_id="H", parent_name="Harborview Care Group (Parent Account)")
        props, _ = build_proposals([loc()], [PARENT, a, b], [], self.today)
        dups = [p for p in props if p.kind == "mark_duplicate"]
        self.assertEqual(len(dups), 1)
        self.assertEqual(dups[0].subject, "A2")
        self.assertEqual(dups[0].actions[0]["body"]["duplicate_of_account"], "A1")

    def test_sold_facility_links_outbound_and_respects_lock(self):
        a = acct(lifetime_revenue=1000, outstanding_ar=10)
        succ = acct(account_id="M1", name="Millstone of Testville", parent_id="M", parent_name="Millstone (Parent Account)")
        props, _ = build_proposals([], [PARENT, a, succ], [], self.today)
        self.assertEqual(kinds(props), ["chow_outbound"])
        self.assertEqual(props[0].actions[0]["body"], {"chow_current_account": "M1"})  # no status change

    def test_missing_with_no_successor_needs_review(self):
        props, _ = build_proposals([], [PARENT, acct()], [], self.today)
        self.assertEqual(kinds(props), ["flag_missing"])
        self.assertEqual(props[0].actions[0]["body"], {"status": "Needs Review"})


if __name__ == "__main__":
    unittest.main()
