"""Offline tests for the price_state.json merge. No network, no git writes, no
build_prices import. No arguments runs all tests; --revision main reads the
baseline via git show (and skips where the baseline has no merge at all).
"""
import argparse
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PARSER = argparse.ArgumentParser()
PARSER.add_argument("--revision")
ARGS = PARSER.parse_args()
PATH = "scripts/merge_price_state.py"


def load_merge():
    if ARGS.revision:
        found = subprocess.run(["git", "show", f"{ARGS.revision}:{PATH}"],
                               capture_output=True, encoding="utf-8")
        if found.returncode:
            raise unittest.SkipTest("comparison revision has no state merge")
        source = found.stdout
    else:
        source = Path(PATH).read_text(encoding="utf-8")
    namespace: dict = {"__name__": "merge_price_state"}
    exec(compile(source, PATH, "exec"), namespace)
    return namespace


NS = load_merge()
merge = NS["merge"]


class MergeTests(unittest.TestCase):
    def test_the_2026_09_19_false_alert_cannot_happen_again(self):
        """CI stands by for hours holding a stale success; the phone keeps enriching.

        CI's heartbeat must publish the phone's timestamp, not re-bury it -- this is
        the exact clobber that pinned enrich_success_at at 15:25 and fired the 6h
        detail watchdog one minute after a successful enrich.
        """
        ci = {"enrich_success_at": 1000.0, "enrich_checked_at": 1000.0,
              "last_heartbeat_utc": "2026-09-19T21:25:00+00:00", "enrich_offset": 1018}
        phone = {"enrich_success_at": 22_000.0, "enrich_checked_at": 22_000.0,
                 "enrich_offset": 1008}
        out = merge(ci, phone)
        self.assertEqual(out["enrich_success_at"], 22_000.0)
        self.assertEqual(out["enrich_checked_at"], 22_000.0)
        # CI's own bookkeeping still lands.
        self.assertEqual(out["enrich_offset"], 1018)
        self.assertEqual(out["last_heartbeat_utc"], "2026-09-19T21:25:00+00:00")

    def test_a_delivered_alert_is_never_forgotten(self):
        """The phone's copy predates the ping CI just sent. Dropping the marker
        would re-send the same digest next cycle instead of once a day."""
        phone = {"enrich_success_at": 500.0}
        ci = {"enrich_success_at": 400.0, "last_enrich_alert_at": 900.0}
        self.assertEqual(merge(phone, ci)["last_enrich_alert_at"], 900.0)
        self.assertEqual(merge(ci, phone)["last_enrich_alert_at"], 900.0)

    def test_success_never_moves_backwards(self):
        for ours, theirs in (({"enrich_success_at": 5.0}, {"enrich_success_at": 9.0}),
                             ({"enrich_success_at": 9.0}, {"enrich_success_at": 5.0})):
            self.assertEqual(merge(ours, theirs)["enrich_success_at"], 9.0)

    def test_a_cleared_error_is_not_resurrected(self):
        """The phone checked later and saw a healthy enrich; CI's older copy still
        carries the error. Half-merging would report an error nobody is seeing."""
        ci = {"enrich_checked_at": 100.0, "enrich_error": "Timeout",
              "enrich_error_since": 90.0}
        phone = {"enrich_checked_at": 500.0, "enrich_success_at": 500.0}
        out = merge(ci, phone)
        self.assertNotIn("enrich_error", out)
        self.assertNotIn("enrich_error_since", out)

    def test_the_skip_reason_follows_the_latest_look_at_steam(self):
        """enrich_last_skip explains a frozen ring, so a stale copy must not
        overwrite it with an older story -- or blank it after a recovery."""
        phone = {"enrich_checked_at": 500.0, "enrich_last_skip": "probe HTTP 429 (signals=4)"}
        ci = {"enrich_checked_at": 100.0, "enrich_last_skip": "PRICES_NOENRICH set"}
        self.assertEqual(merge(ci, phone)["enrich_last_skip"], "probe HTTP 429 (signals=4)")
        recovered = {"enrich_checked_at": 900.0, "enrich_success_at": 900.0}
        self.assertNotIn("enrich_last_skip", merge(recovered, phone))

    def test_a_fresh_error_survives_a_stale_healthy_copy(self):
        phone = {"enrich_checked_at": 500.0, "enrich_error": "ConnectionError",
                 "enrich_error_since": 480.0}
        ci = {"enrich_checked_at": 100.0}
        out = merge(ci, phone)
        self.assertEqual(out["enrich_error"], "ConnectionError")
        self.assertEqual(out["enrich_error_since"], 480.0)

    def test_committers_own_bookkeeping_wins(self):
        """Cooldown/flags describe the run that is committing right now."""
        ours = {"consecutive_failures": 2, "cooldown_until": 9_000.0,
                "last_error": "HTTP 429", "unlocked3_alerted": True}
        theirs = {"consecutive_failures": 0, "cooldown_until": 0, "enrich_offset": 7}
        out = merge(ours, theirs)
        self.assertEqual(out["consecutive_failures"], 2)
        self.assertEqual(out["cooldown_until"], 9_000.0)
        self.assertTrue(out["unlocked3_alerted"])
        self.assertEqual(out["enrich_offset"], 7)   # only they had it -> kept

    def test_keys_only_the_other_side_has_are_kept(self):
        out = merge({"a": 1}, {"b": 2})
        self.assertEqual(out, {"a": 1, "b": 2})

    def test_a_malformed_timestamp_never_wins(self):
        """A hand-edited or half-written field must not beat a good one, and must
        not crash the commit step either."""
        out = merge({"enrich_success_at": 100.0}, {"enrich_success_at": "oops"})
        self.assertEqual(out["enrich_success_at"], 100.0)
        out = merge({"enrich_success_at": "oops"}, {"enrich_success_at": 100.0})
        self.assertEqual(out["enrich_success_at"], "oops")   # ours, not a crash

    def test_missing_on_both_sides_stays_missing(self):
        self.assertNotIn("enrich_success_at", merge({}, {}))

    def test_cli_writes_the_compact_form_over_theirs(self):
        """_write_state uses separators=(",", ":"); a merge must not reformat the
        file into a whole-file diff."""
        with tempfile.TemporaryDirectory() as tmp:
            ours = Path(tmp) / "ours.json"
            theirs = Path(tmp) / "theirs.json"
            ours.write_text(json.dumps({"enrich_success_at": 1.0, "cooldown_until": 5}),
                            encoding="utf-8")
            theirs.write_text(json.dumps({"enrich_success_at": 9.0}), encoding="utf-8")
            self.assertEqual(NS["main"](["merge_price_state.py", str(ours), str(theirs)]), 0)
            text = theirs.read_text(encoding="utf-8")
            self.assertNotIn(", ", text)
            self.assertNotIn(": ", text)
            self.assertEqual(json.loads(text),
                             {"enrich_success_at": 9.0, "cooldown_until": 5})

    def test_cli_handles_a_first_ever_commit(self):
        """A clone with no state file yet must still produce ours, not crash."""
        with tempfile.TemporaryDirectory() as tmp:
            ours = Path(tmp) / "ours.json"
            theirs = Path(tmp) / "absent.json"
            ours.write_text(json.dumps({"enrich_success_at": 1.0}), encoding="utf-8")
            self.assertEqual(NS["main"](["merge_price_state.py", str(ours), str(theirs)]), 0)
            self.assertEqual(json.loads(theirs.read_text(encoding="utf-8")),
                             {"enrich_success_at": 1.0})


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]], verbosity=2)
