#!/usr/bin/env bash
# LOCAL price runner — runs on a residential IP (Termux on a spare Android phone,
# or any always-on home box). Steam blocks the shared GitHub Actions IP range but
# serves homes fine, so this loop is the PRIMARY price source; the prices.yml CI
# chain sees src=local + fresh `t` and stands down (heartbeat only), taking over
# automatically if this runner stops pushing for >20 min (LOCAL_FRESH_SEC).
#
# Cycle (every ~10 min):
#   1. fetch/reset to origin/main (CI heartbeats + code pushes land continuously)
#   2. run build_prices.py with PRICES_SOURCE=local (gate: local runs always proceed)
#   3. SUCCESS -> overlay-commit data/{prices,history,price_state}.json and push
#      (same "never rebase, lay regenerated files on the remote tip" dance as the
#      workflow's commit step, retried 3x)
#      FAILURE -> push NOTHING (a local cooldown must not silence the CI fallback;
#      the stale `t` is what tells CI to take over) and back off 20/30/... min.
#
# Install: scripts/phone_setup.sh sets this up on Termux (clone, token, boot hook).
# Optional: put a Discord webhook URL in ~/.tbh/webhook to get pinged after 3
# consecutive local failures.
set -u

REPO="${TBH_REPO:-$HOME/tbh-appraiser}"
WORK="$HOME/.tbh"
MODE_FILE="$WORK/build_mode"
STASH="$WORK/stash"
LOG="$WORK/runner.log"
CADENCE=600                 # healthy cadence: one cycle per ~10 min
FAIL_STEP=600               # each consecutive failure adds this much extra sleep...
FAIL_SLEEP_MAX=3600         # ...capped at 1h (don't hammer Steam from the home IP)

mkdir -p "$WORK" "$STASH"
fails=0

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"; }

notify() {  # best-effort Discord ping; silent if no webhook file
    local hook
    hook=$(cat "$WORK/webhook" 2>/dev/null) || return 0
    [ -n "$hook" ] || return 0
    curl -s -H "Content-Type: application/json" \
        --data "{\"content\":\"📱 $1\"}" "$hook" >/dev/null 2>&1 || true
}

cycle() {
    cd "$REPO" || { log "repo missing: $REPO"; return 1; }
    git fetch --depth 1 origin main >>"$LOG" 2>&1 || { log "fetch failed (offline?)"; return 1; }
    git reset --hard origin/main >>"$LOG" 2>&1

    rm -f "$MODE_FILE"
    BUILD_MODE_FILE="$MODE_FILE" PRICES_SOURCE=local \
        python scripts/build_prices.py >>"$LOG" 2>&1
    local mode
    mode=$(cat "$MODE_FILE" 2>/dev/null || echo skip)
    if [ "$mode" != "snapshot" ]; then
        # _on_failure wrote a cooldown into the working tree's price_state.json —
        # deliberately NOT pushed (next reset discards it): our IP's failures say
        # nothing about the CI IP, and a pushed cooldown would delay the fallback.
        log "build produced no snapshot (mode=$mode) — nothing pushed"
        return 1
    fi

    cp data/prices.json data/history.json data/price_state.json "$STASH"/ || return 1
    local i
    for i in 1 2 3; do
        git fetch --depth 1 origin main >>"$LOG" 2>&1
        git reset --hard origin/main >>"$LOG" 2>&1
        cp "$STASH"/prices.json "$STASH"/history.json "$STASH"/price_state.json data/
        git add data/prices.json data/history.json data/price_state.json
        if git diff --cached --quiet; then log "no change to commit"; return 0; fi
        git commit -q -m "price snapshot (local)"
        if git push -q origin HEAD:main >>"$LOG" 2>&1; then
            log "pushed snapshot"
            return 0
        fi
        sleep 5
    done
    log "push failed 3x — will retry next cycle"
    return 1
}

log "=== runner started (repo=$REPO) ==="
while true; do
    start=$(date +%s)
    if cycle; then
        [ "$fails" -ge 3 ] && notify "TBH価格ローカル更新が復旧しました"
        fails=0
    else
        fails=$((fails + 1))
        [ "$fails" -eq 3 ] && notify "TBH価格ローカル更新が3回連続失敗（$(tail -n 1 "$LOG")）。CIフォールバックに切替中"
    fi
    # keep the log from growing forever
    tail -n 2000 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"

    elapsed=$(( $(date +%s) - start ))
    extra=$(( fails * FAIL_STEP )); [ "$extra" -gt "$FAIL_SLEEP_MAX" ] && extra=$FAIL_SLEEP_MAX
    nap=$(( CADENCE + extra - elapsed )); [ "$nap" -lt 60 ] && nap=60
    log "cycle done (fails=$fails, elapsed=${elapsed}s) — sleeping ${nap}s"
    sleep "$nap"
done
