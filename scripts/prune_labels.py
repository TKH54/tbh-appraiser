"""Prune old rows from the Supabase `labels` table (archive-first, never lossy).

WHY: a DB trigger rejects inserts once the table holds 50,000 rows ("collection
full") — discovered 2026-07-12 when the fix rate flatlined at 0/h with the table
at exactly 50k. Collection must stay open, but the history must not be lost: the
gzip-JSONL snapshot (see fetch_rows in promote_labels.py) is the durable archive
(Actions cache + GitHub release asset), so rows already snapshotted can be
deleted from Supabase. Keeping the table small also keeps free-tier DB size and
any future full-fetch egress down.

Safety gates (any failure aborts before a single DELETE):
  sync     : fetch_rows() first pulls any rows newer than the snapshot into it
  coverage : EVERY id about to be deleted must exist in the snapshot
  keep     : the newest --keep rows (by id) are never touched

Run (CI; needs the service_role secret + the snapshot):
  SUPABASE_SERVICE_KEY=... LABELS_SNAPSHOT=.labels_snapshot/labels.jsonl.gz \
    python scripts/prune_labels.py [--keep 20000] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

from promote_labels import DEFAULT_URL, _snapshot_load, fetch_rows
from pathlib import Path

CHUNK = 5000  # ids per DELETE request (contiguous id range, so it's exact)


def _delete(url: str, headers: dict) -> None:
    req = urllib.request.Request(url, headers=headers, method="DELETE")
    with urllib.request.urlopen(req, timeout=120):
        pass


def _table_ids(url: str, headers: dict) -> list[int]:
    """All ids currently in the table (id-only keyset paging — a few bytes/row)."""
    ids, last, page = [], None, 10000
    while True:
        q = f"{url}/rest/v1/labels?select=id&order=id.asc&limit={page}" + (
            f"&id=gt.{last}" if last is not None else "")
        with urllib.request.urlopen(urllib.request.Request(q, headers=headers), timeout=60) as r:
            batch = json.loads(r.read())
        if not batch:
            break
        ids += [row["id"] for row in batch]
        last = batch[-1]["id"]
        if len(batch) < page:
            break
    return ids


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", type=int, default=20000, help="newest rows (by id) to keep in the table")
    ap.add_argument("--dry-run", action="store_true", help="report what would be deleted, delete nothing")
    args = ap.parse_args()

    url = os.environ.get("SUPABASE_URL", DEFAULT_URL).rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    snap_path = os.environ.get("LABELS_SNAPSHOT", "")
    if not key:
        print("set SUPABASE_SERVICE_KEY (service_role secret)", file=sys.stderr)
        return 1
    if not snap_path:
        print("set LABELS_SNAPSHOT -- pruning without the archive would lose data", file=sys.stderr)
        return 1
    headers = {"apikey": key, "Authorization": "Bearer " + key}

    # sync: make sure the snapshot holds everything the table holds
    fetch_rows(url, key)
    snap_ids = {r["id"] for r in _snapshot_load(Path(snap_path))}
    if not snap_ids:
        print("snapshot is empty/unreadable -- refusing to prune", file=sys.stderr)
        return 1

    ids = _table_ids(url, headers)
    print(f"table: {len(ids)} rows; snapshot: {len(snap_ids)} rows (full history)")
    to_del = sorted(ids)[:-args.keep] if len(ids) > args.keep else []
    if not to_del:
        print(f"table already at/below --keep {args.keep}; nothing to prune")
        return 0

    missing = [i for i in to_del if i not in snap_ids]
    if missing:
        print(f"ABORT: {len(missing)} rows to delete are NOT in the snapshot "
              f"(e.g. id {missing[:5]}) -- would lose data", file=sys.stderr)
        return 1
    print(f"prune plan: delete {len(to_del)} oldest rows (id {to_del[0]}..{to_del[-1]}), "
          f"keep newest {args.keep} -- all {len(to_del)} are archived in the snapshot")
    if args.dry_run:
        print("dry-run: no rows deleted")
        return 0

    for i in range(0, len(to_del), CHUNK):
        chunk = to_del[i:i + CHUNK]
        _delete(f"{url}/rest/v1/labels?id=gte.{chunk[0]}&id=lte.{chunk[-1]}", headers)
        print(f"deleted {min(i + CHUNK, len(to_del))}/{len(to_del)}")

    remain = len(_table_ids(url, headers))
    print(f"done: table now {remain} rows (expected {len(ids) - len(to_del)})")
    return 0 if remain == len(ids) - len(to_del) else 1


if __name__ == "__main__":
    sys.exit(main())
