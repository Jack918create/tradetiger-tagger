# 🐯 TradeTiger Chart Auto-Tagger & Telegram Storage

Bulk-upload your marked **Sharekhan TradeTiger** chart screenshots. The app reads the stock ticker from the pale-yellow header bar (offline OCR), then sends each full-resolution screenshot to your private Telegram chat with the caption `#TICKER`.

When a **Zerodha Kite** alert fires on your phone, open Telegram, search `JIOFIN`, and your marked chart is right there, even when your PC is off.

```
TradeTiger screenshots ──► Streamlit app ──► OCR "NSE.JIOFIN 10@219.70 …" ──► "JIOFIN"
                                         └─► Telegram sendDocument, caption "#JIOFIN"
```

## Repository layout

```
your-repo/
├── app.py                         # the Streamlit app
├── requirements.txt               # Python packages
├── packages.txt                   # system package (tesseract-ocr) for Streamlit Cloud
├── .gitignore
├── .streamlit/
│   └── secrets.toml.example       # template only (real secrets go in Streamlit Cloud)
└── README.md
```

**Why this stack:** `tesseract-ocr` + `pytesseract` + `Pillow` + `NumPy` run fully offline on Streamlit Cloud's Linux servers. There is no OpenCV (`libGL.so.1` crash) and no external OCR API (empty/JSON errors).

---

## Step 1: Create your Telegram bot (`@BotFather`)

1. Open Telegram and search for **`@BotFather`** (blue tick). Tap **Start**.
2. Send `/newbot`.
3. Enter a display name, e.g. `My Chart Vault`.
4. Enter a username ending in `bot`, e.g. `my_chart_vault_bot`.
5. BotFather replies with a token like:
   `123456789:AAH8x...long-string...`
   **That is your `BOT_TOKEN`. Keep it secret.**
6. Open your new bot (tap the `t.me/...` link) and press **Start**. A bot cannot message you until you do this.

## Step 2: Get your exact `CHAT_ID`

### Option A: Private chat with the bot (simplest)

1. Send any message (e.g. `hi`) to your bot.
2. In a browser open (replace the token):
   `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates`
3. Find `"chat":{"id":123456789,...}`. That number is your `CHAT_ID`.

### Option B: Private group (nice for organising/searching)

1. Create a new Telegram group and add your bot as a member.
2. Send a message in the group that mentions the bot, e.g. `/start@my_chart_vault_bot`.
3. Open the same `getUpdates` URL. In the `"chat"` block look for the `id`. Group IDs are **negative**, often starting with `-100`, e.g. `-1001234567890`. Copy the minus sign too.
4. If `getUpdates` shows `"result":[]`, send another message in the group and refresh. If it stays empty, make sure the bot is in the group, or turn off "Group Privacy" via BotFather → `/mybots` → your bot → *Bot Settings* → *Group Privacy*.

### Test it before going further

Paste this into a browser (fill in your values). You should get a "hello" in Telegram:

```
https://api.telegram.org/bot<BOT_TOKEN>/sendMessage?chat_id=<CHAT_ID>&text=hello
```

## Step 3: Create the GitHub repository

