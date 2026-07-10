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

# Steam serves residential IPs fine but rate-limits/blocks the shared GitHub Actions
# (Azure) IP range, so this bot must be a POLITE, self-throttling tenant. State
# persisted in price_state.json drives a cross-run cooldown; see _gate()/_on_failure().
STATE = Path(__file__).resolve().parent.parent / "data" / "price_state.json"
MODE_MARKER = "/tmp/build_mode"          # commit step reads this: snapshot|stateonly|skip
PACE_SEC = 180                           # push-chain startup pacing -> ~10-min cadence
HEARTBEAT_SLEEP = 540                    # during cooldown: nap this long then re-fire the
#                                          chain WITHOUT hitting Steam (GitHub's cron does
#                                          not fire on this repo, so the chain self-restarts)
HEALTHY_T_SEC = 600                      # a cron skips if the last snapshot is younger
COOLDOWN_STEPS_MIN = [20, 40, 80, 120]   # escalating backoff on repeated failure (capped)
LAST_GET_ERROR = None                    # summary of the latest get() failure (logs/state)


def get(url, **params):
    global LAST_GET_ERROR
    for attempt in range(5):
        try:
            r = S.get(url, params=params, timeout=20)
        except requests.RequestException as e:
            LAST_GET_ERROR = type(e).__name__            # Timeout / ConnectionError / ...
            print(f"  get retry {attempt}: {LAST_GET_ERROR}", file=sys.stderr)
            time.sleep(min(15 * (attempt + 1), 30))
            continue
        if r.status_code == 200:
            try:
                return r.json()
            except ValueError:
                LAST_GET_ERROR = "bad-json"
        else:
            ra = r.headers.get("Retry-After", "")
            LAST_GET_ERROR = f"HTTP {r.status_code}" + (f" Retry-After={ra}" if ra else "")
            print(f"  get retry {attempt}: {LAST_GET_ERROR}", file=sys.stderr)
        time.sleep(min(15 * (attempt + 1), 30))   # back off on 429/5xx, capped
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
        time.sleep(2)
    return items


def derive_rate(items: dict[str, dict]):
    """USD->JPY rate as Steam converts it: compare one liquid item's JPY
    lowest (priceoverview, currency=8) against its USD lowest from the sweep.
    Use a HIGH-priced liquid item so penny rounding doesn't skew the rate
    (e.g. $0.05 vs ¥10 would read as 200). Returns None if it can't derive one
    (throttled/down) — the caller then keeps the last known rate rather than
    grinding through retries and slowing the fast phase. The rate is stable, so a
    carried value is fine."""
    candidates = sorted(items.items(), key=lambda kv: -kv[1]["q"])
    candidates = [kv for kv in candidates if kv[1]["usd"] >= 1.0][:10] or candidates[:10]
    candidates.sort(key=lambda kv: -kv[1]["usd"])
    for hash_name, v in candidates[:4]:
        if v["usd"] <= 0:
            continue
        d = get("https://steamcommunity.com/market/priceoverview/",
                appid=APPID, currency=8, market_hash_name=hash_name)
        if d and d.get("success") and d.get("lowest_price"):
            m = re.search(r"[\d,.]+", d["lowest_price"])
            if m:
                jpy = float(m.group(0).replace(",", ""))
                if jpy > 0:
                    return round(jpy / v["usd"], 2)
        time.sleep(2)
    return None     # caller falls back to the last known rate


def carry_mv_baseline(items: dict[str, dict], prev_doc: dict) -> None:
    """Carry last known median (m) + 24h volume (v) onto every item from the
    previous snapshot. These are 24h aggregates kept <~1h fresh by the rotating
    enrich below, so the carried median keeps its fresh (m) label. Run BEFORE the
    fast write so the early snapshot already carries detail data, then enrich_shard
    overwrites just this run's slice."""
    prev = prev_doc.get("items", {})
    for hn, v in items.items():
        pv = prev.get(hn)
        if pv:
            if "m" in pv:
                v["m"] = pv["m"]
            if "v" in pv:
                v["v"] = pv["v"]


