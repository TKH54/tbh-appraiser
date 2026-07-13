// TBH item recognition, ported from matcher.py / analyzer.py to pure JS so it
// runs in the browser with no Pyodide/OpenCV download (first load stays small).
//
// Internal image format mirrors OpenCV exactly: { w, h, data } where `data` is
// a Uint8 array of length w*h*3 in BGR order (same channel order as the Python
// pipeline and the packed reference vectors), row-major, no padding. Keeping
// BGR means the HSV thresholds and masked colour distances match Python 1:1.
//
// Verified in node against the 127 user-labelled captures (see test_recognize.js).

// ---------- colour helpers (cv2-compatible) ----------

// cv2 BGR->GRAY: 0.299R + 0.587G + 0.114B
function gray(data, i) {
  return 0.114 * data[i] + 0.587 * data[i + 1] + 0.299 * data[i + 2];
}

// cv2 BGR->HSV (H:0-179, S:0-255, V:0-255), per-pixel.
function bgr2hsv(b, g, r) {
  const v = Math.max(b, g, r);
  const mn = Math.min(b, g, r);
  const d = v - mn;
  let h = 0;
  if (d !== 0) {
    if (v === r)      h = 30 * (g - b) / d;
    else if (v === g) h = 60 + 30 * (b - r) / d;
    else              h = 120 + 30 * (r - g) / d;
    if (h < 0) h += 180;
  }
  const s = v === 0 ? 0 : 255 * d / v;
  return [h, s, v];   // h already 0-179 scale (cv2 uses H/2)
}

// ---------- small image ops ----------

// Crop a sub-rectangle into a fresh {w,h,data} (BGR).
function crop(img, x0, y0, w, h) {
  const out = new Uint8Array(w * h * 3);
  for (let y = 0; y < h; y++) {
    const src = ((y0 + y) * img.w + x0) * 3;
    out.set(img.data.subarray(src, src + w * 3), y * w * 3);
  }
  return { w, h, data: out };
}

// Area-average resize to a fixed size (mirrors cv2 INTER_AREA for downscale,
// which is what every call here does: variable cell -> 32x32).
function resizeArea(img, dw, dh) {
  const { w, h, data } = img;
  const out = new Uint8Array(dw * dh * 3);
  for (let oy = 0; oy < dh; oy++) {
    const y0 = oy * h / dh, y1 = (oy + 1) * h / dh;
    const iy0 = Math.floor(y0), iy1 = Math.min(h, Math.ceil(y1));
    for (let ox = 0; ox < dw; ox++) {
      const x0 = ox * w / dw, x1 = (ox + 1) * w / dw;
      const ix0 = Math.floor(x0), ix1 = Math.min(w, Math.ceil(x1));
      let sb = 0, sg = 0, sr = 0, area = 0;
      for (let yy = iy0; yy < iy1; yy++) {
        const wy = Math.min(y1, yy + 1) - Math.max(y0, yy);
        for (let xx = ix0; xx < ix1; xx++) {
          const wx = Math.min(x1, xx + 1) - Math.max(x0, xx);
          const a = wy * wx;
          const idx = (yy * w + xx) * 3;
          sb += data[idx] * a; sg += data[idx + 1] * a; sr += data[idx + 2] * a;
          area += a;
        }
      }
      const o = (oy * dw + ox) * 3;
      out[o] = sb / area; out[o + 1] = sg / area; out[o + 2] = sr / area;
    }
  }
  return { w: dw, h: dh, data: out };
}

// Bright pure-red mask (the can't-equip X), matcher.py _red_mask:
// (H<=7 || H>=172) && S>=130 && V>=110, on an item image.
function redMaskFlags(img) {
  const { w, h, data } = img;
  const out = new Uint8Array(w * h);
  for (let i = 0, p = 0; p < w * h; p++, i += 3) {
    const [hh, s, v] = bgr2hsv(data[i], data[i + 1], data[i + 2]);
    out[p] = ((hh <= 7 || hh >= 172) && s >= 130 && v >= 110) ? 1 : 0;
  }
  return out;
}

// ---------- background removal (the 3 extraction strategies) ----------

