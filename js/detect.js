// Warehouse-panel + item-grid detection, ported from analyzer.py.
// Same BGR {w,h,data:Uint8Array} image format as recognize.js.
//
// find_souko_panel uses multi-scale TM_CCOEFF_NORMED template matching; a
// naive full-res NCC at 13 scales is too slow in JS, so we match on a coarse
// grayscale pyramid first and refine the best candidate at full resolution
// (same result as cv2 on the reference screenshot, see test_detect.js).

import { _internal } from "./recognize.js?v20260616zaf";
const { bgr2hsv } = _internal;

// ---------- gray helpers ----------

export function toGray(img) {
  const { w, h, data } = img;
  const g = new Float32Array(w * h);
  for (let p = 0, i = 0; p < w * h; p++, i += 3) {
    g[p] = 0.114 * data[i] + 0.587 * data[i + 1] + 0.299 * data[i + 2];
  }
  return { w, h, g };
}

function resizeGray(src, dw, dh) {
  const { w, h, g } = src;
  const out = new Float32Array(dw * dh);
  for (let oy = 0; oy < dh; oy++) {
    const y0 = oy * h / dh, y1 = (oy + 1) * h / dh;
    const iy0 = Math.floor(y0), iy1 = Math.min(h, Math.ceil(y1));
    for (let ox = 0; ox < dw; ox++) {
      const x0 = ox * w / dw, x1 = (ox + 1) * w / dw;
      const ix0 = Math.floor(x0), ix1 = Math.min(w, Math.ceil(x1));
      let s = 0, area = 0;
      for (let yy = iy0; yy < iy1; yy++) {
        const wy = Math.min(y1, yy + 1) - Math.max(y0, yy);
        for (let xx = ix0; xx < ix1; xx++) {
          const wx = Math.min(x1, xx + 1) - Math.max(x0, xx);
          s += g[yy * w + xx] * wy * wx; area += wy * wx;
        }
      }
      out[oy * dw + ox] = s / area;
    }
  }
  return { w: dw, h: dh, g: out };
}

// Integral images (sum + sum of squares) for fast local mean/variance.
// Computed once per gray image and reused across all template scales.
function integralOf(img) {
  const { w: W, h: H, g: I } = img;
  const S = new Float64Array((W + 1) * (H + 1));
  const S2 = new Float64Array((W + 1) * (H + 1));
  for (let y = 0; y < H; y++) {
    let rs = 0, rs2 = 0;
    for (let x = 0; x < W; x++) {
      const v = I[y * W + x];
      rs += v; rs2 += v * v;
      S[(y + 1) * (W + 1) + x + 1] = S[y * (W + 1) + x + 1] + rs;
      S2[(y + 1) * (W + 1) + x + 1] = S2[y * (W + 1) + x + 1] + rs2;
    }
  }
  return { S, S2 };
}

// TM_CCOEFF_NORMED of gray template over gray image, restricted to a window.
// `step` > 1 scans a sparse lattice (coarse pass; refine recovers exactness).
function nccBest(img, tpl, wx0 = 0, wy0 = 0, wx1 = -1, wy1 = -1, integ = null, step = 1) {
  const { w: W, h: H, g: I } = img;
  const { w: tw, h: th, g: T } = tpl;
  if (wx1 < 0) wx1 = W - tw; if (wy1 < 0) wy1 = H - th;
  wx0 = Math.max(0, wx0); wy0 = Math.max(0, wy0);
  wx1 = Math.min(W - tw, wx1); wy1 = Math.min(H - th, wy1);
  if (wx1 < wx0 || wy1 < wy0 || tw < 2 || th < 2) return { score: -2, x: 0, y: 0 };
  const n = tw * th;
  let tm = 0; for (let i = 0; i < n; i++) tm += T[i];
  tm /= n;
  const Tc = new Float32Array(n);
  let tden = 0;
  for (let i = 0; i < n; i++) { Tc[i] = T[i] - tm; tden += Tc[i] * Tc[i]; }
  tden = Math.sqrt(tden);
  if (tden < 1e-6) return { score: -2, x: 0, y: 0 };
  const { S, S2 } = integ || integralOf(img);
  const win = (A, x, y) => A[(y + th) * (W + 1) + x + tw] - A[y * (W + 1) + x + tw]
                         - A[(y + th) * (W + 1) + x] + A[y * (W + 1) + x];
  let best = { score: -2, x: 0, y: 0 };
  for (let y = wy0; y <= wy1; y += step) {
    for (let x = wx0; x <= wx1; x += step) {
      let cross = 0;
      for (let ty = 0; ty < th; ty++) {
        const ib = (y + ty) * W + x, tb = ty * tw;
        for (let tx = 0; tx < tw; tx++) cross += Tc[tb + tx] * I[ib + tx];
      }
      const s1 = win(S, x, y), s2 = win(S2, x, y);
      const iden = Math.sqrt(Math.max(0, s2 - s1 * s1 / n));
      const score = iden < 1e-6 ? -2 : cross / (tden * iden);
      if (score > best.score) best = { score, x, y };
    }
  }
  return best;
}

