// TBH 倉庫まるごと査定 — main app logic (static site, no backend).
// Screenshots are processed entirely in this browser; nothing is uploaded.

import { Matcher, _internal } from "./recognize.js?v20260626i";
import { scanImage, variantsByBase } from "./pipeline.js?v20260626i";
import { detectPageTab } from "./detect.js?v20260626i";
import { putPage, deletePage, clearPages, loadPages, dbAvailable } from "./store.js?v20260626i";
import { T, LANGS, pickLang } from "./i18n.js?v20260626i";
const { vecFromItem, extractFlood, crop, resizeArea } = _internal;

const $ = id => document.getElementById(id);
// Steam fee model: the buyer pays P, the seller receives P/1.15 (5% Steam +
// 10% game fee are added ON TOP of the seller's ask) — ≈86.96%, not 85%.
const FEE = 1 / 1.15;
const FEEDBACK_TO = "takahasi599@gmail.com";   // ⑦ goes only to the developer

// ---------------- changelog (⑳ page bottom; newest first) ----------------
const APP_VERSION = "1.7.2";
const CHANGELOG = [
  { v: "1.7.2", d: "2026/6/27",
    ja: "出品プランのアイテムにカーソルを合わせると、左の倉庫画像が自動でそのページに切り替わり、該当アイテムをハイライトするようにしました。複数ページでも在庫の場所が一目で分かります。",
    en: "Hovering an item in the listing plan now switches the left warehouse image to the page that holds it and spotlights that item — so you can see where stock sits across multiple pages at a glance." },
  { v: "1.7.1", d: "2026/6/26",
    ja: "動作を軽量化：サムネイルをキャッシュして起動を高速化し、マイ倉庫の復元を初期表示の後に回すようにしました。出品プランは見ている時だけ再計算します。既にマイ倉庫がある場合はチュートリアル画像を省略。",
    en: "Lighter & faster: thumbnails are cached for a quicker start, My Warehouse restores after the first paint, and the listing plan only recomputes while you’re viewing it. The tutorial image is skipped once you have a saved warehouse." },
  { v: "1.7.0", d: "2026/6/26",
    ja: "複数ページを「マイ倉庫」として自動保存。ブラウザを再起動しても復元されます（端末内のみ・外部送信なし）。ページ番号は自動検出し、数字タブで切り替えできます。",
    en: "Multi-page “My Warehouse”: pages auto-save and are restored even after a browser restart (on-device only, never uploaded). Page numbers are auto-detected; switch pages with the number tabs." },
  { v: "1.6.27", d: "2026/6/26",
    ja: "価格表示の不具合を修正：再開後に暴落した銘柄（オパール等）で、まだ更新されていない古い中央値がそのまま高値で表示され続ける問題を解消しました。深い板の現在最安値が中央値より大幅に下なら実勢を反映します。",
    en: "Fixed a price-display bug: items that crashed after reopening (e.g. Opal) could keep showing a stale, not-yet-updated high median. When a deep live market sits far below that median, the tool now reflects the live price." },
  { v: "1.6.26", d: "2026/6/25",
    ja: "価格精度を改善：中央値の取得が遅れている銘柄で、古い中央値ではなく毎回更新される実勢（深い板の最安値）を使うようにしました。過大・過小どちらのズレも縮みます。",
    en: "Better price accuracy: for items whose median is lagging, the tool now uses the always-fresh live market (deep-market lowest ask) instead of a stale last-median — cutting both over- and under-valuation." },
  { v: "1.6.25", d: "2026/6/25",
    ja: "出品プランを修正：再開直後は在庫の山積みで回転率が当てにならず全部「売れにくい」になるため、手取りの高い順に並べるようにしました（安い量産品より高額品を優先）。",
    en: "Fixed the listing plan: right after reopening, huge backlogs make turnover unreliable (everything reads 'slow'), so items are now ranked by net take-home — high-value first, not cheap commodities." },
  { v: "1.6.23", d: "2026/6/25",
    ja: "「価格を更新」ボタンを追加（記念コインと査定結果の近く）。再スキャンせずに最新の価格・期待値へ更新できます。",
    en: "Added a 'Refresh prices' button (by the coins and your scan results) — pull the latest prices/EV without re-scanning." },
  { v: "1.6.22", d: "2026/6/25",
    ja: "再開直後の価格表示を修正：取引が薄く古い中央値しか無い銘柄が高値のまま固まる問題を解消し、実勢（深い板の現在最安値）を反映するようにしました。",
    en: "Fixed reopening prices: items stuck on a stale pre-freeze median now reflect the live market (a deep lowest-ask far below it)." },
  { v: "1.6.16", d: "2026/6/16",
    ja: "みんなの修正データを反映し、約37種類のアイテムの自動認識を追加・改善しました（認識精度アップ）。",
    en: "Folded in more of everyone's fixes: auto-recognition added or improved for ~37 item types — better accuracy." },
  { v: "1.6.9", d: "2026/6/15",
    ja: "みんなの修正データを反映し、約30種類のアイテムの自動認識を追加・改善しました（認識精度アップ）。",
    en: "Folded in more of everyone's fixes: auto-recognition added or improved for ~30 item types — better accuracy." },
  { v: "1.6.4", d: "2026/6/14",
    ja: "未認識のアイテムが「取引不可」としてグレーで隠れてしまう不具合を修正（トーメントのソウルストーン等）。識別できない品は黄色の「?」で表示され、クリックで修正できます。",
    en: "Fixed unidentified items being greyed out as “untradeable” and hidden (e.g. Torment soulstones). Unknown items now show a yellow “?” for review — click to fix." },
  { v: "1.6.1", d: "2026/6/13",
    ja: "出品プランを追加（どれを・いくらで・いくつ出せば一番稼げるか自動提案）。記念コインをストア価格に合わせ、ドロップ率を更新。売り規制中は売買判定を保留し、再開後に自動表示。",
    en: "Added a listing planner (what to list, at what price, and how many for the best yield). Coin prices aligned to the store with updated drop rates; the sell/keep verdict is paused during the freeze and shows automatically after reopening." },
  { v: "1.6.0", d: "2026/6/13",
    ja: "認識精度と使いやすさを大幅改善。価格の色分け、複数ページのストック査定、最安値の併記など。みんなのアイテム修正が積み重なって、全ユーザーの認識精度が上がっていく仕組みも追加。",
    en: "Big accuracy & usability upgrade: colour-coded prices, multi-page stock appraisal, lowest-ask display, and recognition that keeps improving for all users as everyone’s item fixes add up." },
  { v: "1.5.0", d: "2026/6/12",
    ja: "価格の24時間トレンド表示、🔒ロック中アイテムの認識、手取り計算の精密化、記念コイン期待値の現在価格対応。",
    en: "24h price trends, recognition of locked (🔒) items, more precise net proceeds, and current-price coin spin EV." },
  { v: "1.4.0", d: "2026/6/12",
    ja: "アイテムが少ない・まばらな倉庫の認識漏れを解消（7×7の升目を前提に検出するよう改良）。更新履歴を追加",
    en: "Sparse warehouses no longer drop items (detection now assumes the 7×7 grid). Added this changelog" },
  { v: "1.3.0", d: "2026/6/12",
    ja: "倉庫画像の見切れを自動復元。🔒ロック中アイテムは精度が落ちる旨の注意を追加",
    en: "Clipped warehouse captures auto-recover. Added a note that locked (🔒) items reduce accuracy" },
  { v: "1.2.0", d: "2026/6/12",
    ja: "「倉庫を査定する」のたびに最新価格を自動取得（価格は約10分ごとに更新されています）",
    en: "Every appraisal now fetches the latest prices (refreshed ~every 10 minutes)" },
  { v: "1.1.0", d: "2026/6/12",
    ja: "マーケット再開を自動検知して表示を切替。再開後の出品制限（1人4枠・各枠8時間）の注記を追加",
    en: "Auto-detects the market reopening; notes the post-reopen listing limit (4 slots / 8h each)" },
  { v: "1.0.0", d: "2026/6/12",
    ja: "公開 🎉",
    en: "Initial release 🎉" },
];
// GoatCounter custom events (page views are counted by the script tag in
// index.html; these add "actually connected / actually appraised" counts).
// No-op when the counter script is blocked or absent.
function gcEvent(name) {
  try { window.goatcounter?.count?.({ path: name, title: name, event: true }); } catch (e) {}
}