// matcher.py extract_item: flood-fill the rarity background away from all edges
// of an inner crop. Floating-range fill (compare each candidate to the already
// -filled neighbour that reached it), tolerance `lo` per channel.
function extractFlood(cell, borderFrac = 0.10, lo = 60) {
  const { w, h } = cell;
  const m = Math.floor(borderFrac * Math.min(h, w));
  const iw = w - 2 * m, ih = h - 2 * m;
  if (iw <= 2 || ih <= 2) return crop(cell, 0, 0, w, h);
  const inner = crop(cell, m, m, iw, ih);
  const d = inner.data;
  const filled = new Uint8Array(iw * ih);          // 1 = background (to zero)
  const stack = [];
  const step = Math.max(2, Math.floor(iw / 12));
  const pushSeed = (x, y) => {
    if (x < 0 || y < 0 || x >= iw || y >= ih) return;
    if (!filled[y * iw + x]) { filled[y * iw + x] = 1; stack.push(x, y); }
  };
  for (let x = 0; x < iw; x += step) { pushSeed(x, 0); pushSeed(x, ih - 1); }
  for (let y = 0; y < ih; y += step) { pushSeed(0, y); pushSeed(iw - 1, y); }
  const within = (a, b) => Math.abs(a - b) <= lo;
  while (stack.length) {
    const y = stack.pop(), x = stack.pop();
    const i = (y * iw + x) * 3;
    const nb = [[x - 1, y], [x + 1, y], [x, y - 1], [x, y + 1]];
    for (const [nx, ny] of nb) {
      if (nx < 0 || ny < 0 || nx >= iw || ny >= ih) continue;
      const p = ny * iw + nx;
      if (filled[p]) continue;
      const j = p * 3;
      if (within(d[j], d[i]) && within(d[j + 1], d[i + 1]) && within(d[j + 2], d[i + 2])) {
        filled[p] = 1; stack.push(nx, ny);
      }
    }
  }
  const out = new Uint8Array(iw * ih * 3);
  out.set(d);
  for (let p = 0; p < iw * ih; p++) if (filled[p]) { out[p * 3] = out[p * 3 + 1] = out[p * 3 + 2] = 0; }
  return { w: iw, h: ih, data: out };
}

// matcher.py extract_twotone: k-means the 2 checker tones from top+left edges,
// erase every pixel near either tone.
function extractTwotone(cell, thr) {
  const { w, h } = cell;
  const m = Math.floor(0.10 * Math.min(h, w));
  const iw = w - 2 * m, ih = h - 2 * m;
  if (iw <= 2 || ih <= 2) return crop(cell, 0, 0, w, h);
  const inner = crop(cell, m, m, iw, ih);
  const d = inner.data;
  const t = Math.max(2, Math.floor(ih / 9));
  const ring = [];
  for (let y = 0; y < t; y++) for (let x = 0; x < Math.floor(iw * 0.85); x++) ring.push((y * iw + x) * 3);
  for (let y = 0; y < Math.floor(ih * 0.85); y++) for (let x = 0; x < t; x++) ring.push((y * iw + x) * 3);
  if (ring.length < 8) return inner;
  // 2-means
  let c0 = [d[ring[0]], d[ring[0] + 1], d[ring[0] + 2]];
  let c1 = [d[ring[ring.length - 1]], d[ring[ring.length - 1] + 1], d[ring[ring.length - 1] + 2]];
  const dist2 = (i, c) => (d[i] - c[0]) ** 2 + (d[i + 1] - c[1]) ** 2 + (d[i + 2] - c[2]) ** 2;
  for (let it = 0; it < 10; it++) {
    let s0 = [0, 0, 0, 0], s1 = [0, 0, 0, 0];
    for (const i of ring) {
      const t0 = dist2(i, c0), t1 = dist2(i, c1);
      const s = t0 <= t1 ? s0 : s1;
      s[0] += d[i]; s[1] += d[i + 1]; s[2] += d[i + 2]; s[3]++;
    }
    if (s0[3]) c0 = [s0[0] / s0[3], s0[1] / s0[3], s0[2] / s0[3]];
    if (s1[3]) c1 = [s1[0] / s1[3], s1[1] / s1[3], s1[2] / s1[3]];
  }
  const out = new Uint8Array(iw * ih * 3);
  out.set(d);
  const thr2 = thr * thr;
  for (let p = 0; p < iw * ih; p++) {
    const i = p * 3;
    if (Math.min(dist2(i, c0), dist2(i, c1)) < thr2) { out[i] = out[i + 1] = out[i + 2] = 0; }
  }
  return { w: iw, h: ih, data: out };
}

