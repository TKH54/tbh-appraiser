"""Bump APP_VERSION (patch) + the JS cache-buster so a release reaches RETURNING
visitors, not just new ones.

Two independent cache keys must both move (see js/app.js loadData):
  - data files are fetched `?v=APP_VERSION`  -> bump APP_VERSION so learned_seed.json
    (and the rest of the bundle) refetches
  - the ES-module imports are pinned `?v20260616z<suffix>` in index.html + js/*.js
    -> bump that trailing suffix EVERYWHERE (kept identical across files so modules
    don't double-load) so the new app.js (with the new APP_VERSION) loads. The
    suffix increments spreadsheet-style (a..z, z->aa, ...) so it never runs out.

Used by the crowd-label workflow: it runs ONLY when a label was promoted, so the
PR is a complete, mergeable release. Safe to run by hand too.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# matches any v<8-digit-date><letter-suffix> buster (e.g. v20260626i or the older
# v20260616z<letters>); keeps the date, increments the trailing letter suffix.
BUSTER_RE = re.compile(r"v(\d{8})([a-z]+)")


def bump_app_version() -> str:
    app = ROOT / "js" / "app.js"
    s = app.read_text(encoding="utf-8")
    m = re.search(r'const APP_VERSION = "(\d+)\.(\d+)\.(\d+)";', s)
    if not m:
        sys.exit("APP_VERSION line not found in js/app.js")
    maj, minr, pat = (int(x) for x in m.groups())
    new = f"{maj}.{minr}.{pat + 1}"
    app.write_text(s[:m.start()] + f'const APP_VERSION = "{new}";' + s[m.end():],
                   encoding="utf-8")
    return new


def _next_suffix(s: str) -> str:
    """Spreadsheet-style increment with no upper bound: a..z, z->aa, az->ba, zz->aaa."""
    arr = list(s)
    i = len(arr) - 1
    while i >= 0:
        if arr[i] == "z":
            arr[i] = "a"; i -= 1
        else:
            arr[i] = chr(ord(arr[i]) + 1); return "".join(arr)
    return "a" + "".join(arr)


def bump_buster() -> tuple[str, str]:
    files = [ROOT / "index.html"] + sorted((ROOT / "js").glob("*.js"))
    date = suf = None
    for f in files:
        m = BUSTER_RE.search(f.read_text(encoding="utf-8"))
        if m:
            date, suf = m.group(1), m.group(2); break
    if date is None:
        sys.exit("no v<date><suffix> buster found")
    old, new = f"v{date}{suf}", f"v{date}{_next_suffix(suf)}"
    for f in files:
        t = f.read_text(encoding="utf-8")
        if old in t:
            f.write_text(t.replace(old, new), encoding="utf-8")
    return old, new


if __name__ == "__main__":
    ver = bump_app_version()
    old, new = bump_buster()
    print(f"APP_VERSION -> {ver}; cache-buster {old} -> {new}")
