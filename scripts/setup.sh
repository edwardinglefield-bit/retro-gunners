#!/usr/bin/env bash
# One-time setup: creates the GitHub repo, stores secrets, runs a health check.
# Safe to re-run: existing secrets are kept unless you choose to replace them.
set -euo pipefail
cd "$(dirname "$0")/.."

command -v gh >/dev/null || { echo "Install the GitHub CLI first: brew install gh"; exit 1; }
gh auth status >/dev/null 2>&1 || gh auth login -s workflow
REPO_NAME="${1:-retro-gunners}"

if ! git rev-parse --git-dir >/dev/null 2>&1; then
  git init -q -b main && git add -A && git commit -qm "Retro Gunners pipeline"
fi
if ! git remote get-url origin >/dev/null 2>&1; then
  gh repo create "$REPO_NAME" --public --source=. --push
else
  git push -u origin main
fi

have() { gh secret list | awk '{print $1}' | grep -qx "$1"; }

ask() {  # ask NAME "prompt"
  local name=$1 prompt=$2 val a
  if have "$name"; then
    read -rp "$name is already set. Replace? [y/N] " a
    [[ "$a" == "y" ]] || return 0
  fi
  read -rsp "$prompt (Enter to skip): " val; echo
  [[ -n "$val" ]] && printf '%s' "$val" | gh secret set "$name" && echo "  saved $name"
  return 0
}

echo; echo "== Core =="
have STATE_KEY || { python3 -c "import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())" | gh secret set STATE_KEY; echo "  generated STATE_KEY"; }
ask OPENAI_API_KEY "OpenAI API key (sk-...)"

echo; echo "== Telegram review bot =="
if ! have TELEGRAM_BOT_TOKEN; then
  read -rsp "Bot token from @BotFather (Enter to skip): " TG; echo
  if [[ -n "$TG" ]]; then
    printf '%s' "$TG" | gh secret set TELEGRAM_BOT_TOKEN
    read -rp "Now open your bot in Telegram, send it /start, then press Enter here... " _
    FOUND=$(curl -s "https://api.telegram.org/bot$TG/getUpdates" | python3 -c \
      "import sys,json;r=json.load(sys.stdin).get('result',[]);m=[u['message'] for u in r if u.get('message',{}).get('text','').startswith('/start') and u['message']['chat']['type']=='private'];print(f\"{m[-1]['chat']['id']} @{m[-1]['from'].get('username','?')} {m[-1]['from'].get('first_name','')}\" if m else '')")
    if [[ -n "$FOUND" ]]; then
      read -rp "  Found ${FOUND#* }. Is that you? [Y/n] " ok
      if [[ "$ok" != "n" ]]; then printf '%s' "${FOUND%% *}" | gh secret set TELEGRAM_OWNER_ID; echo "  owner saved"; fi
    else echo "  Couldn't find your /start. Message the bot later; it replies with your id. Save it as TELEGRAM_OWNER_ID."; fi
  fi
fi

echo; echo "== Social accounts (skip any you haven't set up yet; re-run this script later) =="
ask X_API_KEY        "X API key (consumer key)"
ask X_API_SECRET     "X API key secret"
ask X_ACCESS_TOKEN   "X access token (Read and Write)"
ask X_ACCESS_SECRET  "X access token secret"
ask THREADS_ACCESS_TOKEN "Threads long-lived access token"
ask IG_ACCESS_TOKEN  "Instagram access token (Instagram API with Instagram Login)"

echo; echo "Running a health check; results arrive in Telegram and in the Actions log."
gh workflow enable tick.yml >/dev/null 2>&1 || true   # in case it was paused until the keys were in
gh workflow run tick.yml -f command=doctor || echo "(Trigger it from the Actions tab if this failed.)"
echo "Done. The pipeline now runs every 15 minutes."
