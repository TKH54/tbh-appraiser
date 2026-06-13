"""Build web/data/prices.json — the CURRENT-price snapshot for the browser app.

Runs locally or in GitHub Actions on a schedule. Steam-friendly: the whole
catalog costs ~8 search/render pages (100 items each) + 1 priceoverview call
used to derive the USD->JPY rate Steam itself applies.

Output: {"t": iso_utc, "rate": jpy_per_usd, "items": {hash: {"p": jpy, "q": listings}}}
  p = lowest current ask in JPY (search/render is USD-only; converted)
  q = number of active listings
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests

APPID = 3678970
OUT = Path(__file__).resolve().parent.parent / "data" / "prices.json"
HIST = Path(__file__).resolve().parent.parent / "data" / "history.json"

S = requests.Session()
S.headers.update({"User-Agent": "tbh-appraiser-price-snapshot/1.0 (polite; 1 req/5s)"})


def get(url, **params):
    for attempt in range(5):
        r = S.get(url, params=params, timeout=20)
        if r.status_code == 200:
            try:
                return r.json()
            except ValueError:
                pass
        time.sleep(15 * (attempt + 1))      # back off hard on 429/5xx
    return None


def sweep() -> dict[str, dict]:
    """All items' lowest ask (USD cents) + listing counts via search/render."""
    items: dict[str, dict] = {}
    start, total = 0, 1
    while start < total:
        d = get("https://steamcommunity.com/market/search/render/",
                appid=APPID, norender=1, count=100, start=start,
                sort_column="name", sort_dir="asc")
        if not d or not d.get("success"):
            break
        total = d.get("total_count", 0)
        for it in d.get("results", []):
            items[it["hash_name"]] = {
                "usd": it.get("sell_price", 0) / 100.0,      # cents -> USD
                "q": it.get("sell_listings", 0),
            }
        start += len(d.get("results", [])) or 100
        time.sleep(5)
    return items


def derive_rate(items: dict[str, dict]) -> float:
    """USD->JPY rate as Steam converts it: compare one liquid item's JPY
    lowest (priceoverview, currency=8) against its USD lowest from the sweep.
    Use a HIGH-priced liquid item so penny rounding doesn't skew the rate
    (e.g. $0.05 vs ¥10 would read as 200)."""
    candidates = sorted(items.items(), key=lambda kv: -kv[1]["q"])
    candidates = [kv for kv in candidates if kv[1]["usd"] >= 1.0][:10] or candidates[:10]
    candidates.sort(key=lambda kv: -kv[1]["usd"])
    for hash_name, v in candidates[:10]:
        if v["usd"] <= 0:
            continue
        d = get("https://steamcommunity.com/market/priceoverview/",
                appid=APPID, currency=8, market_hash_name=hash_name)
        time.sleep(5)
        if not d or not d.get("success") or not d.get("lowest_price"):
            continue
        m = re.search(r"[\d,.]+", d["lowest_price"])
        if not m:
            continue
        jpy = float(m.group(0).replace(",", ""))
        if jpy > 0:
            return round(jpy / v["usd"], 2)
    return 155.0    # fallback if rate underivable


def enrich_volumes(items: dict[str, dict]) -> None:
    """Per-item priceoverview (JPY): median of recent real sales + units sold
    in 24h. ~5s per listed item (e.g. 250 items ≈ 20 min) — run hourly.

    PRICES_FAST=1 skips the slow fetch and instead CARRIES OVER median/volume
    from the previous prices.json — they are 24h aggregates and barely move
    inside an hour, while the lowest ask / listings (from the cheap sweep) get
    refreshed every run. This lets a fast job run every ~10 minutes."""
    if os.environ.get("PRICES_FAST"):
        try:
            prev = json.loads(OUT.read_text(encoding="utf-8")).get("items", {})
        except Exception:
            prev = {}
        for hn, v in items.items():
            pv = prev.get(hn)
            if pv:
                if "m" in pv:
                    v["m"] = pv["m"]
                if "v" in pv:
                    v["v"] = pv["v"]
        return
    for hn, v in items.items():
        d = get("https://steamcommunity.com/market/priceoverview/",
                appid=APPID, currency=8, market_hash_name=hn)
        time.sleep(4.5)
        if not d or not d.get("success"):
            continue
        m = re.search(r"[\d,.]+", d.get("median_price") or "")
        if m:
            v["m"] = float(m.group(0).replace(",", ""))
        vol = re.sub(r"[^\d]", "", d.get("volume") or "")
        if vol:
            v["v"] = int(vol)


def detect_unlocked(items: dict[str, dict]) -> bool:
    """Market-reopen flag (drives the site's pre/post-unlock copy + default mode).

    Signal: the count of DISTINCT items with at least one listing. While new
    listings are blocked the count can only fall (sold-out items never return);
    a meaningful RISE — or topping the freeze-period ceiling — means listing
    works again. Sticky once true (carried over), and FORCE_UNLOCKED=1/0
    overrides everything (manual switch via workflow_dispatch).
    """
    force = os.environ.get("FORCE_UNLOCKED", "")
    if force in ("0", "1"):
        return force == "1"
    prev_unlocked, prev_n = False, None
    try:
        prev = json.loads(OUT.read_text(encoding="utf-8"))
        prev_unlocked = bool(prev.get("unlocked"))
        prev_n = len(prev.get("items", {}))
    except Exception:
        pass
    n = len(items)
    # 238 distinct items listed during the freeze (2026-06-11), drifting down.
    return prev_unlocked or n >= 320 or (prev_n is not None and n >= prev_n + 30)


