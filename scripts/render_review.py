"""Render a visual review montage for each promoted crowd label, so a human can
eyeball "is this really that item?" right inside the PR.

Reads review_manifest.json (written by promote_labels.py: the promoted items with
their v/m signatures + consensus counts), plus data/refs.{bin,json} and ja_names.
For each item it renders [catalog ref | crowd label] (both mask-applied, so only
the pixels the matcher uses are shown), annotates it, and computes a NEAREST-
NEIGHBOUR check: which catalog item is the label actually closest to. It flags
(⚠) anything where the nearest item isn't the promoted base, or the match is loose
(MSE > FLAG_MSE) — those are the ones a human must look at (e.g. UI overlays like
the can't-equip ✕, or genuine look-alike confusion).

Outputs:
  review/<base>_<grade>.png        one montage per promoted item
  pr_body_review.md                markdown (embeds the images via raw URLs + flags)
  GITHUB_OUTPUT has_flags=...       so the workflow can ⚠ the PR title

Runnable locally too (uses a manifest you point it at, or the default path).
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DATA = ROOT / "data"
REVIEW = ROOT / "review"
MANIFEST = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "review_manifest.json"
FLAG_MSE = 0.06        # looser than the catalog ref than this -> human should look
REPO = os.environ.get("GITHUB_REPOSITORY", "TKH54/tbh-appraiser")
ASSET_BRANCH = "review-assets"


def unpack_label(v_b64: str, m_b64: str):
    """crowd label: v = 32x32x3 uint8, mask = 128 packed bytes -> 1024 bits."""
    v = np.frombuffer(base64.b64decode(v_b64), np.uint8).reshape(32, 32, 3)
    bits = np.unpackbits(np.frombuffer(base64.b64decode(m_b64), np.uint8), bitorder="little")
    return v, bits[:1024].astype(np.uint8).reshape(32, 32)


def load_refs():
    """catalog refs: v = 3072 bytes, mask = 1024 DIRECT bytes (not packed)."""
    meta = json.loads((DATA / "refs.json").read_text(encoding="utf-8"))
    buf = np.frombuffer((DATA / "refs.bin").read_bytes(), np.uint8)
    stride = 4096
    out = []
    for i, m in enumerate(meta):
        o = i * stride
        out.append((m["base"], buf[o:o + 3072].reshape(32, 32, 3),
                    buf[o + 3072:o + stride].reshape(32, 32)))
    return out


def masked_mse(va, ma, vb, mb) -> float:
    keep = (ma | mb).astype(bool)
    if keep.sum() < 60:
        return 1e9
    a = va.astype(np.float32) / 255.0
    b = vb.astype(np.float32) / 255.0
    d = (a - b)[keep]
    return float((d * d).sum() / (keep.sum() * 3))


def _up(a, s=8):
    return np.repeat(np.repeat(a, s, 0), s, 1)


def _masked(v, valid, bg=20):
    o = v.copy(); o[valid == 0] = bg; return o


def _font(size):
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size)
    except Exception:
        return ImageFont.load_default()


def render_one(item, refs):
    base, rarity = item["base"], item.get("rarity")
    lv, lvalid = unpack_label(item["v"], item["m"])
    ref = next(((rv, rvalid) for b, rv, rvalid in refs if b == base), None)
    dists = sorted((masked_mse(lv, lvalid, rv, rvalid), b) for b, rv, rvalid in refs)
    rank = next((i + 1 for i, (_, b) in enumerate(dists) if b == base), -1)
    mse_self = next((d for d, b in dists if b == base), 1e9)
    nearest_base, nearest_mse = dists[0][1], dists[0][0]
    flag = (nearest_base != base) or (mse_self > FLAG_MSE)

    L = _up(_masked(ref[0], ref[1])) if ref else np.full((256, 256, 3), 20, np.uint8)
    R = _up(_masked(lv, lvalid))
    gap = np.full((L.shape[0], 14, 3), 60, np.uint8)
    row = np.concatenate([L, gap, R], axis=1)[:, :, ::-1]      # BGR -> RGB
    img = Image.fromarray(row.astype(np.uint8))

    strip = Image.new("RGB", (img.width, 70), (16, 18, 24))
    d = ImageDraw.Draw(strip)
    d.text((6, 4), f"{'!! ' if flag else ''}{base}  [{rarity or '-'}]",
           fill=(255, 210, 90) if flag else (230, 233, 239), font=_font(18))
    d.text((6, 34), f"left=catalog  right=crowd label   {item.get('n', '?')} agreed",
           fill=(150, 160, 180), font=_font(14))
    d.text((6, 50), f"nearest #{rank}/{len(dists)} = {nearest_base}  (MSE {mse_self:.3f})",
           fill=(255, 140, 120) if flag else (124, 196, 124), font=_font(14))

    canvas = Image.new("RGB", (img.width, img.height + 70), (16, 18, 24))
    canvas.paste(img, (0, 0)); canvas.paste(strip, (0, img.height))
    safe = re.sub(r"[^A-Za-z0-9]+", "_", f"{base}_{rarity or ''}").strip("_")
    REVIEW.mkdir(exist_ok=True)
    canvas.save(REVIEW / f"{safe}.png")
    return {"base": base, "rarity": rarity, "file": f"{safe}.png", "flag": flag,
            "rank": rank, "mse": mse_self, "nearest": nearest_base, "n": item.get("n")}


def main():
    if not MANIFEST.exists():
        print("no review_manifest.json — nothing to render")
        return
    items = json.loads(MANIFEST.read_text(encoding="utf-8"))
    try:
        ja = json.loads((DATA / "ja_names.json").read_text(encoding="utf-8"))
        ja_bases, ja_rar = ja.get("bases", {}), ja.get("rarities", {})
    except Exception:
        ja_bases, ja_rar = {}, {}
    refs = load_refs()
    results = [render_one(it, refs) for it in items]
    any_flag = any(r["flag"] for r in results)

    lines = ["## 🖼 Label review (visual)", "",
             "左=カタログ参照 / 右=みんなのラベル（マスク適用）。**⚠ は要確認**"
             "（別アイテムが最近傍、または一致が緩い＝UIオーバーレイや似たアイテムの疑い）。", ""]
    for r, it in zip(results, items):
        jb = ja_bases.get(r["base"], ""); jr = ja_rar.get(r["rarity"] or "", "")
        url = f"https://raw.githubusercontent.com/{REPO}/{ASSET_BRANCH}/review/{r['file']}"
        tag = "⚠ 要確認" if r["flag"] else "✅"
        lines += [f"### {tag} {r['base']}{('（' + jb + '）') if jb else ''} "
                  f"[{r['rarity'] or '-'}{('/' + jr) if jr else ''}]",
                  f"{r['n']}人一致 · 最近傍 #{r['rank']} = {r['nearest']} · MSE {r['mse']:.3f}",
                  f"![{r['base']}]({url})", ""]
    (ROOT / "pr_body_review.md").write_text("\n".join(lines), encoding="utf-8")

    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"has_flags={'true' if any_flag else 'false'}\n")
    print(f"rendered {len(results)} review image(s); flags={any_flag}")


if __name__ == "__main__":
    main()
