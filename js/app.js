// TBH 倉庫まるごと査定 — main app logic (static site, no backend).
// Screenshots are processed entirely in this browser; nothing is uploaded.

import { Matcher, _internal } from "./recognize.js?v20260613d";
import { scanImage, variantsByBase } from "./pipeline.js?v20260613d";
import { T, LANGS, pickLang } from "./i18n.js?v20260613d";
const { vecFromItem, extractFlood, crop } = _internal;

const $ = id => document.getElementById(id);
const FEE = 0.85;                          // net after Steam's 15% fee
const FEEDBACK_TO = "takahasi599@gmail.com";   // ⑦ goes only to the developer

// ---------------- state ----------------
let LANG = pickLang();
// The market-reopen flag comes from prices.json ("unlocked"), set by the price
// bot when it detects new listings being allowed again (6/15 is only the server
// migration; reopening is announced separately). Until data loads: locked.
let UNLOCKED = false;
let MODE = localStorage.getItem("tbh_mode") || "base";   // 'base' | 'cur'
let GMODE = MODE;   // gacha "sell" basis — switchable INDEPENDENTLY of the main table
let DATA = null;        // {items, vbb, matcher, tpl, baseline, gacha, meta, prices}
let STREAM = null, VIDEO = null;
let SCAN = null;        // {imgW,imgH, cells:[{...item, assigned, ignored}]}
let SORT = JSON.parse(localStorage.getItem("tbh_sort") || '{"k":"total","d":-1}');
let POP_I = -1;

const t = k => (T[LANG] && T[LANG][k] !== undefined) ? T[LANG][k] : T.en[k];
const tu = k => t(UNLOCKED ? k + "_post" : k);   // post-sell-unlock variant of a label/banner
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
// inline ? badge (matches the yellow "?" drawn on unconfirmed cells); i18n
// strings carry a literal [?] placeholder where the badge should appear.
const QBADGE = '<span class="qmark">?</span>';
const withQ = key => esc(t(key)).replace(/\[\?\]/g, QBADGE);

// ---------------- data loading ----------------
async function loadData() {
  const j = async f => (await fetch("data/" + f)).json();
  const b = async f => (await fetch("data/" + f)).arrayBuffer();
  const [meta, items, refsMeta, refsBuf, tplMeta, tplBuf, baseline, gacha, ja] =
    await Promise.all([j("meta.json"), j("items.json"), j("refs.json"), b("refs.bin"),
                       j("tpl.json"), b("tpl.bin"), j("baseline.json"), j("gacha.json"),
                       j("ja_names.json")]);
  let prices = null;
  try { prices = await j("prices.json"); } catch (e) {}
  UNLOCKED = !!prices?.unlocked;
  // post-unlock the live market is the sensible default (unless the user chose)
  if (UNLOCKED && !localStorage.getItem("tbh_mode")) { MODE = "cur"; GMODE = "cur"; }
  let seed = [];
  try { seed = await j("learned_seed.json"); } catch (e) {}   // author's pre-trained labels
  DATA = {
    meta, items, baseline: baseline.items, gacha, prices, ja, seed,
    matcher: new Matcher(refsBuf, refsMeta),
    vbb: variantsByBase(items),
    tpl: { w: tplMeta.w, h: tplMeta.h, data: new Uint8Array(tplBuf) },
  };
}

// item display name (JA names only when UI is Japanese; others use EN market name)
const dispName = h => (LANG === "ja" && DATA.items[h]?.name_ja) ? DATA.items[h].name_ja : h;
const dispBase = bse => {
  if (LANG !== "ja") return bse;
  return (DATA.ja?.bases || {})[bse] || bse;
};
const iconUrl = h => DATA.meta.cdn + (DATA.items[h]?.icon || "") + "/64fx64f";
const iconUrlByIcon = ic => DATA.meta.cdn + ic + "/64fx64f";
// Steam pages open in the UI's language (ja -> 日本語表記)
const STEAM_LANG = { ja: "japanese", en: "english", "zh-CN": "schinese",
                     "zh-TW": "tchinese", ko: "koreana", ru: "russian" };
const marketUrl = h => `https://steamcommunity.com/market/listings/${DATA.meta.appid}/${encodeURIComponent(h)}?l=${STEAM_LANG[LANG] || "english"}`;
// never-listed items have no listing page (404) -> link to a market SEARCH
const marketSearchUrl = base => `https://steamcommunity.com/market/search?appid=${DATA.meta.appid}&q=${encodeURIComponent(base)}&l=${STEAM_LANG[LANG] || "english"}`;

// ---------------- price helpers ----------------
function unitPriceIn(hash, mode) {  // 1個の価格 in a SPECIFIC basis (JPY)
  if (mode === "base") {
    const b = DATA.baseline[hash];
    return b && b[0] != null ? b[0] : null;
  }
  const p = DATA.prices?.items?.[hash];
  if (!p) return null;
  return p.m ?? p.p ?? null;      // median of real sales when known, else lowest ask
}
function unitPrice(hash) { return unitPriceIn(hash, MODE); }   // main table -> global toggle
function volume(hash) {           // baseline: avg sold PER DAY pre-freeze / cur: sold in 24h
  if (MODE === "base") {
    const b = DATA.baseline[hash];
    return b && b[1] != null ? Math.round(b[1] / 7) : null;   // 7-day window -> per day
  }
  const p = DATA.prices?.items?.[hash];
  return p ? (p.v ?? null) : null;
}

// ⑱ flashy price bands (per unit JPY, 10 tiers)
function bandClass(v) {
  if (v == null) return "p0";
  if (v >= 100000) return "p9";
  if (v >= 30000) return "p8";
  if (v >= 10000) return "p7";
  if (v >= 5000) return "p6";
  if (v >= 3000) return "p5";
  if (v >= 2000) return "p4";
  if (v >= 1000) return "p3";
  if (v >= 500) return "p2";
  if (v >= 100) return "p1";
  return "p0";
}
// currency follows the UI language (like the Steam Market itself); values stay
// JPY internally, conversion happens at display time via prices.json's fx
// table (fallback: JPY display when fx is unavailable)
const CURRENCY = { ja: ["JPY", "¥"], en: ["USD", "$"], "zh-CN": ["CNY", "¥"],
                   "zh-TW": ["TWD", "NT$"], ko: ["KRW", "₩"], ru: ["RUB", "₽"] };