// Like nccBest but returns the top-K spatially-separated peaks. The single
// best coarse peak can land on the WRONG panel (e.g. the hero panel in a
// whole-game capture outscores the small warehouse for a given template size),
// so the coarse pass must surface several candidates and let the full-res
// refine decide — otherwise the real warehouse is lost before refinement.
function nccPeaks(img, tpl, integ, step, K, sep) {
  const { w: W, h: H, g: I } = img;
  const { w: tw, h: th, g: T } = tpl;
  const wx1 = W - tw, wy1 = H - th;
  if (wx1 < 0 || wy1 < 0 || tw < 2 || th < 2) return [];
  const n = tw * th;
  let tm = 0; for (let i = 0; i < n; i++) tm += T[i]; tm /= n;
  const Tc = new Float32Array(n); let tden = 0;
  for (let i = 0; i < n; i++) { Tc[i] = T[i] - tm; tden += Tc[i] * Tc[i]; }
  tden = Math.sqrt(tden);
  if (tden < 1e-6) return [];
  const { S, S2 } = integ;
  const win = (A, x, y) => A[(y + th) * (W + 1) + x + tw] - A[y * (W + 1) + x + tw]
                         - A[(y + th) * (W + 1) + x] + A[y * (W + 1) + x];
  const cand = [];
  for (let y = 0; y <= wy1; y += step) {
    for (let x = 0; x <= wx1; x += step) {
      let cross = 0;
      for (let ty = 0; ty < th; ty++) {
        const ib = (y + ty) * W + x, tb = ty * tw;
        for (let tx = 0; tx < tw; tx++) cross += Tc[tb + tx] * I[ib + tx];
      }
      const s1 = win(S, x, y), s2 = win(S2, x, y);
      const iden = Math.sqrt(Math.max(0, s2 - s1 * s1 / n));
      const score = iden < 1e-6 ? -2 : cross / (tden * iden);
      cand.push({ score, x, y });
    }
  }
  cand.sort((a, b) => b.score - a.score);
  const peaks = [];
  for (const c of cand) {
    if (peaks.every(p => Math.abs(p.x - c.x) > sep || Math.abs(p.y - c.y) > sep)) {
      peaks.push(c);
      if (peaks.length >= K) break;
    }
  }
  return peaks;
}

// ---------- panel detection (analyzer.find_souko_panel) ----------

const SCALES = [0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.25, 1.4, 1.5, 1.75, 2.0, 2.5];
const HALF_WIDTH_BASE = 165;

