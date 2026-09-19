"""Merge two views of data/price_state.json instead of overwriting one with the other.

Both writers -- the CI chain (prices.yml) and the local runner (phone_runner.sh) --
regenerate their data files and then lay them on the remote tip, because prices.json
is rebuilt from scratch and must never be rebased. price_state.json does NOT work
that way: it is a shared health RECORD that each side only partly updates, so laying
it down wholesale reverts whatever the other side observed in the meantime.

That is how the 2026-09-19 false "詳細更新に問題があります" alert happened. CI stands
down while the phone is the price source, so its copy of `enrich_success_at` came
only from its own previous heartbeat commit. Each heartbeat republished that stale
value over the phone's fresh one, and because the chain re-fires on its own pushes,
the next CI run read its own commit back -- pinning the field at 15:25 for hours
until the 6h detail watchdog fired at 21:25, one minute after the phone had in fact
enriched successfully.

Ownership, field by field:
  - MONOTONIC observations take the LATER of the two. A success, or an alert we
    delivered, cannot be un-observed by the side that did not see it;
  - the enrich error report goes to whoever checked Steam most recently, as a GROUP:
    taking half from each side resurrects an error the other side already cleared;
  - everything else is the committer's own bookkeeping (cooldown, offsets, flags),
    so ours wins -- that is the value this run just computed.

Usage: merge_price_state.py OURS THEIRS   -> writes the merged state over THEIRS.
"""
import json
import sys
from pathlib import Path

# Both writers watch the same Steam, so for these the later observation is simply
# the true one. Some are epoch floats and some ISO strings; they are only ever
# compared against their own counterpart, never across fields.
MONOTONIC = ("enrich_success_at", "enrich_checked_at", "last_enrich_alert_at",
             "enrich_error_notified", "last_stale_alert_utc", "last_heartbeat_utc",
             "last_recovered_utc")
# Written and cleared together by _record_enrich_health, from ONE look at Steam:
# the error report, and the reason a cycle refreshed nothing.
ERROR_GROUP = ("enrich_error", "enrich_error_since", "enrich_last_skip")


def _newer(a, b) -> bool:
    """Is `a` a strictly later timestamp than `b`? A missing or oddly-typed side
    loses, so a malformed field can never win an argument against a good one."""
    if a is None:
        return False
    if b is None:
        return True
    try:
        return a > b
    except TypeError:
        return False


def merge(ours: dict, theirs: dict) -> dict:
    """Combine the remote tip's state (`theirs`) with the one this run built."""
    out = {**theirs, **ours}            # ours wins unless a rule below says otherwise
    for key in MONOTONIC:
        mine, other = ours.get(key), theirs.get(key)
        latest = other if _newer(other, mine) else mine
        if latest is None:
            out.pop(key, None)
        else:
            out[key] = latest
    # The side that talked to Steam last is the one whose error report is current.
    owner = theirs if _newer(theirs.get("enrich_checked_at"),
                             ours.get("enrich_checked_at")) else ours
    for key in ERROR_GROUP:
        if key in owner:
            out[key] = owner[key]
        else:
            out.pop(key, None)
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    ours_path, theirs_path = Path(argv[1]), Path(argv[2])
    ours = json.loads(ours_path.read_text(encoding="utf-8"))
    theirs = json.loads(theirs_path.read_text(encoding="utf-8")) if theirs_path.exists() else {}
    merged = merge(ours, theirs)
    # Same compact form _write_state uses, so a merge never shows up as a diff of
    # the whole file.
    theirs_path.write_text(json.dumps(merged, separators=(",", ":")), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
