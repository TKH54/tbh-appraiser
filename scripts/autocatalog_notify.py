"""Pending-catalog digest. Reads site data; writes only an Actions cache file.

Run `observe` before folding, then `notify` after shipping. First-seen times are
observations, not Steam listing dates. A cache miss starts a new observation.
The workflow serializes runs; each immutable cache key includes run + attempt.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".autocatalog_notify/state.json"
DAY = 86400
NAME_RE = re.compile(r"^(.*?)\s*\(([^)]+)\)\s*([A-Za-z])?\s*$")


def observe(state, prices, items, now):
    old = state.get("pending", {})
    state["pending"] = {h: old.get(h, now) for h in sorted(set(prices) - set(items))}
    # This guard needs no icon lookup. The other two guards still belong to
    # autocatalog.py; do not claim they were checked when Steam refused us.
    variants = {}
    for item in items.values():
        variants.setdefault(item["base"], []).append(item["rarity"])
    lone = {b for b, rs in variants.items() if rs == [""]}
    state["blocked_without_icons"] = [h for h in state["pending"]
                                      if (m := NAME_RE.match(h))
                                      and m.group(1).strip() in lone]


def digest(state, now):
    pending = state["pending"]
    if not pending or now - state.get("last_sent", 0) < DAY:
        return None
    days = int((now - min(pending.values())) / DAY)
    blocked = len(set(state.get("blocked_without_icons", [])) & set(pending))
    return (f"⚠ **カタログ未取込が {len(pending)} 件あります**"
            f"（残っている項目の初回観測から最長 {days} 日）。"
            f"うち {blocked} 件は素材の等級追加で手動判断待ち。"
            "残りにはSteam取得待ち・画像確認待ち・回帰確認待ちが含まれます。"
            "429中もこの集計は続きます。通知は24時間に1回です。")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["observe", "notify"])
    args = ap.parse_args()
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    now = time.time()
    if args.action == "observe":
        prices = json.loads((ROOT / "data/prices.json").read_text(encoding="utf-8"))["items"]
        items = json.loads((ROOT / "data/items.json").read_text(encoding="utf-8"))
        observe(state, prices, items, now)
    else:
        # Candidates on a review PR are NOT shipped. Remove names only after
        # the corresponding push-to-main step has actually succeeded.
        shipped = set()
        if os.environ.get("PUSH1") == "success":
            report = json.loads((ROOT / "_autocatalog_t1_report.json").read_text(encoding="utf-8"))
            shipped.update(report["tier1"])
        if os.environ.get("PUSH2") == "success":
            shipped.update(json.loads((ROOT / "data/items.json").read_text(encoding="utf-8")))
        state["pending"] = {h: t for h, t in state["pending"].items() if h not in shipped}
        msg = digest(state, now)
        if state["pending"]:
            print(f"::warning::autocatalog: {len(state['pending'])} item(s) still pending")
        if msg:
            hook = os.environ.get("DISCORD_WEBHOOK")
            if not hook:
                raise RuntimeError("DISCORD_WEBHOOK is missing; pending digest was not sent")
            response = requests.post(hook, json={"content": msg + " → " + os.environ["RUN_URL"]},
                                     timeout=15, allow_redirects=False)
            # Never persist a delivery marker for an HTTP error.
            if not 200 <= response.status_code < 300:
                raise RuntimeError(f"Discord returned HTTP {response.status_code}")
            state["last_sent"] = now
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
