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
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

APPID = 3678970
OUT = Path(__file__).resolve().parent.parent / "data" / "prices.json"

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
    import os
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
    enrich_volumes(items)
    out = {
        "t": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rate": rate,
        "fx": fetch_fx(),
        "items": {h: {"p": round(v["usd"] * rate, 1), "q": v["q"],
                      **({"m": v["m"]} if "m" in v else {}),
                      **({"v": v["v"]} if "v" in v else {})}
                  for h, v in sorted(items.items())},
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")),
                   encoding="utf-8")
    print(f"prices.json: {len(items)} items, rate {rate}, {OUT.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
