// Full scan pipeline: screenshot -> warehouse cells -> identified items.
// Ports matcher.py identify() on top of detect.js + recognize.js.

import { readWarehouse } from "./detect.js?v20260616zaa";
import { Matcher, _internal } from "./recognize.js?v20260616zaa";
const { crop, borderRarity, vecFromItem, extractFlood, bgr2hsv } = _internal;

// The red "can't equip" X (lower-right) appears ONLY on equipment — materials
// (incl. Anniversary coins) never show it. Measured on the RAW cell.
function hasCantEquipX(cell) {
  const { w, h, data } = cell;
  let red = 0, n = 0;
  for (let y = Math.floor(h * 0.55); y < h; y++) {
    for (let x = Math.floor(w * 0.45); x < w; x++) {
      const i = (y * w + x) * 3;
      const [hh, s, v] = bgr2hsv(data[i], data[i + 1], data[i + 2]);
      if ((hh <= 8 || hh >= 172) && s >= 140 && v >= 140) red++;
      n++;
    }
  }
  return n > 0 && red / n > 0.12;
}

const MATCH_MAX_DIST = 0.08;
const NON_SELLABLE = new Set(["Common", "Uncommon", "Rare"]);
const GRADE_ORDER = ["Legendary", "Immortal", "Arcana", "Beyond", "Celestial", "Divine", "Cosmic"];

// Build base -> [{rarity, hash, synth}] (grade-ordered) from items.json.
export function variantsByBase(items) {
  const by = new Map();
  for (const [hash, v] of Object.entries(items)) {
    if (!by.has(v.base)) by.set(v.base, []);
    by.get(v.base).push({ rarity: v.rarity, hash, synth: !!v.synth });
  }
  for (const vs of by.values()) {
    vs.sort((a, b) => (GRADE_ORDER.indexOf(a.rarity) + 99 * (GRADE_ORDER.indexOf(a.rarity) < 0))
                    - (GRADE_ORDER.indexOf(b.rarity) + 99 * (GRADE_ORDER.indexOf(b.rarity) < 0)));
  }
  return by;
}