// matcher.py _keep_main_blob: drop small disconnected residue components.
function keepMainBlob(item, minFrac = 0.02) {
  const { w, h, data } = item;
  const red = redMaskFlags(item);
  const fg = new Uint8Array(w * h);
  let total = 0;
  for (let p = 0; p < w * h; p++) { if (gray(data, p * 3) > 12 && !red[p]) { fg[p] = 1; total++; } }
  if (total === 0) return item;
  // connected components (4-conn), areas
  const label = new Int32Array(w * h).fill(0);
  const areas = [0];
  const stack = [];
  let nlab = 0;
  for (let p = 0; p < w * h; p++) {
    if (!fg[p] || label[p]) continue;
    nlab++; areas.push(0); stack.push(p); label[p] = nlab;
    while (stack.length) {
      const q = stack.pop(); areas[nlab]++;
      const x = q % w, y = (q / w) | 0;
      const nb = [];
      if (x > 0) nb.push(q - 1); if (x < w - 1) nb.push(q + 1);
      if (y > 0) nb.push(q - w); if (y < h - 1) nb.push(q + w);
      for (const r of nb) if (fg[r] && !label[r]) { label[r] = nlab; stack.push(r); }
    }
  }
  if (nlab <= 1) return item;
  let maxA = 0, maxL = 1;
  for (let l = 1; l <= nlab; l++) if (areas[l] > maxA) { maxA = areas[l]; maxL = l; }
  const thr = Math.max(total * minFrac, maxA * 0.10);
  const out = new Uint8Array(w * h * 3);
  out.set(data);
  for (let p = 0; p < w * h; p++) {
    if (fg[p] && areas[label[p]] < thr && label[p] !== maxL) {
      out[p * 3] = out[p * 3 + 1] = out[p * 3 + 2] = 0;
    }
  }
  return { w, h, data: out };
}

// ---------- signature (32x32 vector + masks) ----------

// matcher.py _vec_from_item: tight-crop ignoring the red X, resize to 32x32,
// return {vec:Float32(32*32*3 /255), valid:Uint8, red:Uint8}.
// dropRed=true (default) strips bright pure-red — the can't-equip ✕ overlay —
// from the crop box and valid mask. dropRed=false KEEPS it, so genuinely red
// items (Bloodstone, rubies) aren't gutted; cellVariants yields both so the
// ensemble min covers ✕-marked items AND red items.
function vecFromItem(item, dropRed = true) {
  const red = dropRed ? redMaskFlags(item) : new Uint8Array(item.w * item.h);
  const { w, h, data } = item;
  let x0 = w, y0 = h, x1 = -1, y1 = -1, n = 0;
  for (let p = 0; p < w * h; p++) {
    if (gray(data, p * 3) > 12 && !red[p]) {
      const x = p % w, y = (p / w) | 0;
      if (x < x0) x0 = x; if (x > x1) x1 = x; if (y < y0) y0 = y; if (y > y1) y1 = y; n++;
    }
  }
  let cell = item;
  if (n > 5 && x1 >= x0 && y1 >= y0) cell = crop(item, x0, y0, x1 - x0 + 1, y1 - y0 + 1);
  const r = resizeArea(cell, 32, 32);
  const vec = new Float32Array(32 * 32 * 3);
  for (let i = 0; i < vec.length; i++) vec[i] = r.data[i] / 255;
  const rred = dropRed ? redMaskFlags(r) : new Uint8Array(32 * 32);
  const valid = new Uint8Array(32 * 32);
  for (let p = 0; p < 32 * 32; p++) valid[p] = (gray(r.data, p * 3) > 16 && !rred[p]) ? 1 : 0;
  fillMaskHoles(valid, 32, 32);           // re-include dark INTERIOR pixels (shadows) the gray>16 cut dropped
  return { vec, valid, red: rred };
}