function money(vJpy) {
  if (vJpy == null) return "—";
  const [code, sym] = CURRENCY[LANG] || CURRENCY.en;
  const fx = DATA?.prices?.fx;
  const target = code === "USD" ? 1 : fx?.[code];     // fx table is USD-based
  if (code === "JPY" || !fx || !fx.JPY || !target)
    return "¥" + Math.round(vJpy).toLocaleString();
  const v = vJpy / fx.JPY * target;
  const digits = (code === "KRW" || v >= 100) ? 0 : 2;
  return sym + v.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
}
const yen = (v, cls) => v == null ? '<span class="muted">—</span>'
  : `<span class="${cls || bandClass(v)}">${money(v)}</span>`;

// ---------------- learned corrections (⑭ this browser only) ----------------
function loadLearned() {
  try { return JSON.parse(localStorage.getItem("tbh_learned") || "[]"); } catch (e) { return []; }
}
function packSig(sig) {
  const v = new Uint8Array(3072);
  for (let i = 0; i < 3072; i++) v[i] = Math.round(sig.vec[i] * 255);
  const m = new Uint8Array(128);
  for (let p = 0; p < 1024; p++) if (sig.valid[p]) m[p >> 3] |= 1 << (p & 7);
  const b64 = a => btoa(String.fromCharCode(...a));
  return { v: b64(v), m: b64(m) };
}
function unpackSig(e) {
  const un = s => Uint8Array.from(atob(s), c => c.charCodeAt(0));
  const v8 = un(e.v), m8 = un(e.m);
  const vec = new Float32Array(3072);
  for (let i = 0; i < 3072; i++) vec[i] = v8[i] / 255;
  const valid = new Uint8Array(1024);
  for (let p = 0; p < 1024; p++) valid[p] = (m8[p >> 3] >> (p & 7)) & 1;
  return { vec, valid };
}
// the author's bundled labels (DATA.seed) + this user's own corrections
function allLearned() { return (DATA.seed || []).concat(loadLearned()); }
function skipSigs() {
  return allLearned().filter(e => e.base === "__skip__")
    .map(e => ({ rarity: e.rarity, sig: unpackSig(e) }));
}
// inject all positive labels into the matcher so they join the full
// extraction-ensemble matching (same architecture as the desktop version)
function injectLearnedRefs() {
  const entries = allLearned().filter(e => e.base !== "__skip__")
    .map(e => { const s = unpackSig(e); return { vec: s.vec, valid: s.valid, base: e.base }; });
  DATA.matcher.appendRefs(entries);
}
function saveLabel(cellImg, base, rarity = null) {
  const sig = vecFromItem(extractFlood(cellImg));
  const all = loadLearned();
  all.push({ base, rarity, ...packSig(sig) });
  try { localStorage.setItem("tbh_learned", JSON.stringify(all)); } catch (e) {}
  DATA.matcher.appendRefs([{ vec: sig.vec, valid: sig.valid, base }]);   // effective immediately
}

// ---------------- capture (㉓ one-button) ----------------
async function connect() {
  try {
    STREAM = await navigator.mediaDevices.getDisplayMedia({
      video: { displaySurface: "window", frameRate: 5 }, audio: false,
      // privacy hardening for streamers: windows only (no full monitors),
      // never this browser tab, no surface switching mid-session
      monitorTypeSurfaces: "exclude", selfBrowserSurface: "exclude",
      surfaceSwitching: "exclude",
    });
  } catch (e) { setStatus(t("cap_denied")); return; }
  // verify the picked window IS the game (Chrome puts the window title in the
  // track label); if it's clearly another app, drop the stream immediately so
  // nothing else ever gets captured or displayed
  const label = (STREAM.getVideoTracks()[0].label || "").toLowerCase();
  if (label.length > 3 && !label.includes("taskbarhero") && !/^(window|screen|web)[-:\d]*$/.test(label)) {
    STREAM.getTracks().forEach(tr => tr.stop());
    STREAM = null;
    setStatus("⚠ " + t("wrong_window"));
    applyConnState();
    return;
  }
  VIDEO = document.createElement("video");
  VIDEO.srcObject = STREAM;
  VIDEO.muted = true;
  await VIDEO.play();
  // wait until the first frame actually arrives (videoWidth>0); grabbing a
  // 0x0 frame throws IndexSizeError in getImageData
  await new Promise(res => {
    if (VIDEO.videoWidth > 0) return res();
    const t0 = Date.now();
    const tick = () => {
      if (!VIDEO || VIDEO.videoWidth > 0 || Date.now() - t0 > 5000) return res();
      setTimeout(tick, 100);
    };
    VIDEO.onloadeddata = () => res();
    tick();
  });
  STREAM.getVideoTracks()[0].addEventListener("ended", () => {
    STREAM = null; VIDEO = null; applyConnState();
  });
  applyConnState();
  setStatus(t("connected"));
}

function grabFrame() {
  const w = VIDEO.videoWidth, h = VIDEO.videoHeight;
  if (!w || !h) throw new Error("no video frame yet — try again in a second");
  const cv = document.createElement("canvas");
  cv.width = w; cv.height = h;
  const cx = cv.getContext("2d", { willReadFrequently: true });
  cx.drawImage(VIDEO, 0, 0);
  return rgbaToBgr(cx.getImageData(0, 0, w, h));
}
function rgbaToBgr(imgData) {
  const { width: w, height: h, data: rgba } = imgData;
  const out = new Uint8Array(w * h * 3);
  for (let p = 0, i = 0, o = 0; p < w * h; p++, i += 4, o += 3) {
    out[o] = rgba[i + 2]; out[o + 1] = rgba[i + 1]; out[o + 2] = rgba[i];
  }
  return { w, h, data: out };
}