// matcher.py identify(): one cell -> {base, rarity, hash, status, dist, border, candidates}
// Learned (user-corrected) references must be appended INTO the matcher via
// Matcher.appendRefs so they participate in the extraction ensemble.
// `skips` = user "not tradeable" signatures [{sig, rarity}].
export function identifyCell(matcher, vbb, cell, skips = []) {
  const scored = matcher.matchTopK(cell, 40);   // wide pool -> enough distinct bases for the candidate list
  const rarity = borderRarity(cell);

  const isMaterial = b => { const vs = vbb.get(b); return vs && vs.length === 1 && vs[0].rarity === ""; };

  // canonical signature for learned/skip comparison + the xred ratio
  const sig = vecFromItem(extractFlood(cell));
  let xred = 0; for (const r of sig.red) xred += r; xred /= sig.red.length;
  // The red "can't equip" X, measured on the RAW cell's lower-right (xred above
  // misses it: vecFromItem tight-crops the X away before resizing, so a weapon
  // whose blade doesn't reach that corner reads xred≈0 even with the X shown).
  const equipX = hasCantEquipX(cell);

  const sigDist = (a, b) => {
    let sum = 0, n = 0;
    for (let p = 0; p < 1024; p++) {
      if (!(a.valid[p] || b.valid[p])) continue;
      const j = p * 3;
      const d0 = a.vec[j] - b.vec[j], d1 = a.vec[j + 1] - b.vec[j + 1], d2 = a.vec[j + 2] - b.vec[j + 2];
      sum += d0 * d0 + d1 * d1 + d2 * d2; n++;
    }
    return n < 60 ? 1e9 : sum / (n * 3);
  };

  // candidates: top distinct bases for the confirm UI. Weak MATERIAL hits
  // (the coin/herb attractors that garbage extractions collapse into) are
  // demoted below equipment candidates whenever the border colour was read —
  // a genuinely-matched material (d<=0.05) keeps its spot.
  // keep EVERY distinct base whose match is still "realistic" — conf in the UI
  // is (1 - dist/0.16), so dist < ~0.145 means conf >~10%. The popover shows
  // them all (scroll past 6); a 25 safety cap avoids a pathological flood.
  const REALISTIC = 0.145;
  let cands = [];
  const seen = new Set();
  for (const s of scored) {
    if (seen.has(s.base)) continue;
    if (s.dist > REALISTIC) continue;
    seen.add(s.base);
    cands.push({ base: s.base, dist: +s.dist.toFixed(3), icon: s.icon,
                 variants: vbb.get(s.base) || [] });
    if (cands.length >= 25) break;
  }
  // Demote the COIN/MATERIAL attractors in the confirm list. A genuine material
  // matches very close (~0.003–0.012); a poorly-extracted weapon that collapses
  // into a round coin/gem icon sits at ~0.05–0.08. So penalise material hits
  // whose distance is above the genuine band — real materials (on a real
  // material cell) keep their spot, attractor-coins on a weapon cell drop out.
  const adj = c => c.dist + (isMaterial(c.base) && c.dist > 0.025 ? 0.12 : 0);
  // The red "can't equip" X only appears on EQUIPMENT (its ABSENCE doesn't prove
  // material — a character that CAN equip it shows no X). When present, it's a
  // hard "definitely gear" signal: drop every material candidate, even if that
  // leaves the list empty (better to send the user to search than offer a coin
  // we know is wrong).
  if (equipX) cands = cands.filter(c => !isMaterial(c.base));
  cands.sort((a, b) => adj(a) - adj(b));
  // a demoted attractor pushed past the realistic bar shouldn't clutter the list
  for (let i = cands.length - 1; i >= 0; i--) if (adj(cands[i]) > REALISTIC) cands.splice(i, 1);

  // learned refs live INSIDE the matcher (full ensemble, like the desktop);
  // a learned hit gets a looser acceptance since it's a specific captured item
  // and getDisplayMedia's slight colour shift inflates its distance.
  const LEARN_MAX = 0.075;
  let matched = false, base = "?", dist = scored.length ? +scored[0].dist.toFixed(3) : null;
  let isLearned = false;
  if (scored.length) {
    const top = scored[0];
    // Acceptance bars: MATERIAL hits are STRICT (0.05) even via learned refs —
    // garbage extractions of equipment collapse into material icons/labels
    // (the coin attractor) at 0.05–0.08 and would auto-confirm as the wrong
    // item. Real materials match at ~0.003–0.012 so they are unaffected.
    // Equipment learned hits keep the generous bar (getDisplayMedia colour
    // shift inflates capture-vs-capture distances slightly).
    const bar = isMaterial(top.base) ? 0.05 : (top.learned ? LEARN_MAX : MATCH_MAX_DIST);
    if (top.dist <= bar) { matched = true; base = top.base; isLearned = !!top.learned; }
  }

  const fin = (o) => ({ base, rarity, hash: null, tradeable: false, status: "unmatched",
                        dist, border: rarity, candidates: cands, learned: isLearned, ...o });

  // user marked this exact item (at this grade) not tradeable
  for (const e of skips) {
    if (e.rarity && rarity && e.rarity !== rarity) continue;
    if (sigDist(sig, e.sig) <= 0.05) return fin({ status: "not_tradeable" });
  }

  const variants = matched ? (vbb.get(base) || []) : [];

  // materials/decorations (single ""-rarity entry) trade at ANY grade.
  // `material:true` lets the UI hold these to a STRICTER auto-confirm distance:
  // simple round icons (coins/gems) are attractors that a mis-extracted piece of
  // gear collapses into at ~0.02-0.05, while a genuine material matches at
  // ~0.003-0.012 — so a borderline material hit should be reviewed, not silently
  // auto-confirmed as e.g. a coin the player never had.
  if (matched && variants.length === 1 && variants[0].rarity === "") {
    return fin({ hash: variants[0].hash, tradeable: true, status: "ok", material: true });
  }
  // Common/Uncommon/Rare equipment can't be traded
  if (NON_SELLABLE.has(rarity)) return fin({ status: "not_tradeable" });
  if (!matched) return fin({ status: (equipX || xred > 0.08) ? "ambiguous" : "unmatched" });

  const byRar = new Map(variants.map(v => [v.rarity, v]));
  if (byRar.has(rarity)) {
    const v = byRar.get(rarity);
    return fin({ hash: v.hash, tradeable: true, status: "ok", synth: v.synth });
  }
  if (variants.length === 1) {
    const v = variants[0];
    return fin({ rarity: v.rarity, hash: v.hash, tradeable: true, status: "ok", synth: v.synth });
  }
  return fin({ status: "ambiguous" });
}

// Whole screenshot -> {panel, roi, items:[{...identify, col,row,x,y,w,h}]}
// Async: yields to the event loop between cells so the UI can paint progress.
export async function scanImage(img, tpl, matcher, vbb, skips = [], onProgress = null) {
  const { panel, roi, cells } = readWarehouse(img, tpl);
  if (!panel) return { panel: null, roi: null, items: [] };
  const items = [];
  let i = 0;
  for (const c of cells) {
    const cellImg = crop(roi, c.x, c.y, c.w, c.h);
    const it = identifyCell(matcher, vbb, cellImg, skips);
    items.push({ ...it, col: c.col, row: c.row, x: c.x, y: c.y, w: c.w, h: c.h });
    if (onProgress) onProgress(++i, cells.length);
    if (i % 3 === 0) await new Promise(r => setTimeout(r));
  }
  return { panel, roi, items };
}
