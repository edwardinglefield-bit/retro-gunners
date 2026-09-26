# Setup (≈45 min total, once)

Order matters only for the first two steps. Each social account is optional and can be added later: re-run `scripts/setup.sh` and it only asks for what's missing.

## 1. Accounts to create (on your phone is fine)
Pick a handle. The config uses **retro.gunner**; rename in `config.yaml → brand`. Avoid "Arsenal" in the handle because club trademarks get handles reclaimed.

- **Instagram**: create the account → Settings → *Account type and tools* → switch to **Professional (Creator)**. No Facebook Page needed.
- **Threads**: sign in with that Instagram account.
- **X**: create the account.

## 2. Keys (≈10 min each)

| Secret | Where |
|---|---|
| `OPENAI_API_KEY` | platform.openai.com → API keys. Image models need **Organization verification** (Settings → Organization → Verify), which is a one-time ID check. Add ~$10 credit. |
| `TELEGRAM_BOT_TOKEN` | Telegram → @BotFather → `/newbot`. The setup script captures your chat id for you. |
| `X_*` (4 values) | console.x.com → create a Project + App → *User authentication settings*: **Read and write** → *Keys and tokens*: API Key/Secret + **Access Token/Secret** (generate **after** setting Read and write). Buy a little pay-per-use credit: posting costs ~$0.015/post. |
| `IG_ACCESS_TOKEN` | developers.facebook.com → Create app → use case **"Manage messaging & content on Instagram"** → *API setup with Instagram login* → add your IG account (accept the tester invite in Instagram: Settings → *Website permissions / Apps*) → **Generate token**. This is a 60-day token, and the pipeline refreshes it on its own. |
| `THREADS_ACCESS_TOKEN` | Same Meta app → add use case **"Access the Threads API"** → permissions `threads_basic` + `threads_content_publish` → *Roles* → add your Threads account as **Threads Tester** → accept in Threads app: Settings → Account → *Website permissions* → back in the dashboard: **User Token Generator**. It's also refreshed automatically. |

The Meta app can stay in Development mode: you're only posting to your own accounts, so no App Review is needed.

## 3. Run the script (Mac)
```bash
brew install gh          # if needed
cd retro-gunners
./scripts/setup.sh       # creates a PUBLIC repo, stores secrets, runs `doctor`
```
The repo is public on purpose: Actions minutes are free and Instagram/Threads fetch images from `raw.githubusercontent.com` on the `data` branch. Secrets stay private. Stored tokens are encrypted.

## 4. Check
Telegram gets a **doctor** report (✅/❌ per integration). Within ~2 hours the first two previews arrive. Tap **✅ Post** / **🔁 Redo** / **✖ Skip**.

## If something's ❌
- **arsenal.com blocked** from GitHub's servers: the pipeline keeps running. Send photos to the bot yourself (save from IG/X and share them to the bot) until it's fixed. Tell Claude and it'll add a fallback source.
- **OpenAI 403 / "organization must be verified"**: finish org verification (step 2).
- **Meta token error 190**: the token was pasted with spaces or expired. Generate a new one and re-run the script.