1. Sign in at [github.com](https://github.com) → **New repository**.
2. Name it e.g. `tradetiger-tagger`. **Private** works fine with Streamlit Cloud.
3. Add these files to the **root** of the repo (use *Add file → Create new file* and paste the contents, or *Upload files*):
   - `app.py`
   - `requirements.txt`
   - `packages.txt`
   - `.gitignore`
   - `README.md`
   - `.streamlit/secrets.toml.example`. When creating a file in the web UI, typing `.streamlit/` in the name box creates the folder automatically.
4. ⚠️ **Never commit your real token.** Only the `.example` file goes to GitHub. The real secrets are entered inside Streamlit Cloud (Step 5).

## Step 4: Deploy on Streamlit Community Cloud

1. Go to [share.streamlit.io](https://share.streamlit.io) and sign in with GitHub.
2. Click **Create app** → **Deploy a public app from GitHub** (this also works for private repos you have authorised).
3. Choose your **repository**, **branch** `main`, and **main file path** `app.py`.
4. Click **Advanced settings**:
   - **Python version:** 3.11 or 3.12.
   - **Secrets:** paste the block from Step 5.
5. Click **Deploy**. The first build installs `tesseract-ocr` (from `packages.txt`) and the Python packages, which takes a couple of minutes.

## Step 5: Add your Secrets

In **Advanced settings → Secrets** (or later: app **⋮ → Settings → Secrets**), paste:

```toml
BOT_TOKEN = "123456789:AAH8x_your_real_token_here"
CHAT_ID   = "-1001234567890"
```

Keep the quotes, and keep the minus sign for group IDs. Save, and the app restarts. In the app's **sidebar** you should see *"Telegram secrets loaded"* and *"Tesseract OCR … ready"*. Click **📨 Send test message** to confirm delivery.

---

## Daily usage

### On your PC (TradeTiger)

1. Mark your setup on the chart (trendlines, levels, etc.). **Make sure the yellow header line `NSE.JIOFIN 10@219.70 …` is visible** in the screenshot.
2. Take a screenshot: `Win + Shift + S` (Snipping Tool), `Alt + PrtScn`, or the Windows Game Bar. Save as **PNG** into one folder. Full-window and half-window/split-screen shots both work.
3. Open your Streamlit app URL (bookmark it).
4. Drag all screenshots into the uploader (`Ctrl + A` in the folder picks everything), then click **🚀 Process**.
5. Watch the progress bar. Read the summary:
   - **Sent to Telegram**: done.
   - **Need manual tag**: scroll down, type the ticker under each thumbnail (the app shows the header crop it OCR'd), and press **Send tagged charts**.
6. Click **🗑️ Clear uploads** before the next batch.

### On your phone (Kite alert → chart)

1. Kite alert fires for e.g. `JIOFIN`.
2. Open Telegram → open your bot chat/group → tap the 🔍 search icon → type `JIOFIN`. Or tap any `#JIOFIN` tag in a message to see every chart for that stock.
3. Tap the file to view your marked chart at full resolution. Tip: pin the chat to the top of Telegram.

Telegram stores the files in the cloud, so this works 24/7, even if the PC and Streamlit app are off.

---

## How it works

1. **Find the header:** a pure-NumPy colour mask finds pixels near RGB `(250, 248, 215)` in the **top half** of the image. It picks the biggest horizontal yellow band and then its widest continuous stretch, so the left watchlist and the right price axis (not yellow) are ignored.
2. **Crop & OCR:** crops 2–4 px *inside* the bar, upscales 3×, runs Tesseract (greyscale and black/white variants, several modes).
3. **Parse:** `NSE.JIOFIN 10@219.70 15:59:52 25` → strip `NSE.`/`BSE.` → `re.split(r"[\s@]", …)[0]` → `JIOFIN`.
4. **Send:** original file bytes go via `sendDocument` with caption `#JIOFIN`. The filename becomes `JIOFIN_<original name>`, so plain-text search finds it too.
5. **Safety nets:** a fallback OCR strip if the bar isn't found, duplicate protection (same file isn't sent twice in a session), automatic retry when Telegram rate-limits, and manual tagging for failures.

### Notes & limits

- **Hashtags** can only contain letters, digits and `_`. `BAJAJ-AUTO` is tagged `#BAJAJ_AUTO` and `M&M` becomes `#MM`.
- **Telegram groups** allow ~20 messages/minute. For 100+ charts the app paces itself (sidebar slider) and waits whenever Telegram asks it to. A private 1-to-1 chat with the bot is more lenient.
- Telegram bots can upload files up to 50 MB. Screenshots are far smaller.
- Streamlit Community Cloud has limited memory. If you see slowdowns with very large batches, upload in chunks of ~50–100.
- **Test mode** (sidebar) shows what would be detected without sending anything.
- If your TradeTiger theme uses a slightly different yellow, edit `HEADER_RGB`, `TOL_RG` and `TOL_B` at the top of `app.py`.

## Troubleshooting

| Problem | Fix |
|---|---|
| Sidebar says *Tesseract not found* | Make sure `packages.txt` is in the **repo root** and contains exactly `tesseract-ocr`, then reboot the app. |
| `Unauthorized` | `BOT_TOKEN` is wrong or has stray spaces/quotes. |
| `Bad Request: chat not found` | Wrong `CHAT_ID`, or you haven't pressed **Start** on the bot / added it to the group. |
| `Forbidden: bot was blocked / kicked` | Unblock the bot, or re-add it to the group. |
| Yellow header "not found" | The header isn't visible in the screenshot, or the colour differs, so adjust the constants above. Use manual tagging meanwhile. |
| Wrong letter (e.g. `I` vs `1`) | Fix it in the manual box. For recurring problems, make the TradeTiger header font larger. |
| App is asleep | Free apps hibernate after inactivity. Click **Wake up**. Your charts in Telegram are unaffected. |

## Optional: run locally (Windows)

1. Install Python 3.11+ and the [Tesseract Windows installer](https://github.com/UB-Mannheim/tesseract/wiki) (default path `C:\Program Files\Tesseract-OCR`).
2. `pip install -r requirements.txt`
3. Copy `.streamlit/secrets.toml.example` → `.streamlit/secrets.toml` and fill in real values.
4. `streamlit run app.py`

Because the app is pure Python plus the Tesseract executable, there is no OpenCV or ONNX dependency to be blocked.
