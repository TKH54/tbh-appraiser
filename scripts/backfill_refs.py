"""Give a reference vector to catalog bases that never got one.

autocatalog.py only ever looks at names that are LISTED on the market but missing
from items.json. A base added to the catalog by hand is, by that definition, not
missing — so it is never a candidate, and refs.bin never learns its artwork. The
result is a base the site can price but the scanner can never recognise: it reads
as "?" forever, and because a "?" produces no crowd label, it cannot heal itself
the way a mis-read item eventually does.

Eleven bases were in that state on 2026-09-20, including Primordial Sap at ¥7,936
and Copper Nugget at 71,754 listings — an item almost every player is holding.

No market lookup is needed: items.json already carries each item's Steam CDN icon
hash from when it was added, so this only fetches the sprites themselves, from a
different host than the market APIs. Packing is autocatalog's own sprite_ref,
imported rather than copied: a ref computed over a different background sits at a
different point in the same space as the other 300-odd and would quietly mis-rank
against them.

Appending a ref changes what the matcher can answer, so a run of this is a MATCHER
CHANGE: push the result to a branch and let the regression gate measure it
(`gh workflow run autocatalog.yml -f gate_ref=<branch>`), exactly like tier 2.

    python scripts/backfill_refs.py            # dry run: say what is missing
    python scripts/backfill_refs.py --apply    # fetch sprites and append
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _autocatalog():
    """Import autocatalog.py for its packing + HTTP helpers (it is a script, not
    a package, and guards its own main)."""
    spec = importlib.util.spec_from_file_location("autocatalog", HERE / "autocatalog.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def plan(items: dict, refs: list[dict]) -> list[tuple[str, str]]:
    """(base, icon) for every catalog base with no ref, one entry per base.

    Deduped on the ICON as well: two bases that somehow share artwork must not
    get two refs holding the identical vector, because the matcher's top-1
    between them would be a coin flip — the same rule autocatalog applies when
    it packs a tier-2 batch.
    """
    known_bases = {r["base"] for r in refs}
    known_icons = {r["icon"] for r in refs}
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for value in items.values():
        base, icon = value["base"], value.get("icon")
        if base in known_bases or base in seen or not icon:
            continue
        if icon in known_icons:
            # The artwork is already in refs.bin under another base; a second ref
            # would add nothing and could only muddy the ranking between them.
            print(f"  . {base}: artwork already has a ref, skipping", file=sys.stderr)
            seen.add(base)
            continue
        out.append((base, icon))
        seen.add(base)
        known_icons.add(icon)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="write the data files (default is a dry run)")
    args = ap.parse_args()
    ac = _autocatalog()

    items = ac._read("items.json")
    refs = ac._read("refs.json")
    blob = bytearray((ac.DATA / "refs.bin").read_bytes())

    # Same two invariants autocatalog checks before it appends: a ragged refs.bin
    # or a count that disagrees with refs.json means something already went wrong,
    # and appending would bury it.
    if len(blob) % ac.REF_STRIDE:
        print("refs.bin is not a whole number of refs; refusing to append", file=sys.stderr)
        return 1
    n_before = len(blob) // ac.REF_STRIDE
    if n_before != len(refs):
        print(f"refs.bin has {n_before} refs but refs.json has {len(refs)}; "
              f"refusing to append", file=sys.stderr)
        return 1

    todo = plan(items, refs)
    if not todo:
        print("every catalog base already has a ref")
        return 0
    for base, icon in todo:
        print(f"  + {base}  ({icon[:24]}...)")
    print(f"{len(todo)} base(s) without a ref")

    if not args.apply:
        print("(dry run -- nothing written)")
        return 0

    session = ac._session()
    added = 0
    try:
        for base, icon in todo:
            time.sleep(ac.REQ_SLEEP)
            vec, mask = ac.sprite_ref(session, icon)
            blob += vec + mask
            refs.append({"base": base, "icon": icon})
            added += 1
    except Exception as e:
        # Keep whatever packed cleanly: a short run just means the next one has
        # less to do. Half a sprite is the thing we must never write, and
        # sprite_ref either returns a whole ref or raises.
        print(f"sprite fetch stopped after {added} ({type(e).__name__}: {e})",
              file=sys.stderr)
        if not added:
            return 1

    (ac.DATA / "refs.bin").write_bytes(bytes(blob))
    ac._write_compact("refs.json", refs)
    meta = ac._read("meta.json")
    meta["n_refs"] = len(refs)
    ac._write_indent1("meta.json", meta)
    print(f"+{added} ref(s) appended ({n_before} -> {n_before + added}); "
          f"needs the regression gate")
    return 0


if __name__ == "__main__":
    sys.exit(main())