function renderChangelog() {
  $("chVer").textContent = "v" + APP_VERSION;
  $("chList").innerHTML = CHANGELOG.map(e =>
    `<div class="che"><b>v${e.v}</b><span class="d">${e.d}</span><span>${esc(LANG === "ja" ? e.ja : e.en)}</span></div>`
  ).join("");
}
// ㉖ about / selling points (localized via i18n arrays)
function renderAbout() {
  $("aboutTitle").textContent = t("about_title");
  $("aboutFeatures").innerHTML = (t("about_features") || []).map(f =>
    `<div class="feat"><b>${esc(f[0])}</b> — ${esc(f[1])}</div>`).join("");
}

// ---------------- state ----------------
let LANG = pickLang();
// The market-reopen flag comes from prices.json ("unlocked"), set by the price
// bot when it detects new listings being allowed again (6/15 is only the server
// migration; reopening is announced separately). Until data loads: locked.
let UNLOCKED = false;
let MODE = localStorage.getItem("tbh_mode") || "cur";   // 'base' | 'cur'
let GMODE = MODE;   // gacha "sell" basis — switchable INDEPENDENTLY of the main table
let DATA = null;        // {items, vbb, matcher, tpl, baseline, gacha, meta, prices}
let STREAM = null, VIDEO = null;
let SCAN = null;        // {imgW,imgH, cells:[{...item, assigned, ignored}]}
let _shownScan = null;  // which scan is actually painted in #scanBox — equals SCAN
                        // normally, or a stocked page during a listing-plan hover-preview
let SORT = JSON.parse(localStorage.getItem("tbh_sort") || '{"k":"total","d":-1}');
let POP_I = -1;
let STOCKS = [];           // ㉔ stocked pages [{scan,url,pageNo}] — scan is the FULL
                           // mutable scan so a click reloads it as the editable image;
                           // pageNo is the warehouse tab 1-7 (null if unknown)
let TABLE_SRC = "scan";    // item table source: "scan" (current) | "stock" (all pages)
let VIEW = "appraisal";    // right column: "appraisal" (total + item table + coins)
                           // | "plan" (listing plan), toggled by the 出品プラン button
                           // so a big 7-page warehouse needn't be scrolled past
let CURPAGE = null;        // page (1-7|null) chosen for the CURRENT scan — drives the
                           // page selector + whether stocking adds or overwrites

const t = k => (T[LANG] && T[LANG][k] !== undefined) ? T[LANG][k] : T.en[k];
const tu = k => t(UNLOCKED ? k + "_post" : k);   // post-sell-unlock variant of a label/banner
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
// inline ? badge (matches the yellow "?" drawn on unconfirmed cells); i18n
// strings carry a literal [?] placeholder where the badge should appear.
const QBADGE = '<span class="qmark">?</span>';
const withQ = key => esc(t(key)).replace(/\[\?\]/g, QBADGE);

// ---------------- data loading ----------------
async function loadData() {
  // ?v=version on data files: when a release updates the bundled data (e.g.
  // a new learned_seed.json), returning visitors were stuck on the browser/CDN
  // cached copy — pin the cache key to APP_VERSION so each release refetches.
  const j = async f => (await fetch("data/" + f + "?v=" + APP_VERSION)).json();
  const b = async f => (await fetch("data/" + f + "?v=" + APP_VERSION)).arrayBuffer();
  const [meta, items, refsMeta, refsBuf, tplMeta, tplBuf, baseline, gacha, ja] =
    await Promise.all([j("meta.json"), j("items.json"), j("refs.json"), b("refs.bin"),
                       j("tpl.json"), b("tpl.bin"), j("baseline.json"), j("gacha.json"),
                       j("ja_names.json")]);
  let prices = null;
  // prices.json updates every ~10 min independently of releases -> bust by time
  try { prices = await (await fetch("data/prices.json?t=" + Date.now(), { cache: "no-store" })).json(); } catch (e) {}
  UNLOCKED = !!prices?.unlocked;   // drives the *_post copy; default mode is always 現在価格
  let hist = null;   // also bot-updated every ~10 min -> time-bust, not version
  try { hist = await (await fetch("data/history.json?t=" + Date.now(), { cache: "no-store" })).json(); } catch (e) {}
  let seed = [];
  try { seed = await j("learned_seed.json"); } catch (e) {}   // author's pre-trained labels
  DATA = {
    meta, items, baseline: baseline.items, gacha, prices, ja, seed,
    hist: hist?.items || null,
    matcher: new Matcher(refsBuf, refsMeta),
    vbb: variantsByBase(items),
    tpl: { w: tplMeta.w, h: tplMeta.h, data: new Uint8Array(tplBuf) },
  };
}

