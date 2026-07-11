#!/data/data/com.termux/files/usr/bin/sh
# Watchdog: restart the price-runner loop if it died (e.g. Android's
# phantom-process killer culling Termux children). phone_runner.sh itself
# never exits, so "not running" always means it was killed.
#
# Registered with Android's JobScheduler (via the Termux:API app + termux-api
# pkg), which re-spawns Termux to run this even if the whole app was killed:
#   termux-job-scheduler --script ~/tbh-appraiser/scripts/phone_watchdog.sh \
#       --period-ms 900000 --persisted true
# (15 min is Android's minimum period; --persisted survives reboots, so this
# also backs up the Termux:Boot hook.)
pgrep -f phone_runner.sh >/dev/null && exit 0

LOG="$HOME/.tbh/runner.log"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] watchdog: runner not running -> restarting" >> "$LOG"
hook=$(cat "$HOME/.tbh/webhook" 2>/dev/null)
[ -n "$hook" ] && curl -s -H "Content-Type: application/json" \
    --data '{"content":"📱 TBH価格ランナーが停止していたため、watchdogが自動再起動しました"}' \
    "$hook" >/dev/null 2>&1

termux-wake-lock
nohup bash "$HOME/tbh-appraiser/scripts/phone_runner.sh" >/dev/null 2>&1 &
