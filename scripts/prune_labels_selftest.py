"""Offline tests for the prune's safety gates. Extracts main() with AST so
promote_labels, Supabase and the snapshot file are never touched; every fake is
in memory. No arguments runs all tests; --revision main reads the baseline via
git show (and skips where the baseline predates the checks).
"""
import argparse
import ast
import json
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

PARSER = argparse.ArgumentParser()
PARSER.add_argument("--revision")
ARGS = PARSER.parse_args()
PATH = "scripts/prune_labels.py"
SOURCE = (subprocess.check_output(["git", "show", f"{ARGS.revision}:{PATH}"], encoding="utf-8")
          if ARGS.revision else Path(PATH).read_text(encoding="utf-8"))
TREE = ast.parse(SOURCE)


def run(*, table, snapshot, keep=5, dry_run=False, on_delete=None):
    """Run main() against an in-memory table.

    `on_delete(rows, lo, hi)` replaces the delete's effect, which is how a
    silent no-op, an over-delete and a concurrent insert are staged. The default
    is what a PostgREST range DELETE really does.
    """
    defs = [n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name == "main"]
    if not defs:
        raise unittest.SkipTest("revision has no main()")
    rows, calls, out, err = list(table), [], [], []

    def apply_range(rows, lo, hi):
        for i in [r for r in rows if lo <= r <= hi]:
            rows.remove(i)

    def fake_delete(url, headers):
        lo = int(url.split("id=gte.")[1].split("&")[0])
        hi = int(url.split("id=lte.")[1].split("&")[0])
        calls.append((lo, hi))
        (on_delete or apply_range)(rows, lo, hi)

    def fake_print(*a, **kw):
        (err if kw.get("file") == "ERR" else out).append(" ".join(str(x) for x in a))

    ns = dict(
        argparse=SimpleNamespace(ArgumentParser=lambda: SimpleNamespace(
            add_argument=lambda *a, **kw: None,
            parse_args=lambda: SimpleNamespace(keep=keep, dry_run=dry_run))),
        os=SimpleNamespace(environ={"SUPABASE_SERVICE_KEY": "k",
                                   "LABELS_SNAPSHOT": "snap.jsonl.gz"}),
        sys=SimpleNamespace(stderr="ERR"),
        json=json, Path=Path, DEFAULT_URL="https://db.example",
        fetch_rows=lambda url, key: None,
        _snapshot_load=lambda p: [{"id": i} for i in snapshot],
        _table_ids=lambda url, headers: sorted(rows),
        _delete=fake_delete, CHUNK=5000, print=fake_print,
    )
    exec(compile(ast.Module(body=defs, type_ignores=[]), PATH, "exec"), ns)
    return SimpleNamespace(code=ns["main"](), rows=sorted(rows), calls=calls,
                           out=out, err=err)