def enrich_shard(items: dict[str, dict], prev_doc: dict, t0: float) -> int:
    """DETAILED update: refresh median (m) + 24h volume (v) for a small ROTATING
    SHARD via per-item priceoverview, then advance the persisted offset (`_eoff`).

    The catalog is ~740 items; a full per-item pass is far too slow for a ~10-min
    job, so each run only touches PRICES_SHARD items (default 60) starting at the
    saved offset. The whole catalog therefore cycles every ceil(N/shard) runs
    (~1h) while the cheap sweep keeps EVERY item's lowest ask/listing fresh each
    run. Carried-but-not-refreshed items keep their previous m/v (set by
    carry_mv_baseline). A budget caps the wall clock so Steam throttling can't run
    the job long; the offset only advances by what we actually processed, so a
    short run just resumes next time. Returns the new offset."""
    if os.environ.get("PRICES_NOENRICH"):       # keepalive / pure-sweep escape hatch
        return int(prev_doc.get("_eoff", 0) or 0)
    keys = sorted(items)
    n = len(keys)
    if not n:
        return 0
    # env vars may arrive as "" from workflow_dispatch inputs on non-dispatch
    # events, so coalesce empties to the defaults before parsing.
    shard = int(os.environ.get("PRICES_SHARD") or 25)
    delay = float(os.environ.get("PRICES_DELAY") or 2.5)    # per-item pacing (s)
    deadline = time.time() + int(os.environ.get("PRICES_BUDGET_SEC") or 600)
    off = int(prev_doc.get("_eoff", 0) or 0) % n
    done = refreshed = 0
    while done < shard and done < n and time.time() < deadline:
        hn = keys[(off + done) % n]
        v = items[hn]
        d = get("https://steamcommunity.com/market/priceoverview/",
                appid=APPID, currency=8, market_hash_name=hn)
        time.sleep(delay)
        done += 1
        if not d or not d.get("success"):
            continue                            # transient -> keep carried baseline
        refreshed += 1
        m = re.search(r"[\d,.]+", d.get("median_price") or "")
        if m:
            v["m"] = float(m.group(0).replace(",", ""))
        else:
            v.pop("m", None)                    # confirmed no recent sale -> drop
            #                                     stale m so it falls back to lm
        vol = re.sub(r"[^\d]", "", d.get("volume") or "")
        if vol:
            v["v"] = int(vol)
        else:
            v.pop("v", None)                    # no 24h sales -> clear volume
    new_off = (off + done) % n
    print(f"enrich: shard={shard} attempted={done}/{n} refreshed={refreshed} "
          f"offset {off}->{new_off} elapsed={time.time() - t0:.0f}s",
          file=sys.stderr)
    return new_off


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


STALE_FACTOR = 0.5   # ask below this fraction of the median ref = crashed market
STALE_Q = 10         # ...and this deep = real undercutting, not a lone lowball

def _real_unit(v: dict):
    """Trusted per-unit value. A fresh real-sale median (m) is best; absent that, lm is
    the LAST median which the slow/flaky per-item fetch leaves stale in EITHER direction,
    so prefer the always-fresh DEEP ask market (q>=STALE_Q) over it. Mirrors realUnit()."""
    if v.get("m") is not None:
        return v["m"]
    p, q = v.get("p"), v.get("q") or 0
    if p is not None and q >= STALE_Q:
        return p
    return v.get("lm") if v.get("lm") is not None else p