// paste / drop fallback
function fileToImg(fileOrBlob) {
  return new Promise(res => {
    const url = URL.createObjectURL(fileOrBlob);
    const im = new Image();
    im.onload = () => {
      const cv = document.createElement("canvas");
      cv.width = im.naturalWidth; cv.height = im.naturalHeight;
      const cx = cv.getContext("2d", { willReadFrequently: true });
      cx.drawImage(im, 0, 0);
      URL.revokeObjectURL(url);
      res(rgbaToBgr(cx.getImageData(0, 0, cv.width, cv.height)));
    };
    im.src = url;
  });
}

// ---------------- scan ----------------
async function runScan(img) {
  const btn = $("scanBtn");
  btn.disabled = true; btn.textContent = t("scanning");
  setStatus(t("scanning"));
  await new Promise(r => setTimeout(r, 30));     // let the UI paint
  let res;
  try {
    res = await scanImage(img, DATA.tpl, DATA.matcher, DATA.vbb, skipSigs(),
                          (i, n) => { setStatus(`${t("scanning")} ${i}/${n}`); });
  } finally {
    applyConnState();
  }
  if (!res.panel) { renderDiag(img, null); setStatus(t("no_panel")); return; }
  if (!res.items.length) {
    renderDiag(img, res.panel);
    setStatus((t("no_cells") || t("no_panel")) + ` [panel x=${res.panel.x0}-${res.panel.x1} scale=${res.panel.scale} score=${res.panel.score.toFixed(2)}]`);
    return;
  }

  // auto-confirm: learned items (your own repeatedly-labelled gear) get a
  // looser bar since getDisplayMedia inflates their distance; catalog-only
  // matches stay strict to avoid the coin false-positive. CATALOG materials
  // (coins/gems — simple-icon attractors that mis-extracted gear collapses into)
  // get an even STRICTER bar so a borderline hit surfaces as a "?" to review
  // rather than auto-confirming as a coin the player never owned.
  const AUTO_CAT = 0.05, AUTO_LEARN = 0.075, AUTO_MAT = 0.03;
  const autoBar = it => it.learned ? AUTO_LEARN : (it.material ? AUTO_MAT : AUTO_CAT);
  const cells = res.items.map(it => ({
    ...it,
    assigned: (it.status === "ok" && it.hash && it.dist <= autoBar(it)) ? it.hash : null,
    ignored: it.status === "not_tradeable",
    roiImg: null,
  }));
  SCAN = { roi: res.roi, imgW: res.roi.w, imgH: res.roi.h, cells, srcImg: img };
  $("hero").style.display = "none";        // demo gives way to the real thing
  $("guide").removeAttribute("open");      // collapse the tutorial (still re-openable)
  drawScan();
  renderAll();
  const auto = cells.filter(c => c.assigned).length;
  const review = cells.filter(c => !c.assigned && !c.ignored).length;
  setStatus(t("found")(cells.length, auto, review));
}

// diagnostic view: show the captured frame (downscaled) + detected panel area
// so failures are visible instead of silent (user can screenshot & report)
function renderDiag(img, panel) {
  $("scanWrap").style.display = "block";
  $("reviewMsg").textContent = "";
  const cvs = $("scanCanvas");
  const k = Math.min(1, 700 / img.w);
  cvs.width = Math.round(img.w * k); cvs.height = Math.round(img.h * k);
  const im = new ImageData(cvs.width, cvs.height);
  for (let y = 0; y < cvs.height; y++) {
    const sy = Math.min(img.h - 1, Math.round(y / k));
    for (let x = 0; x < cvs.width; x++) {
      const sx = Math.min(img.w - 1, Math.round(x / k));
      const s = (sy * img.w + sx) * 3, d = (y * cvs.width + x) * 4;
      im.data[d] = img.data[s + 2]; im.data[d + 1] = img.data[s + 1];
      im.data[d + 2] = img.data[s]; im.data[d + 3] = 255;
    }
  }
  const cx = cvs.getContext("2d");
  cx.putImageData(im, 0, 0);
  if (panel) {
    cx.strokeStyle = "#ffd60a"; cx.lineWidth = 3;
    cx.strokeRect(panel.x0 * k, 0, (panel.x1 - panel.x0) * k, cvs.height);
    cx.strokeStyle = "#ff6b6b";
    cx.beginPath(); cx.moveTo(panel.x0 * k, panel.title_y * k);
    cx.lineTo(panel.x1 * k, panel.title_y * k); cx.stroke();
  }
  cvs.style.width = Math.min(700, cvs.width) + "px";
  $("scanBox").querySelectorAll(".ov").forEach(e => e.remove());
  // offer the FULL-RES failing frame as a PNG download for bug reports
  const full = document.createElement("canvas");
  full.width = img.w; full.height = img.h;
  const fim = new ImageData(img.w, img.h);
  for (let p = 0; p < img.w * img.h; p++) {
    fim.data[p * 4] = img.data[p * 3 + 2]; fim.data[p * 4 + 1] = img.data[p * 3 + 1];
    fim.data[p * 4 + 2] = img.data[p * 3]; fim.data[p * 4 + 3] = 255;
  }
  full.getContext("2d").putImageData(fim, 0, 0);
  full.toBlob(blob => {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "tbh_debug.png";
    a.textContent = "📥 tbh_debug.png";
    a.style.marginLeft = ".5rem";
    $("reviewMsg").appendChild(a);
  }, "image/png");
}

function drawScan() {
  if (!SCAN) return;
  $("scanWrap").style.display = "block";
  const cvs = $("scanCanvas");
  // draw ROI cropped to the cells + padding
  const cs = SCAN.cells;
  const pad = 8;
  const x0 = Math.max(0, Math.min(...cs.map(c => c.x)) - pad);
  const y0 = Math.max(0, Math.min(...cs.map(c => c.y)) - pad);
  const x1 = Math.min(SCAN.imgW, Math.max(...cs.map(c => c.x + c.w)) + pad);
  const y1 = Math.min(SCAN.imgH, Math.max(...cs.map(c => c.y + c.h)) + pad);
  SCAN.view = { x0, y0, w: x1 - x0, h: y1 - y0 };
  cvs.width = x1 - x0; cvs.height = y1 - y0;
  const im = new ImageData(x1 - x0, y1 - y0);
  const src = SCAN.roi;
  for (let y = 0; y < cvs.height; y++) {
    for (let x = 0; x < cvs.width; x++) {
      const s = ((y + y0) * src.w + x + x0) * 3, d = (y * cvs.width + x) * 4;
      im.data[d] = src.data[s + 2]; im.data[d + 1] = src.data[s + 1];
      im.data[d + 2] = src.data[s]; im.data[d + 3] = 255;
    }
  }
  cvs.getContext("2d").putImageData(im, 0, 0);
  cvs.style.width = Math.min(560, cvs.width * 1.2) + "px";
  drawOverlays();
}