class PruneTests(unittest.TestCase):
    def test_a_label_arriving_mid_prune_is_not_a_failure(self):
        """The 2026-10-05 regression, reproduced: a visitor corrects an item while
        the delete runs, so the table ends one row above the plan. The old check
        compared totals and failed -- and because `bash -e` stops the job here,
        it threw away a promotion whose gates had already passed (the release
        bump, the PR and the auto-merge all sit downstream of this step)."""
        def delete_then_insert(rows, lo, hi):
            for i in [r for r in rows if lo <= r <= hi]:
                rows.remove(i)
            rows.append(max(rows) + 1)      # new label, id above the keep window
        r = run(table=range(1, 11), snapshot=range(1, 11), keep=5,
                on_delete=delete_then_insert)
        self.assertEqual(r.code, 0, f"stderr: {r.err}")
        self.assertEqual(r.rows, [6, 7, 8, 9, 10, 11])

    def test_a_delete_that_silently_did_nothing_fails(self):
        """PostgREST has silently no-op'd a request before (the 1000-row cap).
        Nothing downstream would notice, while the table keeps growing toward the
        50k trigger that blocks collection entirely."""
        r = run(table=range(1, 11), snapshot=range(1, 11), keep=5,
                on_delete=lambda rows, lo, hi: None)
        self.assertEqual(r.code, 1)
        self.assertTrue(any("still there" in m for m in r.err), r.err)

    def test_a_partial_delete_fails(self):
        """Half the planned rows gone is still a broken prune, not a small one."""
        def half(rows, lo, hi):
            for i in [r for r in rows if lo <= r <= hi][:2]:
                rows.remove(i)
        r = run(table=range(1, 11), snapshot=range(1, 11), keep=5, on_delete=half)
        self.assertEqual(r.code, 1)
        self.assertTrue(any("still there" in m for m in r.err), r.err)

    def test_rows_outside_the_plan_vanishing_fails(self):
        """Coverage was only ever checked for `to_del`, so rows beyond it may not
        be archived anywhere. Losing them must be loud even though the direction
        looks like 'extra tidy'."""
        def overshoot(rows, lo, hi):
            for i in [r for r in rows if lo <= r <= hi + 2]:
                rows.remove(i)
        r = run(table=range(1, 11), snapshot=range(1, 11), keep=5, on_delete=overshoot)
        self.assertEqual(r.code, 1)
        self.assertTrue(any("outside the plan" in m for m in r.err), r.err)

    def test_one_survivor_plus_one_overshoot_is_still_caught(self):
        """The case a total-count check cannot see: the two errors cancel out, so
        the old `remain == expected` test passed while a row we never verified as
        archived was gone AND a row we meant to delete was still there."""
        def skip_one_take_one_extra(rows, lo, hi):
            doomed = [r for r in rows if lo <= r <= hi]
            for i in doomed[1:]:                     # leave doomed[0] behind
                rows.remove(i)
            rows.remove(max(rows))                   # and take one from the keep set
        r = run(table=range(1, 11), snapshot=range(1, 11), keep=5,
                on_delete=skip_one_take_one_extra)
        self.assertEqual(len(r.rows), 5)             # total looks exactly right
        self.assertEqual(r.code, 1)
        self.assertTrue(any("still there" in m for m in r.err), r.err)

    def test_an_insert_cannot_mask_a_kept_row_going_missing(self):
        """Codex found this hole in the first version of the fix: compare the kept
        side by COUNT and two fresh labels pay for one row we were told to keep.
        The total clears, and a row the snapshot was never checked against is gone
        for good. Both sides are compared as id sets for this reason."""
        def lose_one_kept_gain_two(rows, lo, hi):
            for i in [r for r in rows if lo <= r <= hi]:
                rows.remove(i)
            rows.remove(min(rows))                   # a row we promised to keep
            rows.extend([max(rows) + 1, max(rows) + 2])
        r = run(table=range(1, 11), snapshot=range(1, 11), keep=5,
                on_delete=lose_one_kept_gain_two)
        self.assertGreaterEqual(len(r.rows), 5)      # total looks fine
        self.assertEqual(r.code, 1)
        self.assertTrue(any("outside the plan" in m for m in r.err), r.err)

    def test_the_healthy_prune_keeps_exactly_the_newest_rows(self):
        r = run(table=range(1, 11), snapshot=range(1, 11), keep=5)
        self.assertEqual(r.code, 0, f"stderr: {r.err}")
        self.assertEqual(r.rows, [6, 7, 8, 9, 10])
        self.assertEqual(r.calls, [(1, 5)])

    def test_nothing_to_prune_is_success_and_touches_nothing(self):
        r = run(table=range(1, 4), snapshot=range(1, 4), keep=5)
        self.assertEqual(r.code, 0)
        self.assertEqual(r.calls, [])
        self.assertEqual(r.rows, [1, 2, 3])

    def test_unarchived_rows_abort_before_any_delete(self):
        """The coverage gate: never delete a row the snapshot does not hold."""
        r = run(table=range(1, 11), snapshot=range(4, 11), keep=5)
        self.assertEqual(r.code, 1)
        self.assertEqual(r.calls, [])
        self.assertEqual(len(r.rows), 10)
        self.assertTrue(any("ABORT" in m for m in r.err), r.err)

    def test_an_empty_snapshot_refuses_to_prune(self):
        r = run(table=range(1, 11), snapshot=[], keep=5)
        self.assertEqual(r.code, 1)
        self.assertEqual(r.calls, [])

    def test_dry_run_deletes_nothing(self):
        r = run(table=range(1, 11), snapshot=range(1, 11), keep=5, dry_run=True)
        self.assertEqual(r.code, 0)
        self.assertEqual(r.calls, [])
        self.assertEqual(len(r.rows), 10)


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]], verbosity=2)