def display_price(v: dict, rate: float):
    """The price the site shows. Priority:
      1. m  = median of recent REAL sales (Steam priceoverview) — the true value
      2. lm = last KNOWN median, carried over. During the sell-freeze Steam stops
              returning a median (no 24h sales) and priceoverview gives only a
              lone, inflated lowest ask; falling back to that ask overvalues items
              ~10x (e.g. Emerald ¥3,688 real -> ¥31,471 ask). Carrying the last
              real median keeps the value honest until trading resumes.
      3. lowest ask, only if we have never seen a median for this item."""
    return v.get("m") or v.get("lm") or round(v["usd"] * rate, 1)


def carry_last_median(items: dict[str, dict], rate: float) -> None:
    """Persist a per-item 'last known real-sale median' (lm). A fresh median
    becomes the new lm; when there's no fresh median (freeze), the previous lm is
    carried forward so the displayed value never collapses to the inflated ask.
    If neither exists yet, SEED lm from history.json — the last recorded price
    that isn't the current (inflated) lowest ask (the pre-freeze real value)."""
    try:
        prev = json.loads(OUT.read_text(encoding="utf-8")).get("items", {})
    except Exception:
        prev = {}
    try:
        hist = json.loads(HIST.read_text(encoding="utf-8")).get("items", {})
    except Exception:
        hist = {}
    for hn, v in items.items():
        if "m" in v:
            v["lm"] = v["m"]                       # fresh real sale -> new baseline
            continue
        plm = prev.get(hn, {}).get("lm")
        if plm is not None:
            v["lm"] = plm                          # carry the last real median
            continue
        ask = round(v["usd"] * rate, 1)            # seed from history: the most
        for _, val in reversed(hist.get(hn, [])):  # recent real value clearly below
            if val and val <= ask * 0.6:           # the freeze-inflation plateau (the
                v["lm"] = round(val, 1)            # inflated tail hovers near `ask`,
                break                              # often varying a few %)


def update_history(items: dict[str, dict], rate: float) -> None:
    """Hourly per-item price points, pruned to 72h — drives the site's 24h
    trend arrows. Format: {"items": {hash: [[epochHour, jpy], ...]}}."""
    now_h = int(time.time() // 3600)
    try:
        hist = json.loads(HIST.read_text(encoding="utf-8"))
    except Exception:
        hist = {"items": {}}
    hi = hist.setdefault("items", {})
    for hn, v in items.items():
        price = display_price(v, rate)
        if not price:
            continue
        pts = hi.setdefault(hn, [])
        if pts and pts[-1][0] == now_h:
            pts[-1][1] = price          # refine this hour's point on every run
        else:
            pts.append([now_h, price])
    cutoff = now_h - 72
    for hn in list(hi):
        hi[hn] = [pt for pt in hi[hn] if pt[0] >= cutoff]
        if not hi[hn]:
            del hi[hn]
    HIST.write_text(json.dumps(hist, separators=(",", ":")), encoding="utf-8")


def grade_averages(items: dict[str, dict], rate: float) -> dict:
    """Per-grade mean of CURRENT prices over ' (Grade) A' gear — the coin-gacha
    spin EV's 現在価格 basis. Grades with <3 listed samples are omitted (the
    site falls back to the pre-freeze baseline for those)."""
    vals = defaultdict(list)
    for hn, v in items.items():
        m = re.match(r"^.* \((\w+)\) A$", hn)
        if not m:
            continue
        price = display_price(v, rate)
        if price:
            vals[m.group(1)].append(price)
    return {g: round(sum(xs) / len(xs), 2) for g, xs in vals.items() if len(xs) >= 3}


def fetch_fx() -> dict:
    """USD-based fx for displaying prices in each UI language's currency
    (JPY/CNY/TWD/KRW/RUB). Free endpoint, no key; falls back to static rates."""
    try:
        r = S.get("https://open.er-api.com/v6/latest/USD", timeout=20).json()
        rates = r.get("rates", {})
        out = {"USD": 1.0, **{c: rates[c] for c in ("JPY", "CNY", "TWD", "KRW", "RUB") if c in rates}}
        if len(out) == 6:
            return out
    except Exception:
        pass
    return {"JPY": 155.0, "CNY": 7.1, "TWD": 32.0, "KRW": 1380.0, "RUB": 90.0}


def main() -> None:
    items = sweep()
    if not items:
        print("sweep failed", file=sys.stderr)
        sys.exit(1)
    rate = derive_rate(items)
    unlocked = detect_unlocked(items)      # before enrich: reads previous OUT
    enrich_volumes(items)
    carry_last_median(items, rate)         # after enrich (needs m), before history/gev
    update_history(items, rate)
    out = {
        "t": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rate": rate,
        "unlocked": unlocked,
        "gev": grade_averages(items, rate),
        "fx": fetch_fx(),
        "items": {h: {"p": round(v["usd"] * rate, 1), "q": v["q"],
                      **({"m": v["m"]} if "m" in v else {}),
                      **({"lm": v["lm"]} if "lm" in v else {}),
                      **({"v": v["v"]} if "v" in v else {})}
                  for h, v in sorted(items.items())},
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")),
                   encoding="utf-8")
    print(f"prices.json: {len(items)} items, rate {rate}, "
          f"unlocked {unlocked}, {OUT.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