// Set interior holes (invalid cells not reachable from the border) to valid, so
// dark shadows inside the sprite stop reading as background "dropout".
function fillMaskHoles(valid, w, h) {
  const bg = new Uint8Array(w * h);       // 1 = background reachable from edge
  const st = [];
  for (let x = 0; x < w; x++) { st.push(x, 0, x, h - 1); }
  for (let y = 0; y < h; y++) { st.push(0, y, w - 1, y); }
  while (st.length) {
    const y = st.pop(), x = st.pop();
    if (x < 0 || y < 0 || x >= w || y >= h) continue;
    const i = y * w + x;
    if (bg[i] || valid[i]) continue;      // stop at item pixels / already seen
    bg[i] = 1;
    st.push(x - 1, y, x + 1, y, x, y - 1, x, y + 1);
  }
  for (let i = 0; i < w * h; i++) if (!valid[i] && !bg[i]) valid[i] = 1;   // enclosed hole -> fill
}

// In-game item LOCK overlays a padlock on the cell's top-right corner; it
// joins the sprite's tight-crop bounding box and wrecks the signature. Blank
// that corner so a lock-free variant can enter the ensemble (min over
// variants → harmless for unlocked items, rescues locked ones).
function blankLockCorner(cell) {
  const { w, h } = cell;
  const out = new Uint8Array(cell.data);
  const x0 = Math.floor(w * 0.64), y1 = Math.ceil(h * 0.36);
  for (let y = 0; y < y1; y++) for (let x = x0; x < w; x++) {
    const i = (y * w + x) * 3;
    out[i] = out[i + 1] = out[i + 2] = 0;
  }
  return { w, h, data: out };
}

// Companion to blankLockCorner for sprites that genuinely REACH the corner
// (diagonal swords/bows): instead of erasing pixels, exclude the 32×32
// signature's top-right from scoring on BOTH sides (reuses the red-mask
// exclusion path in Matcher._scoreInto).
function maskCornerSig(sig) {
  const red = new Uint8Array(sig.red);
  const valid = new Uint8Array(sig.valid);
  for (let y = 0; y < 12; y++) for (let x = 20; x < 32; x++) {
    red[y * 32 + x] = 1; valid[y * 32 + x] = 0;
  }
  return { vec: sig.vec, valid, red };
}

// Geometry-aware background removal: icons are centred, so the corners are
// background and the centre is the item. extractFlood seeds from the WHOLE
// border (every `step` px on all 4 edges) -> a wide item touching an edge gets a
// flood seed ON it and erodes; and dark item regions adjacent to the dark bg get
// flooded away (the "dropout" seen in captures). Here we seed ONLY from the 4
// corner patches AND never flood a protected central disk, so the centred item
// (incl. its dark parts) survives. Added as an EXTRA ensemble variant (the
// matcher takes the min over variants, so this can only help, not hurt).
function extractCorner(cell, lo = 60) {
  const { w, h } = cell;
  const m = Math.floor(0.10 * Math.min(h, w));
  const iw = w - 2 * m, ih = h - 2 * m;
  if (iw <= 2 || ih <= 2) return crop(cell, 0, 0, w, h);
  const inner = crop(cell, m, m, iw, ih);
  const d = inner.data;
  const filled = new Uint8Array(iw * ih);
  const stack = [];
  const cx = (iw - 1) / 2, cy = (ih - 1) / 2;
  const pr2 = (0.34 * Math.min(iw, ih)) ** 2;       // protected central disk
  const cn = Math.max(2, Math.floor(iw * 0.18));    // corner seed patch size
  const pushSeed = (x, y) => {
    if (x < 0 || y < 0 || x >= iw || y >= ih) return;
    if (!filled[y * iw + x]) { filled[y * iw + x] = 1; stack.push(x, y); }
  };
  for (let y = 0; y < cn; y++) for (let x = 0; x < cn; x++) {
    pushSeed(x, y); pushSeed(iw - 1 - x, y);
    pushSeed(x, ih - 1 - y); pushSeed(iw - 1 - x, ih - 1 - y);
  }
  const within = (a, b) => Math.abs(a - b) <= lo;
  while (stack.length) {
    const y = stack.pop(), x = stack.pop();
    const i = (y * iw + x) * 3;
    const nb = [[x - 1, y], [x + 1, y], [x, y - 1], [x, y + 1]];
    for (const [nx, ny] of nb) {
      if (nx < 0 || ny < 0 || nx >= iw || ny >= ih) continue;
      if ((nx - cx) ** 2 + (ny - cy) ** 2 < pr2) continue;   // never flood the centre
      const p = ny * iw + nx;
      if (filled[p]) continue;
      const j = p * 3;
      if (within(d[j], d[i]) && within(d[j + 1], d[i + 1]) && within(d[j + 2], d[i + 2])) {
        filled[p] = 1; stack.push(nx, ny);
      }
    }
  }
  const out = new Uint8Array(iw * ih * 3);
  out.set(d);
  for (let p = 0; p < iw * ih; p++) if (filled[p]) { out[p * 3] = out[p * 3 + 1] = out[p * 3 + 2] = 0; }
  return { w: iw, h: ih, data: out };
}