def grade_averages(items: dict[str, dict], rate: float) -> dict:
    """Per-grade mean over ' (Grade) A' gear — the coin-gacha spin EV's 現在価格
    basis. Only TRUSTED prices count: a real recent-sale median (m) or the last
    known median (lm). The bare lowest ask is excluded — during the sell-freeze
    it is wildly inflated (e.g. Dusk Bow ¥254k, Iron Plate ¥100k) and would
    otherwise pull a whole grade's average up ~10x, inflating coin spin EVs.
    Grades with <3 trusted samples are omitted -> the site falls back to the
    pre-freeze baseline for those."""
    vals = defaultdict(list)
    for hn, v in items.items():
        m = re.match(r"^.* \((\w+)\) A$", hn)
        if not m:
            continue
        price = _real_unit(v)                # trusted median, crash-corrected to the live deep ask
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


def write_snapshot(items, rate, unlocked, eoff, fx) -> None:
    out = {
        "t": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rate": rate,
        "unlocked": unlocked,
        "_eoff": eoff,                     # rotating enrich offset (persisted)
        "gev": grade_averages(items, rate),
        "fx": fx,
        "items": {h: {"p": round(v["usd"] * rate, 1), "q": v["q"],
                      **({"m": v["m"]} if "m" in v else {}),
                      **({"lm": v["lm"]} if "lm" in v else {}),
                      **({"v": v["v"]} if "v" in v else {})}
                  for h, v in sorted(items.items())},
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")),
                   encoding="utf-8")


def _load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, separators=(",", ":")), encoding="utf-8")


def _set_mode(mode: str) -> None:
    """Tell the workflow's commit step what we produced: snapshot|stateonly|skip."""
    try:
        Path(MODE_MARKER).write_text(mode, encoding="utf-8")
    except Exception:
        pass


def _prev_t_epoch():
    """Epoch seconds of the last snapshot's `t`, or None."""
    try:
        t = json.loads(OUT.read_text(encoding="utf-8")).get("t")
        return datetime.fromisoformat(t).timestamp() if t else None
    except Exception:
        return None


def _gate() -> str:
    """Decide what to do this run: "proceed" | "heartbeat" | "skip".

    Steam is hit ONLY on "proceed". workflow_dispatch always proceeds (manual). Else:
      - inside a persisted cooldown -> nap briefly, then "heartbeat": a state-only
        commit that RE-FIRES the chain WITHOUT hitting Steam. GitHub's cron does not
        fire reliably on this push-busy repo, so the chain must restart ITSELF; the
        heartbeat keeps it breathing while the cooldown gate throttles Steam to ~once
        per cooldown instead of every run;
      - a scheduled (cron) run is only a backstop, so it skips while the push-chain is
        clearly alive (recent `t`) to avoid doubling it;
      - a push (chain) run paces itself so the healthy cadence lands near ~10 min."""
    event = os.environ.get("GITHUB_EVENT_NAME", "workflow_dispatch")
    if event == "workflow_dispatch":
        return "proceed"
    now = time.time()
    cd = float(_load_state().get("cooldown_until", 0) or 0)
    if now < cd:
        nap = min(cd - now, HEARTBEAT_SLEEP)
        print(f"gate: cooldown {int((cd - now) / 60)}min left -> heartbeat nap {int(nap)}s",
              file=sys.stderr)
        time.sleep(nap)
        if time.time() < cd:
            return "heartbeat"          # still cooling -> re-fire, don't touch Steam
        return "proceed"                # cooldown ended during the nap -> retry Steam now
    if event == "schedule":
        pt = _prev_t_epoch()
        if pt is not None and (now - pt) < HEALTHY_T_SEC:
            print(f"gate: chain healthy (t {int((now - pt) / 60)}min old) -> skip cron",
                  file=sys.stderr)
            return "skip"
    if event == "push":
        print(f"gate: push-chain pacing {PACE_SEC}s", file=sys.stderr)
        time.sleep(PACE_SEC)
    return "proceed"


