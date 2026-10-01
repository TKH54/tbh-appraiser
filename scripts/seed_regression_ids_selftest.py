"""Offline checks for seed_regression's label-by-label gate support. No Supabase.

The autocatalog gate measures confident mis-IDs before and after appending refs,
~35 min apart. Two flags make that comparison exact (2026-10-01):

  * --mis-out writes every label id measured and the ids that came out MIS
  * --only-ids restricts a later pass to a previous pass's label ids, so labels
    arriving in between (or labels of the base being added) cannot change the
    set, and "new MIS" = after.mis - before.mis means what it says

Steam, Supabase and the real refs are stubbed; resolve_all and the CLI are real.

    python scripts/seed_regression_ids_selftest.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import seed_regression as sr

FAILS: list[str] = []


def ck(cond, msg):
    print(("  ok    " if cond else "  FAIL  ") + msg)
    if not cond:
        FAILS.append(msg)


def main() -> int:
    items = json.loads((sr.DATA / "items.json").read_text(encoding="utf-8"))
    a, b = sorted({v["base"] for v in items.values()})[:2]     # two real catalog bases
    ones = np.ones(1024, dtype=bool)
    sigs = {"zero": (np.zeros(3072, dtype=np.float32), ones),
            "half": (np.full(3072, 0.5, dtype=np.float32), ones)}
    V = np.stack([np.zeros(3072, dtype=np.float32), np.ones(3072, dtype=np.float32)])
    rows = [
        {"id": 1, "base": a, "sig": "zero"},          # resolves to a: correct
        {"id": 2, "base": b, "sig": "zero"},          # resolves to a: MIS
        {"id": 3, "base": a, "sig": "half"},          # too far from both: '?'
        {"id": 4, "base": "Not A Catalog Base", "sig": "zero"},   # filtered out
    ]
    sr.fetch_rows = lambda url, key: list(rows)
    sr.unpack_sig = lambda s: sigs.get(s)
    sr.load_refs = lambda: []
    sr.load_seed = lambda p: []
    sr.stack = lambda entries: ([a, b], V, np.ones((2, 1024), dtype=bool))
    os.environ["SUPABASE_SERVICE_KEY"] = "offline-test"

    def run(*extra):
        old = sys.argv
        sys.argv = ["seed_regression.py", "--bar", "0.075", *extra]
        try:
            sr.main()
        finally:
            sys.argv = old

    with tempfile.TemporaryDirectory() as t:
        before, after = Path(t) / "before.json", Path(t) / "after.json"
        run("--mis-out", str(before))
        got = json.loads(before.read_text(encoding="utf-8"))
        ck(got == {"ids": [1, 2, 3], "mis": [2]},
           f"--mis-out lists every measured id and the MIS ones: {got}")

        # a crowd label arrives between the passes, and it is a MIS
        rows.append({"id": 5, "base": b, "sig": "zero"})
        run("--only-ids", str(before), "--mis-out", str(after))
        got = json.loads(after.read_text(encoding="utf-8"))
        ck(got["ids"] == [1, 2, 3], f"--only-ids keeps the first pass's label set: {got['ids']}")
        ck(set(got["mis"]) - {2} == set(), "a label that arrived later cannot count as new MIS")

        # labels without ids (offset-paging fallback) -> no file, never a fake pass
        rows[:] = [{"base": a, "sig": "zero"}]
        lost = Path(t) / "noids.json"
        run("--mis-out", str(lost))
        ck(not lost.exists(), "labels without ids write no --mis-out (gate reads unmeasured)")

    print("\nFAILED" if FAILS else "\nall checks passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
