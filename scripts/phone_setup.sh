#!/usr/bin/env bash
# One-command Termux setup for the LOCAL price runner (see phone_runner.sh).
# On the phone, inside Termux:
#   curl -s https://raw.githubusercontent.com/TKH54/tbh-appraiser/main/scripts/phone_setup.sh | bash
# It installs python+git, clones the repo shallow, asks for a GitHub token
# (fine-grained PAT, Contents:Read&Write on TKH54/tbh-appraiser ONLY — stored
# only inside Termux's private storage, never committed), wires Termux:Boot so
# the runner survives reboots, and starts the loop immediately.
set -eu

REPO_DIR="$HOME/tbh-appraiser"
REPO_PATH="TKH54/tbh-appraiser"

echo "== TBH price runner setup =="

echo "[1/5] packages (python, git)..."
pkg update -y >/dev/null 2>&1 || true
pkg install -y python git >/dev/null
pip install -q requests

echo "[2/5] GitHub token..."
# stdin is the curl pipe, so read the token from the terminal directly
printf "GitHubトークンを貼り付けてEnter (入力は表示されません): "
read -rs TOKEN < /dev/tty
echo
[ -n "$TOKEN" ] || { echo "トークンが空です。中断。"; exit 1; }
AUTH_URL="https://x-access-token:${TOKEN}@github.com/${REPO_PATH}.git"
if ! git ls-remote "$AUTH_URL" main >/dev/null 2>&1; then
    echo "NG: このトークンでリポジトリにアクセスできません。権限(Contents: Read and write)と対象リポジトリを確認してください。"
    exit 1
fi
echo "OK: トークン確認できました"

echo "[3/5] repo clone..."
if [ -d "$REPO_DIR/.git" ]; then
    git -C "$REPO_DIR" remote set-url origin "$AUTH_URL"
    git -C "$REPO_DIR" fetch --depth 1 origin main
    git -C "$REPO_DIR" reset --hard origin/main
else
    git clone --depth 1 "$AUTH_URL" "$REPO_DIR"
fi
git -C "$REPO_DIR" config user.name "price-bot-phone"
git -C "$REPO_DIR" config user.email "actions@users.noreply.github.com"

echo "[4/5] 再起動後の自動開始 (Termux:Boot)..."
mkdir -p "$HOME/.termux/boot" "$HOME/.tbh"
cat > "$HOME/.termux/boot/tbh-runner.sh" <<'EOF'
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock
nohup bash "$HOME/tbh-appraiser/scripts/phone_runner.sh" >/dev/null 2>&1 &
EOF
chmod +x "$HOME/.termux/boot/tbh-runner.sh"

echo "[5/5] いま起動..."
termux-wake-lock 2>/dev/null || true
pkill -f phone_runner.sh 2>/dev/null || true
nohup bash "$REPO_DIR/scripts/phone_runner.sh" >/dev/null 2>&1 &

echo
echo "== 完了！バックグラウンドで10分ごとに価格を更新します =="
echo "状態確認:   tail -n 20 ~/.tbh/runner.log"
echo "止める:     pkill -f phone_runner.sh"
echo "再開:       nohup bash ~/tbh-appraiser/scripts/phone_runner.sh >/dev/null 2>&1 &"
echo
echo "※ Termux:Boot アプリを一度開いておくと、スマホ再起動後も自動で再開します"
echo "※ Androidの設定で Termux の電池最適化を『最適化しない』にしてください"