// matcher.py cell_variants: extraction ENSEMBLE (cheap-first generator).
function* cellVariants(cell) {
  const base = extractFlood(cell);
  const vBase = vecFromItem(base);
  yield vBase;
  yield vecFromItem(keepMainBlob(base));
  // red-KEPT variants: rescue genuinely red items (Bloodstone, rubies) that the
  // default ✕-stripping mangles. Ensemble min still covers ✕-marked items via
  // the red-dropped variants above.
  yield vecFromItem(base, false);
  yield vecFromItem(keepMainBlob(base), false);
  // lock-tolerant variants (see blankLockCorner / maskCornerSig)
  const noLock = extractFlood(blankLockCorner(cell));
  yield vecFromItem(noLock);
  yield vecFromItem(keepMainBlob(noLock));
  yield maskCornerSig(vBase);
  yield maskCornerSig(vecFromItem(noLock));
  // geometry-aware: corner-seeded flood + protected centre (reduces dark-region dropout)
  const geom = extractCorner(cell);
  yield vecFromItem(geom);
  yield vecFromItem(keepMainBlob(geom));
  const more = [extractFlood(cell, 0.10, 35),
               extractTwotone(cell, 40), extractTwotone(cell, 60), extractTwotone(cell, 85),
               extractTwotone(blankLockCorner(cell), 60)];
  for (const it of more) { yield vecFromItem(it); yield vecFromItem(keepMainBlob(it)); }
}

// ---------- border rarity ----------

// Hue bands (cv2 0-179 scale). Calibrated against real captures: getDisplayMedia
// reads ~+10-12 hue higher than the reference swatches, so in-game borders sit at
// Arcana≈140, Beyond≈169 — Beyond was pinned to the very top of its old 145-169
// band and any extra shift tipped it past 170 into Immortal (red), making Beyond
// items resolve as the wrong grade / "?". Boundaries moved up to give Beyond margin
// on both sides while real red (median hue ≈0) stays firmly in Immortal.
const RARITY_HUE = [
  ["Legendary", 9, 22], ["Immortal", 0, 8], ["Arcana", 123, 154],
  ["Beyond", 155, 175], ["Rare", 90, 122], ["Uncommon", 35, 85],
];

// Celestial shares the Rare HUE band — its cyan border (hue ≈94) is only ~11 apart
// from Rare's blue (≈105), too close to split by hue. But the two differ sharply in
// SATURATION + VALUE: Celestial is a PALE, BRIGHT cyan; Rare is a DEEP, darker blue.
// Measured from a real getDisplayMedia capture (per-pixel border, S>80 kept):
// Celestial S≈100 V≈234  vs  Rare S≈170 V≈126. So within the Rare band, a pale+bright
// pixel is Celestial. Gate on BOTH (S<150 AND V>180): a real Rare (deep+dark) fails
// both, so this can't turn a Rare into a Celestial (verified: Rare cell 385 Rare / 4
// Celestial votes; Celestial cells flip 54/22).
const CEL_S_MAX = 150, CEL_V_MIN = 180;

