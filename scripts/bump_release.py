"""Bump APP_VERSION (patch) + the JS cache-buster so a release reaches RETURNING
visitors, not just new ones.

Two independent cache keys must both move (see js/app.js loadData):
  - data files are fetched `?v=APP_VERSION`  -> bump APP_VERSION so learned_seed.json
    (and the rest of the bundle) refetches
  - the ES-module imports are pinned `?v20260616z<letter>` in index.html + js/*.js
    -> bump that single trailing letter EVERYWHERE (kept identical across files so
    modules don't double-load) so the new app.js (with the new APP_VERSION) loads

Used by the crowd-label workflow: it runs ONLY when a label was promoted, so the
PR is a complete, mergeable release. Safe to run by hand too.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUSTER_RE = re.compile(r"v20260616z([a-z])")


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


def bump_buster() -> tuple[str, str]:
    files = [ROOT / "index.html"] + sorted((ROOT / "js").glob("*.js"))
    cur = None
    for f in files:
        m = BUSTER_RE.search(f.read_text(encoding="utf-8"))
        if m:
            cur = m.group(1); break
    if cur is None:
        sys.exit("no v20260616z<letter> buster found")
    if cur >= "z":
        sys.exit("buster reached 'z' — extend the scheme manually")
    old, new = f"v20260616z{cur}", f"v20260616z{chr(ord(cur) + 1)}"
    for f in files:
        t = f.read_text(encoding="utf-8")
        if old in t:
            f.write_text(t.replace(old, new), encoding="utf-8")
    return old, new


if __name__ == "__main__":
    ver = bump_app_version()
    old, new = bump_buster()
    print(f"APP_VERSION -> {ver}; cache-buster {old} -> {new}")