// re-fetch the price snapshot (~13 KB) so each appraisal uses fresh data even
// if the tab has been open for hours (the bot refreshes it every ~10 min).
// The unique query string skips both browser and CDN caches; on failure we
// silently keep the copy already in memory.
// "Refresh prices" button: pull the latest snapshot and re-render (table/coin/
// plan) WITHOUT re-scanning. applyMode() also re-stamps the banner time/unlock.
// count-up a single DISPLAYED number (already in the user's currency) from 0 to
// its value, preserving the symbol/decimals. Animates the shown number only, so
// no double fx-conversion (re-calling money() would convert again). Exact at end.
function countUpNum(el, dur = 600) {
  const txt = el.textContent;
  const m = txt.match(/-?[\d,]+(\.\d+)?/);
  if (!m) return;
  const target = parseFloat(m[0].replace(/,/g, ""));
  if (!isFinite(target) || target < 1) return;
  const pre = txt.slice(0, m.index), suf = txt.slice(m.index + m[0].length);
  const dec = m[1] ? m[1].length - 1 : 0;
  const fmt = n => pre + n.toLocaleString(undefined, { minimumFractionDigits: dec, maximumFractionDigits: dec }) + suf;
  const t0 = performance.now();
  const step = now => {
    const k = Math.min(1, (now - t0) / dur);
    el.textContent = fmt(target * (1 - Math.pow(1 - k, 3)));
    if (k < 1) requestAnimationFrame(step); else el.textContent = txt;
  };
  requestAnimationFrame(step);
}
// テレレレ: count every price / coin-EV cell up from 0 (table + coin panel)
function animatePriceCells() {
  document.querySelectorAll("#rows td.num1 > span:first-child, #rows td > span.num, #gRows td > span:first-child")
    .forEach(el => countUpNum(el));
}
async function refreshPricesAndRender(btn) {
  const lbl = btn ? btn.textContent : null;
  if (btn) { btn.disabled = true; btn.textContent = (lbl || "") + " …"; }
  try { await refreshPrices(); applyMode(); window._lastTotal = 0; renderAll(); animatePriceCells(); }
  finally { if (btn) { btn.disabled = false; btn.textContent = lbl; } }
}
async function refreshPrices() {
  try {
    const r = await fetch("data/prices.json?t=" + Date.now(), { cache: "no-store" });
    if (!r.ok) return;
    DATA.prices = await r.json();
    UNLOCKED = !!DATA.prices?.unlocked;
  } catch (e) {}
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
// Realistic per-unit value. Normally the median of real sales (p.m) or the last
// known median (p.lm). Two market distortions are corrected via the live ask (p.p):
//  - sell-freeze: a lone INFLATED ask sits ABOVE the real median -> trust the median.
//  - reopen CRASH: the median is STALE-HIGH while a DEEP ask market (q>=STALE_Q) sits
//    far below it (ask < ref*STALE_FACTOR) -> the market moved down, use the live ask.
const STALE_FACTOR = 0.5;   // ask below this fraction of the median ref = crashed market
const STALE_Q = 10;         // ...this many listings deep = real undercutting, not a lone lowball
function realUnit(p) {
  if (!p) return null;
  // reopen-CRASH override (must run even when a fresh median EXISTS): the per-item
  // median (p.m) is slow/flaky, so right after a crash it can stay STALE-HIGH while a
  // DEEP ask market (q>=STALE_Q) already sits far below it (ask < ref*STALE_FACTOR).
  // That gap = the market moved DOWN; the median just hasn't reprinted -> use the live ask.
  // (Restores v1.6.22; the v1.6.26 rewrite dropped this by returning p.m unconditionally.)
  const ref = p.m ?? p.lm;
  if (ref != null && p.p != null && (p.q || 0) >= STALE_Q && p.p < ref * STALE_FACTOR) return p.p;
  if (p.m != null) return p.m;                          // a fresh real-sale median = the best value
  // no fresh median -> lm is the LAST median, which the slow/flaky per-item fetch leaves
  // stale in EITHER direction (crashed OR recovered). The ask + listings refresh every run
  // (cheap sweep), so a DEEP ask market (q>=STALE_Q) is the always-fresh live floor -> trust
  // it over the stale lm. (Thin market: keep lm; a lone ask is unreliable.)
  if (p.p != null && (p.q || 0) >= STALE_Q) return p.p;
  return p.lm ?? p.p ?? null;
}
function unitPriceIn(hash, mode) {  // 1個の価格 in a SPECIFIC basis (JPY)
  if (mode === "base") {
    const b = DATA.baseline[hash];
    return b && b[0] != null ? b[0] : null;
  }
  const p = DATA.prices?.items?.[hash];
  if (!p) return null;
  // median of real sales (p.m) is the true value; if Steam has no recent sales
  // (during the sell-freeze it returns only a lone, inflated lowest ask) use the
  // last KNOWN median (p.lm) instead of that ask; ask (p.p) only as last resort.
  return realUnit(p);
}
function unitPrice(hash) { return unitPriceIn(hash, MODE); }   // main table -> global toggle

// 24h price trend from history.json: current price vs the recorded point
// nearest 24h ago (needs one within ±6h of that mark). Only meaningful on
// the 現在価格 basis — the baseline is a frozen snapshot.
function trend24(hash) {
  const pts = DATA.hist?.[hash];
  const cur = unitPriceIn(hash, "cur");
  if (!pts || !pts.length || cur == null) return null;
  const tgt = Date.now() / 3.6e6 - 24;       // epoch-hours, 24h back
  let ref = null, bd = Infinity;
  for (const [hh, p] of pts) {
    const d = Math.abs(hh - tgt);
    if (d < bd) { bd = d; ref = p; }
  }
  if (ref == null || ref <= 0 || bd > 6) return null;
  return (cur - ref) / ref;
}
function trendChip(hash) {
  if (MODE !== "cur") return "";
  const r = trend24(hash);
  if (r == null) return "";
  const pct = r * 100;
  const cls = pct >= 2 ? "up" : pct <= -2 ? "down" : "flat";
  const arrow = pct >= 2 ? "↗" : pct <= -2 ? "↘" : "→";
  const txt = `${arrow} ${pct >= 0 ? "+" : ""}${Math.abs(pct) >= 10 ? Math.round(pct) : pct.toFixed(1)}%`;
  return `<span class="trend ${cls}" title="${esc(t("trend_tip"))}">${txt}</span>`;
}
// hybrid display: the main price is the median of real sales (p.m); when a
// distinct current lowest ask exists, show it small underneath so it matches
// the store page's "starting at" and tells you the list-under-this price.
function lowestAskNote(hash) {
  if (MODE !== "cur") return "";
  const p = DATA.prices?.items?.[hash];
  if (!p || p.p == null) return "";
  const shown = realUnit(p);                // displayed value (incl. reopen stale-median override)
  if (shown == null) return "";             // we're already showing the ask itself
  if (Math.abs(p.p - shown) < Math.max(1, shown * 0.02)) return "";   // ~equal: don't clutter
  return `<span class="ask" title="${esc(t("price_low_tip"))}">${esc(t("price_low"))} ${money(p.p)}</span>`;
}
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
  if (v >= 50000) return "p9";    // ¥50k+ = rare special tier (glowing pill)
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
function saveLabel(cellImg, base, rarity = null, meta = {}) {
  // store the signature at a few resampled scales so a label made at one
  // in-game window scale still matches captures taken at a different scale
  // (otherwise changing the scale made confirmed cells go back to "?").
  const all = loadLearned();
  const sigs = [];
  for (const k of [1, 0.72, 1.4]) {
    const src = k === 1 ? cellImg
      : resizeArea(cellImg, Math.max(8, Math.round(cellImg.w * k)), Math.max(8, Math.round(cellImg.h * k)));
    const sig = vecFromItem(extractFlood(src));
    sigs.push(sig);
    all.push({ base, rarity, ...packSig(sig) });
  }
  try { localStorage.setItem("tbh_learned", JSON.stringify(all)); } catch (e) {}
  DATA.matcher.appendRefs(sigs.map(sig => ({ vec: sig.vec, valid: sig.valid, base })));   // effective immediately
  uploadLabel(base, rarity, sigs[0], meta);   // ㉕ crowd-collect (gated before any reuse)
}

// ㉕ Crowd-sourced labels -> Supabase (insert-only via the public anon key).
// Sends the user-verified (signature -> item) pair, NEVER an image. Collected
// rows are a candidate pool only: promotion into the shipped data is gated
// (consensus + rarity cross-check + regression test), so a wrong/malicious
// label here cannot affect anyone else. Fire-and-forget; failures are ignored.
const CROWD_URL = "https://ebtaabxbfracykncjhfc.supabase.co/rest/v1/labels";
const CROWD_KEY = "sb_publishable_Y3JKuEbN5OuUeiNM2GSG_A__AMHHR-j";
function uploadLabel(base, rarity, sig, meta = {}) {
  try {
    const packed = packSig(sig);                       // {v, m} base64
    const body = {
      base, rarity: rarity || null, border: meta.border || null,
      sig: JSON.stringify(packed), dist: meta.dist ?? null,
      lang: LANG, ver: APP_VERSION,
    };
    fetch(CROWD_URL, {
      method: "POST", keepalive: true,
      headers: { "Content-Type": "application/json", apikey: CROWD_KEY,
                 Authorization: "Bearer " + CROWD_KEY, Prefer: "return=minimal" },
      body: JSON.stringify(body),
    }).catch(() => {});
  } catch (e) {}
}

// ---------------- capture (㉓ one-button) ----------------
async function connect() {
  setStatus(t("connect_pick"));   // shown behind the browser's picker dialog
  try {
    STREAM = await navigator.mediaDevices.getDisplayMedia({
      // request the source's PHYSICAL resolution: high-DPI windows otherwise
      // capture at the downscaled logical size (≈half the per-cell detail). The
      // browser caps at the real surface size, so over-asking is harmless and
      // sharpens the 32x32 cell crop where it can (better recognition input).
      video: { displaySurface: "window", frameRate: 5,
               width: { ideal: 3840 }, height: { ideal: 2160 } }, audio: false,
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
  gcEvent("connect");
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
    // Gray out only RECOGNIZED untradeable gear (base!="?"). An UNMATCHED cell
    // auto-classed not_tradeable purely on a low-grade BORDER hue (base="?") is
    // often a misread material (soulstones/gems read as Uncommon/Rare) — keep it
    // as a yellow "?" review so it isn't hidden AND feeds the crowd-label loop.
    ignored: it.status === "not_tradeable" && it.base !== "?",
    roiImg: null,
  }));
  SCAN = { roi: res.roi, imgW: res.roi.w, imgH: res.roi.h, cells, srcImg: img };
  // best-effort warehouse page (1-7) from the gold tab.
  let guess = null;
  try {
    const pg = detectPageTab(res.roi, res.items, res.panel.scale, res.panel.title_y);
    console.log("[pageTab] scores", pg.scores, "-> guess", pg.pageNo, "confident", pg.confident);
    guess = pg.pageNo;
  } catch (e) { console.warn("pageTab detect failed", e); }
  SCAN.pageGuess = guess;
  CURPAGE = guess;
  $("hero").style.display = "none";        // demo gives way to the real thing
  $("guide").removeAttribute("open");      // collapse the tutorial (still re-openable)
  drawScan();
  renderAll();
  const auto = cells.filter(c => c.assigned).length;
  const review = cells.filter(c => !c.assigned && !c.ignored).length;
  setStatus(t("found")(cells.length, auto, review));
  // ㉕ auto-save to My Warehouse when the page tab was read confidently — a
  // re-scan of page N then transparently updates the saved page N. If the page
  // is uncertain, leave it for the user to pick (the picker is shown); they can
  // correct a wrong guess with one click, or hit "?" to drop it.
  if (guess != null) saveActive(guess);
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
  cvs.style.width = "";   // fill the fixed-width #scanBox (avoid clipping)
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

// draws a scan's warehouse image + cell overlays into #scanBox. Defaults to the
// live editable SCAN; the listing-plan hover-preview passes another page's scan
// with interactive=false (read-only overlays) so peeking can't mis-target edits.
function drawScan(scan = SCAN, interactive = true) {
  if (!scan) return;
  $("scanWrap").style.display = "block";
  const cvs = $("scanCanvas");
  // draw ROI cropped to the cells + padding
  const cs = scan.cells;
  const pad = 8;
  const x0 = Math.max(0, Math.min(...cs.map(c => c.x)) - pad);
  const y0 = Math.max(0, Math.min(...cs.map(c => c.y)) - pad);
  const x1 = Math.min(scan.imgW, Math.max(...cs.map(c => c.x + c.w)) + pad);
  const y1 = Math.min(scan.imgH, Math.max(...cs.map(c => c.y + c.h)) + pad);
  scan.view = { x0, y0, w: x1 - x0, h: y1 - y0 };
  cvs.width = x1 - x0; cvs.height = y1 - y0;
  const im = new ImageData(x1 - x0, y1 - y0);
  const src = scan.roi;
  for (let y = 0; y < cvs.height; y++) {
    for (let x = 0; x < cvs.width; x++) {
      const s = ((y + y0) * src.w + x + x0) * 3, d = (y * cvs.width + x) * 4;
      im.data[d] = src.data[s + 2]; im.data[d + 1] = src.data[s + 1];
      im.data[d + 2] = src.data[s]; im.data[d + 3] = 255;
    }
  }
  cvs.getContext("2d").putImageData(im, 0, 0);
  // display size is fixed by #scanBox CSS (560px) and the canvas fills it via
  // width:100%, so the on-screen size no longer follows the capture's scale
  cvs.style.width = "";
  _shownScan = scan;
  drawOverlays(scan, interactive);
}

// price-band border colours for confirmed cells (index = band p0..p9)
const BAND_BORDER = ["#4a515c", "#e6e9ef", "#7cb4ff", "#9cc8ff", "#ffff64",
                     "#ffe14a", "#3dd463", "#ff8000", "#ffd700", "#ff4040"];
// brighter neon variants for the table-row glow backgrounds (派手好き向け)
// Diablo tier ramp (matches .p0-.p9): gray/white/blue/blue/yellow/yellow/
// green/ORANGE/GOLD/RED — cells and row glows use the same instinct colours
const BAND_BRIGHT = ["#6e7681", "#e6e9ef", "#7cb4ff", "#9cc8ff", "#ffff64",
                     "#ffe14a", "#3dd463", "#ff8000", "#ffd700", "#ff4040"];

function drawOverlays(scan = SCAN, interactive = true) {
  const box = $("scanBox");
  box.querySelectorAll(".ov").forEach(e => e.remove());
  const { x0, y0, w, h } = scan.view;
  scan.cells.forEach((c, i) => {
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
      // reached only when the USER manually ignored an unidentified cell (scan
      // no longer auto-greys base="?" cells — see the `ignored` rule above) —
      // show a muted ? so they know it's still clickable & rescuable
      o.innerHTML = '<span class="badge" style="background:#6e7681; box-shadow:none; animation:none;">?</span>';
    }
    // read-only preview (plan hover) draws another page: don't wire edits to it,
    // and let clicks fall through so nothing targets the wrong page's cell.
    if (interactive) o.addEventListener("click", ev => openPop(i, ev));
    else o.style.pointerEvents = "none";
    box.appendChild(o);
  });
  if (!interactive) return;            // preview must not touch the live page's UI
  const need = scan.cells.filter(c => !c.assigned && !c.ignored).length;
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
function scanCounts(scan) {
  const m = new Map();
  if (scan) for (const c of scan.cells)
    if (c.assigned && !c.ignored) m.set(c.assigned, (m.get(c.assigned) || 0) + 1);
  return m;
}
function aggregate() {
  // ストック合算: sum every stocked page; otherwise just the live scan
  let counts;
  if (TABLE_SRC === "stock" && STOCKS.length) {
    counts = new Map();
    for (const st of STOCKS)
      for (const [h, q] of scanCounts(st.scan)) counts.set(h, (counts.get(h) || 0) + q);
  } else {
    if (!SCAN) return [];
    counts = scanCounts(SCAN);
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
    const arrow = SORT.k === k ? (dir > 0 ? " ▲" : " ▼") : "";
    // visible ⓘ marker carrying an instant custom tooltip (see .info CSS)
    const info = tips[k] ? ` <span class="info" data-tip="${esc(tips[k])}">ⓘ</span>` : "";
    th.innerHTML = esc(labels[k]) + arrow + info;
    th.title = t("sort_tip");
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
    // premium price-tier bar behind the item name: a SOLID tier-colour left edge
    // (instant, unambiguous tier ID) + a glossy gradient tint that gets richer
    // with value; 10k+ adds an outer glow. Intensity rises with the band so
    // cheap items stay calm and valuable ones clearly stand out.
    const band = +bandClass(r.unit).slice(1);
    const col = BAND_BRIGHT[band];
    let nameAttr = ' class="l"';
    if (band >= 1) {
      const hi = band >= 7;
      const a = hi ? 0.30 : band >= 4 ? 0.21 : 0.13;     // tint near the right
      const b = hi ? 0.58 : band >= 4 ? 0.44 : 0.30;     // deepest tint
      nameAttr = ` class="l lux${hi ? " lux-hi" : ""}" style="--lc:${col};--la:${rgba(col, a)};--lb:${rgba(col, b)};"`;
    }
    return `<tr data-hash="${esc(r.hash)}">
      <td class="l"><img class="icon" style="border:2px solid ${bc}" src="${iconUrl(r.hash)}" loading="lazy" alt=""></td>
      <td${nameAttr}><a class="name" href="${href}" target="_blank" rel="noopener">${esc(r.name)}</a>${badge}
        <br><span class="rar" style="color:${bc}">${esc(r.rarity || "")}</span></td>
      <td>${r.qty}</td>
      <td class="num1">${yen(r.unit)}${(() => { const tr = trendChip(r.hash), ak = lowestAskNote(r.hash); return (tr || ak) ? `<br><span class="sub">${tr}${ak}</span>` : ""; })()}</td>
      <td>${yen(r.net, "num")}</td>
      <td>${yen(r.total, "num")}</td>
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
  // spin EV follows the basis too: on 現在価格 the bot ships per-grade current
  // averages ("gev", grades with <3 samples omitted) — fall back per-grade to
  // the pre-freeze baseline for the missing ones.
  const gradeEV = (gcur && DATA.prices?.gev)
    ? { ...g.grade_ev_baseline, ...DATA.prices.gev }
    : g.grade_ev_baseline;
  const rows = [];
  for (const [coin, odds] of Object.entries(g.coins)) {
    // show Steam market prices (gross) so they match the store; the 15% sell fee
    // hits BOTH spin gear and the coin equally, so the verdict is unchanged.
    const spin = Object.entries(odds).reduce((s, [gr, p]) => s + p / 100 * (gradeEV[gr] || 0), 0);
    const sellU = unitPriceIn(coin, GMODE);
    const sell = sellU != null ? sellU : null;
    rows.push({ coin, spin, sell });
  }
  // sell-freeze gate: while you can't list, a 回す/売る verdict isn't actionable
  // (and the freeze price will normalize on reopening), so show 回す EV only and
  // mark selling as locked. The full comparison activates once trading reopens.
  const sellable = !!DATA.prices?.unlocked || location.hash === "#demoscan" || location.hash === "#planpreview";
  $("gRows").innerHTML = rows.map(r => {
    const spinWins = r.sell == null || r.spin > r.sell;
    const sellCell = !sellable ? '<span class="muted">—</span>'
      : (r.sell == null ? `<span class="muted">${esc(t("gacha_noprice"))}</span>`
                        : yen(r.sell) + `<span class="foot">${esc(t("gacha_per"))}</span>`);
    const verdict = !sellable ? `<span class="muted">${esc(t("gacha_locked"))}</span>`
      : (spinWins ? `<span class="verdict-spin">🎰 ${esc(t("gacha_verdict_spin"))}</span>`
                  : `<span class="verdict-sell">💰 ${esc(t("gacha_verdict_sell"))}</span>`);
    return `<tr data-hash="${esc(r.hash)}">
      <td class="l"><img class="icon" style="width:1.6rem;height:1.6rem;" src="${iconUrl(r.coin)}" loading="lazy" alt="">
        <a class="name" href="${marketUrl(r.coin)}" target="_blank" rel="noopener" style="font-size:.78rem;">${esc(dispName(r.coin))}</a></td>
      <td>${yen(r.spin)}<span class="foot">${esc(t("gacha_per"))}</span></td>
      <td>${sellCell}</td>
      <td class="l">${verdict}</td>
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
  // border colour unreadable -> very often a Common/Uncommon/Rare piece the
  // hue bands missed; nudge the user toward 出品不可/無視 (friend feedback)
  $("popIgnore").classList.toggle("suggest", !c.border);
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
  // candidates — the #1 (best-match) candidate gets a highlighted frame. All
  // realistic matches are listed; the box scrolls past ~6 (see #popCands CSS).
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
  $("popCands").scrollTop = 0;   // new cell -> show candidates from the top (#1 best match), don't keep the previous scroll
  let x = ev.clientX + 12, y = ev.clientY + 8;
  if (x + pop.offsetWidth > innerWidth - 8) x = innerWidth - pop.offsetWidth - 8;
  if (y + pop.offsetHeight > innerHeight - 8) y = Math.max(8, innerHeight - pop.offsetHeight - 8);
  pop.style.left = x + "px"; pop.style.top = y + "px";
}

function assign(hash) {
  if (POP_I < 0) return;
  const c = SCAN.cells[POP_I];
  // crowd signal (㉕): log a manual fix ONLY when auto-recognition actually
  // missed — auto status wasn't "ok", or it auto-matched a DIFFERENT base.
  // Sent to GoatCounter as a count of which items need attention (item name
  // only — no image, no signature). Promotion to the shipped data is gated
  // separately, so a wrong pick here can't affect anyone else.
  const pickedBase = DATA.items[hash]?.base || hash;
  const autoBase = c.hash ? (DATA.items[c.hash]?.base || c.hash) : null;
  if (c.status !== "ok" || autoBase !== pickedBase) gcEvent("fix/" + pickedBase);
  c.assigned = hash; c.ignored = false;
  const cellImg = crop(SCAN.roi, c.x, c.y, c.w, c.h);
  saveLabel(cellImg, DATA.items[hash]?.base || hash, DATA.items[hash]?.rarity || null,
            { border: c.border, dist: c.dist });   // ⑭ learn in-browser + ㉕ crowd-collect
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
  schedulePersistActive();   // if this scan is a saved page, persist the edit
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
  schedulePersistActive();   // persist the edit if this scan is a saved page
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
  applyMode(); renderTable(); renderGacha(); renderPlan();
  // recolour the borders on whatever page is on screen (the live page, or a
  // stocked page held by a plan hover-preview) so image & overlays stay in sync
  if (_shownScan) drawOverlays(_shownScan, _shownScan === SCAN);
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
  $("planJump").textContent = t("plan_btn");
  $("planTop").textContent = t("plan_top");
  $("modeCur").textContent = t("mode_cur");
  $("modeBase").textContent = tu("mode_base");
  $("modeBase").title = tu("mode_base_tip");
  set("tTotalLabel", "total_label"); set("netNote", "net_note");
  set("gTitle", "gacha_title"); set("gNote", "gacha_note");
  set("ghSpin", "gacha_spin"); set("ghSell", "gacha_sell");
  $("quickSteps").innerHTML = t("steps_quick");
  set("guideTitle", "steps_title");
  $("gd1").innerHTML = t("step1");
  $("gd2").innerHTML = t("step2");                          // step2/3 carry colored <span class='btnref'>
  $("gd2").querySelector(".btnref")?.classList.add("bconn"); // connect = accent gradient; appraise stays blue
  $("gd3").innerHTML = t("step3").replace(/\[\?\]/g, QBADGE);
  set("guideNote", "verify_note");
  set("gdScale", "guide_scale");
  set("gdPrivacy", "guide_privacy");
  set("guideMoreTitle", "guide_more");
  renderAbout();
  updateStockUI();
  $("dmNote").textContent = t("dm_note");
  set("chTitle", "chlog_title");
  renderChangelog();
  $("tPriceRefresh").textContent = t("price_refresh");
  for (const _id of ["priceRefreshG", "priceRefreshR"]) {
    const _b = $(_id);
    if (_b) { _b.textContent = t("price_refresh_btn"); _b.onclick = () => refreshPricesAndRender(_b); }
  }
  set("tPrivacy", "privacy"); set("tUnofficial", "unofficial");
  set("popTitle", "pop_title"); set("popRarLbl", "pop_rar");
  set("learnNote", "learn_note");
  $("popSearch").placeholder = t("pop_search");
  set("popIgnore", "pop_ignore"); set("popClose", "pop_close");
  set("fbTitle", "fb_title"); set("fbSend", "fb_send"); set("fbCancel", "fb_cancel");
  $("fbText").placeholder = t("fb_placeholder");
  $("legend").innerHTML = `<span class="hint">${withQ("review_hint")}</span>`;
  $("heroCap").textContent = t("hero_cap");
  if (DATA) { applyMode(); renderTable(); renderGacha(); renderPlan(); applyView(); }
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
  // pulse the NEXT action like the yellow review cells: connect first. Once the
  // game screen is being shared, keep "appraise" glowing the WHOLE time — it's
  // the main repeatable action (appraise again after every warehouse change,
  // whether or not you stock the page).
  cap.classList.toggle("cta", !STREAM);
  scan.classList.toggle("cta", !!STREAM);
}
// hover a table row -> spotlight that item's cells in the warehouse image
function hlCells(hash, on) {
  document.querySelectorAll(".ov").forEach(o => {
    o.classList.toggle("hl", on && !!hash && o.dataset.hash === hash);
    o.classList.toggle("dim", on && !!hash && o.dataset.hash !== hash);
  });
}
// the main appraisal table only ever lists the page on screen, so hovering a row
// just spotlights that item's cells (delegated; rows are re-rendered each scan).
$("rows").addEventListener("mouseover", e => {
  const tr = e.target.closest("tr[data-hash]");
  if (tr) hlCells(tr.dataset.hash, true);
});
$("rows").addEventListener("mouseleave", () => hlCells(null, false));

// ---- listing-plan hover preview: switch the left image to the item's page ----
// the plan pools EVERY warehouse page, so a hovered item may live on a page the
// left image isn't showing. Hovering draws that page and spotlights the item; the
// image then STAYS on the last item you looked at (per request) instead of
// snapping back. A stocked page is drawn read-only so the peek can never edit the
// wrong page; the live SCAN page is drawn interactive as usual.
let _hoverHash = null;
// the page (live SCAN or a stocked scan) that actually holds this item's cell;
// prefer the page already on screen so a hover there needs no redraw.
function pageForHash(hash) {
  const has = sc => !!sc && sc.cells.some(c => c.assigned === hash && !c.ignored);
  if (has(SCAN)) return SCAN;
  for (const st of STOCKS) if (has(st.scan)) return st.scan;
  return null;
}
function previewItem(hash) {
  if (hash === _hoverHash) return;             // still on the same row -> no rework
  _hoverHash = hash;
  const scan = pageForHash(hash);
  if (scan && scan !== _shownScan) drawScan(scan, scan === SCAN);   // switch the left image
  hlCells(hash, true);                         // spotlight it on the now-shown page
}
// snap the left image back to the live editable page (e.g. when leaving the plan
// for the appraisal view, so the image matches the item table again).
function syncShownToScan() { if (SCAN && _shownScan !== SCAN) drawScan(); }
$("planBody").addEventListener("mouseover", e => {
  const tr = e.target.closest("tr[data-hash]");
  if (tr) previewItem(tr.dataset.hash);
});
// leaving the list keeps the last page shown (no redraw) — just drop the spotlight
$("planBody").addEventListener("mouseleave", () => { _hoverHash = null; hlCells(null, false); });

$("capBtn").addEventListener("click", connect);
$("planJump").addEventListener("click", () => setView(VIEW === "plan" ? "appraisal" : "plan"));
$("planTop").addEventListener("click", () => setView("appraisal"));   // "back to appraisal"
$("scanBtn").addEventListener("click", async () => {
  if (!VIDEO) return;
  try {
    gcEvent("scan");
    await refreshPrices();          // each appraisal prices against the latest snapshot
    await runScan(grabFrame());
  }
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

// ---------------- ㉔ warehouse snapshot stock ----------------
const pageLabel = n => t("stock_page").replace("{n}", n);
// display order: by page 1-7, then unknown-page pages last in add order (stable).
function sortStocks() { STOCKS.sort((a, b) => (a.pageNo ?? 99) - (b.pageNo ?? 99)); }

// ---- ㉕ "My Warehouse": persist STOCKS in IndexedDB across browser restarts ----
const SCHEMA = 1;
// build a cropped thumbnail PNG Blob straight from a scan's ROI pixels (same
// crop as drawScan) — only used as a FALLBACK for old saves with no cached thumb.
function makeThumb(scan) {
  return new Promise(res => {
    const r = scan.roi, cs = scan.cells;
    if (!r || !cs || !cs.length) { res(null); return; }
    const pad = 8;
    const x0 = Math.max(0, Math.min(...cs.map(c => c.x)) - pad);
    const y0 = Math.max(0, Math.min(...cs.map(c => c.y)) - pad);
    const x1 = Math.min(scan.imgW, Math.max(...cs.map(c => c.x + c.w)) + pad);
    const y1 = Math.min(scan.imgH, Math.max(...cs.map(c => c.y + c.h)) + pad);
    const w = x1 - x0, h = y1 - y0;
    if (w <= 0 || h <= 0) { res(null); return; }
    const cvs = document.createElement("canvas"); cvs.width = w; cvs.height = h;
    const im = new ImageData(w, h);
    for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
      const s = ((y + y0) * r.w + x + x0) * 3, d = (y * w + x) * 4;
      im.data[d] = r.data[s + 2]; im.data[d + 1] = r.data[s + 1];
      im.data[d + 2] = r.data[s]; im.data[d + 3] = 255;
    }
    cvs.getContext("2d").putImageData(im, 0, 0);
    cvs.toBlob(b => res(b || null), "image/png");
  });
}
// write one page to IndexedDB (key = pageNo for 1-7, else "u:<savedAt>"); raw ROI
// pixels + cells so a restored page is fully re-drawable. Local-only, no upload.
function persistEntry(entry) {
  if (!dbAvailable || !entry || !entry.scan || !entry.scan.roi) return;
  const sc = entry.scan;
  const rec = {
    v: SCHEMA, savedAt: entry.savedAt || Date.now(), pageNo: entry.pageNo ?? null,
    roiW: sc.roi.w, roiH: sc.roi.h, roi: sc.roi.data, cells: sc.cells,
  };
  if (entry.thumbBlob) rec.thumb = entry.thumbBlob;   // cached PNG -> fast restore (no makeThumb)
  putPage(entry.dbKey, rec).catch(e => console.warn("warehouse save failed", e));
  try { localStorage.setItem("tbh_has_warehouse", "1"); } catch (e) {}   // skip the hero next time
}
// re-save the active page after in-place edits (assign/ignore), debounced.
let _persistTimer = null;
function schedulePersistActive() {
  if (!dbAvailable || !SCAN) return;
  const entry = STOCKS.find(st => st.scan === SCAN);
  if (!entry) return;
  entry.savedAt = Date.now();
  clearTimeout(_persistTimer);
  _persistTimer = setTimeout(() => persistEntry(entry), 800);
}
// rebuild STOCKS from IndexedDB on startup. Uses the CACHED thumbnail Blob when
// present (cheap) — only old saves without one pay makeThumb (and get re-cached).
async function restoreWarehouse() {
  if (!dbAvailable) return;
  let pages;
  try { pages = await loadPages(); } catch (e) { console.warn("warehouse restore failed", e); return; }
  if (!pages || !pages.length) return;
  let added = false;
  for (const { key, rec } of pages) {
    if (!rec || rec.v !== SCHEMA || !rec.roi) continue;
    if (STOCKS.some(s => s.dbKey === key)) continue;   // user scanned this page during the defer window
    const data = rec.roi instanceof Uint8Array ? rec.roi : new Uint8Array(rec.roi);
    const scan = { roi: { w: rec.roiW, h: rec.roiH, data }, imgW: rec.roiW, imgH: rec.roiH,
                   cells: rec.cells || [], srcImg: null, pageGuess: rec.pageNo ?? null, restored: true };
    let thumbBlob = rec.thumb || null;
    if (!thumbBlob) thumbBlob = await makeThumb(scan);  // old save: regenerate once
    const entry = { scan, url: thumbBlob ? URL.createObjectURL(thumbBlob) : null,
                    pageNo: rec.pageNo ?? null, dbKey: key, savedAt: rec.savedAt || 0, thumbBlob };
    STOCKS.push(entry);
    added = true;
    if (!rec.thumb && thumbBlob) persistEntry(entry);   // back-fill the cache so next start is fast
  }
  if (added) { sortStocks(); TABLE_SRC = "stock"; renderAll(); }
}
// page picker (1-7 + "?") for the CURRENT scan; pre-filled with the auto guess.
function renderPageSel() {
  const el = $("pageSel");
  if (!el) return;
  // tab bar of the SAVED pages — click a number to switch to (view/edit) that
  // page, like the in-game warehouse tabs. The page currently shown is lit.
  if (!STOCKS.length) { el.innerHTML = ""; return; }
  let legacy = 0;
  let html = `<span class="pglabel">${esc(t("page_label"))}</span>`;
  STOCKS.forEach((st, i) => {
    const label = st.pageNo != null ? st.pageNo : ("#" + (++legacy));
    const active = st.scan === SCAN;
    html += `<button type="button" class="pgb${active ? " sel" : ""}" data-i="${i}">${esc(String(label))}</button>`;
  });
  el.innerHTML = html;
}
function updateStockUI() {
  $("stockBar").style.display = (SCAN || STOCKS.length) ? "" : "none";
  renderPageSel();
  const btn = $("stockBtn");
  const exact = SCAN ? STOCKS.find(st => st.scan === SCAN) : null;            // this scan already saved?
  let label = t("stock_btn"), disabled = !SCAN, cta = false;
  if (exact) {                                  // already in My Warehouse -> just a status
    label = exact.pageNo != null ? t("stock_added_page").replace("{n}", exact.pageNo) : t("stock_added");
    disabled = true;
  } else if (SCAN) {                            // unsaved (page unknown) -> manual save
    label = t("stock_btn"); cta = true;
  }
  btn.disabled = disabled;
  btn.textContent = label;
  // pulse after a scan (not yet stocked) so multi-page users notice they can
  // stock this page — same yellow CTA glow as the connect/appraise buttons
  btn.classList.toggle("cta", !!SCAN && cta);
  // keep the appraise button glowing the whole time the screen is shared (it's
  // the main repeatable action) — never let the next-step nudge vanish from it
  $("scanBtn").classList.toggle("cta", !!STREAM);
  $("stockInfo").textContent = STOCKS.length ? t("stock_info").replace("{n}", STOCKS.length) : "";
  $("stockClear").textContent = STOCKS.length ? t("stock_clear") : "";
  // item-table source toggle (appears once a page is stocked)
  const seg = $("srcSeg");
  seg.style.display = STOCKS.length ? "" : "none";
  if (!STOCKS.length) TABLE_SRC = "scan";
  $("srcScan").textContent = t("src_scan");
  $("srcStock").textContent = t("src_stock").replace("{n}", STOCKS.length);
  const lit = (el, is, col) => { el.style.background = is ? col : "#21262d"; el.style.color = is ? "#fff" : "#8b93a7"; };
  lit($("srcScan"), TABLE_SRC === "scan", "#2d6cdf");
  lit($("srcStock"), TABLE_SRC === "stock", "#7048e8");
  // Diablo tier legend: which colour means how much. ¥30k+ and ¥50k+ are the
  // rare "special" tiers — omitted here so the legend stays short; they show
  // with their own treatment on the item itself.
  $("tierLegend").innerHTML = esc(t("tier_legend")) + " " +
    [[1, "100"], [2, "500"], [3, "1k"], [4, "2k"], [5, "3k"], [6, "5k"], [7, "10k"]]
      .map(([b, lb]) => `<span class="p${b}" style="margin-right:.4rem;">¥${lb}+</span>`).join("");
}
// Save the current scan into My Warehouse at slot `pageNo` (1-7, or null=unknown).
// One scan = one entry; this stocks it, MOVES it if the scan was already at
// another page, and OVERWRITES whatever page `pageNo` held (freeing its image).
// Called automatically after a scan (confident page), on a page-picker click, and
// by the stock button. Pooled appraisal + listing plan recompute via renderAll().
function saveActive(pageNo) {
  if (!SCAN) return;
  const cur = STOCKS.find(st => st.scan === SCAN);          // this scan already stocked?
  const slot = pageNo != null ? STOCKS.find(st => st.pageNo === pageNo) : null;
  if (cur && cur === slot) {                                // already at this page -> re-save edits
    CURPAGE = pageNo; persistEntry(cur); updateStockUI(); return;
  }
  const cv = $("scanCanvas");
  cv.toBlob(b => {
    if (!b) return;
    const url = URL.createObjectURL(b);
    const savedAt = Date.now();
    let target;
    if (slot && slot !== cur) {                             // overwrite the target page slot
      if (slot.url) URL.revokeObjectURL(slot.url);
      slot.scan = SCAN; slot.url = url; slot.savedAt = savedAt; slot.pageNo = pageNo; slot.dbKey = pageNo;
      target = slot;
      if (cur) {                                            // this scan was elsewhere -> vacate it
        if (cur.url) URL.revokeObjectURL(cur.url);
        if (cur.dbKey != null && cur.dbKey !== pageNo) deletePage(cur.dbKey).catch(() => {});
        STOCKS.splice(STOCKS.indexOf(cur), 1);
      }
    } else if (cur) {                                       // re-key this scan to a new page
      if (cur.url) URL.revokeObjectURL(cur.url);
      const oldKey = cur.dbKey;
      cur.url = url; cur.savedAt = savedAt; cur.pageNo = pageNo;
      cur.dbKey = pageNo != null ? pageNo : (oldKey ?? ("u:" + savedAt));
      if (oldKey != null && oldKey !== cur.dbKey) deletePage(oldKey).catch(() => {});
      target = cur;
    } else {                                                // fresh entry
      target = { scan: SCAN, url, pageNo, savedAt, dbKey: pageNo != null ? pageNo : ("u:" + savedAt) };
      STOCKS.push(target);
    }
    target.thumbBlob = b;        // cache the PNG thumb so restore skips makeThumb
    CURPAGE = pageNo;
    sortStocks();
    TABLE_SRC = "stock";
    renderAll();
    persistEntry(target);
    if (pageNo != null) setStatus(t("stock_saved").replace("{n}", pageNo));
  }, "image/png");
}
// click a stocked thumbnail -> reload it as the MAIN editable warehouse image
function loadStock(i) {
  const st = STOCKS[i];
  if (!st) return;
  SCAN = st.scan;                 // same mutable object -> edits update this page
  CURPAGE = st.pageNo ?? null;    // selector follows the page you're editing
  TABLE_SRC = "scan";             // show just this page while you edit it
  VIEW = "appraisal";             // opening a page to view/edit -> show its items
  drawScan(); renderAll();
  $("scanWrap").scrollIntoView({ behavior: "smooth", block: "nearest" });
}
function renderStock() {
  let legacy = 0;
  $("stockStrip").innerHTML = STOCKS.map((st, i) => {
    const cap = st.pageNo != null ? pageLabel(st.pageNo) : "#" + (++legacy);
    return `<div class="stockcard${st.scan === SCAN ? " active" : ""}" data-i="${i}" title="${esc(t("stock_edit_tip"))}">
       <span class="cap">${esc(cap)}</span>
       <span class="del" data-del="${i}" title="${esc(t("stock_del_tip"))}">×</span>
       <img src="${st.url}" alt=""></div>`;
  }).join("");
}
function removeStock(i) {
  const st = STOCKS[i];
  if (!st) return;
  if (st.url) URL.revokeObjectURL(st.url);
  if (st.dbKey != null) deletePage(st.dbKey).catch(e => console.warn("warehouse delete failed", e));
  STOCKS.splice(i, 1);            // order already sorted; labels recomputed on render
  if (!STOCKS.length) { TABLE_SRC = "scan"; try { localStorage.removeItem("tbh_has_warehouse"); } catch (e) {} }
  renderAll();
}
$("stockBtn").addEventListener("click", () => saveActive(CURPAGE));
$("pageSel").addEventListener("click", e => {
  const b = e.target.closest(".pgb");
  if (!b) return;
  loadStock(+b.dataset.i);                       // tab = switch to that saved page
});
$("stockClear").addEventListener("click", e => {
  e.preventDefault();
  if (!confirm(t("stock_clear_confirm"))) return;     // wipes the saved My Warehouse
  STOCKS.forEach(st => { if (st.url) URL.revokeObjectURL(st.url); });
  STOCKS = []; TABLE_SRC = "scan";
  if (dbAvailable) clearPages().catch(e => console.warn("warehouse clear failed", e));
  try { localStorage.removeItem("tbh_has_warehouse"); } catch (e) {}   // hero shows again
  renderAll();
});
$("stockStrip").addEventListener("click", e => {
  const del = e.target.closest("[data-del]");
  if (del) { removeStock(+del.dataset.del); return; }   // × removes that page
  const card = e.target.closest(".stockcard");
  if (card) loadStock(+card.dataset.i);
});
$("srcScan").addEventListener("click", () => { TABLE_SRC = "scan"; renderAll(); });
$("srcStock").addEventListener("click", () => { TABLE_SRC = "stock"; renderAll(); });

// ---------------- ㉒ listing-slot plan (出品プラン) ----------------
// Reopening rule: 4 slots, 1 new listing/slot/8h (=12 listings/day, 1 unit
// each). Smart mode ranks by a slot's daily yield (net take-home × turnover):
// an unsold listing occupies its slot, so yield/day = net × 24/max(8h, est.
// sell time), est. sell time = current listings ÷ sale rate. When there's no
// sales volume yet (during the freeze / right after reopening) it falls back
// to ranking by net take-home so the order still makes sense.
// the plan always covers your WHOLE inventory: every stocked page + the live
// scan pooled together (independent of the table's scan/stock toggle).
function planCounts() {
  const m = new Map(), seen = new Set();
  const add = scan => {
    if (!scan || seen.has(scan)) return; seen.add(scan);
    for (const [h, q] of scanCounts(scan)) m.set(h, (m.get(h) || 0) + q);
  };
  STOCKS.forEach(st => add(st.scan));
  add(SCAN);                                       // include current scan if not yet stocked
  return m;
}
function planItems() {
  const out = []; let anyVol = false;
  for (const [hash, qty] of planCounts()) {        // all pages pooled
    const unit = unitPriceIn(hash, "cur");          // sell at the CURRENT market
    if (unit == null) continue;
    const p = DATA.prices?.items?.[hash];
    const net = unit * FEE;
    const v = p?.v || 0; if (v > 0) anyVol = true;
    const sellH = v > 0 ? (p?.q || 0) / (v / 24) : Infinity;
    // one slot can post a new listing every 8h => at most 3/day, fewer if the
    // item sells slower; capped by how many you actually own.
    const turnover = v > 0 ? 24 / Math.max(8, sellH) : 0;   // realistic listings/day per slot
    const dailyCap = Math.min(qty, Math.max(1, Math.round(turnover)));
    const yieldDay = net * Math.min(qty, turnover);         // stock-capped daily yield
    out.push({ hash, qty, unit, net, sellH, dailyCap, yieldDay });
  }
  // turnover-rank ONLY when items actually sell fast; else NET rank. At/after the
  // reopening, sellH = listings(q) / volume is huge for backlogged commodities (q in
  // the tens of thousands) and Infinity for no-volume gear, so turnover collapses to
  // ~0 and inverts the pick to cheap junk. Net rank = each scarce 4-slot/8h listing
  // holds the highest-value item (matches the reopening playbook).
  const fastEnough = out.filter(r => r.sellH <= 72).length >= 3;
  const smart = anyVol && fastEnough;
  out.sort((a, b) => smart ? b.yieldDay - a.yieldDay : b.net - a.net);
  return { smart, rows: out };
}
function fmtSellH(h) {
  if (h === Infinity || h > 72) return t("plan_slow");
  if (h <= 1) return t("plan_fast");
  return "~" + Math.ceil(h) + "h";
}
// cheap part of the plan panel: keep the panel present + the jump-button glow
// current on every renderAll, WITHOUT recomputing the (heavier) plan body.
function renderPlanChrome() {
  const el = $("plan"); if (!el) return;
  el.style.display = "block";
  $("planTitle").textContent = t("plan_title");
  // pulse the plan-jump button (purple) ONLY once the planner is live (unlocked / preview),
  // after a scan or with pooled stock — stays dormant during the freeze, auto-lights at reopening.
  const planLive = !!DATA.prices?.unlocked || location.hash === "#planpreview" || location.hash === "#demoscan";
  $("planJump").classList.toggle("ctap", planLive && !!(SCAN || STOCKS.length));
}
// the full plan body is computed lazily (only when the panel is on-screen or the
// user jumps to it) via the _planDirty flag, so off-screen edits don't recompute it.
let _planDirty = true, _planVisible = false;
function refreshPlanIfNeeded() { if (_planDirty && _planVisible) renderPlan(); }
function renderPlan() {
  const el = $("plan"); if (!el) return;
  _planDirty = false;
  renderPlanChrome();
  // pre-reopen teaser (unless forced via #planpreview / the demo) so the feature
  // advertises itself. Includes a static EXAMPLE table — the real planner needs
  // live prices+volume which don't exist during the freeze, and #demoscan can't
  // run in production, so this is how visitors see the feature before reopening.
  if (!DATA.prices?.unlocked && location.hash !== "#planpreview" && location.hash !== "#demoscan") {
    $("planNote").textContent = "";
    const exItems = t("plan_ex_items") || [];
    const ex = [
      { i: 0, qty: 2,  take: 1, unit: 5000, net: 4340, sellH: 20, yd: 4340 },
      { i: 1, qty: 4,  take: 3, unit: 1500, net: 1300, sellH: 8,  yd: 3900 },
      { i: 2, qty: 12, take: 4, unit: 400,  net: 340,  sellH: 4,  yd: 1360 },
    ];
    const exRows = ex.map(e => `<tr>
      <td class="l" style="font-size:.8rem;">${esc(exItems[e.i] || "")}</td>
      <td>×${e.qty}</td>
      <td>×${e.take}</td>
      <td>${money(e.unit)} <span class="foot">→ ${money(e.net)}</span></td>
      <td>${esc(fmtSellH(e.sellH))}</td>
      <td>${yen(e.yd)}</td>
    </tr>`).join("");
    $("planBody").innerHTML = `
      <div class="pteaser">${esc(t("plan_teaser"))}</div>
      <div class="foot" style="margin:.6rem 0 .25rem;">${esc(t("plan_ex_lead"))}</div>
      <table class="planex"><thead><tr>
        <th class="l">${esc(t("th_item"))}</th><th>${esc(t("plan_count"))}</th><th>${esc(t("plan_today"))}</th>
        <th>${esc(t("plan_price"))}</th><th>${esc(t("plan_sell"))}</th><th>${esc(t("plan_yield"))}</th>
      </tr></thead><tbody>${exRows}</tbody></table>`;
    return;
  }
  if (!SCAN && !STOCKS.length) {
    $("planNote").textContent = "";
    $("planBody").innerHTML = `<div class="pteaser">${esc(t("plan_scan_first"))}</div>`;
    return;
  }
  const { smart, rows } = planItems();
  $("planNote").textContent = t(smart ? "plan_note" : "plan_note_b");
  if (!rows.length) {
    $("planBody").innerHTML = `<div class="pteaser">${esc(t("plan_empty"))}</div>`;
    return;
  }
  // allocate today's 12 listings greedily in rank order; show every item so
  // users can see where the rest of the warehouse stands
  let left = 12, before = 0;
  const alloc = rows.map(r => {
    const take = Math.min(r.qty, r.dailyCap, left); left -= take;   // realistic daily count
    const startUnit = before; before += take;
    return { r, take, startUnit };
  });
  const perDay = smart
    ? rows.slice(0, 4).reduce((s, r) => s + r.yieldDay, 0)
    : alloc.reduce((s, a) => s + a.r.net * a.take, 0);
  const body = alloc.map(({ r, take, startUnit }) => {
    const cls = take > 0 && startUnit < 4 ? "slotnow" : take === 0 ? "slotlater" : "";
    return `<tr class="${cls}" data-hash="${esc(r.hash)}">
      <td class="l"><img class="icon" style="width:1.6rem;height:1.6rem;" src="${iconUrl(r.hash)}" loading="lazy" alt="">
        <a class="name" href="${marketUrl(r.hash)}" target="_blank" rel="noopener" style="font-size:.78rem;">${esc(dispName(r.hash))}</a></td>
      <td>×${r.qty}</td>
      <td>${take > 0 ? "×" + take : '<span class="muted">—</span>'}</td>
      <td>${money(r.unit)} <span class="foot">→ ${money(r.net)}</span></td>
      <td>${esc(fmtSellH(r.sellH))}</td>
      <td>${smart ? yen(r.yieldDay) : '<span class="muted">—</span>'}</td>
    </tr>`;
  }).join("");
  // plain-language action callout: the immediate 4 slots = the first 4 listing units
  const nowItems = alloc.map(a => {
    const n = Math.max(0, Math.min(a.startUnit + a.take, 4) - a.startUnit);
    return n > 0 ? `${esc(dispName(a.r.hash))} ×${n}` : null;
  }).filter(Boolean);
  const th = (label, tip) => `<th>${esc(label)}${tip ? ` <span class="info" data-tip="${esc(tip)}">ⓘ</span>` : ""}</th>`;
  $("planBody").innerHTML = `
    <div class="plannow"><b>${esc(t("plan_now_label"))}</b> ${nowItems.join("　/　")}
      <div class="foot" style="margin-top:.15rem;">${esc(t("plan_then"))}</div></div>
    <div class="ptotal">${esc(t(smart ? "plan_perday" : "plan_total"))} <b>${money(perDay)}</b></div>
    <table><thead><tr>
      <th class="l">${esc(t("th_item"))}</th><th>${esc(t("plan_count"))}</th><th>${esc(t("plan_today"))}</th>
      ${th(t("plan_price"), t("plan_price_tip"))}<th>${esc(t("plan_sell"))}</th>${th(t("plan_yield"), t("plan_yield_tip"))}
    </tr></thead><tbody>${body}</tbody></table>
    <div class="foot" style="margin-top:.3rem;">${esc(t("plan_now4_note"))}</div>`;
}

function renderAll() {
  // total + results table only make sense after a scan (or pooled stock pages)
  const rc = $("results"); if (rc) rc.style.display = (SCAN || STOCKS.length) ? "" : "none";
  renderTable(); renderGacha(); updateStockUI(); renderStock();
  renderPlanChrome(); _planDirty = true; refreshPlanIfNeeded();   // body only if on-screen
  applyView();                                                    // show appraisal OR plan
}
// the 出品プラン button switches the right column between the appraisal (total +
// item table + coins) and the listing plan, instead of stacking them — a 7-page
// warehouse no longer needs a long scroll. The button doubles as "back".
function applyView() {
  const planView = VIEW === "plan";
  // single authority on the right-column visibility so toggling either way fully
  // restores the other view (results+coins for appraisal, the plan for plan).
  $("plan").style.display = planView ? "block" : "none";
  $("results").style.display = (!planView && (SCAN || STOCKS.length)) ? "" : "none";
  $("gacha").style.display = (!planView && DATA && DATA.gacha) ? "block" : "none";
  const btn = $("planJump");
  if (btn) btn.textContent = planView ? t("to_appraisal") : t("plan_btn");
}
function setView(v) {
  VIEW = v;
  if (v === "plan") renderPlan();                 // force a fresh body before showing
  else syncShownToScan();                         // back to appraisal -> image follows SCAN again
  applyView();
  $("resCol")?.scrollIntoView({ behavior: "smooth", block: "start" });
}

// ---------------- boot ----------------
applyLang();
setStatus("…");
// demo hero shows only if assets/demo.png exists (and until the first scan).
// NOTE: the image may have finished loading BEFORE this code runs, in which
// case onload never fires — check .complete as well.
{
  const hi = $("heroImg");
  // skip the tutorial hero for returning users who already have a saved My
  // Warehouse — a synchronous localStorage flag decides instantly (no IndexedDB
  // read, no flicker even though the restore is deferred).
  const show = () => {
    if (!SCAN && !localStorage.getItem("tbh_has_warehouse")) $("hero").style.display = "block";
  };
  hi.onload = show;
  hi.onerror = () => { $("hero").style.display = "none"; };
  if (hi.complete && hi.naturalWidth > 0) show();
}
// recompute the (gated) plan body when its panel scrolls into view. Without
// IntersectionObserver support, treat the plan as always visible so behaviour is
// unchanged (renderPlan runs on every renderAll).
try {
  const obs = new IntersectionObserver(es => {
    _planVisible = es.some(e => e.isIntersecting);
    refreshPlanIfNeeded();
  }, { rootMargin: "300px" });
  obs.observe($("plan"));
} catch (e) { _planVisible = true; }
// debug hook: lets a test harness drive a scan with a raw BGR image
window.__runScan = img => runScan(img);
// #demoscan: auto-scan the bundled test capture (dev/self-verification only;
// silently skipped when data/_test is absent, as in production)
async function demoScan() {
  try {
    const m = await (await fetch("data/_test/sample.json")).json();
    const buf = new Uint8Array(await (await fetch("data/_test/sample.bin")).arrayBuffer());
    await runScan({ w: m.w, h: m.h, data: buf });
  } catch (e) { console.warn("demoscan unavailable", e); }
}
loadData().then(() => {
  injectLearnedRefs(); applyLang(); setStatus(t("not_connected")); applyConnState();
  // restore My Warehouse AFTER the initial UI paints — reading IndexedDB (and
  // makeThumb on old saves) shouldn't block first render. Idle if available.
  const idle = window.requestIdleCallback || (cb => setTimeout(cb, 150));
  idle(() => restoreWarehouse());
  if (location.hash === "#demoscan") demoScan();
})
  .catch(e => setStatus("data load error: " + e));