// Divine shares Legendary's gold HUE band, so hue alone can't split them. Measured
// from the game's own ItemSlot_GradeBg_* textures (which line up 1:1 with the
// captured bands above — Legendary hue≈14, Divine hue≈19), the two differ sharply in
// SATURATION + VALUE: Divine is a PALE, BRIGHT gold; Legendary a DEEP, dark gold —
// Legendary S≈248 V≈141  vs  Divine S≈129 V≈226. Gate on BOTH (S<175 AND V>195): a
// real Legendary (deep+dark) fails both, so this can't flip a Legendary into a Divine.
// NOTE: Cosmic ALSO lives in this gold band — its border is a fiery ORANGE (NOT white
// as once assumed) that mixes red+gold, so per-pixel hue can't tell it from Legendary.
// It is detected by that red+gold MIX in borderRarity() below, not here.
const DIV_S_MAX = 175, DIV_V_MIN = 195;
function hueToRarity(hue, s, v) {
  if (hue >= 176) return "Immortal";   // red wrap-around (was 170; raised so Beyond≈169 isn't swallowed)
  for (const [name, lo, hi] of RARITY_HUE) if (hue >= lo && hue <= hi) {
    if (name === "Rare" && s < CEL_S_MAX && v > CEL_V_MIN) return "Celestial";
    if (name === "Legendary" && s < DIV_S_MAX && v > DIV_V_MIN) return "Divine";
    return name;
  }
  // Divine's actual ring (game asset ItemSlot_GradeBg_DIVINE border strip) sits at
  // hue ≈23-24 — just PAST Legendary's band into the 23-34 gap — so the in-band
  // Divine gate above never fired and Divine read as "?" (unknown). Extend the
  // pale+bright gold gate a little into the gap; deep-saturated oranges (hue 23-27
  // with S≥175 or dark) still return null, so nothing that used to resolve to a
  // grade can flip.
  if (hue >= 23 && hue <= 27 && s < DIV_S_MAX && v > DIV_V_MIN) return "Divine";
  return null;
}

// Sample the top+left border strip and take the PLURALITY rarity band, not the
// median hue. A median is fragile when a long icon (staff/spear) pokes its tip
// into the thin strip: e.g. a teal comet (hue ≈86-89, which sits in a band gap)
// drags an Arcana-purple border's median into the gap and the grade reads as
// "unknown" even though the frame is plainly Arcana. The ring is still the
// dominant colour, so voting per-pixel into bands lets the real band win and
// ignores the minority contamination. (Verified identical to the old median on
// clean cells.)
function borderRarity(cell) {
  const { w, h, data } = cell;
  const t = Math.max(2, Math.floor(0.08 * w));
  const votes = new Map();
  let total = 0, pos = 0, paleCel = 0;
  const sample = (x, y) => {
    const o = (y * w + x) * 3;
    const [hh, s, v] = bgr2hsv(data[o], data[o + 1], data[o + 2]);
    pos++;
    if (s <= 80 || v <= 60) {
      // Celestial's ring is a PALE bright cyan (game asset border strip:
      // H≈96-98, S≈19-37, V≈245-250) — the s>80 chroma gate above throws away
      // the ENTIRE ring, total stays 0, and a real Celestial falls through to
      // the "Common" fallback (hit live: Dragonite Crystal, a Celestial
      // material, read as Common). Count pale-cyan pixels separately here and
      // only believe them when they DOMINATE the strip (>=30% of all sampled
      // positions below): a gray Common ring has S≈0 and fails s>=15, and a
      // pale icon tip poking into the strip can't reach 30% of it.
      if (hh >= 85 && hh <= 125 && s >= 15 && v >= 200) paleCel++;
      return;
    }
    total++;
    const r = hueToRarity(hh, s, v);
    if (r) votes.set(r, (votes.get(r) || 0) + 1);
  };
  for (let y = 0; y < t; y++) for (let x = 0; x < Math.floor(w * 0.60); x++) sample(x, y);
  for (let y = 0; y < Math.floor(h * 0.60); y++) for (let x = 0; x < t; x++) sample(x, y);
  if (paleCel >= 5 && paleCel >= pos * 0.30) {
    votes.set("Celestial", (votes.get("Celestial") || 0) + paleCel);
    total += paleCel;
  }
  if (total < 5) return "Common";
  // Cosmic's fiery border is the ONLY grade that mixes RED (Immortal-band) and GOLD
  // (Legendary/Divine-band) pixels in a single frame — measured from the game's
  // ItemSlot_GradeBg_* textures: Cosmic ~44% red + ~53% gold, vs Legendary 0%+100%,
  // Immortal 100%+0%, Divine 0%+52% (verified unique across all 10 grades). Its
  // plurality bucket is Legendary, so this MUST run before the plurality pick below
  // or a Cosmic mislabels as Legendary. Requiring BOTH bands ≥20% of sampled px is
  // far from any solid-colour grade, so a real Legendary/Immortal can't trip it.
  const cosRed = votes.get("Immortal") || 0;
  const cosGold = (votes.get("Legendary") || 0) + (votes.get("Divine") || 0);
  if (total >= 20 && cosRed >= total * 0.2 && cosGold >= total * 0.2) return "Cosmic";
  let best = null, bestN = 0;
  for (const [name, n] of votes) if (n > bestN) { bestN = n; best = name; }
  return best;
}

