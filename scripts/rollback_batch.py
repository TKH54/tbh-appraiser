"""Roll back ONE crowd-label promotion batch from data/learned_seed.json.

Every cluster promote_labels.py ships is tagged `"batch": <github_run_id>`. When an
auto-merged batch later turns out bad (a Discord alert, a --full re-trim that blames
a batch, a user report), this surgically removes just that batch's entries — undo one
promotion without the coverage cost of a full re-trim. Offline: it only rewrites the
JSON, so it needs no Supabase key and is safe to run/test locally.

  python scripts/rollback_batch.py --list                    # show batches present
  python scripts/rollback_batch.py --batch <id> [--dry-run]  # remove that batch
"""
from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "data"
SEED = DATA / "learned_seed.json"

UNTAGGED = "(untagged / pre-batch)"


def batch_of(entry: dict) -> str:
    return str(entry.get("batch")) if entry.get("batch") is not None else UNTAGGED


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="list batch ids present + counts")
    ap.add_argument("--batch", help="batch id to remove from the seed")
    ap.add_argument("--seed", default=str(SEED), help="learned_seed.json to operate on")
    ap.add_argument("--out", help="where to write (default: overwrite --seed)")
    ap.add_argument("--dry-run", action="store_true", help="report only, don't write")
    args = ap.parse_args()

    seed = json.loads(Path(args.seed).read_text(encoding="utf-8"))
    counts = Counter(batch_of(e) for e in seed)

    if args.list or not args.batch:
        print(f"{len(seed)} entries across {len(counts)} batch(es):")
        for b, n in sorted(counts.items(), key=lambda kv: (kv[0] == UNTAGGED, -kv[1])):
            print(f"  {n:5d}  {b}")
        if not args.batch:
            if not args.list:
                print("\n(pass --batch <id> to roll one back)")
            return

    target = str(args.batch)
    if target not in counts:
        print(f"[!] batch '{target}' not found - nothing to roll back. Use --list to see ids.")
        raise SystemExit(1)
    kept = [e for e in seed if batch_of(e) != target]
    removed = len(seed) - len(kept)
    print(f"\nbatch {target}: removing {removed} entr(ies) -> {len(kept)} remain "
          f"(was {len(seed)})")

    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"removed={removed}\n")
            f.write(f"remaining={len(kept)}\n")

    if args.dry_run:
        print("(dry-run: not writing)")
        return
    dest = Path(args.out) if args.out else Path(args.seed)
    dest.write_text(json.dumps(kept, separators=(",", ":")), encoding="utf-8")
    print(f"wrote {dest}")


if __name__ == "__main__":
    main()
