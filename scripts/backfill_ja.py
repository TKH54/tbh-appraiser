"""Fill in Japanese names for catalog entries that were added before the PC had
them, then drop their "untranslated" flag.

autocatalog folds a newly-listed item into items.json the moment the market
shows it, but the Japanese name of a brand-new BASE only exists in the game's
Unity string tables -- so an item added before someone ran localize.py on the
updated client carries `nj: 1` and shows its English market name.

autocatalog never revisits an existing entry (by design: it must not rewrite
rows a human may have corrected), so those flags would otherwise stick forever.
docs/PATCHDAY.md used to say "delete the entries and let autocatalog re-add
them", which costs a Steam round-trip per item on a runner that is already
rate-limited. This does the same job offline, from data/ja_names.json.

    python scripts/backfill_ja.py            # dry run
    python scripts/backfill_ja.py --apply

Only ever ADDS a name to a flagged entry. An entry that already has name_ja is
left exactly as it is, so a hand-fixed translation is never overwritten.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from autocatalog import NAME_RE, compose_ja  # identical composition rules

DATA = Path(__file__).resolve().parents[1] / "data"
ITEMS = DATA / "items.json"
JA = DATA / "ja_names.json"

# The key order build_web_data.build_items() and autocatalog.entry_for() emit.
# Rebuilding in that order keeps a backfilled row byte-identical to one that had
# the translation from the start.
KEY_ORDER = ["base", "rarity", "icon", "tradeable", "synth", "name_ja", "name_en"]


def backfill(items: dict, ja_bases: dict, ja_rarities: dict) -> tuple[dict, list]:
    out, filled = {}, []
    for h, e in items.items():
        if "name_ja" in e or not e.get("nj"):
            out[h] = e
            continue
        m = NAME_RE.match(h)
        base = (m.group(1).strip() if m else h)
        rarity = (m.group(2).strip() if m else "")
        variant = ((m.group(3) or "").strip() if m else "")
        ja = ja_bases.get(e.get("base") or base)
        if not ja:                          # still no translation for this base
            out[h] = e
            continue
        e = {**e, "name_ja": compose_ja(ja, rarity, ja_rarities, variant)}
        e.pop("nj", None)
        out[h] = {k: e[k] for k in KEY_ORDER if k in e} | {
            k: v for k, v in e.items() if k not in KEY_ORDER
        }
        filled.append((h, out[h]["name_ja"]))
    return out, filled


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write items.json")
    args = ap.parse_args()

    items = json.loads(ITEMS.read_text(encoding="utf-8"))
    ja = json.loads(JA.read_text(encoding="utf-8"))
    flagged = sum(1 for e in items.values() if e.get("nj"))
    out, filled = backfill(items, ja.get("bases", {}), ja.get("rarities", {}))

    print(f"entries: {len(items)}   flagged untranslated: {flagged}")
    print(f"backfilled: {len(filled)}   still untranslated: {flagged - len(filled)}")
    for h, name in filled[:20]:
        print(f"   {h:42} -> {name}")
    if len(filled) > 20:
        print(f"   ... and {len(filled) - 20} more")
    if not args.apply:
        print("(dry run -- nothing written)")
        return
    if filled:
        # Match the writer in build_web_data/autocatalog: compact separators, no
        # trailing newline, UTF-8 kept as real characters.
        ITEMS.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")),
                         encoding="utf-8")
        print("wrote", ITEMS)


if __name__ == "__main__":
    main()
