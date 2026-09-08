"""Offline checks for enrich_shard's two-ring rotation. No network, no game.

The price bot spends a fixed request budget per 10-minute cycle: a few slots on
the items the most money moves through (the "hot ring") and the rest on the
plain rotation that guarantees every item is eventually refreshed. Getting the
two offsets wrong is the kind of bug that shows up as a median quietly going
stale weeks later, so the invariants are pinned here:

  * the hot ring is ordered by unit x 24h volume, richest first
  * a cycle spends exactly `shard` requests, hot slots first
  * `_eoff` advances by LAP items only and `_hoff` by HOT items only, so a
    cycle cut short by throttling resumes both rings where it stopped
  * the lap still visits every item (adding the hot ring must not create a
    starved tail)
  * PRICES_HOT=0 is exactly the old pure lap

    python scripts/enrich_selftest.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_prices as bp

CALLS: list[str] = []


def fake_get(url, **kw):
    CALLS.append(kw["market_hash_name"])
    return {"success": True, "median_price": "$1.00", "volume": "10"}


def items(n=100):
    """item<i> has median i and volume i, so turnover ranks with i."""
    return {f"item{i:03d}": {"usd": 1.0, "q": 1, "m": float(i), "v": i}
            for i in range(n)}


def run(prev, **env):
    for k, v in env.items():
        os.environ[k] = str(v)
    CALLS.clear()
    off, hoff = bp.enrich_shard(items(), prev, time.time())
    return list(CALLS), off, hoff


FAILS: list[str] = []


def ck(cond, msg):
    print(("  ok    " if cond else "  FAIL  ") + msg)
    if not cond:
        FAILS.append(msg)


def main() -> int:
    bp.get = fake_get
    bp.time.sleep = lambda s: None
    os.environ["PRICES_DELAY"] = "0"

    hot = bp.hot_ring(items(), 5)
    ck(hot == [f"item{i:03d}" for i in (99, 98, 97, 96, 95)],
       f"hot ring is richest-first: {hot}")

    calls, off, hoff = run({}, PRICES_SHARD=12, PRICES_HOT=2, PRICES_HOT_POOL=24)
    ck(calls[:2] == ["item099", "item098"], f"hot slots come first: {calls[:2]}")
    ck(calls[2:] == [f"item{i:03d}" for i in range(10)], "the rest is the lap")
    ck(len(calls) == 12, f"a cycle spends exactly shard requests: {len(calls)}")
    ck(off == 10, f"_eoff advances by lap items only: {off}")
    ck(hoff == 2, f"_hoff advances by hot items only: {hoff}")

    calls, _, _ = run({"_eoff": off, "_hoff": hoff}, PRICES_SHARD=12,
                      PRICES_HOT=2, PRICES_HOT_POOL=24)
    ck(calls[:2] == ["item097", "item096"], f"hot ring resumes: {calls[:2]}")
    ck(calls[2:4] == ["item010", "item011"], f"lap resumes: {calls[2:4]}")

    seen, prev = set(), {}
    for _ in range(12):                       # 100 items at 10 lap slots/cycle
        calls, o, h = run(prev, PRICES_SHARD=12, PRICES_HOT=2, PRICES_HOT_POOL=24)
        seen |= set(calls[2:])
        prev = {"_eoff": o, "_hoff": h}
    ck(len(seen) == 100, f"the lap still covers everything: {len(seen)}/100")

    calls, off, hoff = run({}, PRICES_SHARD=12, PRICES_HOT=0)
    ck(calls == [f"item{i:03d}" for i in range(12)] and (off, hoff) == (12, 0),
       "PRICES_HOT=0 is the old pure lap")

    calls, _, _ = run({}, PRICES_SHARD=12, PRICES_HOT=5, PRICES_HOT_POOL=3)
    ck(calls[:3] == ["item099", "item098", "item097"] and len(set(calls[:3])) == 3,
       "a hot pool smaller than the slot count does not repeat an item")

    thin = items(5)
    thin["item004"].pop("m")
    thin["item004"].pop("v")
    ck(bp.hot_ring(thin, 2) == ["item003", "item002"],
       "an item with no median scores zero and stays off the hot ring")

    print("\nFAILED" if FAILS else "\nall checks passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
