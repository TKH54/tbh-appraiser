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


def bump_buster() -> list[tuple[str, str]]:
    """Bump EVERY distinct v<date><suffix> cache-buster across index.html + js/*.js,
    not just the first. The app.js ENTRY buster (index.html) and the module-graph
    buster (the ES-module imports) had drifted to different values; bumping only the
    first left the other stale, so a cached app.js kept importing the old module and
    returning visitors never saw the change. Each distinct buster keeps its date and
    gets its suffix incremented; a single-pass regex sub avoids chained double-bumps
    when two busters differ only by suffix. Returns the (old, new) mappings applied."""
    files = [ROOT / "index.html"] + sorted((ROOT / "js").glob("*.js"))
    texts = {f: f.read_text(encoding="utf-8") for f in files}
    busters = {m.group(0) for t in texts.values() for m in BUSTER_RE.finditer(t)}
    if not busters:
        sys.exit("no v<date><suffix> buster found")
    mapping = {}
    for old in busters:
        m = BUSTER_RE.fullmatch(old)
        mapping[old] = f"v{m.group(1)}{_next_suffix(m.group(2))}"
    for f, t in texts.items():
        new_t = BUSTER_RE.sub(lambda mo: mapping.get(mo.group(0), mo.group(0)), t)
        if new_t != t:
            f.write_text(new_t, encoding="utf-8")
    return sorted(mapping.items())


if __name__ == "__main__":
    ver = bump_app_version()
    mapping = bump_buster()
    print(f"APP_VERSION -> {ver}")
    for old, new in mapping:
        print(f"cache-buster {old} -> {new}")
