"""Promote crowd-sourced recognition labels (Supabase) into data/learned_seed.json.

Collection (the browser app, public insert-only anon key) and PROMOTION (this
script, service_role key) are deliberately separate so a wrong/malicious label
can never reach users automatically. A label ships only if it survives every
gate below — and a human still reviews the PR this produces before it merges.
The user's own LOCAL labels always override on their machine regardless.

Gates:
  catalog     : base must exist in data/items.json
  dedup       : near-identical signatures from one capture collapse to one vote
                (a single person re-submitting can't fake a crowd)
  consensus   : a signature cluster needs >= K distinct submissions, all agreeing
  conflict    : reject a cluster that collides with a DIFFERENT base (ambiguous)
  redundancy  : drop labels already matched well by the bundled refs ...
  already-seeded : ... or already present in learned_seed.json (keeps re-runs idempotent)

Output: merges survivors into data/learned_seed.json (same {base,rarity,v,m}
format build_web_data.py emits). With --dry-run it only reports.

Run:
  SUPABASE_SERVICE_KEY=<service_role secret> python scripts/promote_labels.py [--k 3] [--dry-run]
  (SUPABASE_URL defaults to the project URL; the service_role key is a SECRET —
   pass via env / GitHub Secret, never commit it, never put it in the browser app.)
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "data"
SEED = DATA / "learned_seed.json"

DEFAULT_URL = "https://ebtaabxbfracykncjhfc.supabase.co"  # public (in the client too)
DEDUP_DIST = 0.008     # sigs closer than this are "the same capture" -> one vote
CONSENSUS_DIST = 0.06  # sigs within this form one cluster (same item, varied captures)
DEFAULT_K = 3          # distinct submissions required to promote
REDUNDANT_DIST = 0.05  # already matched this well by an existing ref/seed -> skip


def fetch_rows(url: str, key: str) -> list[dict]:
    """All label rows via the REST API using the service_role key (bypasses RLS)."""
    rows, offset, page = [], 0, 1000
    while True:
        req = urllib.request.Request(
            f"{url}/rest/v1/labels?select=*&order=created_at.asc"
            f"&offset={offset}&limit={page}",
            headers={"apikey": key, "Authorization": "Bearer " + key})
        with urllib.request.urlopen(req, timeout=30) as r:
            batch = json.loads(r.read())
        rows += batch
        if len(batch) < page:
            break
        offset += page
    return rows


def unpack(v_b64: str, m_b64: str):
    """base64 v,m -> (vec float32[3072], valid uint8[1024]). None if malformed."""
    try:
        v = np.frombuffer(base64.b64decode(v_b64), np.uint8).astype(np.float32) / 255.0
        m = np.frombuffer(base64.b64decode(m_b64), np.uint8)
        if v.size != 3072 or m.size != 128:
            return None
        valid = np.unpackbits(m, bitorder="little")[:1024].astype(np.uint8)
        return v, valid
    except Exception:
        return None


def unpack_sig(sig_json: str):
    """Supabase 'sig' column is a JSON string {v,m}."""
    try:
        d = json.loads(sig_json)
        return unpack(d["v"], d["m"])
    except Exception:
        return None


def masked_mse(a, b) -> float:
    """Symmetric masked colour-MSE between two (vec, valid) sigs — same metric
    the browser matcher uses."""
    (va, ma), (vb, mb) = a, b
    keep = (ma | mb).astype(bool)
    if keep.sum() < 60:
        return 1e9
    ka = np.repeat(keep, 3)
    diff = (va - vb)[ka]
    return float((diff * diff).sum() / (keep.sum() * 3))


def cluster(sigs: list, thr: float) -> list[list[int]]:
    """Greedy single-link clustering of signature indices by masked-MSE < thr."""
    clusters: list[list[int]] = []
    reps: list = []
    for i, s in enumerate(sigs):
        placed = False
        for c, rep in zip(clusters, reps):
            if masked_mse(s, rep) < thr:
                c.append(i); placed = True; break
        if not placed:
            clusters.append([i]); reps.append(s)
    return clusters


def load_refs():
    """Bundled catalog refs (32x32 vec + mask) from data/refs.bin + refs.json,
    used by the redundancy gate."""
    meta = json.loads((DATA / "refs.json").read_text(encoding="utf-8"))
    buf = np.frombuffer((DATA / "refs.bin").read_bytes(), np.uint8)
    stride = 3072 + 1024
    refs = []
    for i, m in enumerate(meta):
        o = i * stride
        v = buf[o:o + 3072].astype(np.float32) / 255.0
        valid = buf[o + 3072:o + stride]
        refs.append((m["base"], (v, valid)))
    return refs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=DEFAULT_K, help="min distinct submissions")
    ap.add_argument("--dry-run", action="store_true", help="report only, don't write")
    args = ap.parse_args()

    url = os.environ.get("SUPABASE_URL", DEFAULT_URL).rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not key:
        print("set SUPABASE_SERVICE_KEY (service_role secret)", file=sys.stderr)
        sys.exit(2)

    items = json.loads((DATA / "items.json").read_text(encoding="utf-8"))
    bases = {v["base"] for v in items.values()}
    refs = load_refs()
    try:                                   # English->Japanese names for a readable report
        ja = json.loads((DATA / "ja_names.json").read_text(encoding="utf-8"))
        ja_bases, ja_rar = ja.get("bases", {}), ja.get("rarities", {})
    except Exception:
        ja_bases, ja_rar = {}, {}

    # existing seed, indexed by base for the already-seeded gate (idempotent re-runs)
    seed = json.loads(SEED.read_text(encoding="utf-8"))
    seeded = defaultdict(list)
    for e in seed:
        s = unpack(e["v"], e["m"])
        if s:
            seeded[e["base"]].append(s)

    rows = fetch_rows(url, key)
    print(f"fetched {len(rows)} rows")

    parsed = []
    for r in rows:
        if r.get("base") not in bases:
            continue
        sig = unpack_sig(r.get("sig", ""))
        if sig is None or int(sig[1].sum()) < 60:
            continue
        parsed.append({"base": r["base"], "rarity": r.get("rarity"), "sig": sig})
    print(f"{len(parsed)} valid rows after catalog/format gate")

    promoted, details, stats = [], [], defaultdict(int)
    by_base = defaultdict(list)
    for p in parsed:
        by_base[p["base"]].append(p)

    for base, group in by_base.items():
        sigs = [g["sig"] for g in group]
        dedup_clusters = cluster(sigs, DEDUP_DIST)         # collapse same-capture repeats
        reps = [sigs[c[0]] for c in dedup_clusters]
        cons = cluster(reps, CONSENSUS_DIST)               # consensus over distinct submissions
        for c in cons:
            if len(c) < args.k:
                stats["below_consensus"] += 1
                continue
            rep = reps[c[0]]
            # conflict: does any OTHER base have a near-identical cluster rep?
            conflict = False
            for ob, og in by_base.items():
                if ob == base:
                    continue
                if any(masked_mse(rep, g["sig"]) < CONSENSUS_DIST * 0.6 for g in og):
                    conflict = True; break
            if conflict:
                stats["conflict"] += 1
                continue
            # redundancy: already nailed by a bundled ref?
            if min((masked_mse(rep, rv) for rb, rv in refs if rb == base), default=1e9) < REDUNDANT_DIST:
                stats["already_matched"] += 1
                continue
            # already seeded by a previous promotion? -> keep re-runs idempotent
            if min((masked_mse(rep, sv) for sv in seeded.get(base, [])), default=1e9) < REDUNDANT_DIST:
                stats["already_seeded"] += 1
                continue
            rarity = next((g.get("rarity") for g in group if g.get("rarity")), None)
            vu8 = np.clip(rep[0] * 255, 0, 255).astype(np.uint8)
            bits = np.packbits(rep[1].astype(bool), bitorder="little")
            entry = {"base": base, "rarity": rarity,
                     "v": base64.b64encode(vu8.tobytes()).decode(),
                     "m": base64.b64encode(bits.tobytes()).decode()}
            promoted.append(entry)
            details.append({"base": base, "rarity": rarity, "n": len(c), "total": len(group)})
            seeded[base].append(rep)        # so a second cluster of the same base dedups too
            stats["promoted"] += 1

    def line_for(d: dict) -> str:
        """One readable line per promoted item: name (JP) [rarity] — N agreed."""
        jb, jr = ja_bases.get(d["base"]), ja_rar.get(d["rarity"] or "")
        name = d["base"] + (f"（{jb}）" if jb else "")
        rar = (d["rarity"] or "—") + (f"/{jr}" if jr else "")
        return (f"- {name} [{rar}] — {d['n']}人が一致"
                f"（この候補への投稿 {d['total']}件）")

    lines = [line_for(d) for d in sorted(details, key=lambda x: -x["n"])]
    print("gate stats:", dict(stats))
    print(f"-> {len(promoted)} new labels promoted, covering "
          f"{len({p['base'] for p in promoted})} items"
          + (":" if lines else ""))
    for ln in lines:
        print(ln)

    # GitHub Actions step summary (rendered on the run page)
    summ = os.environ.get("GITHUB_STEP_SUMMARY")
    if summ:
        with open(summ, "a", encoding="utf-8") as f:
            f.write(f"### crowd-label promotion\n- fetched: {len(rows)} rows\n"
                    f"- gate stats: `{dict(stats)}`\n"
                    f"- **promoted: {len(promoted)} labels**\n"
                    + ("".join(ln + "\n" for ln in lines) if lines
                       else "- (nothing crossed the gate)\n"))

    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"promoted={len(promoted)}\n")

    if not promoted:
        print("nothing to promote — learned_seed.json unchanged")
        return
    if args.dry_run:
        print("(dry-run: not writing)")
        return
    seed.extend(promoted)
    SEED.write_text(json.dumps(seed, separators=(",", ":")), encoding="utf-8")
    print(f"wrote {SEED} ({len(seed)} entries total, +{len(promoted)})")


if __name__ == "__main__":
    main()