// price-band border colours for confirmed cells (index = band p0..p9)
const BAND_BORDER = ["#3a4150", "#8ddba4", "#7ee787", "#58a6ff", "#bc8cff",
                     "#f778ba", "#ffa657", "#ff6b6b", "#ffd166", "#ffd166"];
// brighter neon variants for the table-row glow backgrounds (派手好き向け)
const BAND_BRIGHT = ["#9aa4b8", "#a9f0bb", "#7dffa0", "#7cc8ff", "#d9a8ff",
                     "#ff9ad4", "#ffc27d", "#ff8d8d", "#ffe27a", "#ffe27a"];

function drawOverlays() {
  const box = $("scanBox");
  box.querySelectorAll(".ov").forEach(e => e.remove());
  const { x0, y0, w, h } = SCAN.view;
  SCAN.cells.forEach((c, i) => {
    const o = document.createElement("div");
    o.className = "ov " + (c.ignored ? "skip" : c.assigned ? "ok" : "review");
    o.style.left = ((c.x - x0) / w * 100) + "%";
    o.style.top = ((c.y - y0) / h * 100) + "%";
    o.style.width = (c.w / w * 100) + "%";
    o.style.height = (c.h / h * 100) + "%";
    o.dataset.hash = c.assigned || "";
    if (c.assigned) {
      const band = +bandClass(unitPrice(c.assigned)).slice(1);
      o.style.setProperty("--bc", BAND_BORDER[band]);
      if (band >= 5) o.classList.add("hot");        // pulsing glow like loot (3k+)
      else if (band >= 2) o.classList.add("glow");
      if (band >= 7) o.innerHTML = '<span class="spark">✨</span>';
    } else if (!c.ignored) {
      o.innerHTML = '<span class="badge">?</span>';
    } else if (c.status === "not_tradeable" && c.base === "?") {
      // unrecognized cell auto-classed as untradeable (e.g. a Common-border
      // material) — show a muted ? so users know it's clickable & rescuable
      o.innerHTML = '<span class="badge" style="background:#6e7681; box-shadow:none; animation:none;">?</span>';
    }
    o.addEventListener("click", ev => openPop(i, ev));
    box.appendChild(o);
  });
  const need = SCAN.cells.filter(c => !c.assigned && !c.ignored).length;
  // the click-to-fix hint now lives prominently in the legend above the canvas;
  // keep the area below the image clear so the layout stays put
  $("reviewMsg").innerHTML = "";
}

function addDebugSave(img) {
  const rm = $("reviewMsg");
  rm.querySelectorAll("a.dbg").forEach(a => a.remove());
  const full = document.createElement("canvas");
  full.width = img.w; full.height = img.h;
  const fim = new ImageData(img.w, img.h);
  for (let p = 0; p < img.w * img.h; p++) {
    fim.data[p * 4] = img.data[p * 3 + 2]; fim.data[p * 4 + 1] = img.data[p * 3 + 1];
    fim.data[p * 4 + 2] = img.data[p * 3]; fim.data[p * 4 + 3] = 255;
  }
  full.getContext("2d").putImageData(fim, 0, 0);
  full.toBlob(blob => {
    const a = document.createElement("a");
    a.className = "dbg"; a.href = URL.createObjectURL(blob);
    a.download = "tbh_scan.png"; a.textContent = " 🐛 この読み取り画像を保存";
    a.style.cssText = "margin-left:.6rem;color:#8b93a7;";
    rm.appendChild(a);
  }, "image/png");
}

// ---------------- aggregation + table (⑲ column order, ⑱ colors) ----------------
function aggregate() {
  const counts = new Map();
  if (!SCAN) return [];
  for (const c of SCAN.cells) {
    if (!c.assigned || c.ignored) continue;
    counts.set(c.assigned, (counts.get(c.assigned) || 0) + 1);
  }
  const rows = [];
  for (const [hash, qty] of counts) {
    const it = DATA.items[hash] || {};
    const unit = unitPrice(hash);
    const net = unit != null ? unit * FEE : null;
    rows.push({
      hash, qty, unit, vol: volume(hash),
      net, total: net != null ? net * qty : null,
      rarity: it.rarity, synth: !!it.synth,
      name: dispName(hash),
    });
  }
  return rows;
}

const RARITY_COLOR = {
  Common: "#6b7280", Uncommon: "#3fb950", Rare: "#2f81f7", Legendary: "#e3a008",
  Immortal: "#f85149", Arcana: "#a371f7", Beyond: "#e055c0",
  Celestial: "#56d4dd", Divine: "#f2cc60", Cosmic: "#c9d1d9",
};

