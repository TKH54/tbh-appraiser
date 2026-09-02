#!/usr/bin/env python3
"""Fold newly-listed Steam Market items into the catalog without a human.

WHY: every new item reaches the tool through two files -- data/items.json
(display) and data/refs.bin + data/refs.json (in-browser recognition). Until
now both were rebuilt by hand from the local monitor (catalog.py ->
build_web_data.py), which needs a PC with the game and the sprite cache. The
2026-09-08 Plaguelands update adds Lv90 gear, new runes, Corrupted Soulstones
and Corrosion materials all at once, so "wait for the author to be at the PC"
is the wrong failure mode.

The price bot already keeps data/prices.json in step with the market, so
anything listed but absent from items.json is, by definition, a new item.
Everything this script needs beyond that comes from Steam's public endpoints.

TIERS -- the split exists to honour the no-false-positive policy:

  tier 1  the item's Steam icon is one the catalog ALREADY has a ref for
          (a new grade of a known base: same artwork, different border).
          items.json only; refs.bin is untouched, so which BASE a capture
          matches cannot change. What does change is which grade of that base
          the cell resolves to, and that only moves the right way: pipeline.js
          picks by exact border rarity and previously had to fall back (or
          return "ambiguous") for a grade the catalog was missing.
          The one exception is carved out below as `blocked`.

  blocked three shapes that would create a confident mis-ID this script cannot
          gate away. Never applied, only reported.

          (a) a GRADED item whose base the catalog holds as a LONE material.
              pipeline.js calls a base a material only while it has a single
              ""-rarity variant, and that flag applies the strict 0.05
              auto-confirm bar, which exists because round material icons are
              attractors that mis-extracted gear collapses into at ~0.02-0.05.
              A graded sibling switches that bar off for the base.
              (302 bases today, 102 lone materials, 0 single-variant equipment.)

          (b) an item reusing a KNOWN icon under a NEW base. Steam icon <-> base
              is strictly 1:1 across the catalog today (302/302, no collisions)
              and the matcher relies on it: a ref resolves a sprite to exactly
              one base, so the newcomer's cells would come back as whichever
              base already owns that artwork.

          (c) two NEW bases sharing one NEW icon. Left alone this script would
              append two refs holding the identical vector under different
              bases, making the matcher's top-1 between them a coin flip -- a
              mis-ID of its own making. Both sides are held back.

          (a) and (b) move no ref at all, so the tier-2 regression gate could
          never have caught them.

  tier 2  brand-new artwork -> a new ref is APPENDED to refs.bin/refs.json.
          Appending never moves an existing ref (fixed 4096-byte stride, so
          indices stay put), but it does add a nearest-neighbour competitor
          that existing captures can now lose to. Gated on seed_regression
          in the workflow; this script only stages the change.

Japanese names come from the game's Unity string tables (localize.py), which
needs the updated client on a PC. Until that runs, a new item is written with
NO name_ja and a "nj" flag; app.js already falls back to the English market
name, and the flag is what lets the UI mark it as not-yet-translated.

Usage:
    python scripts/autocatalog.py --tier 1 --apply
    python scripts/autocatalog.py --tier 2 --apply
    python scripts/autocatalog.py --tier all          # dry run
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from io import BytesIO
from pathlib import Path

import cv2
import numpy as np
import requests
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

APPID = 3678970
CDN = "https://community.cloudflare.steamstatic.com/economy/image/"
SEARCH = "https://steamcommunity.com/market/search/render/"

# "Rune Sword (Arcana) A" -> base="Rune Sword" rarity="Arcana" variant="A"
NAME_RE = re.compile(r"^(.*?)\s*\(([^)]+)\)\s*([A-Za-z])?\s*$")

TRADEABLE = {"Legendary", "Immortal", "Arcana", "Beyond", "Celestial",
             "Divine", "Cosmic"}

# refs.bin layout, one ref: 32*32*3 uint8 vector + 32*32 uint8 mask.
VEC_BYTES, MASK_BYTES = 32 * 32 * 3, 32 * 32
REF_STRIDE = VEC_BYTES + MASK_BYTES

# Steam throttles hard and this job is never urgent (it runs on a schedule and
# anything it skips is picked up next run), so it stays deliberately small: a
# capped number of new items per run, a slow pace, and an immediate stop on the
# first 429 rather than a retry storm. See tbh-sweep-steam-throttle.
MAX_NEW_PER_RUN = int(os.environ.get("AUTOCAT_MAX") or 60)
REQ_SLEEP = float(os.environ.get("AUTOCAT_SLEEP") or 2.0)
# Steam's 429s are SPIKY, not a sustained ban: a request can be refused and the
# next one 3s later succeeds (observed 2026-09-01 from the residential IP). So a
# single 429 must not abort the run -- back off and retry -- but a run that
# keeps collecting them is genuinely rate-limited and should stop.
MAX_429 = int(os.environ.get("AUTOCAT_MAX_429") or 8)

# Kept OUT of data/: it is a per-run work record (including the resolved-icon
# cache the two workflow phases share), not site content, and committing it on
# every scheduled run would bury the real catalog diffs in noise.
REPORT = ROOT / "_autocatalog_report.json"


class Throttled(Exception):
    """Steam kept refusing us; give up for this run and retry on the next one."""


# ---------------------------------------------------------------- file I/O
# Each data file has its own on-disk shape and the repo diffs get reviewed by
# eye, so every writer here reproduces the style build_web_data.py used.
def _read(name: str):
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def _write_compact(name: str, obj) -> None:
    (DATA / name).write_text(
        json.dumps(obj, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8")


def _write_indent1(name: str, obj) -> None:
    (DATA / name).write_text(
        json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


# ------------------------------------------------------------------ Steam
def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": "tbh-appraiser-autocatalog/1.0"})
    s.n429 = 0                          # 429s seen across the whole run
    return s


def _get(sess, url, params=None, tries=4):
    """GET with backoff on 429. Raises Throttled once the run has collected
    MAX_429 refusals in total, which is the signal that backing off further is
    pointless and the job should just come back next schedule."""
    delay = 5.0
    r = None
    for _ in range(tries):
        try:
            r = sess.get(url, params=params, timeout=25)
        except requests.RequestException:
            time.sleep(delay)
            delay *= 3
            continue
        if r.status_code != 429:
            return r
        sess.n429 += 1
        if sess.n429 >= MAX_429:
            raise Throttled(f"{sess.n429} rate-limit replies this run")
        time.sleep(delay)
        delay *= 3
    return r


def resolve_icons(sess, names: list[str], cache: dict) -> dict[str, str]:
    """market_hash_name -> Steam CDN economy-image hash.

    One `query=` search per item. That is more requests per item than a paged
    sweep, but it is proportional to what is NEW (normally zero, a few dozen on
    an update day) instead of to the whole 900-item catalog -- which is the
    pattern that got the price bot rate-limited in 2026-07.

    Steam's search is fuzzy and caps a page at 10 results, so the exact item is
    not guaranteed to be first: measured 2026-09-01, both probes ranked 3rd out
    of ~15 hits. Two pages is comfortably enough; anything still unfound is
    deferred rather than guessed.

    `cache` carries icons resolved by an earlier phase of the same update (the
    workflow runs --tier 1 and --tier 2 as separate steps so tier 1 can ship
    without waiting for the regression gate). Reusing it keeps the request cost
    proportional to the new items, not to the number of phases.
    """
    out: dict[str, str] = {}
    for i, h in enumerate(names):
        if h in cache:                  # resolved by an earlier phase this run
            out[h] = cache[h]
            continue
        if i:
            time.sleep(REQ_SLEEP)
        found = False
        for start in (0, 10):
            r = _get(sess, SEARCH, params={
                "appid": APPID, "norender": 1, "count": 10,
                "start": start, "query": h})
            if r is None or r.status_code != 200:
                print(f"  ! {h}: HTTP {getattr(r, 'status_code', 'error')}",
                      file=sys.stderr)
                break
            try:
                d = r.json()
            except ValueError:
                print(f"  ! {h}: non-JSON reply", file=sys.stderr)
                break
            results = d.get("results") or []
            for res in results:
                if res.get("hash_name") == h:
                    icon = (res.get("asset_description") or {}).get("icon_url", "")
                    if icon:
                        out[h] = icon
                    found = True
                    break
            if found or len(results) < 10:
                break
            time.sleep(REQ_SLEEP)
        if not found:
            # Listed in prices.json but the search index cannot see it yet.
            # Leave it for a later run rather than guessing an icon.
            print(f"  . {h}: not in search yet, deferring", file=sys.stderr)
    return out


def sprite_ref(sess, icon: str) -> tuple[bytes, bytes]:
    """Download one sprite and pack it exactly as build_web_data.py would.

    The 291 refs already in refs.bin were built from sprites catalog.py had
    flattened onto BLACK with alpha_composite before saving, then read back
    through cv2 (BGR). Reproducing that byte-for-byte matters: a ref computed
    over a different background sits at a different point in the same space as
    the existing refs and would quietly mis-rank against them.
    """
    r = _get(sess, CDN + icon)
    if r is None:
        raise requests.RequestException("sprite fetch failed")
    r.raise_for_status()
    img = Image.open(BytesIO(r.content)).convert("RGBA")
    bg = Image.new("RGBA", img.size, (0, 0, 0, 255))
    flat = Image.alpha_composite(bg, img).convert("RGB")
    bgr = np.array(flat)[:, :, ::-1]                     # PIL RGB -> cv2 BGR

    g = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    ys, xs = np.where(g > 12)
    if len(xs) > 5:
        bgr = bgr[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    rv = cv2.resize(bgr, (32, 32),
                    interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0
    v = (np.clip(rv, 0, 1) * 255).astype(np.uint8)
    mask = (cv2.cvtColor(v, cv2.COLOR_BGR2GRAY) > 16).astype(np.uint8)
    return v.tobytes(), mask.tobytes()


# ------------------------------------------------------------------- main
def compose_ja(base_ja: str, rarity: str, rarity_ja: dict, variant: str) -> str:
    """Reproduce matcher._compose_ja exactly: '<base>（<rarity>）<variant>' with
    FULL-WIDTH parentheses for equipment, bare '<base>' for materials. Getting
    the punctuation wrong would make every auto-added name look foreign next to
    the 1082 that localize.py produced."""
    if not rarity:
        return base_ja
    return f"{base_ja}（{rarity_ja.get(rarity, rarity)}）{variant or ''}".rstrip()


def entry_for(h: str, icon: str, ja_bases: dict, ja_rarities: dict) -> dict:
    m = NAME_RE.match(h)
    base = m.group(1).strip() if m else h
    rarity = m.group(2).strip() if m else ""
    variant = (m.group(3) or "").strip() if m else ""
    ja = ja_bases.get(base)
    # Key order mirrors build_web_data.build_items() so an auto-added entry is
    # indistinguishable from the 1082 hand-built ones in a diff.
    e = {
        "base": base,
        "rarity": rarity,
        "icon": icon,
        "tradeable": (not rarity) or rarity in TRADEABLE,
        # discovered BECAUSE it is listed, so never a synth-only grade
        "synth": False,
    }
    if ja:
        # A new GRADE of a base the game already localised: the Japanese name is
        # fully derivable, so it needs no flag and no trip to the PC.
        e["name_ja"] = compose_ja(ja, rarity, ja_rarities, variant)
    e["name_en"] = h
    if not ja:
        # A new BASE. matcher._compose_ja would splice the English base into a
        # Japanese frame ("Empire Gloves（イミュータル）A"); showing the clean
        # English market name and flagging it is honest instead of half-done.
        e["nj"] = 1
    return e


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", choices=["1", "2", "all"], default="all")
    ap.add_argument("--apply", action="store_true",
                    help="write the data files (default is a dry run)")
    args = ap.parse_args()

    items = _read("items.json")
    prices = _read("prices.json")["items"]
    pending = sorted(set(prices) - set(items))

    # A previous phase this run already paid for these lookups.
    prev = {}
    if REPORT.exists():
        try:
            prev = json.loads(REPORT.read_text(encoding="utf-8"))
        except ValueError:
            prev = {}
    # Which pending items this run works on. Rotating beats always taking the
    # alphabetically-first N: an item Steam's search index cannot resolve stays
    # pending forever, so a fixed head would let a few such names sit at the
    # front and starve everything behind them. The offset steps by a WHOLE
    # window per 6h slot (stepping by one would take ~10 days to cover 101
    # pending items instead of two slots).
    if prev.get("window"):
        # phase 2 of the same run: keep phase 1's window so both phases agree
        # even if the 6h slot ticked over between them, minus whatever phase 1
        # already folded into items.json.
        still = set(pending)
        new_names = [h for h in prev["window"] if h in still]
    elif len(pending) > MAX_NEW_PER_RUN:
        slot = int(time.time()) // 21600
        off = (slot * MAX_NEW_PER_RUN) % len(pending)
        new_names = (pending[off:] + pending[:off])[:MAX_NEW_PER_RUN]
    else:
        new_names = pending
    cache = {k: v for k, v in (prev.get("icons") or {}).items() if k in new_names}

    report = {"checked": len(prices), "pending": len(pending),
              "new": len(new_names), "window": list(new_names), "tier1": [],
              "tier2": [], "deferred": [], "blocked": [], "throttled": False,
              "refs_added": 0, "icons": dict(cache)}

    def save_report():
        REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=1),
                          encoding="utf-8")

    if not new_names:
        print("no new market items; the catalog is in step")
        save_report()
        return 0

    print(f"{len(new_names)} item(s) listed but missing from the catalog")
    sess = _session()
    known_icons = {v.get("icon") for v in items.values() if v.get("icon")}
    try:
        icons = resolve_icons(sess, new_names, cache)
    except Throttled as e:
        print(f"steam throttled us ({e}); leaving the catalog alone",
              file=sys.stderr)
        report["throttled"] = True
        report["icons"] = dict(cache)
        save_report()
        return 0                      # not a failure: just try again next run

    # A GRADED sibling of a base the catalog currently holds as a lone material
    # must never be folded in unattended. pipeline.js treats a base as a
    # material only while it has exactly one ""-rarity variant, and that flag is
    # what applies the STRICT 0.05 auto-confirm bar -- which exists because a
    # round material icon is an attractor that mis-extracted gear collapses into
    # at ~0.02-0.05. Adding a graded sibling silently turns that bar off for the
    # base. It changes no ref, so the tier-2 regression gate would never see it;
    # the only safe answer is to leave it for a human.
    variants = {}
    for v in items.values():
        variants.setdefault(v["base"], []).append(v["rarity"])
    lone_materials = {b for b, rs in variants.items() if len(rs) == 1 and rs[0] == ""}

    # Steam icon <-> base is strictly 1:1 across the whole catalog today (302
    # icons, 302 bases, zero collisions), and the matcher depends on it: a ref
    # resolves a sprite to exactly ONE base. An item that reuses a known icon
    # under a NEW base would look like tier 1 (icon already has a ref, so no ref
    # change) while actually being unresolvable -- its cells would come back as
    # the base that already owns that artwork, i.e. a confident mis-ID. No ref
    # moves, so the tier-2 gate cannot see it either.
    icon_owner = {}
    for v in items.values():
        if v.get("icon"):
            icon_owner.setdefault(v["icon"], v["base"])

    def base_of(h: str) -> str:
        m = NAME_RE.match(h)
        return m.group(1).strip() if m else h

    # Same 1:1 rule, but for collisions BETWEEN the newcomers: two new bases
    # sharing one new icon would have this script append two refs holding the
    # identical vector under different bases, so the matcher's top-1 between
    # them is a coin flip. That is a mis-ID this script would have created
    # itself, so neither side may go in unattended.
    new_icon_bases: dict[str, set] = {}
    for h in new_names:
        if h in icons and icons[h] not in icon_owner:
            new_icon_bases.setdefault(icons[h], set()).add(base_of(h))
    contested = {i for i, bs in new_icon_bases.items() if len(bs) > 1}

    def blocked(h: str) -> str | None:
        """Reason this item must not be folded in unattended, or None."""
        b = base_of(h)
        if b in lone_materials:
            return "would un-material an existing base"
        owner = icon_owner.get(icons[h])
        if owner is not None and owner != b:
            return f"reuses {owner!r}'s artwork under a new base"
        if icons[h] in contested:
            others = sorted(new_icon_bases[icons[h]] - {b})
            return f"shares brand-new artwork with {', '.join(map(repr, others))}"
        return None

    usable = [h for h in new_names if h in icons and not blocked(h)]
    t1 = [(h, icons[h]) for h in usable if icons[h] in known_icons]
    t2 = [(h, icons[h]) for h in usable if icons[h] not in known_icons]
    report["tier1"] = [h for h, _ in t1]
    report["tier2"] = [h for h, _ in t2]
    report["deferred"] = [h for h in new_names if h not in icons]
    reasons = {h: blocked(h) for h in new_names if h in icons}
    report["blocked"] = [h for h, r in reasons.items() if r]
    for h in report["blocked"]:
        print(f"BLOCKED {h}: {reasons[h]} -- needs a human", file=sys.stderr)
    report["icons"] = dict(icons)

    _ja = _read("ja_names.json")
    ja_bases = _ja.get("bases") or {}
    ja_rarities = _ja.get("rarities") or {}
    want1 = args.tier in ("1", "all")
    want2 = args.tier in ("2", "all")
    changed = False

    if want1 and t1:
        for h, icon in t1:
            items[h] = entry_for(h, icon, ja_bases, ja_rarities)
        changed = True
        print(f"tier 1: +{len(t1)} item(s), refs untouched")

    if want2 and t2:
        refs_meta = _read("refs.json")
        blob = bytearray((DATA / "refs.bin").read_bytes())
        if len(blob) % REF_STRIDE:
            print("refs.bin is not a whole number of refs; refusing to append",
                  file=sys.stderr)
            save_report()
            return 1
        n_before = len(blob) // REF_STRIDE
        if n_before != len(refs_meta):
            print(f"refs.bin has {n_before} refs but refs.json has "
                  f"{len(refs_meta)}; refusing to append", file=sys.stderr)
            save_report()
            return 1
        added = 0
        try:
            for h, icon in t2:
                m = NAME_RE.match(h)
                base = m.group(1).strip() if m else h
                if any(r["base"] == base for r in refs_meta):
                    # another grade of a base a previous loop already packed
                    items[h] = entry_for(h, icon, ja_bases, ja_rarities)
                    continue
                time.sleep(REQ_SLEEP)
                v, mask = sprite_ref(sess, icon)
                blob += v + mask
                # The 291 existing entries carry a sprite FILENAME here
                # (sprite_000.png), but no sprite file is shipped with the site
                # and nothing reads this field -- it is vestigial. The CDN hash
                # is at least the sprite's real identity, so store that.
                refs_meta.append({"base": base, "icon": icon})
                items[h] = entry_for(h, icon, ja_bases, ja_rarities)
                added += 1
        except Throttled as e:
            print(f"steam throttled us mid-sprite ({e}); keeping what we packed",
                  file=sys.stderr)
            report["throttled"] = True
        except requests.RequestException as e:
            print(f"sprite fetch failed ({e}); keeping what we packed",
                  file=sys.stderr)
        report["refs_added"] = added
        if added:
            if args.apply:
                (DATA / "refs.bin").write_bytes(bytes(blob))
                _write_compact("refs.json", refs_meta)
                meta = _read("meta.json")
                meta["n_refs"] = len(refs_meta)
                _write_indent1("meta.json", meta)
            changed = True
            print(f"tier 2: +{added} ref(s) appended "
                  f"({n_before} -> {n_before + added}); needs the regression gate")

    if changed and args.apply:
        _write_compact("items.json", items)
        print("catalog written")
    elif changed:
        print("(dry run -- nothing written)")

    if report["deferred"]:
        print(f"deferred to a later run: {len(report['deferred'])}")
    save_report()
    return 0


if __name__ == "__main__":
    sys.exit(main())