def _heartbeat() -> None:
    """Keep the chain alive during a cooldown WITHOUT hitting Steam: bump a timestamp
    in price_state.json so the commit is non-empty and re-fires the chain. Committed
    alone -> pages.yml paths-ignore keeps it from redeploying the site."""
    state = _load_state()
    state["last_heartbeat_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _write_state(state)
    _set_mode("stateonly")


def _on_failure() -> None:
    """Sweep couldn't reach Steam. Record an escalating cooldown in price_state.json
    ONLY (never touch prices.json/`t`). The state commit re-fires the chain, but the
    cooldown gate makes the next run heartbeat instead of hitting Steam -> Steam sees
    ~one hit per cooldown, not per run."""
    state = _load_state()
    cf = int(state.get("consecutive_failures", 0) or 0) + 1
    backoff = COOLDOWN_STEPS_MIN[min(cf, len(COOLDOWN_STEPS_MIN)) - 1]
    state["consecutive_failures"] = cf
    state["cooldown_until"] = time.time() + backoff * 60
    state["last_error"] = LAST_GET_ERROR or "sweep empty"
    state["last_failure_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _write_state(state)
    _set_mode("stateonly")
    print(f"build failed ({state['last_error']}) x{cf} -> cooldown {backoff}min",
          file=sys.stderr)


def _on_success() -> None:
    """Clear the cooldown after a good sweep. Only rewrite state if it actually
    changed (was failing), to avoid churning price_state.json every healthy run."""
    state = _load_state()
    if state.get("consecutive_failures") or state.get("cooldown_until"):
        _write_state({"consecutive_failures": 0, "cooldown_until": 0,
                      "last_recovered_utc":
                          datetime.now(timezone.utc).isoformat(timespec="seconds")})
    _set_mode("snapshot")


def main() -> None:
    t0 = time.time()
    action = _gate()
    if action == "skip":
        print("skipped (gate)", file=sys.stderr)
        return
    if action == "heartbeat":
        _heartbeat()
        print("heartbeat (cooling); chain kept alive without touching Steam", file=sys.stderr)
        return
    # PHASE 1 — FAST: lowest ask + listing count for EVERY item (cheap sweep),
    # median/volume carried from the previous snapshot. Written + ready to push in
    # ~2-3 min, so prices.json `t` advances every cycle even if the detail phase
    # below is slow or dies.
    items = sweep()
    if not items:
        _on_failure()
        return
    try:
        prev_doc = json.loads(OUT.read_text(encoding="utf-8"))
    except Exception:
        prev_doc = {}
    rate = derive_rate(items) or prev_doc.get("rate") or 155.0
    unlocked = detect_unlocked(items)      # reads previous OUT
    prev_off = int(prev_doc.get("_eoff", 0) or 0)
    fx = fetch_fx()
    carry_mv_baseline(items, prev_doc)     # carry prev median/volume onto all items
    carry_last_median(items, rate)
    update_history(items, rate)
    write_snapshot(items, rate, unlocked, prev_off, fx)
    print(f"fast snapshot: {len(items)} items, {OUT.stat().st_size // 1024} KB, "
          f"elapsed={time.time() - t0:.0f}s", file=sys.stderr)

    # PHASE 2 — DETAIL: refresh a small rotating shard's median/volume, then
    # rewrite. Bounded by shard size + budget so the whole job stays well under
    # the timeout; a failure here still leaves the fast snapshot on disk.
    try:
        eoff = enrich_shard(items, prev_doc, t0)
    except Exception as e:
        print(f"enrich_shard failed ({e}); keeping fast snapshot", file=sys.stderr)
        eoff = prev_off
    carry_last_median(items, rate)         # apply lm to the freshly enriched items
    update_history(items, rate)
    write_snapshot(items, rate, unlocked, eoff, fx)
    print(f"final snapshot: {len(items)} items, rate {rate}, unlocked {unlocked}, "
          f"_eoff={eoff}, {OUT.stat().st_size // 1024} KB, "
          f"elapsed={time.time() - t0:.0f}s")
    _on_success()


if __name__ == "__main__":
    main()
