// ㉕ "My Warehouse" persistence — keeps the multi-page stock across browser
// restarts in IndexedDB (localStorage is too small for several captures).
// Everything stays on the user's device; nothing is uploaded. Records are stored
// with OUT-OF-LINE keys so a page can be upserted by its tab number:
//   key = pageNo (1-7)          -> put() overwrites the same page (re-scan = update)
//   key = "u:<savedAt>"         -> unknown-page captures (legacy add), unique
// A record is { v, savedAt, pageNo, roiW, roiH, roi:Uint8Array(BGR), cells:[...] }.
// roi pixels are stored raw (structured clone handles typed arrays) so a restored
// page is fully re-drawable/editable; the thumbnail is regenerated from roi.
const DB_NAME = "tbh-appraiser";
const STORE = "warehouse";
const DB_VER = 1;

function openDB() {
  return new Promise((resolve, reject) => {
    let req;
    try { req = indexedDB.open(DB_NAME, DB_VER); }
    catch (e) { reject(e); return; }
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(STORE)) db.createObjectStore(STORE);
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function run(mode, fn) {
  const db = await openDB();
  try {
    return await new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, mode);
      let result;
      const r = fn(tx.objectStore(STORE), v => { result = v; });
      tx.oncomplete = () => resolve(result !== undefined ? result : r);
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error || new Error("tx aborted"));
    });
  } finally { db.close(); }
}

// available? (private mode / disabled storage degrades to in-memory only)
export const dbAvailable = typeof indexedDB !== "undefined";

export function putPage(key, record) { return run("readwrite", s => s.put(record, key)); }
export function deletePage(key) { return run("readwrite", s => s.delete(key)); }
export function clearPages() { return run("readwrite", s => s.clear()); }

// -> [{ key, rec }] for every saved page
export function loadPages() {
  return run("readonly", (s, set) => {
    const out = [];
    s.openCursor().onsuccess = e => {
      const c = e.target.result;
      if (c) { out.push({ key: c.key, rec: c.value }); c.continue(); }
      else set(out);
    };
  });
}