export function findSoukoPanel(img, tplImg, minScore = 0.6) {
  const W = img.w, H = img.h;
  const gImg = toGray(img);
  const gTpl = toGray(tplImg);
  // Coarse pass on a downscaled pyramid level finds each scale's candidate
  // position; tiny coarse templates give noisy scores, so EVERY scale is then
  // re-scored at full resolution around its own candidate and the best
  // refined score wins (matches cv2's full-res multi-scale result).
  // A modest coarse level keeps it fast; robustness comes from refining the
  // top-K coarse PEAKS per scale (not just the single best), so a small
  // warehouse that loses the per-scale coarse max to another panel is still
  // recovered at full resolution.
  const k = Math.min(1, 560 / W);
  const cImg = k < 1 ? resizeGray(gImg, Math.round(W * k), Math.round(H * k)) : gImg;
  const cInteg = integralOf(cImg);
  const fInteg = integralOf(gImg);
  const pad = Math.ceil(2 / k) + 7;          // +1 covers the coarse step lattice
  let best = null;   // refined: {score, f, x, y, tw, th}
  for (const f of SCALES) {
    const ctw = Math.round(gTpl.w * f * k), cth = Math.round(gTpl.h * f * k);
    if (ctw < 5 || cth < 4 || ctw >= cImg.w || cth >= cImg.h) continue;
    const sep = Math.max(4, Math.round(ctw * 0.5));
    const peaks = nccPeaks(cImg, resizeGray(gTpl, ctw, cth), cInteg, 2, 4, sep);
    const tw = Math.max(8, Math.round(gTpl.w * f));
    const th = Math.max(8, Math.round(gTpl.h * f));
    if (tw >= W || th >= H) continue;
    for (const c of peaks) {
      if (c.score < -1) continue;
      const cx0 = Math.round(c.x / k), cy0 = Math.round(c.y / k);
      const r = nccBest(gImg, resizeGray(gTpl, tw, th),
                        cx0 - pad, cy0 - pad, cx0 + pad, cy0 + pad, fInteg);
      if (!best || r.score > best.score) best = { score: r.score, f, x: r.x, y: r.y, tw, th };
    }
  }
  if (!best || best.score < minScore) return null;
  const cx = best.x + (best.tw >> 1);
  const hw = Math.round(HALF_WIDTH_BASE * best.f);
  return { score: best.score, scale: best.f, cx, title_y: best.y,
           x0: Math.max(0, cx - hw), x1: Math.min(W, cx + hw) };
}

// ---------- cell detection (analyzer.detect_cell_boxes etc.) ----------

// ---------- projection-based grid detection (robust to packed cells) ----------
// Connected-component boxes fail when adjacent same-rarity cells touch (their
// bright backgrounds merge a whole row into one bar). Instead we find the
// regular grid from row/column projection profiles of the saturation mask,
// recover a clean lattice (pitch + phase) so missing/merged bands are filled
// in, and keep lattice cells that actually contain an item.

function runs1d(flags, n) {
  const out = []; let st = -1;
  for (let i = 0; i < n; i++) {
    if (flags[i] && st < 0) st = i;
    else if (!flags[i] && st >= 0) { out.push([st, i]); st = -1; }
  }
  if (st >= 0) out.push([st, n]);
  return out;
}

// Recover a regular lattice from possibly-incomplete band centers.
// Returns {pts:[centers...], pitch} fit to the dominant evenly-spaced subset.
function fitLattice(centers) {
  centers = [...centers].sort((a, b) => a - b);
  if (centers.length < 2) return null;
  const diffs = []; for (let i = 1; i < centers.length; i++) diffs.push(centers[i] - centers[i - 1]);
  const small = Math.min(...diffs);
  const consistent = diffs.filter(d => d <= small * 1.5).sort((a, b) => a - b);
  let pitch = consistent[consistent.length >> 1] || diffs.sort((a, b) => a - b)[diffs.length >> 1];
  if (pitch < 10) return null;
  // dominant phase: the (c mod pitch) cluster the most centers agree with
  let best = [];
  for (const c0 of centers) {
    const ph = c0 % pitch;
    const agree = centers.filter(c => {
      const d = ((c - ph) % pitch + pitch) % pitch;
      return Math.min(d, pitch - d) <= pitch * 0.22;
    });
    if (agree.length > best.length) best = agree;
  }
  const lo = Math.min(...best), hi = Math.max(...best);
  const pts = [];
  for (let c = lo; c <= hi + 1; c += pitch) pts.push(Math.round(c));
  return { pts, pitch };
}

// The in-game warehouse grid is a fixed 7×7; cell pitch is ~40 px at UI scale
// 1.0 (measured on the reference sample; scan2 shows the title-match scale can
// read 1.4 when the true scale is 1.5, so priors get a generous ±30% window).
const GRID_COLS = 7;
const GRID_ROWS = 7;
const CELL_PITCH_BASE = 40;

// Sparse layouts have no adjacent items, so the fitted pitch comes out as a
// MULTIPLE of the true one; divide it back down when a prior says so.
function fixPitch(fit, prior) {
  if (!fit) return fit;
  const m = Math.round(fit.pitch / prior);
  if (m >= 2 && Math.abs(fit.pitch / m - prior) < prior * 0.3) {
    const p = fit.pitch / m;
    const lo = fit.pts[0], hi = fit.pts[fit.pts.length - 1];
    const pts = [];
    for (let c = lo; c <= hi + 1; c += p) pts.push(Math.round(c));
    return { pts, pitch: p };
  }
  return fit;
}

