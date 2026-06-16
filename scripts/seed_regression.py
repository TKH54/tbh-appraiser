"""Offline seed-regression check — does the current learned_seed make crowd
labels resolve to the WRONG base?

Runs entirely on the stored Supabase crowd labels (signature + the base a user
assigned). NO game launch, NO screen capture, NO item ownership needed — items
you don't own are covered because other players labelled them.

For every collected label it re-runs nearest-ref recognition against
(catalog refs + learned_seed) and checks whether it still resolves to the SAME
base. With --baseline <old learned_seed.json> it diffs two seed sets so a
regression can be attributed to the latest change (was correct/unknown before ->
confidently WRONG now). That diff is traffic-independent: it does NOT depend on
how many users there are, so it cleanly separates "more users => more fixes" from
"this merge caused mis-matches => more fixes".

Caveat: this is an APPROXIMATION of the live matcher (one canonical sig per label
vs the app's extraction ensemble) — good for catching gross regressions, not a
pixel-perfect replica.

Run (needs the service_role key, same as promote_labels.py):
  SUPABASE_SERVICE_KEY=<secret> python scripts/seed_regression.py \
      [--baseline baseline_seed.json] [--bar 0.075]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

import numpy as np

from promote_labels import DATA, DEFAULT_URL, fetch_rows, load_refs, unpack, unpack_sig


def load_seed(path: Path):
    """learned_seed.json -> [(base, (vec float32[3072], valid uint8[1024]))]."""
    out = []
    for e in json.loads(Path(path).read_text(encoding="utf-8")):
        s = unpack(e["v"], e["m"])
        if s:
            out.append((e["base"], s))
    return out


def rate_report(rows, seeded_bases, hours):
    """Fixes (labels) per hour over the last `hours`, to see if fixing is rising or
    falling. 'seeded' = fix on an item we ALREADY recognise (should shrink if the
    recognition work is sticking; the long tail of NOT-yet-seeded items is healthy)."""
    import datetime as dt
    now = dt.datetime.now(dt.timezone.utc)
    per, seeded = {}, {}
    for r in rows:
        ts = r.get("created_at")
        if not ts:
            continue
        try:
            t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            continue
        b = int((now - t).total_seconds() // 3600)
        if b < 0 or b >= hours:
            continue
        per[b] = per.get(b, 0) + 1
        if r.get("base") in seeded_bases:
            seeded[b] = seeded.get(b, 0) + 1
    print(f"\nFIX RATE — labels/hour over the last {hours}h (newest first; "
          f"'seeded' = fix on an already-recognised item):")
    for b in range(hours):
        n = per.get(b, 0)
        print(f"  {b:2d}-{b+1:2d}h ago: {n:4d}  (seeded {seeded.get(b, 0)})")


def render_base(rows, base, n, out_path):
    """Render up to n crowd-label sigs (post-extraction 32x32) for one base, next
    to the clean catalog ref, so we can SEE where decorations land vs the base."""
    from PIL import Image, ImageDraw
    vecs = []
    for r in rows:
        if r.get("base") != base:
            continue
        s = unpack_sig(r.get("sig", ""))
        if s is not None:
            vecs.append(s[0])
        if len(vecs) >= n:
            break
    ref_vec = next((v for b, (v, _) in load_refs() if b == base), None)

    def to_img(vec):
        a = (np.clip(vec, 0, 1) * 255).astype(np.uint8).reshape(32, 32, 3)[:, :, ::-1]  # BGR->RGB
        return Image.fromarray(a, "RGB").resize((96, 96), Image.NEAREST)

    cells = ([("REF", to_img(ref_vec))] if ref_vec is not None else []) + \
            [(f"L{i}", to_img(v)) for i, v in enumerate(vecs)]
    cols = 8
    rowsn = (len(cells) + cols - 1) // cols
    canvas = Image.new("RGB", (96 * cols, 112 * rowsn), (18, 18, 26))
    d = ImageDraw.Draw(canvas)
    for i, (lab, img) in enumerate(cells):
        x, y = (i % cols) * 96, (i // cols) * 112
        canvas.paste(img, (x, y))
        d.text((x + 2, y + 98), lab, fill=(210, 210, 210))
    canvas.save(out_path)
    print(f"rendered ref + {len(vecs)} '{base}' label sigs -> {out_path}")


def diag_report(rows, seed_clusters, hours):
    """Which ALREADY-SEEDED items dominate recent re-fixes, and how many seed
    clusters each already has. Concentrated on a few items -> just promote their
    extra clusters; spread out / items with many clusters still re-fixed -> the
    appearance space (decorations/scale/light) isn't saturated -> needs better
    generalisation, not just more seeds."""
    import datetime as dt
    from collections import Counter
    now = dt.datetime.now(dt.timezone.utc)
    seeded = set(seed_clusters)
    on_seeded, total = Counter(), 0
    for r in rows:
        ts, b = r.get("created_at"), r.get("base")
        if not ts or not b:
            continue
        try:
            t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            continue
        if (now - t).total_seconds() > hours * 3600:
            continue
        total += 1
        if b in seeded:
            on_seeded[b] += 1
    tot_s = sum(on_seeded.values())
    print(f"\nSEEDED-ITEM RE-FIX DIAG (last {hours}h): {total} fixes, "
          f"{tot_s} on already-seeded items ({100*tot_s/max(total,1):.0f}%)")
    top = on_seeded.most_common(20)
    print(f"  top {len(top)} = {100*sum(n for _,n in top)/max(tot_s,1):.0f}% of seeded re-fixes")
    print(f"  {'base':34s} re-fixes  seed-clusters")
    for b, n in top:
        print(f"  {b:34s} {n:5d}      {seed_clusters.get(b, 0)}")


def edge_map(vec):
    """Luminance gradient-magnitude (matches recognize.js edgeMap). vec: (3072,) -> (1024,)."""
    lum = (0.114 * vec[0::3] + 0.587 * vec[1::3] + 0.299 * vec[2::3]).reshape(32, 32)
    gx = np.zeros((32, 32), np.float32); gy = np.zeros((32, 32), np.float32)
    gx[:, 1:-1] = lum[:, 2:] - lum[:, :-2]; gx[:, 0] = lum[:, 1] - lum[:, 0]; gx[:, -1] = lum[:, -1] - lum[:, -2]
    gy[1:-1, :] = lum[2:, :] - lum[:-2, :]; gy[0, :] = lum[1, :] - lum[0, :]; gy[-1, :] = lum[-1, :] - lum[-2, :]
    return np.sqrt(gx * gx + gy * gy).reshape(1024)


def edge_sweep(labels, bases, V, M, weights):
    """Threshold-free top1 over the big label set for each edge weight W:
    distance = colourMSE + W * edgeMSE (single-sig, the seed_regression approximation).
    Reports how full-edge changes RANKING accuracy at statistical scale."""
    R = V.shape[0]
    Vr = V.reshape(R, 1024, 3)
    REDGE = np.stack([edge_map(V[i]) for i in range(R)])          # (R,1024)
    correct = {w: 0 for w in weights}
    used = 0
    for base_L, (va, ma) in labels:
        keep = (M | ma)
        cnt = keep.sum(1)
        ok = cnt >= 60
        if not ok.any():
            continue
        used += 1
        cdiff = Vr - va.reshape(1024, 3)
        cmse = np.einsum("ijk,ijk->ij", cdiff, cdiff)
        cmse = (cmse * keep).sum(1) / np.maximum(cnt * 3, 1)
        qe = edge_map(va)
        ediff = REDGE - qe
        emse = (ediff * ediff * keep).sum(1) / np.maximum(cnt, 1)
        for w in weights:
            d = cmse + w * emse
            d[~ok] = 1e9
            if bases[int(np.argmin(d))] == base_L:
                correct[w] += 1
    return correct, used


def stack(refs):
    """[(base,(vec,valid))] -> (bases, V(R,3072) float32, M(R,1024) bool)."""
    bases = [b for b, _ in refs]
    V = np.stack([v for _, (v, _) in refs]).astype(np.float32)
    M = np.stack([m.astype(bool) for _, (_, m) in refs])
    return bases, V, M


def resolve_all(labels, bases, V, M, bar):
    """For each label return (claimed_base, resolved_base|None, dist).
    resolved is None when the nearest ref is farther than `bar` (= would show as
    a '?' for review in the app, i.e. NOT a confident false positive)."""
    R = V.shape[0]
    Vr = V.reshape(R, 1024, 3)
    res = []
    for claimed, (va, ma) in labels:
        keep = M | ma                       # (R,1024)
        cnt = keep.sum(1)                    # (R,)
        diff = Vr - va.reshape(1024, 3)      # (R,1024,3)
        per = np.einsum("ijk,ijk->ij", diff, diff)  # (R,1024) sum over 3 channels
        s = (per * keep).sum(1)              # (R,)
        with np.errstate(divide="ignore", invalid="ignore"):
            mse = s / (cnt * 3)
        mse[cnt < 60] = 1e9                  # too little overlap -> unusable
        j = int(np.argmin(mse))
        d = float(mse[j])
        res.append((claimed, bases[j] if d <= bar else None, d))
    return res


def classify(claimed, resolved):
    if resolved is None:
        return "unresolved"          # would show as '?' (safe — not a false positive)
    return "correct" if resolved == claimed else "mis"   # mis = confident WRONG id


def summarize(res):
    c = Counter(classify(cl, rb) for cl, rb, _ in res)
    return c["correct"], c["mis"], c["unresolved"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", help="prior learned_seed.json to diff against")
    ap.add_argument("--bar", type=float, default=0.075,
                    help="confident-match distance (app learned-ref auto bar = 0.075)")
    ap.add_argument("--edge-sweep",
                    help="comma weights (e.g. 0,0.1,0.3,0.5,1.0) -> threshold-free top1 per W")
    ap.add_argument("--rate", type=int, default=0,
                    help="hours: report fixes(labels)/hour over the last N hours")
    ap.add_argument("--diag", type=int, default=0,
                    help="hours: which already-seeded items dominate recent re-fixes")
    ap.add_argument("--render", help="base name: render its label sigs + ref to a PNG")
    ap.add_argument("--render-n", type=int, default=23)
    ap.add_argument("--render-out", default="probe.png")
    args = ap.parse_args()

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
    print(f"fetched {len(rows)} rows -> {len(labels)} valid catalog labels")

    if args.render:
        render_base(rows, args.render, args.render_n, args.render_out)
        return
    if args.rate or args.diag:
        seed_clusters = Counter(e["base"] for e in
                                json.loads((DATA / "learned_seed.json").read_text(encoding="utf-8")))
        if args.rate:
            rate_report(rows, set(seed_clusters), args.rate)
        if args.diag:
            diag_report(rows, seed_clusters, args.diag)
        return

    catalog = load_refs()
    cur_seed = load_seed(DATA / "learned_seed.json")
    cb, cV, cM = stack(catalog + cur_seed)
    if args.edge_sweep:
        weights = [float(x) for x in args.edge_sweep.split(",")]
        correct, used = edge_sweep(labels, cb, cV, cM, weights)
        b0 = correct[weights[0]]
        print(f"\nEDGE SWEEP (single-sig top1 over {used} labels, threshold-free; "
              f"distance = colourMSE + W*edgeMSE):")
        for w in weights:
            tag = "(baseline)" if w == weights[0] else f"{correct[w]-b0:+d} cells vs W={weights[0]}"
            print(f"  W={w:<5} top1={100*correct[w]/used:5.2f}%  ({correct[w]})  {tag}")
        return
    cur = resolve_all(labels, cb, cV, cM, args.bar)
    cc, cm, cu = summarize(cur)
    print(f"\nCURRENT learned_seed ({len(cur_seed)} entries) @ bar {args.bar}:")
    print(f"  correct {cc} | MIS(confident wrong) {cm} | unresolved/'?' {cu}  "
          f"(of {len(labels)})")

    lines = []
    if args.baseline:
        base_seed = load_seed(Path(args.baseline))
        bb, bV, bM = stack(catalog + base_seed)
        base = resolve_all(labels, bb, bV, bM, args.bar)
        regressions, improvements = [], 0
        for (cl, rb_cur, _), (_, rb_base, _) in zip(cur, base):
            cur_cls = classify(cl, rb_cur)
            base_cls = classify(cl, rb_base)
            if base_cls != "mis" and cur_cls == "mis":
                regressions.append((cl, rb_cur))      # got broken by the new seeds
            elif base_cls != "correct" and cur_cls == "correct":
                improvements += 1
        print(f"\nDIFF vs baseline ({len(base_seed)} entries) — attributable to the "
              f"{len(cur_seed) - len(base_seed)}-entry change:")
        print(f"  *** REGRESSIONS (now confidently WRONG): {len(regressions)} ***")
        print(f"      improvements (now correct): {improvements}")
        pairs = Counter((a, b) for a, b in regressions)
        for (a, b), n in pairs.most_common(25):
            an = f"{a}（{ja.get(a)}）" if ja.get(a) else a
            bn = f"{b}（{ja.get(b)}）" if ja.get(b) else b
            lines.append(f"- {an}  ->誤→  {bn}   ({n})")
            print("   " + lines[-1])
        verdict = ("CLEAN — no new mis-matches" if not regressions
                   else f"{len(regressions)} label(s) regressed — inspect above")
        print(f"\nVERDICT: {verdict}")

    summ = os.environ.get("GITHUB_STEP_SUMMARY")
    if summ:
        with open(summ, "a", encoding="utf-8") as f:
            f.write("### seed regression check\n")
            f.write(f"- labels tested: **{len(labels)}** (bar {args.bar})\n")
            f.write(f"- current: correct {cc} / **MIS {cm}** / unresolved {cu}\n")
            if args.baseline:
                f.write(f"- **regressions from this change: {len(regressions)}** "
                        f"(improvements {improvements})\n")
                f.write("".join("  " + ln + "\n" for ln in lines)
                        or ("  (none)\n" if not lines else ""))


if __name__ == "__main__":
    main()