function renderTable() {
  const rows = aggregate();
  const key = SORT.k, dir = SORT.d;
  rows.sort((a, b) => {
    const va = key === "name" ? a.name : a[key], vb = key === "name" ? b.name : b[key];
    if (va == null && vb == null) return 0;
    if (va == null) return 1; if (vb == null) return -1;
    return key === "name" ? dir * String(va).localeCompare(String(vb), LANG) : dir * (va - vb);
  });
  const labels = { name: t("th_item"), qty: t("th_qty"), unit: t("th_unit"),
                   net: t("th_net"), total: t("th_total"),
                   vol: MODE === "base" ? t("th_vol") : t("th_vol_cur") };
  const tips = { unit: t("th_unit_tip"), net: t("th_net_tip"),
                 vol: MODE === "base" ? t("th_vol_tip") : t("th_vol_cur_tip") };
  document.querySelectorAll("th[data-k]").forEach(th => {
    const k = th.dataset.k;
    th.textContent = labels[k] + (SORT.k === k ? (dir > 0 ? " ▲" : " ▼") : "");
    th.title = (tips[k] ? tips[k] + "\n" : "") + t("sort_tip");
    th.classList.toggle("on", SORT.k === k);
  });
  let total = 0;
  const rgba = (hex, a) => {
    const n = parseInt(hex.slice(1), 16);
    return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
  };
  const html = rows.map(r => {
    total += r.total || 0;
    const bc = RARITY_COLOR[r.rarity] || "#30363d";
    let badge = "";
    // "never listed" needs THREE signals: not in the catalog (synth), zero
    // pre-freeze sales, and no current listings — the catalog alone misses
    // items that just happened to be sold out when it was built.
    const bl = DATA.baseline[r.hash];
    const neverListed = r.synth && !(bl && bl[1] > 0) && !DATA.prices?.items?.[r.hash];
    if (neverListed) badge = `<span class="pill never" title="${esc(t("badge_never_tip"))}">${esc(t("badge_never"))}</span>`;
    else if (r.unit == null) badge = `<span class="pill info" title="${esc(t(MODE === "base" ? "badge_nosale_tip" : "badge_noprice"))}">${esc(t(MODE === "base" ? "badge_nosale" : "badge_noprice"))}</span>`;
    // never-listed items have no Steam listing page -> send to a market search
    const href = neverListed ? marketSearchUrl(DATA.items[r.hash]?.base || r.name) : marketUrl(r.hash);
    // luxurious price-tier glow behind the item name (the row's value at a glance)
    const band = +bandClass(r.unit).slice(1);
    const lux = band >= 1
      ? ` style="background:linear-gradient(90deg, transparent, ${rgba(BAND_BRIGHT[band], 0.22)} 40%, ${rgba(BAND_BRIGHT[band], band >= 7 ? 0.48 : 0.34)}); border-radius:.35rem;"`
      : "";
    return `<tr data-hash="${esc(r.hash)}">
      <td class="l"><img class="icon" style="border:2px solid ${bc}" src="${iconUrl(r.hash)}" loading="lazy" alt=""></td>
      <td class="l"${lux}><a class="name" href="${href}" target="_blank" rel="noopener">${esc(r.name)}</a>${badge}
        <br><span class="rar" style="color:${bc}">${esc(r.rarity || "")}</span></td>
      <td>${r.qty}</td>
      <td>${yen(r.unit, "p0")}</td>
      <td>${yen(r.net, "p0")}</td>
      <td>${yen(r.total, "p0")}</td>
      <td>${r.vol == null ? '<span class="muted">—</span>' : r.vol.toLocaleString()}</td>
    </tr>`;
  }).join("");
  $("rows").innerHTML = html || `<tr><td colspan="7" class="muted" style="text-align:center; padding:1.2rem;">${esc(t("step1"))} → ${esc(t("step2"))}</td></tr>`;
  animateTotal(Math.round(total));
  renderTopFind(rows);
}