// Lattice centers from a projection profile: scan every phase offset of the
// given pitch and keep the one whose cell windows capture the most signal.
// Band centers (the old approach) shift with each item's glow, which threw
// the grid off by half a cell in sparse layouts; window-sum maximisation
// self-centres on however many items there are.
// count > 0 clips to the best `count`-window (ties -> nearest `anchor`).
function latticeFit(profile, n0, n1, pitch, count, anchor) {
  const N = profile.length;
  const cum = new Float64Array(N + 1);
  for (let i = 0; i < N; i++) cum[i + 1] = cum[i] + profile[i];
  const wHalf = Math.max(2, pitch * 0.45);
  const winSum = c => {
    const a = Math.max(n0, Math.round(c - wHalf)), b = Math.min(n1, Math.round(c + wHalf));
    return b > a ? cum[b] - cum[a] : 0;
  };
  const ip = Math.max(4, Math.round(pitch));
  let bestPhi = -1, bestScore = -1;
  for (let phi = 0; phi < ip; phi++) {
    // sum of SQUARED window sums: a plain sum is phase-blind over a uniform
    // band (any phase captures ~everything across two windows); squaring
    // rewards concentrating each item's mass into a single window
    let s = 0;
    for (let c = n0 + phi; c < n1; c += pitch) { const v = winSum(c); s += v * v; }
    if (s > bestScore) { bestScore = s; bestPhi = phi; }
  }
  if (bestScore <= 0) return [];
  const centers = [];
  for (let c = n0 + bestPhi; c < n1; c += pitch) centers.push(Math.round(c));
  if (count > 0 && centers.length > count) {
    let bi = 0, bs = -1, bd = Infinity;
    for (let i = 0; i + count <= centers.length; i++) {
      let s = 0;
      for (let k = 0; k < count; k++) s += winSum(centers[i + k]);
      const d = Math.abs((centers[i] + centers[i + count - 1]) / 2 - anchor);
      if (s > bs || (s === bs && d < bd)) { bs = s; bd = d; bi = i; }
    }
    return centers.slice(bi, bi + count);
  }
  return centers;
}

