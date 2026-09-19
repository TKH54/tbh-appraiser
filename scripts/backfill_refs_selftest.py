"""Offline tests for the ref backfill's planning rules. No network, no data
files touched: only `plan`, which is the part that decides what gets a ref.
No arguments runs all tests.
"""
import importlib.util
import sys
import unittest
from pathlib import Path

PATH = Path("scripts/backfill_refs.py")
spec = importlib.util.spec_from_file_location("backfill_refs", PATH)
MOD = importlib.util.module_from_spec(spec)
spec.loader.exec_module(MOD)
plan = MOD.plan


def item(base, icon, rarity=""):
    return {"base": base, "icon": icon, "rarity": rarity}


class PlanTests(unittest.TestCase):
    def test_picks_up_a_base_that_has_no_ref(self):
        items = {"Mithril Ore": item("Mithril Ore", "icon-mithril")}
        self.assertEqual(plan(items, []), [("Mithril Ore", "icon-mithril")])

    def test_leaves_bases_that_already_have_one(self):
        items = {"Empire Gloves": item("Empire Gloves", "icon-gloves")}
        refs = [{"base": "Empire Gloves", "icon": "sprite_000.png"}]
        self.assertEqual(plan(items, refs), [])

    def test_one_ref_per_base_not_per_variant(self):
        """Buckler ships four grades off one sprite; four identical refs would
        make the matcher's top-1 between them a coin flip."""
        items = {f"Buckler ({g}) C": item("Buckler", "icon-buckler", g)
                 for g in ("Legendary", "Immortal", "Arcana", "Beyond")}
        self.assertEqual(plan(items, []), [("Buckler", "icon-buckler")])

    def test_artwork_already_referenced_under_another_base_is_skipped(self):
        """Adding a second ref holding the identical vector can only muddy the
        ranking between the two bases -- the same rule autocatalog packs by."""
        items = {"Ember Gem": item("Ember Gem", "icon-amber")}
        refs = [{"base": "Amber Gem", "icon": "icon-amber"}]
        self.assertEqual(plan(items, refs), [])

    def test_two_new_bases_sharing_one_sprite_get_one_ref(self):
        items = {"A": item("A", "shared"), "B": item("B", "shared")}
        self.assertEqual(plan(items, []), [("A", "shared")])

    def test_an_entry_with_no_icon_is_left_alone(self):
        """Nothing to fetch, and a base with no artwork is a data problem to
        look at, not something to paper over with a guess."""
        items = {"Ghost": {"base": "Ghost", "rarity": ""}}
        self.assertEqual(plan(items, []), [])

    def test_order_follows_the_catalog(self):
        items = {"Z": item("Z", "iz"), "A": item("A", "ia")}
        self.assertEqual(plan(items, []), [("Z", "iz"), ("A", "ia")])


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]], verbosity=2)