// ---------- matcher ----------

export class Matcher {
  // refsBuf: ArrayBuffer of N*(32*32*3 + 32*32) bytes; refsMeta: [{base,icon}]
  constructor(refsBuf, refsMeta) {
    const u8 = new Uint8Array(refsBuf);
    const N = refsMeta.length;
    this.n = N;
    this.base = refsMeta.map(m => m.base);
    this.icon = refsMeta.map(m => m.icon);
    this.isLearned = new Array(N).fill(false);
    this.rvec = new Float32Array(N * 3072);   // 32*32*3
    this.rmask = new Uint8Array(N * 1024);
    const stride = 3072 + 1024;
    for (let i = 0; i < N; i++) {
      const o = i * stride;
      for (let k = 0; k < 3072; k++) this.rvec[i * 3072 + k] = u8[o + k] / 255;
      this.rmask.set(u8.subarray(o + 3072, o + stride), i * 1024);
    }
  }

  // Add learned (user-labelled) reference signatures so they participate in
  // the FULL extraction-ensemble matching — same architecture as the desktop
  // matcher; a single-variant side-comparison misses whenever the canonical
  // extraction is the one that failed. entries: [{vec, valid, base}]
  appendRefs(entries) {
    if (!entries.length) return;
    const N = this.n + entries.length;
    const rvec = new Float32Array(N * 3072);
    const rmask = new Uint8Array(N * 1024);
    rvec.set(this.rvec); rmask.set(this.rmask);
    entries.forEach((e, j) => {
      rvec.set(e.vec, (this.n + j) * 3072);
      rmask.set(e.valid, (this.n + j) * 1024);
      this.base.push(e.base);
      this.icon.push(null);
      this.isLearned.push(true);
    });
    this.rvec = rvec; this.rmask = rmask; this.n = N;
  }

  // One variant signature vs all refs -> writes min into dbest (in place).
  _scoreInto(sig, dbest) {
    const { vec, valid, red } = sig;
    let vcount = 0;
    for (let p = 0; p < 1024; p++) if (valid[p]) vcount++;
    if (vcount < 60) return;
    const keep = new Uint8Array(1024);
    for (let p = 0; p < 1024; p++) keep[p] = red[p] ? 0 : 1;
    for (let i = 0; i < this.n; i++) {
      const rb = i * 3072, mb = i * 1024;
      let sum = 0, nn = 0;
      for (let p = 0; p < 1024; p++) {
        if (!keep[p]) continue;
        if (!(valid[p] || this.rmask[mb + p])) continue;
        const j = p * 3, k = rb + p * 3;
        const db = vec[j] - this.rvec[k];
        const dg = vec[j + 1] - this.rvec[k + 1];
        const dr = vec[j + 2] - this.rvec[k + 2];
        sum += db * db + dg * dg + dr * dr; nn++;
      }
      if (nn < 60) continue;
      const d = sum / (nn * 3);
      if (d < dbest[i]) dbest[i] = d;
    }
  }

  // matcher.py _match_topk: min distance over the ensemble, early-exit at 0.03.
  matchTopK(cell, k = 8) {
    const dbest = new Float32Array(this.n).fill(1e9);
    for (const sig of cellVariants(cell)) {
      this._scoreInto(sig, dbest);
      let mn = 1e9; for (let i = 0; i < this.n; i++) if (dbest[i] < mn) mn = dbest[i];
      if (mn <= 0.03) break;
    }
    const order = Array.from({ length: this.n }, (_, i) => i)
      .sort((a, b) => dbest[a] - dbest[b]).slice(0, k);
    return order.filter(i => dbest[i] < 1e8)
      .map(i => ({ dist: dbest[i], base: this.base[i], icon: this.icon[i],
                   learned: this.isLearned[i] }));
  }
}

export const _internal = {
  extractFlood, extractTwotone, keepMainBlob, vecFromItem, cellVariants,
  borderRarity, redMaskFlags, resizeArea, bgr2hsv, crop,
};