// roi (BGR) + title_y + UI scale -> grid cells [{col,row,x,y,w,h}]
// cxRoi = panel centre in ROI coords; anchors the 7-column window.
export function detectGrid(roi, titleY, scale, cxRoi = -1) {
  const { w: W, h: H, data } = roi;
  const sat = new Uint8Array(W * H);     // saturated pixels: drive the projections
  const lit = new Uint8Array(W * H);     // saturated OR bright: drives "is the slot filled"
  for (let p = 0, i = 0; p < W * H; p++, i += 3) {
    const [, s, v] = bgr2hsv(data[i], data[i + 1], data[i + 2]);
    sat[p] = (s > 55 && v > 70) ? 1 : 0;
    // pale items (parchment scrolls, silver gear) on a black Common background
    // have almost no saturation — brightness still separates them from the
    // dark empty-slot pattern
    lit[p] = (sat[p] || v > 110) ? 1 : 0;
  }
  // column projection, limited to where the grid can actually be — the tab row
  // above, the UI below the panel and the panel side borders otherwise dominate
  // the profile when only a few items are present and drag the lattice phase off
  const prior0 = CELL_PITCH_BASE * scale;
  const ymin = titleY + Math.round(78 * scale);    // grid starts below the tab row
  const yHi = Math.min(H, ymin + Math.round(prior0 * 7.5));   // ≤7 visible rows
  const target = cxRoi >= 0 ? cxRoi : W / 2;
  const xLo = Math.max(0, Math.round(target - 4 * prior0));
  const xHi = Math.min(W, Math.round(target + 4 * prior0));
  const colsum = new Float64Array(W);
  for (let y = ymin; y < yHi; y++) { const yb = y * W; for (let x = xLo; x < xHi; x++) colsum[x] += sat[yb + x]; }
  let cmax = 0; for (let x = 0; x < W; x++) if (colsum[x] > cmax) cmax = colsum[x];
  // Cell-band width acceptance, relative to the detected UI scale. The window
  // only ever GROWS vs the old fixed [24,64] (identical at scale 1.0), so normal
  // captures are unchanged — but at 1.25/1.5/2x in-game window-scale or hi-DPI
  // monitors the cells are wider than 64px and were being filtered out, which
  // dropped whole rows/columns ("升目が入りきらない / ラベルが剥がれる").
  const bandLo = Math.min(24, Math.round(24 * scale));
  const bandHi = Math.max(64, Math.round(64 * scale));
  const colOn = new Uint8Array(W); for (let x = 0; x < W; x++) colOn[x] = colsum[x] > cmax * 0.20 ? 1 : 0;
  let colBands = runs1d(colOn, W).filter(([a, b]) => b - a >= bandLo && b - a <= bandHi);
  // `scale` comes from the title template match and can be mis-read when the
  // window moves to a monitor with different DPI; if the relative window
  // rejected everything, retry with a permissive absolute one and let
  // fitLattice + the fill checks sort out the noise.
  if (colBands.length < 2)
    colBands = runs1d(colOn, W).filter(([a, b]) => b - a >= 20 && b - a <= 110);
  if (cmax === 0) return [];
  // Pitch: from the spacing of detected bands when ≥2 agree, else the prior.
  // Band CENTERS are only used for pitch — phase comes from latticeFit, which
  // is immune to each item's asymmetric glow.
  const prior = prior0;
  let colPitch = prior;
  const cFit = fixPitch(fitLattice(colBands.map(([a, b]) => (a + b) >> 1)), prior);
  if (cFit && cFit.pitch >= prior * 0.7 && cFit.pitch <= prior * 1.45) colPitch = cFit.pitch;
  // Phase-fit only inside accepted bands: the panel scrollbar (tall but
  // narrow) and merged multi-cell blobs never pass the width filter, so they
  // can't drag the lattice. Falls back to the raw profile if the bands would
  // discard most of the signal.
  const maskedCol = new Float64Array(W);
  let cIn = 0, cAll = 0;
  for (let x = xLo; x < xHi; x++) cAll += colsum[x];
  for (const [a, b] of colBands) for (let x = a; x < b; x++) { maskedCol[x] = colsum[x]; cIn += colsum[x]; }
  // the warehouse always has 7 columns
  const cols = latticeFit(cAll > 0 && cIn / cAll >= 0.3 ? maskedCol : colsum,
                          xLo, xHi, colPitch, GRID_COLS, target);
  if (!cols.length) return [];
  const cw = Math.round(colPitch * 0.9);   // cell box from pitch — sparse-layout
                                           // band widths are just item glow
  // row projection, restricted to the column regions (ignore gutters)
  const colMask = new Uint8Array(W);
  for (const c of cols) for (let x = Math.max(0, c - (cw >> 1)); x < Math.min(W, c + (cw >> 1)); x++) colMask[x] = 1;
  const rowsum = new Float64Array(H);
  for (let y = ymin; y < yHi; y++) { const yb = y * W; let s = 0; for (let x = 0; x < W; x++) if (colMask[x]) s += sat[yb + x]; rowsum[y] = s; }
  let rmax = 0; for (let y = 0; y < H; y++) if (rowsum[y] > rmax) rmax = rowsum[y];
  if (rmax === 0) return [];
  const rowOn = new Uint8Array(H); for (let y = 0; y < H; y++) rowOn[y] = rowsum[y] > rmax * 0.12 ? 1 : 0;
  let rowBands = runs1d(rowOn, H).filter(([a, b]) => b - a >= bandLo && b - a <= bandHi);
  if (rowBands.length < 1)
    rowBands = runs1d(rowOn, H).filter(([a, b]) => b - a >= 20 && b - a <= 110);
  let rowPitch = colPitch;                 // cells are square
  const rFit = fixPitch(fitLattice(rowBands.map(([a, b]) => (a + b) >> 1)), colPitch);
  if (rFit && rFit.pitch >= colPitch * 0.7 && rFit.pitch <= colPitch * 1.45) rowPitch = rFit.pitch;
  // Bands only inform pitch/phase — never which rows EXIST. An isolated item
  // in an otherwise empty row falls below the relative threshold (or its
  // saturated core is thinner than the width filter) and used to vanish; the
  // visible grid is always 7 rows, so emit all of them and let the fill
  // check drop the empty ones.
  const maskedRow = new Float64Array(H);
  let rIn = 0, rAll = 0;
  for (let y = ymin; y < yHi; y++) rAll += rowsum[y];
  for (const [a, b] of rowBands) for (let y = a; y < b; y++) { maskedRow[y] = rowsum[y]; rIn += rowsum[y]; }
  const rows = latticeFit(rAll > 0 && rIn / rAll >= 0.3 ? maskedRow : rowsum,
                          ymin, yHi, rowPitch, GRID_ROWS, ymin + 3.5 * rowPitch);
  if (!rows.length) return [];
  const rh = Math.round(rowPitch * 0.9);
  // emit filled cells; coverage is measured on the INNER box so a neighbour's
  // glow bleeding over the gutter can't mark an empty cell as filled
  const cells = [];
  const ix = Math.max(2, Math.round(cw * 0.15)), iy = Math.max(2, Math.round(rh * 0.15));
  rows.forEach((ry, ri) => cols.forEach((cx, ci) => {
    const x0 = Math.max(0, cx - (cw >> 1)), y0 = Math.max(0, ry - (rh >> 1));
    const x1 = Math.min(W, cx + (cw >> 1)), y1 = Math.min(H, ry + (rh >> 1));
    let cov = 0;
    const n = Math.max(0, (x1 - ix) - (x0 + ix)) * Math.max(0, (y1 - iy) - (y0 + iy));
    for (let y = y0 + iy; y < y1 - iy; y++) { const yb = y * W; for (let x = x0 + ix; x < x1 - ix; x++) cov += lit[yb + x]; }
    if (n > 0 && cov / n > 0.10 && x1 - x0 > cw * 0.6 && y1 - y0 > rh * 0.6)
      cells.push({ col: ci, row: ri, x: x0, y: y0, w: x1 - x0, h: y1 - y0 });
  }));
  return cells;
}