// satisfying count-up for the warehouse total
function animateTotal(to) {
  const tv = $("totalVal");
  if (!to) { tv.textContent = "—"; tv.className = "p0"; window._lastTotal = 0; return; }
  const from = window._lastTotal || 0;
  window._lastTotal = to;
  tv.className = bandClass(to);
  const t0 = performance.now(), dur = 900;
  const step = now => {
    const k = Math.min(1, (now - t0) / dur);
    tv.textContent = money(Math.round(from + (to - from) * (1 - Math.pow(1 - k, 3))));
    if (k < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

// 💎 loot-reveal moment: spotlight your single most valuable item,
// framed in the colour of its price band
function renderTopFind(rows) {
  const tf = $("topFind");
  const best = rows.filter(r => r.unit != null).sort((a, b) => b.unit - a.unit)[0];
  if (!best || best.unit < 500) { tf.style.display = "none"; return; }
  const band = +bandClass(best.unit).slice(1);
  const bc = BAND_BORDER[band];
  tf.className = "";
  tf.style.display = "flex";
  tf.style.borderColor = bc;
  tf.style.boxShadow = band >= 5 ? `0 0 .45rem ${bc}33` : "none";   // soft, side-monitor friendly
  tf.innerHTML = `<img src="${iconUrl(best.hash)}" alt="">
    <span>💎 <b>${esc(t("top_item"))}</b>: ${esc(best.name)} ×${best.qty}</span>
    <span class="${bandClass(best.unit)}" style="font-size:1.15rem;font-weight:800;margin-left:auto;">
      ${money(best.unit)}</span>`;
}

document.querySelector("thead").addEventListener("click", e => {
  const th = e.target.closest("th[data-k]");
  if (!th) return;
  const k = th.dataset.k;
  SORT = SORT.k === k ? { k, d: -SORT.d } : { k, d: k === "name" ? 1 : -1 };
  localStorage.setItem("tbh_sort", JSON.stringify(SORT));
  renderTable();
});

// ---------------- gacha panel (⑪) ----------------
function renderGacha() {
  const g = DATA.gacha;
  if (!g) return;
  $("gacha").style.display = "block";
  // the "sell" column basis is a button switchable HERE, independent of the main table
  const gcur = GMODE === "cur";
  $("gNote").innerHTML = `${esc(t("gacha_basis"))} `
    + `<button type="button" id="gBasisBtn" class="bchip ${gcur ? "cur" : "base"}" title="${esc(t("gacha_basis_tip"))}">`
    + `${esc(gcur ? t("mode_cur") : tu("mode_base"))} ⇄</button><br>${esc(t("gacha_note"))}`;
  $("gBasisBtn").onclick = () => { GMODE = GMODE === "cur" ? "base" : "cur"; renderGacha(); };
  // use baseline grade EVs always (current per-grade averages aren't in prices.json);
  // coins' sell price follows the displayed basis.
  const gradeEV = g.grade_ev_baseline;
  const rows = [];
  for (const [coin, odds] of Object.entries(g.coins)) {
    const spin = Object.entries(odds).reduce((s, [gr, p]) => s + p / 100 * (gradeEV[gr] || 0), 0) * FEE;
    const sellU = unitPriceIn(coin, GMODE);
    const sell = sellU != null ? sellU * FEE : null;
    rows.push({ coin, spin, sell });
  }
  $("gRows").innerHTML = rows.map(r => {
    const spinWins = r.sell == null || r.spin > r.sell;
    return `<tr data-hash="${esc(r.hash)}">
      <td class="l"><img class="icon" style="width:1.6rem;height:1.6rem;" src="${iconUrl(r.coin)}" loading="lazy" alt="">
        <a class="name" href="${marketUrl(r.coin)}" target="_blank" rel="noopener" style="font-size:.78rem;">${esc(dispName(r.coin))}</a></td>
      <td>${yen(r.spin)}<span class="foot">${esc(t("gacha_per"))}</span></td>
      <td>${r.sell == null ? '<span class="muted">—</span>' : yen(r.sell) + `<span class="foot">${esc(t("gacha_per"))}</span>`}</td>
      <td class="l">${spinWins ? `<span class="verdict-spin">🎰 ${esc(t("gacha_verdict_spin"))}</span>`
                               : `<span class="verdict-sell">💰 ${esc(t("gacha_verdict_sell"))}</span>`}</td>
    </tr>`;
  }).join("");
}

// ---------------- popover ----------------
function openPop(i, ev) {
  POP_I = i;
  const c = SCAN.cells[i];
  // confirmed cell -> show the item's NAME as the title instead of the question
  $("popTitle").textContent = c.assigned ? dispName(c.assigned) : t("pop_title");
  $("popRar").textContent = c.border || "—";
  // thumbnail
  const th = $("popThumb");
  const cellImg = crop(SCAN.roi, c.x, c.y, c.w, c.h);
  const im = new ImageData(c.w, c.h);
  for (let p = 0; p < c.w * c.h; p++) {
    im.data[p * 4] = cellImg.data[p * 3 + 2]; im.data[p * 4 + 1] = cellImg.data[p * 3 + 1];
    im.data[p * 4 + 2] = cellImg.data[p * 3]; im.data[p * 4 + 3] = 255;
  }
  const tcx = th.getContext("2d");
  th.width = c.w; th.height = c.h;
  tcx.putImageData(im, 0, 0);
  // candidates — the #1 (best-match) candidate gets a highlighted frame
  $("popCands").innerHTML = (c.candidates || []).map((cd, k) => {
    const conf = Math.max(0, Math.round((1 - cd.dist / 0.16) * 100));
    const chips = (cd.variants || []).map(v => {
      if (v.rarity === "") return `<span class="chip sug" data-h="${esc(v.hash)}">${esc(t("pop_use"))}</span>`;
      const sug = v.rarity === c.border ? " sug" : "";
      return `<span class="chip${sug}" data-h="${esc(v.hash)}">${esc(v.rarity)}</span>`;
    }).join("");
    return `<div class="cand${k === 0 ? " top" : ""}">
      <img src="${iconUrlByIcon(cd.iconHash || "")}" data-icon="${esc(cd.base)}" alt="">
      <div class="cb"><div class="cbase">${esc(dispBase(cd.base))}</div>
        <div class="cd">${esc(t("pop_match"))} ${conf}%</div>${chips}</div></div>`;
  }).join("") || `<div class="foot">${esc(t("pop_none"))}</div>`;
  // candidate icons: we only know sprite filenames from desktop; use first variant's CDN icon
  $("popCands").querySelectorAll("img[data-icon]").forEach(img => {
    const base = img.dataset.icon;
    const vs = DATA.vbb.get(base);
    if (vs && vs.length) img.src = iconUrl(vs[0].hash);
  });
  $("popSearch").value = ""; $("popRes").innerHTML = "";
  const pop = $("pop");
  pop.style.display = "block";
  let x = ev.clientX + 12, y = ev.clientY + 8;
  if (x + pop.offsetWidth > innerWidth - 8) x = innerWidth - pop.offsetWidth - 8;
  if (y + pop.offsetHeight > innerHeight - 8) y = Math.max(8, innerHeight - pop.offsetHeight - 8);
  pop.style.left = x + "px"; pop.style.top = y + "px";
}

function assign(hash) {
  if (POP_I < 0) return;
  const c = SCAN.cells[POP_I];
  c.assigned = hash; c.ignored = false;
  const cellImg = crop(SCAN.roi, c.x, c.y, c.w, c.h);
  saveLabel(cellImg, DATA.items[hash]?.base || hash);   // ⑭ learn in-browser
  // propagate to identical unassigned cells in this scan
  const sig = vecFromItem(extractFlood(cellImg));
  for (const o of SCAN.cells) {
    if (o === c || o.assigned || o.ignored) continue;
    const oImg = crop(SCAN.roi, o.x, o.y, o.w, o.h);
    const oSig = vecFromItem(extractFlood(oImg));
    let sum = 0, n = 0;
    for (let p = 0; p < 1024; p++) {
      if (!(sig.valid[p] || oSig.valid[p])) continue;
      const j = p * 3;
      for (let k = 0; k < 3; k++) { const d = sig.vec[j + k] - oSig.vec[j + k]; sum += d * d; }
      n++;
    }
    if (n >= 60 && sum / (n * 3) <= 0.05) o.assigned = hash;
  }
  closePop(); drawOverlays(); renderTable();
}

$("pop").addEventListener("click", e => {
  const chip = e.target.closest(".chip[data-h]");
  if (chip) assign(chip.dataset.h);
  const cand = e.target.closest("#popRes .cand[data-h]");
  if (cand) assign(cand.dataset.h);
});
$("popIgnore").addEventListener("click", () => {
  if (POP_I < 0) return;
  const c = SCAN.cells[POP_I];
  c.assigned = null; c.ignored = true;
  const cellImg = crop(SCAN.roi, c.x, c.y, c.w, c.h);
  const sig = vecFromItem(extractFlood(cellImg));
  const all = loadLearned();
  all.push({ base: "__skip__", rarity: c.border, ...packSig(sig) });
  try { localStorage.setItem("tbh_learned", JSON.stringify(all)); } catch (e) {}
  closePop(); drawOverlays(); renderTable();
});
// the correction popup is draggable (grab anywhere except controls)
{
  const pop = $("pop");
  let drag = null;
  pop.addEventListener("mousedown", e => {
    if (e.target.closest("input, button, .chip, .cand, .x, a, canvas")) return;
    drag = { dx: e.clientX - pop.offsetLeft, dy: e.clientY - pop.offsetTop };
    e.preventDefault();
  });
  document.addEventListener("mousemove", e => {
    if (!drag) return;
    pop.style.left = Math.max(0, Math.min(innerWidth - 80, e.clientX - drag.dx)) + "px";
    pop.style.top = Math.max(0, Math.min(innerHeight - 60, e.clientY - drag.dy)) + "px";
  });
  document.addEventListener("mouseup", () => { drag = null; });
}
function closePop() { $("pop").style.display = "none"; POP_I = -1; }
$("popX").addEventListener("click", closePop);
$("popClose").addEventListener("click", closePop);
document.addEventListener("keydown", e => { if (e.key === "Escape") { closePop(); $("fbModal").style.display = "none"; } });
// hidden: Ctrl+Shift+E exports YOUR browser labels (for merging into the
// bundled seed so every user benefits from your corrections)
document.addEventListener("keydown", e => {
  if (!(e.ctrlKey && e.shiftKey && e.key.toLowerCase() === "e")) return;
  const labels = loadLearned();
  if (!labels.length) { setStatus("(no browser labels yet)"); return; }
  const blob = new Blob([JSON.stringify(labels)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "tbh_labels.json";
  a.click();
  setStatus(`labels exported: ${labels.length}`);
});
// hidden debug: Ctrl+Shift+D downloads the last captured frame for bug reports
document.addEventListener("keydown", e => {
  if (!(e.ctrlKey && e.shiftKey && e.key.toLowerCase() === "d")) return;
  const img = SCAN?.srcImg;
  if (!img) return;
  const cv = document.createElement("canvas");
  cv.width = img.w; cv.height = img.h;
  const im = new ImageData(img.w, img.h);
  for (let p = 0; p < img.w * img.h; p++) {
    im.data[p*4] = img.data[p*3+2]; im.data[p*4+1] = img.data[p*3+1];
    im.data[p*4+2] = img.data[p*3]; im.data[p*4+3] = 255;
  }
  cv.getContext("2d").putImageData(im, 0, 0);
  cv.toBlob(b => { const a = document.createElement("a");
    a.href = URL.createObjectURL(b); a.download = "tbh_frame.png"; a.click(); }, "image/png");
});

let searchTimer = null;
$("popSearch").addEventListener("input", e => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    const q = e.target.value.trim().toLowerCase();
    if (!q) { $("popRes").innerHTML = ""; return; }
    const toks = q.split(/\s+/);
    // one card per BASE item with grade chips inside (same as candidates) —
    // not one card per grade, which flooded the list
    const hits = [];
    for (const [base, vs] of DATA.vbb) {
      const ja = (DATA.ja?.bases || {})[base] || DATA.items[vs[0].hash]?.name_ja || "";
      const hay = (base + " " + ja).toLowerCase();
      if (!toks.every(tk => hay.includes(tk))) continue;
      hits.push([base, vs]);
      if (hits.length >= 15) break;
    }
    const border = SCAN?.cells[POP_I]?.border;
    $("popRes").innerHTML = hits.map(([base, vs]) => {
      const chips = (vs.length === 1 && vs[0].rarity === "")
        ? `<span class="chip sug" data-h="${esc(vs[0].hash)}">${esc(t("pop_use"))}</span>`
        : vs.map(v => `<span class="chip${v.rarity === border ? " sug" : ""}" data-h="${esc(v.hash)}">${esc(v.rarity)}</span>`).join("");
      return `<div class="cand">
        <img src="${iconUrl(vs[0].hash)}" loading="lazy" alt="">
        <div class="cb"><div class="cbase">${esc(dispBase(base))}</div>${chips}</div></div>`;
    }).join("");
  }, 200);
});

// ---------------- mode / banner ----------------
function applyMode() {
  $("modeCur").style.background = MODE === "cur" ? "#2ea043" : "#21262d";
  $("modeCur").style.color = MODE === "cur" ? "#fff" : "#8b93a7";
  $("modeBase").style.background = MODE === "base" ? "#5a4a80" : "#21262d";
  $("modeBase").style.color = MODE === "base" ? "#fff" : "#8b93a7";
  const bn = $("banner");
  bn.style.display = "block";
  if (MODE === "base") {
    bn.className = "base";
    bn.innerHTML = esc(tu("banner_base")) + ` <a href="#" id="bnSwap">${esc(t("to_cur"))}</a>`;
  } else {
    bn.className = "cur";
    const ts = DATA?.prices?.t ? new Date(DATA.prices.t).toLocaleString(LANG) : "—";
    bn.innerHTML = esc(tu("banner_cur")(ts)) + ` <a href="#" id="bnSwap">${esc(t("to_base"))}</a>`;
  }
  $("bnSwap").onclick = ev => { ev.preventDefault(); setMode(MODE === "base" ? "cur" : "base"); };
}
function setMode(m) {
  MODE = m;
  localStorage.setItem("tbh_mode", m);
  applyMode(); renderTable(); renderGacha();
  if (SCAN) drawOverlays();        // cell borders are price-band coloured
}
$("modeCur").addEventListener("click", () => setMode("cur"));
$("modeBase").addEventListener("click", () => setMode("base"));

// ---------------- i18n binding ----------------
function applyLang() {
  document.documentElement.lang = LANG;
  const set = (id, key) => { $(id).textContent = t(key); };
  $("tTitle").textContent = t("title");
  set("tTagline", "tagline");
  $("capBtn").textContent = t("capture_btn");
  $("capBtn").title = t("capture_help");
  $("scanBtn").textContent = STREAM ? t("rescan_btn") : t("scan_btn");
  $("modeCur").textContent = t("mode_cur");
  $("modeBase").textContent = tu("mode_base");
  $("modeBase").title = tu("mode_base_tip");
  set("tTotalLabel", "total_label"); set("netNote", "net_note");
  set("gTitle", "gacha_title"); set("gNote", "gacha_note");
  set("ghSpin", "gacha_spin"); set("ghSell", "gacha_sell");
  set("guideTitle", "steps_title");
  $("gd1").innerHTML = t("step1");
  $("gd2").innerHTML = t("step2");                          // step2/3 carry colored <span class='btnref'>
  $("gd2").querySelector(".btnref")?.classList.add("bconn"); // connect = accent gradient; appraise stays blue
  $("gd3").innerHTML = t("step3").replace(/\[\?\]/g, QBADGE);
  set("guideNote", "verify_note");
  $("dmNote").textContent = t("dm_note");
  $("tPriceRefresh").textContent = t("price_refresh");
  set("tPrivacy", "privacy"); set("tUnofficial", "unofficial");
  set("popTitle", "pop_title"); set("popRarLbl", "pop_rar");
  set("learnNote", "learn_note");
  $("popSearch").placeholder = t("pop_search");
  set("popIgnore", "pop_ignore"); set("popClose", "pop_close");
  set("fbTitle", "fb_title"); set("fbSend", "fb_send"); set("fbCancel", "fb_cancel");
  $("fbText").placeholder = t("fb_placeholder");
  $("legend").innerHTML = `<span class="hint">${withQ("review_hint")}</span>`;
  $("heroCap").textContent = t("hero_cap");
  if (DATA) { applyMode(); renderTable(); renderGacha(); }
}
const sel = $("langSel");
for (const [code, label] of LANGS) {
  const o = document.createElement("option");
  o.value = code; o.textContent = label;
  sel.appendChild(o);
}
sel.value = LANG;
sel.addEventListener("change", () => {
  LANG = sel.value;
  localStorage.setItem("tbh_lang", LANG);
  applyLang();
});

// ---------------- feedback (⑦ private to developer) ----------------
// One-click sending: POST to FEEDBACK_ENDPOINT when configured (e.g. a free
// Formspree form URL); otherwise falls back to a mailto link.
const FEEDBACK_ENDPOINT = "";   // ←公開前に Formspree のURLを入れると完全ワンクリック送信になる
$("fbCancel").addEventListener("click", () => { $("fbModal").style.display = "none"; });
$("fbSend").addEventListener("click", async () => {
  const body = $("fbText").value.trim();
  if (!body) return;
  if (FEEDBACK_ENDPOINT) {
    try {
      await fetch(FEEDBACK_ENDPOINT, {
        method: "POST",
        headers: { "Content-Type": "application/json", "Accept": "application/json" },
        body: JSON.stringify({ message: body, lang: LANG, ua: navigator.userAgent }),
      });
    } catch (e) {}
  } else {
    location.href = `mailto:${FEEDBACK_TO}?subject=${encodeURIComponent("[TBH査定] feedback")}&body=${encodeURIComponent(body)}`;
  }
  // thanks INSIDE the modal, then it closes itself
  $("fbText").value = "";
  const msg = $("fbMsg");
  msg.textContent = "✅ " + t("fb_thanks");
  msg.style.display = "block";
  setTimeout(() => { $("fbModal").style.display = "none"; }, 1600);
});

// ---------------- wiring ----------------
function setStatus(s) { $("status").textContent = s; }
function applyConnState() {
  const cap = $("capBtn"), scan = $("scanBtn");
  scan.disabled = !STREAM;
  scan.textContent = STREAM ? (SCAN ? t("rescan_btn") : t("scan_btn")) : t("scan_btn");
  cap.textContent = STREAM ? "✅ " + t("connected").split("—")[0].trim() : t("capture_btn");
  // fixed button colours: connect = green, appraise = blue (matches the step chips)
  cap.classList.add("connected"); cap.classList.remove("accent");
  scan.classList.remove("accent");
  // nudge the user through the steps: once connected (and not yet scanned),
  // light up the guide card and flag step 2 ("open the warehouse, then appraise")
  const guide = $("guide");
  if (guide) {
    guide.classList.toggle("connected", !!STREAM && !SCAN);
  }
}
// hover a table row -> spotlight that item's cells in the warehouse image
function hlCells(hash, on) {
  document.querySelectorAll(".ov").forEach(o => {
    o.classList.toggle("hl", on && !!hash && o.dataset.hash === hash);
    o.classList.toggle("dim", on && !!hash && o.dataset.hash !== hash);
  });
}
$("rows").addEventListener("mouseover", e => {
  const tr = e.target.closest("tr[data-hash]");
  if (tr) hlCells(tr.dataset.hash, true);
});
$("rows").addEventListener("mouseout", () => hlCells(null, false));

$("capBtn").addEventListener("click", connect);
$("scanBtn").addEventListener("click", async () => {
  if (!VIDEO) return;
  try { await runScan(grabFrame()); }
  catch (e) { setStatus("⚠ " + (e.message || e)); applyConnState(); console.error(e); }
});

// surface any unexpected error in the status line so users can report it
window.addEventListener("error", e => setStatus("⚠ " + e.message));
window.addEventListener("unhandledrejection", e =>
  setStatus("⚠ " + (e.reason?.message || e.reason)));

// paste stays as a hidden fallback (no visible dropzone)
document.addEventListener("paste", async e => {
  for (const item of e.clipboardData.items) {
    if (item.type.startsWith("image/")) { runScan(await fileToImg(item.getAsFile())); break; }
  }
});

function renderAll() { renderTable(); renderGacha(); }

// ---------------- boot ----------------
applyLang();
setStatus("…");
// demo hero shows only if assets/demo.png exists (and until the first scan).
// NOTE: the image may have finished loading BEFORE this code runs, in which
// case onload never fires — check .complete as well.
{
  const hi = $("heroImg");
  const show = () => { if (!SCAN) $("hero").style.display = "block"; };
  hi.onload = show;
  hi.onerror = () => { $("hero").style.display = "none"; };
  if (hi.complete && hi.naturalWidth > 0) show();
}
// debug hook: lets a test harness drive a scan with a raw BGR image
window.__runScan = img => runScan(img);
loadData().then(() => { injectLearnedRefs(); applyLang(); setStatus(t("not_connected")); })
  .catch(e => setStatus("data load error: " + e));
