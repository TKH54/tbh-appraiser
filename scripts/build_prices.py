"""Build web/data/prices.json — the CURRENT-price snapshot for the browser app.

Runs locally or in GitHub Actions on a schedule. Steam-friendly: search/render
now serves only 10 items/page (a full catalog pass = ~75 requests, which got the
residential runner IP rate-limited on 2026-07-11), so each cycle sweeps only a
rotating SWEEP_PAGES-page shard and carries the rest of the catalog from the
previous snapshot; +1 priceoverview call derives the USD->JPY rate Steam applies.

Output: {"t": iso_utc, "rate": jpy_per_usd, "items": {hash: {"p": jpy, "q": listings}}}
  p = lowest current ask in JPY (search/render is USD-only; converted)
  q = number of active listings
  ls = epoch hour the sweep last saw the item (delisting expiry, see merge_carry)
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
MODE_MARKER = os.environ.get("BUILD_MODE_FILE") or "/tmp/build_mode"
#                                          commit step reads this: snapshot|stateonly|skip
#                                          (env override for local runners: Termux has no /tmp)
SOURCE = os.environ.get("PRICES_SOURCE") or "ci"
#                                          who produced this snapshot: "ci" (GitHub Actions,
#                                          Steam-blocked IP range) or "local" (a residential-IP
#                                          runner: the phone/PC — see scripts/phone_runner.sh).
#                                          Written into prices.json as `src`; _gate() makes CI
#                                          STAND DOWN while local snapshots are fresh.
LOCAL_FRESH_SEC = 1800                   # local runner is "delivering" if t is younger than
#                                          this; older -> CI takes over. 3x the 10-min cadence:
#                                          Steam throttling stretches healthy phone cycles to
#                                          ~23 min (observed 2026-07-12), and 2x caused CI to
#                                          ping-pong takeovers against a merely-slow phone
STALE_ALERT_SEC = 7200                   # t older than this -> phone AND CI are both failing;
#                                          ping Discord so the user can reboot the phone
STALE_ALERT_REPEAT_SEC = 43200           # ...re-ping at most every 12h while it stays stale
PACE_SEC = 180                           # push-chain startup pacing -> ~10-min cadence
HEARTBEAT_SLEEP = 540                    # during cooldown: nap this long then re-fire the
#                                          chain WITHOUT hitting Steam (GitHub's cron does
#                                          not fire on this repo, so the chain self-restarts)
HEALTHY_T_SEC = 600                      # a cron skips if the last snapshot is younger
COOLDOWN_STEPS_MIN = [20, 40, 80, 120]   # escalating backoff on repeated failure (capped)
FALLBACK_ALERT_GAP_SEC = 3600            # min gap between repeated local->CI fallback pings
#                                          (a flapping phone would otherwise spam Discord)
FALLBACK_ALERT_AGE_SEC = 2700            # ping only when local has been quiet THIS long.
#                                          Deliberately looser than LOCAL_FRESH_SEC: CI may
#                                          quietly cover a slow/backing-off phone (that
#                                          self-heals), but a 45-min silence is a real outage
#                                          worth waking the user for (observed 2026-07-12:
#                                          a 25-min slow cycle pinged the user for nothing)
TOP3_RE = re.compile(r"\((Celestial|Divine|Cosmic)\)")   # top-3 listing-restricted grades
UNLOCK3_ABS = 25                         # distinct listed top-grade items >= this -> unlocked.
#                                          9 pre-restriction leftovers were live 2026-07-12;
#                                          while restricted the count can only FALL, so...
UNLOCK3_RISE = 5                         # ...any rise this big means new listings work again
LAST_GET_ERROR = None                    # summary of the latest get() failure (logs/state)

# ---- adaptive load-shedding (recurrence fix for the 2026-07-12 slow-cycle) ----
# Steam throttles SUSTAINED load adaptively, in two shapes: hard 429s (retry
# chains) and TARPITTING — every request succeeds but takes ~30s instead of ~1s
# (observed 2026-07-12 evening: cycles uniformly stretched to ~23 min with data
# intact = slow-but-successful requests, not failures). Pre-throttle the bot
# pushed its full request budget through anyway, sustaining the very load Steam
# was squeezing. Both shapes now count as THROTTLE SIGNALS; past THROTTLED_AT
# the run finishes in low-power mode: a couple of sweep pages (so `t` still
# advances with real data), a trickle of enrich, no rate probe. Cadence stays
# ~10 min, Steam sees ~1/4 the requests, and the budget restores itself the
# first cycle Steam stops throttling.
THROTTLE_SIGNALS = 0                     # bumped by get(): +1 per retry (429/5xx/timeout)
#                                          and +1 per SLOW response (tarpit)
THROTTLED_AT = 3                         # signals this run >= this -> low-power mode
#                                          (healthy runs see 0-1 sporadic signals)
SLOW_REQ_SEC = 5                         # a 200-OK slower than this is a tarpit signal
#                                          (healthy Steam answers in ~1s; observed stealth
#                                          tarpit ~8-9s slipped under the original 10s)
THROTTLED_MIN_PAGES = 2                  # sweep floor: keep t advancing on real data
THROTTLED_MIN_ENRICH = 5                 # enrich floor: median/volume still trickle
SWEEP_TIME_BUDGET_SEC = 240              # belt-and-braces: however Steam slows us,
#                                          the sweep phase never runs longer than this
THROTTLED_ENRICH_BUDGET_SEC = 120        # once throttled, enrich is cut by TIME too:
#                                          2026-07-12 the count floor alone didn't help —
#                                          priceoverview was so squeezed (~2 min/item incl.
#                                          retries) that 5 items still ate the full 600s
THROTTLED_GET_ATTEMPTS = 2               # once throttled, stop retry-chaining each request
#                                          (5 attempts + backoffs = ~2 min on ONE item; a
#                                          missed item just keeps its carried value)


def _throttled() -> bool:
    return THROTTLE_SIGNALS >= THROTTLED_AT


def get(url, **params):
    global LAST_GET_ERROR, THROTTLE_SIGNALS
    for attempt in range(5):
        if attempt >= THROTTLED_GET_ATTEMPTS and _throttled():
            print(f"  get: throttled -> giving up after {attempt} attempts",
                  file=sys.stderr)
            return None
        t_req = time.time()
        try:
            r = S.get(url, params=params, timeout=20)
        except requests.RequestException as e:
            LAST_GET_ERROR = type(e).__name__            # Timeout / ConnectionError / ...
            THROTTLE_SIGNALS += 1
            print(f"  get retry {attempt}: {LAST_GET_ERROR}", file=sys.stderr)
            time.sleep(min(15 * (attempt + 1), 30))
            continue
        if time.time() - t_req > SLOW_REQ_SEC:           # tarpit: slow even when it works
            THROTTLE_SIGNALS += 1
            print(f"  slow response ({time.time() - t_req:.0f}s) -> throttle signal "
                  f"{THROTTLE_SIGNALS}", file=sys.stderr)
        if r.status_code == 200:
            try:
                return r.json()
            except ValueError:
                LAST_GET_ERROR = "bad-json"
        else:
            ra = r.headers.get("Retry-After", "")
            LAST_GET_ERROR = f"HTTP {r.status_code}" + (f" Retry-After={ra}" if ra else "")
            print(f"  get retry {attempt}: {LAST_GET_ERROR}", file=sys.stderr)
        THROTTLE_SIGNALS += 1
        time.sleep(min(15 * (attempt + 1), 30))   # back off on 429/5xx, capped
    return None


def sweep(prev_doc: dict) -> tuple[dict[str, dict], int]:
    """Lowest ask (USD) + listing count for a ROTATING page shard via search/render.

    Steam quietly capped search/render at 10 results/page (count=100 is ignored,
    observed 2026-07), so a full catalog pass is ~75 requests — repeating that
    every cycle tripped Steam's rate limiter even on the residential runner IP
    (429 storm + half-day outage, 2026-07-11). Each cycle now fetches at most
    SWEEP_PAGES pages from the persisted item offset (`_soff`); the caller
    carries every other item from the previous snapshot (merge_carry), and the
    offset only advances by what was actually fetched, so a mid-shard failure
    just means a shorter shard — never a lost catalog. Full lowest-ask coverage
    still lands every ceil(N/(SWEEP_PAGES*10)) cycles (~5 at the defaults)."""
    max_pages = int(os.environ.get("SWEEP_PAGES") or 15)
    start = int(prev_doc.get("_soff", 0) or 0)
    items: dict[str, dict] = {}
    pages, wrapped = 0, False
    t_sweep = time.time()
    while pages < max_pages:
        slow = time.time() - t_sweep > SWEEP_TIME_BUDGET_SEC
        if pages >= THROTTLED_MIN_PAGES and (_throttled() or slow):
            print(f"sweep: {'time budget spent' if slow else 'throttled'} "
                  f"(signals={THROTTLE_SIGNALS}, {time.time() - t_sweep:.0f}s) -> "
                  f"low-power, stopping after {pages} pages", file=sys.stderr)
            break               # offset advanced only by what we fetched -> no loss
        d = get("https://steamcommunity.com/market/search/render/",
                appid=APPID, norender=1, count=100, start=start,
                sort_column="name", sort_dir="asc")
        if not d or not d.get("success"):
            break
        total = d.get("total_count", 0)
        results = d.get("results", [])
        if not results:                 # ran off the end (catalog shrank) -> wrap once
            if wrapped or start == 0:
                break
            start, wrapped = 0, True
            time.sleep(2)
            continue
        for it in results:
            items[it["hash_name"]] = {
                "usd": it.get("sell_price", 0) / 100.0,      # cents -> USD
                "q": it.get("sell_listings", 0),
            }
        start += len(results)
        pages += 1
        if start >= total:              # full circle -> next cycle restarts at 0
            start = 0
            break
        time.sleep(2)
    return items, start


CARRY_MAX_H = 24    # sweep hasn't seen an item for this long -> delisted, drop it


def merge_carry(fresh: dict[str, dict], prev_doc: dict) -> dict[str, dict]:
    """Overlay the freshly swept shard onto the previous snapshot's full catalog.

    Carried items keep their last ask/listing count (`p` converted back to USD
    via the snapshot's own rate) so the doc always covers every item even though
    each cycle only sweeps a slice. `ls` (last-swept epoch hour) rides along in
    the doc; an item the rotating sweep hasn't confirmed for CARRY_MAX_H hours
    (many full laps) has left the market and is dropped — that replaces the old
    full-sweep behavior where sold-out items simply stopped appearing."""
    now_h = int(time.time() // 3600)
    for v in fresh.values():
        v["ls"] = now_h
    out = dict(fresh)
    prev_rate = float(prev_doc.get("rate") or 0)
    if not prev_rate:
        return out                      # no previous snapshot -> nothing to carry
    for hn, pv in (prev_doc.get("items") or {}).items():
        if hn in out:
            continue
        ls = int(pv.get("ls") or now_h)     # pre-`ls` docs: grandfather as fresh
        if now_h - ls > CARRY_MAX_H:
            continue
        p = pv.get("p")
        if p is None:
            continue
        out[hn] = {"usd": round(p / prev_rate, 4), "q": pv.get("q", 0), "ls": ls}
    return out


def derive_rate(items: dict[str, dict]):
    """USD->JPY rate as Steam converts it: compare one liquid item's JPY
    lowest (priceoverview, currency=8) against its USD lowest from the sweep.
    Use a HIGH-priced liquid item so penny rounding doesn't skew the rate
    (e.g. $0.05 vs ¥10 would read as 200). Returns None if it can't derive one
    (throttled/down) — the caller then keeps the last known rate rather than
    grinding through retries and slowing the fast phase. The rate is stable, so a
    carried value is fine."""
    if _throttled():
        return None             # skip the probe under throttle; the rate is stable
        #                         and sane_rate carries the previous one anyway
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


def sane_rate(derived, prev_rate, fx_jpy):
    """derive_rate reads ONE item's JPY quote against its swept USD ask; with the
    small rotating shard the candidate pool can be all cheap/illiquid items, and a
    listing change between the two reads skews the ratio (observed +6% on a 2-page
    shard -> EVERY displayed price would jump 6%). Accept the derived rate only if
    it roughly agrees with the previous snapshot's rate or the market fx (Steam's
    conversion tracks market fx closely); otherwise carry the previous rate."""
    for ref in (prev_rate, fx_jpy):
        if derived and ref and abs(derived / ref - 1) <= 0.04:
            return derived
    return prev_rate or fx_jpy or 155.0


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
    t_enrich = time.time()
    while done < shard and done < n and time.time() < deadline:
        # under throttle, cut by COUNT or TIME, whichever first: priceoverview has
        # been squeezed to ~2 min/item, where the count floor alone still ate the
        # whole 600s budget and stretched the cycle to ~20 min
        if _throttled() and (done >= THROTTLED_MIN_ENRICH
                             or time.time() - t_enrich > THROTTLED_ENRICH_BUDGET_SEC):
            print(f"enrich: throttled (signals={THROTTLE_SIGNALS}) -> low-power, "
                  f"stopping after {done} items / {time.time() - t_enrich:.0f}s",
                  file=sys.stderr)
            break               # offset advances by `done` -> the lap just resumes
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


def _count_top3_listed(items: dict[str, dict]) -> int:
    """DISTINCT top-3-grade items with at least one active listing. Works on both
    the in-memory sweep shape and the persisted prices.json shape (both carry q)."""
    return sum(1 for hn, v in items.items()
               if TOP3_RE.search(hn) and (v.get("q") or 0) > 0)


def detect_unlocked3(items: dict[str, dict], prev_doc: dict) -> bool:
    """Top-3-grade (Celestial/Divine/Cosmic) LISTING-unlock flag — the roadmap says
    the restriction lifts sometime 2026-07. While restricted, no new listing can be
    created, so the distinct listed count can only FALL (leftover pre-restriction
    listings selling out); a clear RISE — or topping UNLOCK3_ABS — means listing
    works again. Sticky once true (carried over); FORCE_UNLOCKED3=1/0 overrides
    everything (manual switch via workflow_dispatch). The site reads this as
    prices.json "unlocked3" and drops the gacha-EV top-grade exclusion by itself."""
    force = os.environ.get("FORCE_UNLOCKED3", "")
    if force in ("0", "1"):
        return force == "1"
    if prev_doc.get("unlocked3"):
        return True
    n = _count_top3_listed(items)
    prev_items = prev_doc.get("items") or {}
    # the rise rule needs a real previous catalog to diff against — on a bootstrap
    # run (no prior snapshot) the leftover listings alone would fake a "rise"
    return n >= UNLOCK3_ABS or (bool(prev_items)
                                and n >= _count_top3_listed(prev_items) + UNLOCK3_RISE)


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


def write_snapshot(items, rate, unlocked, unlocked3, eoff, soff, fx) -> None:
    out = {
        "t": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "src": SOURCE,                     # ci|local — _gate() reads this to stand down
        "rate": rate,
        "unlocked": unlocked,
        "unlocked3": unlocked3,            # top-3 grade listing restriction lifted
        #                                    (site auto-drops the gacha GEX exclusion)
        "_eoff": eoff,                     # rotating enrich offset (persisted)
        "_soff": soff,                     # rotating sweep offset (persisted)
        "gev": grade_averages(items, rate),
        "fx": fx,
        "items": {h: {"p": round(v["usd"] * rate, 1), "q": v["q"],
                      **({"ls": v["ls"]} if "ls" in v else {}),
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


def _prev_snapshot_meta():
    """(epoch seconds of the last snapshot's `t` or None, its `src` or "ci")."""
    try:
        doc = json.loads(OUT.read_text(encoding="utf-8"))
        t = doc.get("t")
        return (datetime.fromisoformat(t).timestamp() if t else None,
                doc.get("src") or "ci")
    except Exception:
        return None, "ci"


def _post_discord(msg: str) -> bool:
    """Best-effort Discord ping. False when there is no webhook (the phone runner
    has none — CI is the alerting side) or the post failed, so callers can leave
    their dedupe flag unset and let a later CI run deliver the alert instead."""
    hook = os.environ.get("DISCORD_WEBHOOK")
    if not hook:
        return False
    try:
        requests.post(hook, json={"content": msg}, timeout=15)
        return True
    except Exception as e:
        print(f"discord alert failed: {e}", file=sys.stderr)
        return False


def _maybe_alert_fallback() -> None:
    """One-shot 'the phone runner stopped and CI took over' ping. The stale-t
    watchdog below only fires when BOTH sources are dead for hours; when the CI
    fallback works, `t` stays fresh and the phone's death would otherwise go
    completely unnoticed (CI = Steam-blocked IP range = degraded service, so the
    user should still go reboot the phone). Detected at the local->ci transition:
    a CI run that proceeds to snapshot while the LAST snapshot still says
    src=local. Rate-limited against a flapping phone; the recovery ping is sent
    by _standby_alerts() when the phone delivers again."""
    if SOURCE != "ci":
        return
    pt, src = _prev_snapshot_meta()
    if src != "local":
        return
    if pt is None or (time.time() - pt) < FALLBACK_ALERT_AGE_SEC:
        return    # merely slow (Steam-throttled cycles) or a manual dispatch — CI may
        #           be covering, but that self-heals; only a long silence is worth a ping
    state = _load_state()
    try:
        last = datetime.fromisoformat(state.get("last_fallback_alert_utc")).timestamp()
    except Exception:
        last = 0
    if time.time() - last < FALLBACK_ALERT_GAP_SEC:
        return
    age_min = int((time.time() - pt) / 60)
    # fallback_active is set ONLY when the ping was actually delivered, so the
    # ✅ recovery ping in _standby_alerts always pairs with a real 📵 — an unsent
    # flag would otherwise emit lone ✅ noise every time a slow phone cycle let
    # one CI run proceed.
    if _post_discord(
            f"📵 **スマホランナーの更新が{age_min}分止まっています（CIフォールバック中）**。"
            f"よくある原因は2つ: (1) Steamが自宅IPを一時的に絞っていてスマホが遅い/失敗中"
            f"→何もしなくても自然復旧します。(2) スマホ側の問題（電源・Wi-Fi・Termux停止）"
            f"→スマホ再起動で復旧。CIはSteamに弾かれやすいので、長引くようならスマホの確認を。"
            f"復旧したら✅を送ります。"):
        state["fallback_active"] = True
        state["last_fallback_alert_utc"] = datetime.now(
            timezone.utc).isoformat(timespec="seconds")
        _write_state(state)


def _standby_alerts() -> None:
    """CI standby housekeeping (the phone is delivering again): send the recovery
    ping if a fallback was flagged, and relay the top-3 listing-unlock alert —
    the phone has no webhook, so it only WRITES unlocked3 into prices.json and
    this CI side does the talking. State rides the heartbeat commit."""
    state = _load_state()
    dirty = False
    if state.get("fallback_active"):
        if _post_discord("✅ **スマホランナー復旧**。CIはスタンバイに戻りました。"):
            state.pop("fallback_active", None)
            state.pop("last_fallback_alert_utc", None)
            dirty = True
    if not state.get("unlocked3_alerted"):
        try:
            doc = json.loads(OUT.read_text(encoding="utf-8"))
        except Exception:
            doc = {}
        if doc.get("unlocked3") and _maybe_alert_unlocked3(doc.get("items") or {}):
            state["unlocked3_alerted"] = True
            dirty = True
    if dirty:
        _write_state(state)


def _maybe_alert_unlocked3(items: dict[str, dict]) -> bool:
    """The 'top-3 grades are listable again' ping (roadmap: sometime 2026-07).
    Returns True once actually delivered so the caller can set the dedupe flag."""
    n = _count_top3_listed(items)
    return _post_discord(
        f"🔓 **上位3グレード（セレスティアル/ディバイン/コズミック）の出品解禁を検知**"
        f"（出品中の該当アイテム: {n}種）。サイトのガチャEVは自動で全グレード込みに切替"
        f"済みです。高額コインのプレミアムが圧縮される局面なので、コインの売却/回すの"
        f"判断はお早めに。誤検知なら `gh workflow run prices.yml -f force_unlocked3=0` "
        f"で戻せます。")


def _maybe_alert_stale() -> None:
    """Prolonged staleness watchdog. The phone runner is the primary source and the
    CI fallback is often Steam-blocked, so a long-stale `t` usually means THE PHONE
    IS DOWN and nobody noticed. CI keeps running (heartbeats) independent of the
    phone, so alert from here. Rate-limited via price_state.json, which the
    heartbeat/stateonly commits persist across runs. Best-effort: never raises."""
    hook = os.environ.get("DISCORD_WEBHOOK")
    if not hook:
        return
    pt, src = _prev_snapshot_meta()
    if pt is None:
        return
    age = time.time() - pt
    if age < STALE_ALERT_SEC:
        return
    state = _load_state()
    try:
        last = datetime.fromisoformat(state.get("last_stale_alert_utc")).timestamp()
    except Exception:
        last = 0
    if time.time() - last < STALE_ALERT_REPEAT_SEC:
        return
    msg = (f"📵 **価格が約{int(age / 3600)}時間更新されていません**（最終更新元: {src}）。"
           f"スマホランナーが止まっている可能性が高いです。スマホの再起動（または充電/Wi-Fi確認）"
           f"で自動復旧します。CIフォールバックはSteamに弾かれがちなので当てにしないでください。")
    try:
        requests.post(hook, json={"content": msg}, timeout=15)
        print(f"stale alert sent (t {int(age / 60)}min old)", file=sys.stderr)
    except Exception as e:
        print(f"stale alert failed: {e}", file=sys.stderr)
    state["last_stale_alert_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _write_state(state)


def _gate() -> str:
    """Decide what to do this run: "proceed" | "heartbeat" | "skip".

    Steam is hit ONLY on "proceed". workflow_dispatch always proceeds (manual). Else:
      - a LOCAL runner's snapshot is fresh (src=local) -> standby heartbeat: CI never
        touches Steam while a residential-IP runner is the primary source;
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
    # LOCAL-PRIMARY standby: a residential-IP runner (src=local, e.g. the phone via
    # scripts/phone_runner.sh) is delivering fresh snapshots — Steam serves homes fine
    # but blocks the shared Actions IPs, so CI stands down COMPLETELY (no Steam, no
    # snapshot) and just heartbeats to keep the self-restarting chain breathing. The
    # moment the local runner dies, `t` ages past LOCAL_FRESH_SEC and the next chain
    # run falls through to the normal proceed path = automatic CI fallback. The nap
    # paces the standby loop so heartbeat commits stay ~10 min apart, and the NEXT
    # run (fresh checkout) re-evaluates against the local runner's latest push.
    pt, src = _prev_snapshot_meta()
    if src == "local" and pt is not None and (now - pt) < LOCAL_FRESH_SEC:
        print(f"gate: local runner delivering (t {int((now - pt) / 60)}min old) "
              f"-> CI standby, heartbeat after {HEARTBEAT_SLEEP}s nap", file=sys.stderr)
        _standby_alerts()               # phone-recovery ping + unlocked3 relay (CI
        #                                 has the webhook; the phone only writes data)
        time.sleep(HEARTBEAT_SLEEP)
        return "heartbeat"
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
    changed (was failing), to avoid churning price_state.json every healthy run.
    UPDATE, don't replace: state also carries the alert-dedupe flags
    (fallback_active/unlocked3_alerted), which a wholesale rewrite would wipe."""
    state = _load_state()
    if state.get("consecutive_failures") or state.get("cooldown_until"):
        state.update({"consecutive_failures": 0, "cooldown_until": 0,
                      "last_recovered_utc":
                          datetime.now(timezone.utc).isoformat(timespec="seconds")})
        _write_state(state)
    _set_mode("snapshot")


def main() -> None:
    t0 = time.time()
    _maybe_alert_stale()
    action = _gate()
    if action == "skip":
        print("skipped (gate)", file=sys.stderr)
        return
    if action == "heartbeat":
        _heartbeat()
        print("heartbeat (cooling); chain kept alive without touching Steam", file=sys.stderr)
        return
    # PHASE 1 — FAST: lowest ask + listing count for a rotating page shard
    # (sweep), every other item carried from the previous snapshot (merge_carry),
    # median/volume carried too. Written + ready to push in minutes, so
    # prices.json `t` advances every cycle even if the detail phase below is
    # slow or dies.
    _maybe_alert_fallback()   # CI proceeding while the last snapshot says local
    #                           = the phone just went quiet -> one-shot ping
    try:
        prev_doc = json.loads(OUT.read_text(encoding="utf-8"))
    except Exception:
        prev_doc = {}
    fresh, soff = sweep(prev_doc)
    if not fresh:
        _on_failure()
        return
    fx = fetch_fx()
    # rate from FRESH items only (a carried/stale ask would skew it), then sanity-
    # checked against the previous rate / market fx (see sane_rate).
    rate = sane_rate(derive_rate(fresh), prev_doc.get("rate"), fx.get("JPY"))
    items = merge_carry(fresh, prev_doc)
    unlocked = detect_unlocked(items)      # reads previous OUT
    unlocked3 = detect_unlocked3(items, prev_doc)
    prev_off = int(prev_doc.get("_eoff", 0) or 0)
    carry_mv_baseline(items, prev_doc)     # carry prev median/volume onto all items
    carry_last_median(items, rate)
    update_history(items, rate)
    write_snapshot(items, rate, unlocked, unlocked3, prev_off, soff, fx)
    print(f"fast snapshot: {len(fresh)} fresh (_soff {prev_doc.get('_soff', 0) or 0}"
          f"->{soff}) / {len(items)} items, {OUT.stat().st_size // 1024} KB, "
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
    write_snapshot(items, rate, unlocked, unlocked3, eoff, soff, fx)
    print(f"final snapshot: {len(items)} items, rate {rate}, unlocked {unlocked}, "
          f"unlocked3 {unlocked3}, _eoff={eoff}, {OUT.stat().st_size // 1024} KB, "
          f"elapsed={time.time() - t0:.0f}s, signals={THROTTLE_SIGNALS}"
          f"{' [LOW-POWER: throttled]' if _throttled() else ''}")
    _on_success()
    # unlock ping from a CI-proceed run (fallback mode); in the normal phone-primary
    # regime the CI standby relays it instead (_standby_alerts — the phone has no
    # webhook, so _post_discord returns False there and the flag stays unset)
    if unlocked3:
        state = _load_state()
        if not state.get("unlocked3_alerted") and _maybe_alert_unlocked3(items):
            state["unlocked3_alerted"] = True
            _write_state(state)


if __name__ == "__main__":
    main()