function morphClose3(mask, w, h) {
  const dil = new Uint8Array(w * h);
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
    let v = 0;
    for (let dy = -1; dy <= 1 && !v; dy++) for (let dx = -1; dx <= 1; dx++) {
      const yy = y + dy, xx = x + dx;
      if (yy >= 0 && yy < h && xx >= 0 && xx < w && mask[yy * w + xx]) { v = 1; break; }
    }
    dil[y * w + x] = v;
  }
  const out = new Uint8Array(w * h);
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
    let v = 1;
    for (let dy = -1; dy <= 1 && v; dy++) for (let dx = -1; dx <= 1; dx++) {
      const yy = y + dy, xx = x + dx;
      if (yy < 0 || yy >= h || xx < 0 || xx >= w || !dil[yy * w + xx]) { v = 0; break; }
    }
    out[y * w + x] = v;
  }
  return out;
}

// bounding boxes of 8-connected components (== external contours' boundingRect)
function componentBoxes(mask, w, h) {
  const label = new Int32Array(w * h);
  const boxes = [];
  const stack = [];
  let nlab = 0;
  for (let p = 0; p < w * h; p++) {
    if (!mask[p] || label[p]) continue;
    nlab++;
    let x0 = w, y0 = h, x1 = 0, y1 = 0;
    stack.push(p); label[p] = nlab;
    while (stack.length) {
      const q = stack.pop();
      const x = q % w, y = (q / w) | 0;
      if (x < x0) x0 = x; if (x > x1) x1 = x;
      if (y < y0) y0 = y; if (y > y1) y1 = y;
      for (let dy = -1; dy <= 1; dy++) for (let dx = -1; dx <= 1; dx++) {
        const yy = y + dy, xx = x + dx;
        if (yy < 0 || yy >= h || xx < 0 || xx >= w) continue;
        const r = yy * w + xx;
        if (mask[r] && !label[r]) { label[r] = nlab; stack.push(r); }
      }
    }
    boxes.push([x0, y0, x1 - x0 + 1, y1 - y0 + 1]);
  }
  return boxes;
}

const median = a => { const s = [...a].sort((x, y) => x - y); return s[s.length >> 1]; };

