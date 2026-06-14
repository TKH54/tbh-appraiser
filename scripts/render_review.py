"""Render a visual review LINE-UP for each promoted crowd label, so a human can
confirm by elimination: "is this really that item, and not one of the look-alikes?"

For each promoted item it renders, left to right:
  [ crowd label ]  [ ★ promoted-base ref ]  [ nearest other candidates... ]
all mask-applied (only the pixels the matcher uses) and each candidate captioned
with its name + masked-MSE distance to the label, sorted nearest-first. The
promoted base gets a green ★ border so you can spot it among the look-alikes.

It also flags (⚠) anything where the promoted base ISN'T the nearest candidate,
or the match is loose (MSE > FLAG_MSE) — the cases a human must eyeball (UI
overlays like the can't-equip ✕, or genuine confusion).

Outputs: review/<base>_<grade>.png, pr_body_review.md (embeds via raw URLs),
GITHUB_OUTPUT has_flags=...  Runnable locally: python render_review.py [manifest]
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
FLAG_MSE = 0.06         # looser than this -> human should look
N_CANDIDATES = 5        # nearest other items shown for elimination
REPO = os.environ.get("GITHUB_REPOSITORY", "TKH54/tbh-appraiser")
ASSET_BRANCH = "review-assets"


def unpack_label(v_b64, m_b64):
    v = np.frombuffer(base64.b64decode(v_b64), np.uint8).reshape(32, 32, 3)
    bits = np.unpackbits(np.frombuffer(base64.b64decode(m_b64), np.uint8), bitorder="little")
    return v, bits[:1024].astype(np.uint8).reshape(32, 32)


def load_refs():
    meta = json.loads((DATA / "refs.json").read_text(encoding="utf-8"))
    buf = np.frombuffer((DATA / "refs.bin").read_bytes(), np.uint8)
    stride = 4096
    return [(m["base"], buf[i * stride:i * stride + 3072].reshape(32, 32, 3),
             buf[i * stride + 3072:i * stride + stride].reshape(32, 32))
            for i, m in enumerate(meta)]


def masked_mse(va, ma, vb, mb):
    keep = (ma | mb).astype(bool)
    if keep.sum() < 60:
        return 1e9
    d = (va.astype(np.float32) / 255.0 - vb.astype(np.float32) / 255.0)[keep]
    return float((d * d).sum() / (keep.sum() * 3))


def _up(a, s):
    return np.repeat(np.repeat(a, s, 0), s, 1)


def _masked(v, valid, bg=20):
    o = v.copy(); o[valid == 0] = bg; return o


def _font(size):
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size)
    except Exception:
        return ImageFont.load_default()


SCALE = 7                                # 32 -> 224
TW = 32 * SCALE                          # thumb size
TILE_W = TW + 16                         # tile width
CAP_H = 46                               # caption height


def _tile(v, valid, title, sub, *, mark=False, accent=False):
    """One labelled thumbnail (mask-applied). mark=green ★ promoted, accent=yellow."""
    tile = Image.new("RGB", (TILE_W, TW + CAP_H), (16, 18, 24))
    thumb = Image.fromarray(_up(_masked(v, valid), SCALE)[:, :, ::-1].astype(np.uint8))
    x = (TILE_W - TW) // 2
    tile.paste(thumb, (x, 4))
    d = ImageDraw.Draw(tile)
    border = (110, 230, 130) if mark else (240, 200, 80) if accent else None
    if border:
        d.rectangle([x - 1, 3, x + TW, TW + 4], outline=border, width=3)
    d.text((6, TW + 8), title, fill=(border or (230, 233, 239)), font=_font(15))
    if sub:
        d.text((6, TW + 28), sub, fill=(150, 160, 180), font=_font(13))
    return tile


def render_one(item, refs):
    base, rarity = item["base"], item.get("rarity")
    lv, lvalid = unpack_label(item["v"], item["m"])
    best = {}                                            # base -> (mse, v, valid)
    for b, rv, rvalid in refs:
        d = masked_mse(lv, lvalid, rv, rvalid)
        if b not in best or d < best[b][0]:
            best[b] = (d, rv, rvalid)
    ranked = sorted(best.items(), key=lambda kv: kv[1][0])        # nearest-first
    rank = next((i + 1 for i, (b, _) in enumerate(ranked) if b == base), -1)
    mse_self = best[base][0]
    nearest_base = ranked[0][0]
    flag = (nearest_base != base) or (mse_self > FLAG_MSE)

    # candidates = top-N nearest distinct items, guaranteed to include the promoted base
    cands = ranked[:N_CANDIDATES]
    if base not in [b for b, _ in cands]:
        cands.append((base, best[base]))

    tiles = [_tile(lv, lvalid, "← crowd label", f"{item.get('n','?')} agreed", accent=True)]
    for b, (d, rv, rvalid) in cands:
        tag = ("★ " if b == base else "") + b
        tiles.append(_tile(rv, rvalid, tag, f"MSE {d:.3f}", mark=(b == base)))

    gap = 10
    W = sum(t.width for t in tiles) + gap * (len(tiles) - 1)
    H = tiles[0].height + 34
    canvas = Image.new("RGB", (W, H), (16, 18, 24))
    d = ImageDraw.Draw(canvas)
    head = f"{'!! ' if flag else ''}{base} [{rarity or '-'}]  —  pick by elimination (★ = promoted)"
    d.text((6, 8), head, fill=(255, 210, 90) if flag else (124, 196, 124), font=_font(16))
    x = 0
    for t in tiles:
        canvas.paste(t, (x, 34)); x += t.width + gap

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

    lines = ["## 🖼 Label review (visual, by elimination)", "",
             "左端＝みんなのラベル。右に**最近傍の候補**を並べています（各：名前＋距離MSE、近い順）。"
             "**★＝昇格先**。ラベルがどの候補に一番似ているかを見比べて、★で合っていれば妥当。"
             "**⚠ は要確認**（★が最近傍でない／一致が緩い）。", ""]
    for r in results:
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
