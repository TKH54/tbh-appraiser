#!/usr/bin/env python3
"""Network-free self-test for autocatalog.py, run on every autocatalog job.

The three `blocked` rules exist because each one is a way to create a confident
mis-ID that the tier-2 regression gate structurally cannot catch (two of them
move no ref at all). They are easy to delete by accident while refactoring, and
nothing else in CI would notice -- the damage only shows up later as items
resolving to the wrong base. So they get asserted here, along with the refs.bin
packing arithmetic and the exact on-disk shapes the site's loaders depend on.

Steam is stubbed out: resolve_icons and sprite_ref are replaced, so this runs
offline in a few seconds and never costs a rate-limit budget.

    python scripts/autocatalog_selftest.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STRIDE = 32 * 32 * 3 + 32 * 32

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def make_fixture(tmp: Path) -> Path:
    """A miniature but structurally real data/ directory."""
    d = tmp / "data"
    d.mkdir(parents=True)
    items = {
        # equipment base with two grades -> byRar path, safe to extend
        "Rune Sword (Immortal) A": {"base": "Rune Sword", "rarity": "Immortal",
                                    "icon": "ICON_SWORD", "tradeable": True,
                                    "synth": False, "name_ja": "ルーンソード（イモータル）A",
                                    "name_en": "Rune Sword (Immortal) A"},
        "Rune Sword (Arcana) A": {"base": "Rune Sword", "rarity": "Arcana",
                                  "icon": "ICON_SWORD", "tradeable": True,
                                  "synth": False, "name_ja": "ルーンソード（アルカナ）A",
                                  "name_en": "Rune Sword (Arcana) A"},
        # lone material -> pipeline.js isMaterial() applies the strict bar
        "Amber Gem": {"base": "Amber Gem", "rarity": "", "icon": "ICON_GEM",
                      "tradeable": True, "synth": False, "name_ja": "アンバージェム",
                      "name_en": "Amber Gem"},
    }
    (d / "items.json").write_text(json.dumps(items, ensure_ascii=False,
                                             separators=(",", ":")), encoding="utf-8")
    refs = [{"base": "Rune Sword", "icon": "sprite_000.png"},
            {"base": "Amber Gem", "icon": "sprite_001.png"}]
    (d / "refs.json").write_text(json.dumps(refs, ensure_ascii=False,
                                            separators=(",", ":")), encoding="utf-8")
    (d / "refs.bin").write_bytes(bytes(len(refs) * STRIDE))
    (d / "meta.json").write_text(json.dumps({"n_refs": len(refs)}, indent=1),
                                 encoding="utf-8")
    (d / "ja_names.json").write_text(json.dumps(
        {"bases": {"Rune Sword": "ルーンソード", "Amber Gem": "アンバージェム"},
         "rarities": {"Immortal": "イモータル", "Cosmic": "コズミック",
                      "Divine": "ディバイン"}},
        ensure_ascii=False, indent=1), encoding="utf-8")
    return d


def run(tmp: Path, listed: dict[str, str], tier: str = "all"):
    """Run main() against the fixture with Steam stubbed. `listed` maps a new
    market_hash_name to the icon Steam would report for it."""
    d = tmp / "data"
    items = json.loads((d / "items.json").read_text(encoding="utf-8"))
    prices = {"items": {h: {"p": 1.0, "q": 1} for h in list(items) + list(listed)}}
    (d / "prices.json").write_text(json.dumps(prices, ensure_ascii=False,
                                              separators=(",", ":")), encoding="utf-8")

    sys.path.insert(0, str(ROOT / "scripts"))
    import autocatalog as a
    import importlib
    importlib.reload(a)
    a.ROOT, a.DATA = tmp, d
    a.REPORT = tmp / "_autocatalog_report.json"
    a.resolve_icons = lambda sess, names, cache: {h: listed[h] for h in names if h in listed}
    # A real sprite fetch is the one thing that needs the network. The byte
    # layout is what matters here, so hand back a correctly-sized blob.
    a.sprite_ref = lambda sess, icon: (bytes([7]) * (32 * 32 * 3), bytes([1]) * (32 * 32))
    old = sys.argv
    sys.argv = ["autocatalog.py", "--tier", tier, "--apply"]
    try:
        rc = a.main()
    finally:
        sys.argv = old
    return (rc,
            json.loads((d / "items.json").read_text(encoding="utf-8")),
            json.loads((d / "refs.json").read_text(encoding="utf-8")),
            (d / "refs.bin").read_bytes(),
            json.loads((d / "meta.json").read_text(encoding="utf-8")),
            json.loads(a.REPORT.read_text(encoding="utf-8")))


def main() -> int:
    print("autocatalog self-test")

    # --- the three blocked rules -------------------------------------------
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        make_fixture(tmp)
        _, items, refs, blob, meta, rep = run(tmp, {
            "Rune Sword (Cosmic) A": "ICON_SWORD",       # tier 1: known art, same base
            "Amber Gem (Divine) A": "ICON_GEM",          # blocked (a): un-materials a base
            "Ember Gem (Divine) A": "ICON_GEM",          # blocked (b): known art, new base
            "Plague Sword (Divine) A": "ICON_NEW1",      # blocked (c): shares new art ...
            "Blight Sword (Divine) A": "ICON_NEW1",      # ... with a different new base
            "Rune Sword (Plagued) A": "ICON_NEW2",       # tier 2: known base, NEW artwork
        })
        check("tier 1 takes a new grade of a known base",
              "Rune Sword (Cosmic) A" in rep["tier1"])
        for name, why in [("Amber Gem (Divine) A", "un-materials a lone material"),
                          ("Ember Gem (Divine) A", "known artwork under a new base"),
                          ("Plague Sword (Divine) A", "two new bases share new artwork"),
                          ("Blight Sword (Divine) A", "two new bases share new artwork")]:
            check(f"blocked: {why} ({name.split(' (')[0]})",
                  name in rep["blocked"] and name not in items)
        check("an existing base gaining NEW artwork still gets its own ref",
              "Rune Sword (Plagued) A" in items
              and sum(1 for r in refs if r["base"] == "Rune Sword") == 2)

        # --- on-disk shapes the site's loaders depend on -------------------
        check("refs.bin stays a whole number of refs", len(blob) % STRIDE == 0)
        check("refs.bin, refs.json and meta.n_refs agree",
              len(blob) // STRIDE == len(refs) == meta["n_refs"],
              f"{len(blob)//STRIDE}/{len(refs)}/{meta['n_refs']}")
        check("appending leaves the existing refs byte-identical",
              blob[:2 * STRIDE] == bytes(2 * STRIDE))
        check("items.json is written compact and unescaped, no trailing newline",
              (tmp / "data" / "items.json").read_text(encoding="utf-8")
              == json.dumps(items, ensure_ascii=False, separators=(",", ":")))

        # --- entry shape ----------------------------------------------------
        e = items["Rune Sword (Cosmic) A"]
        check("entry key order matches build_web_data.build_items()",
              list(e) == ["base", "rarity", "icon", "tradeable", "synth",
                          "name_ja", "name_en"], str(list(e)))
        check("Japanese name composes with FULL-WIDTH parens and the variant",
              e["name_ja"] == "ルーンソード（コズミック）A", e["name_ja"])
        check("a known base needs no untranslated flag", "nj" not in e)
        nb = items["Rune Sword (Plagued) A"]
        check("an unknown GRADE falls back to the English grade word",
              nb.get("name_ja") == "ルーンソード（Plagued）A", str(nb.get("name_ja")))

    # --- a brand-new base carries the untranslated flag ---------------------
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        make_fixture(tmp)
        _, items, _, _, _, _ = run(tmp, {"Corrupted Soulstone": "ICON_CS"})
        e = items["Corrupted Soulstone"]
        check("a new BASE is flagged untranslated rather than half-Japanese",
              e.get("nj") == 1 and "name_ja" not in e, str(e))
        check("a material entry has empty rarity and stays tradeable",
              e["rarity"] == "" and e["tradeable"] is True)

    # --- the rotating window covers everything ------------------------------
    for n, size in ((101, 60), (250, 60), (61, 60)):
        seen, slot = set(), 0
        while len(seen) < n and slot < 1000:
            off = (slot * size) % n
            seen |= set((list(range(n))[off:] + list(range(n))[:off])[:size])
            slot += 1
        check(f"rotation covers all {n} pending items ({slot} slots)",
              len(seen) == n and slot <= 6, f"{len(seen)}/{n} in {slot}")

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)}")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