export function detectCellBoxes(roi, sMin = 55, vMin = 70) {
  const { w, h, data } = roi;
  let mask = new Uint8Array(w * h);
  for (let p = 0, i = 0; p < w * h; p++, i += 3) {
    const [, s, v] = bgr2hsv(data[i], data[i + 1], data[i + 2]);
    mask[p] = (s > sMin && v > vMin) ? 1 : 0;
  }
  mask = morphClose3(mask, w, h);
  const raw = componentBoxes(mask, w, h);
  if (!raw.length) return [];
  const sq = raw.filter(r => r[2] / r[3] > 0.6 && r[2] / r[3] < 1.6 && r[2] > 12 && r[3] > 12);
  if (!sq.length) return [];
  const med = median(sq.map(r => r[2]).concat(sq.map(r => r[3])));
  const lo = (med * 0.55) ** 2, hi = (med * 1.6) ** 2;
  return raw.filter(([x, y, bw, bh]) => {
    const ar = bw / bh, area = bw * bh;
    return ar > 0.6 && ar < 1.6 && area > lo && area < hi;
  });
}

function clusterCenters(values, tol) {
  if (!values.length) return [];
  const v = [...values].sort((a, b) => a - b);
  const clusters = [[v[0]]];
  for (const x of v.slice(1)) {
    const last = clusters[clusters.length - 1];
    if (x - last[last.length - 1] <= tol) last.push(x);
    else clusters.push([x]);
  }
  return clusters.map(c => Math.round(c.reduce((a, b) => a + b, 0) / c.length));
}

export function snapToGrid(boxes) {
  if (!boxes.length) return [];
  const medW = median(boxes.map(b => b[2]));
  const medH = median(boxes.map(b => b[3]));
  const colC = clusterCenters(boxes.map(b => b[0] + (b[2] >> 1)), Math.max(8, medW >> 1));
  const rowC = clusterCenters(boxes.map(b => b[1] + (b[3] >> 1)), Math.max(8, medH >> 1));
  const nearest = (v, cs) => {
    let bi = 0, bd = Infinity;
    cs.forEach((c, i) => { const d = Math.abs(v - c); if (d < bd) { bd = d; bi = i; } });
    return bi;
  };
  return boxes.map(([x, y, w, h]) => ({
    col: nearest(x + (w >> 1), colC), row: nearest(y + (h >> 1), rowC), x, y, w, h,
  }));
}

export function cleanGrid(cells, titleY = null) {
  if (!cells.length) return [];
  const medH = median(cells.map(c => c.h));
  let out = cells;
  if (titleY != null) {
    out = out.filter(c => c.y + c.h / 2 > titleY + 1.3 * medH);
  }
  if (!out.length) out = cells;
  const rowc = {}, colc = {};
  for (const c of out) { rowc[c.row] = (rowc[c.row] || 0) + 1; colc[c.col] = (colc[c.col] || 0) + 1; }
  return out.filter(c => rowc[c.row] >= 2 && colc[c.col] >= 2);
}

// ---------- top-level: screenshot -> panel + cells ----------

export function readWarehouse(img, tplImg) {
  const panel = findSoukoPanel(img, tplImg);
  if (!panel) return { panel: null, roi: null, cells: [] };
  // The crop width is title-scale × a fixed half-width, so a mis-read scale
  // (window moved to a different-DPI monitor) or a wider panel clips the
  // outer columns. Cells touching a crop edge mean the grid continues past
  // it — widen that side and re-detect, up to 3 times.
  let x0 = panel.x0, x1 = panel.x1;
  let roi = _internal.crop(img, x0, 0, x1 - x0, img.h);
  let cells = detectGrid(roi, panel.title_y, panel.scale, panel.cx - x0);
  for (let pass = 0; pass < 3 && cells.length; pass++) {
    const grow = Math.max(40, Math.round((x1 - x0) * 0.2));
    const left = cells.some(c => c.x <= 2) && x0 > 0;
    const right = cells.some(c => c.x + c.w >= roi.w - 2) && x1 < img.w;
    if (!left && !right) break;
    if (left) x0 = Math.max(0, x0 - grow);
    if (right) x1 = Math.min(img.w, x1 + grow);
    roi = _internal.crop(img, x0, 0, x1 - x0, img.h);
    cells = detectGrid(roi, panel.title_y, panel.scale, panel.cx - x0);
  }
  panel.x0 = x0; panel.x1 = x1;
  return { panel, roi, cells };
}
