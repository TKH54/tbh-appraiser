"""Offender-trim — enforce the 100%-accuracy / no-false-positive policy mechanically.

A crowd-label promotion (promote_labels.py) APPENDS new seed clusters to
data/learned_seed.json. Some of those new clusters become the nearest-and-WRONG
ref for a DIFFERENT item's labels — turning a previously correct/'?' label into a
confident WRONG id. Per policy that is unacceptable regardless of the item's price
(money is NOT a factor — a ¥5 gear↔gear twin counts the same as a ¥5000 coin).

This script drops EXACTLY those offending NEW clusters, iterating until the batch
adds ZERO new mis-IDs, and writes the trimmed seed. Survivors keep the recognition
gains; the dropped clusters' items simply revert to '?' (the desired fallback).
"迷うもの(over-greedy clusters)は昇格させず ? に倒す" made automatic.

Efficiency: one full baseline resolve (catalog + old seed), then a cached
distance matrix to the NEW entries only (~hundreds), so each trim iteration is
pure numpy — no repeated O(labels x all-refs) passes.

  SUPABASE_SERVICE_KEY=<service_role secret> python scripts/trim_offenders.py \
      --baseline baseline_seed.json --candidate data/learned_seed.json \
      --out data/learned_seed.json [--bar 0.075] [--manifest review_manifest.json]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

import numpy as np

from promote_labels import DATA, DEFAULT_URL, fetch_rows, unpack, unpack_sig
from seed_regression import classify, load_refs, load_seed, resolve_all, stack


def new_entry_dists(labels, NV, NM, bar):
    """(L, Nnew) masked colour-MSE from each label to each NEW entry — the same
    metric resolve_all uses (mask union; <60 overlap px -> unusable)."""
    L, Nnew = len(labels), NV.shape[0]
    NVr = NV.reshape(Nnew, 1024, 3)
    D = np.empty((L, Nnew), np.float32)
    for li, (_claimed, (va, ma)) in enumerate(labels):
        keep = NM | ma                                   # (Nnew, 1024)
        cnt = keep.sum(1)
        diff = NVr - va.reshape(1024, 3)
        per = np.einsum("ijk,ijk->ij", diff, diff)
        s = (per * keep).sum(1)
        with np.errstate(divide="ignore", invalid="ignore"):
            mse = s / (cnt * 3)
        mse[cnt < 60] = 1e9
        D[li] = mse
    return D


def full_retrim(labels, claimed, bar, out, dry_run, ja):
    """FULL re-trim: baseline = catalog refs ONLY; EVERY current learned_seed
    cluster (new or long-shipped) is a removal candidate. Drops any cluster that
    is the nearest-and-WRONG ref for a label the catalog alone did not already
    mis, until no seed cluster causes a confident wrong id. Unlike the per-batch
    trim this can remove clusters that drifted into offenders after they shipped.
    Reports the COVERAGE COST (correct labels / distinct items that revert to '?')
    so the accuracy↔coverage tradeoff is explicit. Writes survivors unless dry-run."""
    L = len(labels)
    catalog = load_refs()
    cand_raw = json.loads((DATA / "learned_seed.json").read_text(encoding="utf-8"))
    cols, cbase, NVl, NMl = [], [], [], []
    for ci, e in enumerate(cand_raw):
        s = unpack(e["v"], e["m"])
        if s is None:
            continue
        cols.append(ci); cbase.append(e["base"]); NVl.append(s[0]); NMl.append(s[1].astype(bool))
    N = len(cols)
    base_arr = np.array(cbase, dtype=object)

    # catalog-only baseline: db + which labels the catalog alone already mis-IDs
    bb, bV, bM = stack(catalog)
    bres = resolve_all(labels, bb, bV, bM, bar)
    db = np.array([d for _, _, d in bres], np.float32)
    cat_is_mis = np.array([rb is not None and rb != cl for cl, rb, _ in bres])
    cat_resolved = [rb for _, rb, _ in bres]

    NV = np.stack(NVl).astype(np.float32); NM = np.stack(NMl)
    D = new_entry_dists(labels, NV, NM, bar)
    idx = np.arange(L)

    def state(removed):
        """resolved base per label given a set of removed seed columns."""
        live = [k for k in range(N) if k not in removed]
        if live:
            la = np.array(live); sub = D[:, live]
            loc = sub.argmin(1); dn = sub[idx, loc]; col = la[loc]
            win = (dn < db) & (dn <= bar)
        else:
            win = np.zeros(L, bool); col = np.zeros(L, int)
        resolved = [base_arr[col[i]] if win[i] else (cat_resolved[i] if db[i] <= bar else None)
                    for i in range(L)]
        return resolved, win, col

    def tally(resolved):
        c = sum(1 for i in range(L) if resolved[i] == claimed[i])
        m = sum(1 for i in range(L) if resolved[i] is not None and resolved[i] != claimed[i])
        return c, m

    res0, _, _ = state(set())
    c0, m0 = tally(res0)

    removed: set[int] = set()
    while True:
        live = [k for k in range(N) if k not in removed]
        if not live:
            break
        la = np.array(live); sub = D[:, live]
        loc = sub.argmin(1); dn = sub[idx, loc]; col = la[loc]
        win = (dn < db) & (dn <= bar)
        wrong = win & (base_arr[col] != claimed) & (~cat_is_mis)
        if not wrong.any():
            break
        removed.update(int(c) for c in np.unique(col[wrong]))

    res1, _, _ = state(removed)
    c1, m1 = tally(res1)
    items_before = {claimed[i] for i in range(L) if res0[i] == claimed[i]}
    items_after = {claimed[i] for i in range(L) if res1[i] == claimed[i]}
    lost_items = sorted(items_before - items_after)

    def f(b):
        return f"{b}（{ja[b]}）" if ja.get(b) else b
    print(f"\n================ FULL RE-TRIM {'(dry-run)' if dry_run else ''} "
          f"({N} clusters) ================")
    print(f"  クラスタ: {N} → 生存 {N - len(removed)}（削除 {len(removed)}）")
    print(f"  認識(正解)   : {c0} → {c1}   ({c1 - c0:+d})   ← これがカバー減少ぶん")
    print(f"  誤爆(確信誤り): {m0} → {m1}   ({m1 - m0:+d})   ← 残りはカタログ由来(シード削除では消せない)")
    print(f"  完全に認識できなくなるアイテム種: {len(lost_items)}（以前は当たっていたが全滅）")
    for b in lost_items[:30]:
        print(f"    - {f(b)}")

    # attribute the dropped offender clusters to their promotion batch, so a single
    # bad batch can be surgically rolled back (rollback_batch.py --batch <id>)
    # instead of paying the whole-seed coverage cost above.
    drop_batches = Counter(str(cand_raw[cols[k]].get("batch", "(untagged)")) for k in removed)
    if drop_batches:
        print("  削除クラスタの由来バッチ（ロールバック候補・多い順）:")
        for b, n in drop_batches.most_common(15):
            print(f"    - batch {b}: {n} クラスタ")

    summ = os.environ.get("GITHUB_STEP_SUMMARY")
    if summ:
        with open(summ, "a", encoding="utf-8") as fh:
            fh.write(f"### full re-trim {'(dry-run)' if dry_run else ''}\n"
                     f"- clusters {N} → kept {N - len(removed)} (dropped {len(removed)})\n"
                     f"- 認識(正解) {c0} → {c1} (**{c1 - c0:+d}** = coverage cost)\n"
                     f"- 誤爆 {m0} → {m1} ({m1 - m0:+d}; residual = catalog-caused)\n"
                     f"- 認識できなくなるアイテム種: **{len(lost_items)}**\n"
                     + ("- 削除バッチ: " + ", ".join(f"`{b}`×{n}" for b, n in drop_batches.most_common(10)) + "\n"
                        if drop_batches else ""))

    if not dry_run:
        drop = {cols[k] for k in removed}
        Path(out).write_text(
            json.dumps([e for ci, e in enumerate(cand_raw) if ci not in drop],
                       separators=(",", ":")), encoding="utf-8")
        print(f"\nwrote {out} ({N - len(removed)} clusters)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", help="seed BEFORE this promotion (per-batch mode)")
    ap.add_argument("--candidate",
                    help="seed AFTER this promotion (= baseline + appended new clusters)")
    ap.add_argument("--out", help="where to write the trimmed seed")
    ap.add_argument("--bar", type=float, default=0.075,
                    help="confident-match distance (app learned-ref auto bar = 0.075)")
    ap.add_argument("--manifest", help="review_manifest.json to prune to survivors (optional)")
    ap.add_argument("--full", action="store_true",
                    help="FULL re-trim: every current learned_seed cluster is a removal "
                         "candidate (baseline = catalog only). Reports coverage cost.")
    ap.add_argument("--dry-run", action="store_true", help="measure only; do not write")
    args = ap.parse_args()
    if not args.full and not (args.baseline and args.candidate and args.out):
        ap.error("per-batch mode needs --baseline, --candidate and --out (or use --full)")
    if args.full and not args.dry_run and not args.out:
        ap.error("--full write mode needs --out (or add --dry-run to only measure)")

    url = os.environ.get("SUPABASE_URL", DEFAULT_URL).rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not key:
        sys.exit("set SUPABASE_SERVICE_KEY (service_role secret)")

    items = json.loads((DATA / "items.json").read_text(encoding="utf-8"))
    bases_set = {v["base"] for v in items.values()}
    try:
        ja = json.loads((DATA / "ja_names.json").read_text(encoding="utf-8")).get("bases", {})
    except Exception:
        ja = {}

    rows = fetch_rows(url, key)
    labels = []
    for r in rows:
        if r.get("base") not in bases_set:
            continue
        sig = unpack_sig(r.get("sig", ""))
        if sig is None or int(sig[1].sum()) < 60:
            continue
        labels.append((r["base"], sig))
    L = len(labels)
    print(f"fetched {len(rows)} rows -> {L} valid catalog labels")
    claimed = np.array([b for b, _ in labels], dtype=object)

    if args.full:
        full_retrim(labels, claimed, args.bar, args.out, args.dry_run, ja)
        return

    catalog = load_refs()
    base_seed = load_seed(Path(args.baseline))
    cand_raw = json.loads(Path(args.candidate).read_text(encoding="utf-8"))
    base_sig = {(e["v"], e["m"]) for e in json.loads(Path(args.baseline).read_text(encoding="utf-8"))}

    # NEW = candidate clusters not present in baseline (promote appends them).
    new_cols, new_base = [], []          # new_cols[k] = candidate-array index of column k
    NVl, NMl = [], []
    for ci, e in enumerate(cand_raw):
        if (e["v"], e["m"]) in base_sig:
            continue
        s = unpack(e["v"], e["m"])
        if s is None:                    # malformed: can't match anything; keep it, don't trim
            continue
        new_cols.append(ci)
        new_base.append(e["base"])
        NVl.append(s[0]); NMl.append(s[1].astype(bool))
    Nnew = len(new_cols)
    print(f"candidate has {len(cand_raw)} entries; {Nnew} are NEW (vs baseline {len(base_sig)})")

    # Baseline resolution: db = nearest-ref distance, base_is_mis = already-wrong.
    bb, bV, bM = stack(catalog + base_seed)
    base_res = resolve_all(labels, bb, bV, bM, args.bar)
    db = np.array([d for _, _, d in base_res], np.float32)
    base_is_mis = np.array([rb is not None and rb != cl for cl, rb, _ in base_res])
    base_resolved = [rb for _, rb, _ in base_res]
    bc = sum(1 for cl, rb, _ in base_res if classify(cl, rb) == "correct")
    bm = int(base_is_mis.sum())

    if Nnew == 0:
        print("no new clusters to trim")
        Path(args.out).write_text(json.dumps(cand_raw, separators=(",", ":")), encoding="utf-8")
        _emit_outputs(0, 0)
        return

    NV = np.stack(NVl).astype(np.float32)
    NM = np.stack(NMl)
    new_base_arr = np.array(new_base, dtype=object)
    D = new_entry_dists(labels, NV, NM, args.bar)

    # Iteratively drop every NEW cluster that wins a label AND is wrong AND the
    # label was NOT already mis in baseline (= a regression caused by this batch).
    removed: set[int] = set()
    blame: Counter = Counter()           # (true, wrong) -> labels broken (for the log)
    idx = np.arange(L)
    while True:
        live = [k for k in range(Nnew) if k not in removed]
        if not live:
            break
        live_arr = np.array(live)
        sub = D[:, live]
        loc = sub.argmin(1)
        dn = sub[idx, loc]
        col = live_arr[loc]                          # winning new-column per label
        win_new = (dn < db) & (dn <= args.bar)       # a new cluster beats baseline & is confident
        win_base = new_base_arr[col]
        new_mis = win_new & (win_base != claimed) & (~base_is_mis)
        if not new_mis.any():
            break
        offenders = np.unique(col[new_mis])
        for li in np.where(new_mis)[0]:
            blame[(claimed[li], new_base_arr[col[li]])] += 1
        removed.update(int(c) for c in offenders)

    kept = Nnew - len(removed)

    # Final resolution with survivors, to report the net effect.
    live = [k for k in range(Nnew) if k not in removed]
    if live:
        live_arr = np.array(live)
        sub = D[:, live]
        loc = sub.argmin(1)
        dn = sub[idx, loc]
        col = live_arr[loc]
        win_new = (dn < db) & (dn <= args.bar)
    else:
        win_new = np.zeros(L, bool); col = np.zeros(L, int)
    fc = fm = fnew_mis = 0
    for li in range(L):
        if win_new[li]:
            resolved = new_base_arr[col[li]]
        else:
            resolved = base_resolved[li] if db[li] <= args.bar else None
        cls = classify(claimed[li], resolved)
        if cls == "correct":
            fc += 1
        elif cls == "mis":
            fm += 1
            if not base_is_mis[li]:
                fnew_mis += 1

    def f(b):
        return f"{b}（{ja[b]}）" if ja.get(b) else b

    print(f"\n================ OFFENDER-TRIM ({Nnew} new -> kept {kept}, dropped {len(removed)}) ================")
    print(f"  認識(正解)   : {bc:5d} → {fc:5d}   ({fc - bc:+d})   ← ↑がよい")
    print(f"  誤爆(確信誤り): {bm:5d} → {fm:5d}   ({fm - bm:+d})   ← ↓がよい")
    print(f"  この昇格が新たに生む確信誤爆: {fnew_mis}件   ← 0でなければNG")
    if blame:
        print(f"\n  削除した犯人クラスタが壊していたラベル（上位）:")
        for (tb, wb), n in blame.most_common(20):
            print(f"    - {f(tb)} ->誤→ {f(wb)}   (×{n})")

    Path(args.out).write_text(
        json.dumps([e for ci, e in enumerate(cand_raw)
                    if ci not in {new_cols[k] for k in removed}],
                   separators=(",", ":")),
        encoding="utf-8")
    print(f"\nwrote {args.out} ({kept + len(base_sig)} entries: baseline {len(base_sig)} + kept new {kept})")

    # Prune the review montage manifest so the PR shows only survivors.
    if args.manifest and Path(args.manifest).exists():
        removed_sig = {(cand_raw[new_cols[k]]["v"], cand_raw[new_cols[k]]["m"]) for k in removed}
        man = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
        man2 = [m for m in man if (m.get("v"), m.get("m")) not in removed_sig]
        Path(args.manifest).write_text(json.dumps(man2), encoding="utf-8")
        print(f"pruned manifest: {len(man)} -> {len(man2)}")

    _emit_outputs(kept, fnew_mis)
    if fnew_mis != 0:
        sys.exit(f"trim did not reach 0 new mis-IDs ({fnew_mis} left) — investigate")


def _emit_outputs(survivors: int, new_mis: int) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"survivors={survivors}\n")
            f.write(f"new_mis={new_mis}\n")
    summ = os.environ.get("GITHUB_STEP_SUMMARY")
    if summ:
        with open(summ, "a", encoding="utf-8") as f:
            f.write(f"### offender-trim\n- survivors kept: **{survivors}**\n"
                    f"- new mis-IDs after trim: **{new_mis}** (must be 0)\n")


if __name__ == "__main__":
    main()
