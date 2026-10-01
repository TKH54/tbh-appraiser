"""Offline checks for what the price sweep now records besides prices. No network.

Two things ride along in prices.json since 2026-10-01, and both feed decisions
made from far away, so their invariants are pinned here:

  * `_icons` -- the icon hash of every listed item the catalog lacks, taken from
    the sweep's own search results so CI's autocatalog never has to ask Steam
    search (which refuses GitHub's IP range). Only a result whose appid and
    market_hash_name match may contribute; cataloged and delisted names drop
    out; a name this cycle's shard did not reach keeps its previous hash.
  * `_req` -- the run's real HTTP attempts / 200s / 429s per endpoint. The
    offsets count items, and get() may spend several attempts on one, so this
    is the only exact request count the git history holds.

Also pins the enrich default (PRICES_SHARD 6): the 12h budget analysis in
enrich_shard depends on it.

    python scripts/sweep_ledger_selftest.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_prices as bp

FAILS: list[str] = []
ICON_A = "eBLtYAl6ntbtQ8HLU9Nwq_iconA"
ICON_B = "eBLtYAl6ntbtQ8HLU9Nwq_iconB"


def ck(cond, msg):
    print(("  ok    " if cond else "  FAIL  ") + msg)
    if not cond:
        FAILS.append(msg)


def result(name, icon, appid=bp.APPID, desc_name=None):
    return {"hash_name": name, "sell_price": 100, "sell_listings": 3,
            "asset_description": {"appid": appid, "icon_url": icon,
                                  "market_hash_name": desc_name or name}}


class Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self.headers = {}
        self._body = body if body is not None else {}

    def json(self):
        return self._body


def main() -> int:
    bp.time.sleep = lambda s: None

    # --- the sweep keeps only icons it can vouch for -------------------------
    page = {"success": True, "total_count": 5, "results": [
        result("New Axe (Cosmic) C", ICON_A),
        result("Wrong App Item", ICON_B, appid=730),
        result("Mismatched Name", ICON_B, desc_name="Some Other Item"),
        result("Bad Icon", "has spaces/and slashes"),
        result("Known Sword", ICON_B),
    ]}
    bp.SWEPT_ICONS.clear()
    bp.get = lambda url, **kw: page
    os.environ["SWEEP_PAGES"] = "1"
    fresh, _ = bp.sweep({})
    ck(len(fresh) == 5, "every result still gets its price, icon or not")
    ck(bp.SWEPT_ICONS == {"New Axe (Cosmic) C": ICON_A, "Known Sword": ICON_B},
       f"only appid- and name-matched, well-formed icons are kept: {bp.SWEPT_ICONS}")

    # --- pending_icons: pending names only, carried across shards -----------
    with tempfile.TemporaryDirectory() as t:
        cat = Path(t) / "items.json"
        cat.write_text(json.dumps({"Known Sword": {}}), encoding="utf-8")
        bp.CATALOG = cat
        listed = {"New Axe (Cosmic) C": {}, "Known Sword": {}, "Unseen Ingot": {}}
        prev = {"_icons": {"Unseen Ingot": ICON_B, "Delisted Ring": ICON_A,
                           "Known Sword": ICON_B, "New Axe (Cosmic) C": ICON_B}}
        got = bp.pending_icons(listed, prev)
        ck("Known Sword" not in got, "a cataloged name is not published")
        ck(got.get("Unseen Ingot") == ICON_B,
           "a pending name this shard missed keeps its previous hash")
        ck("Delisted Ring" not in got, "a name the snapshot no longer lists drops out")
        ck(got.get("New Axe (Cosmic) C") == ICON_A, "a fresh sweep result beats the carried hash")
        bad = bp.pending_icons({"Odd": {}}, {"_icons": {"Odd": "no good"}})
        ck(bad == {}, "a malformed carried hash is dropped, not republished")
        bp.CATALOG = Path(t) / "missing.json"
        ck(bp.pending_icons(listed, prev) == {},
           "no readable catalog -> publish nothing rather than everything")

    # --- get() keeps an exact per-endpoint ledger ---------------------------
    import importlib
    importlib.reload(bp)
    bp.time.sleep = lambda s: None
    replies = iter([Resp(429), Resp(200, {"success": True})])
    bp.S.get = lambda url, params=None, timeout=None: next(replies)
    bp.REQ_COUNTS.clear()
    bp.get("https://steamcommunity.com/market/priceoverview/", appid=1)
    replies = iter([Resp(200, {"success": True})])
    bp.get("https://steamcommunity.com/market/search/render/", appid=1)
    ck(bp.REQ_COUNTS == {"po": [2, 1, 1], "sr": [1, 1, 0]},
       f"attempts / 200s / 429s counted per endpoint, retries included: {bp.REQ_COUNTS}")

    # --- the snapshot carries both ------------------------------------------
    with tempfile.TemporaryDirectory() as t:
        bp.OUT = Path(t) / "prices.json"
        it = {"Known Sword": {"usd": 1.0, "q": 1}}
        bp.write_snapshot(it, 150.0, False, False, 0, 0, {"JPY": 150.0}, 0,
                          {"New Axe (Cosmic) C": ICON_A})
        doc = json.loads(bp.OUT.read_text(encoding="utf-8"))
        ck(doc.get("_req") == {"po": [2, 1, 1], "sr": [1, 1, 0]}, "prices.json carries `_req`")
        ck(doc.get("_icons") == {"New Axe (Cosmic) C": ICON_A}, "prices.json carries `_icons`")
        bp.write_snapshot(it, 150.0, False, False, 0, 0, {"JPY": 150.0}, 0, {})
        doc = json.loads(bp.OUT.read_text(encoding="utf-8"))
        ck("_icons" not in doc, "nothing pending -> no `_icons` key at all")

    # --- enrich default: 2 hot + 4 lap --------------------------------------
    for k in ("PRICES_SHARD", "PRICES_HOT", "PRICES_HOT_POOL"):
        os.environ.pop(k, None)
    os.environ["PRICES_DELAY"] = "0"
    calls = []
    bp.get = lambda url, **kw: (calls.append(kw["market_hash_name"])
                                or {"success": True, "median_price": "$1.00", "volume": "10"})
    many = {f"item{i:03d}": {"usd": 1.0, "q": 1, "m": float(i), "v": i} for i in range(100)}
    off, hoff = bp.enrich_shard(many, {}, time.time())
    ck(len(calls) == 6 and (off, hoff) == (4, 2),
       f"default enrich spends 6 items a cycle, 2 hot + 4 lap: {len(calls)} ({off},{hoff})")

    print("\nFAILED" if FAILS else "\nall checks passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
