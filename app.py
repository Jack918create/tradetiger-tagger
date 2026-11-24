"""
TradeTiger Chart Auto-Tagger & Telegram Storage
------------------------------------------------
Bulk-upload TradeTiger screenshots -> detect the pale-yellow header bar ->
OCR the ticker (offline, Tesseract) -> send the full-resolution screenshot
to your private Telegram chat with the caption  #TICKER.

Stack: streamlit + pillow + numpy + requests + pytesseract (+ tesseract-ocr
system package).  No OpenCV, no external OCR APIs.
"""

import hashlib
import io
import os
import re
import time
from pathlib import Path

import numpy as np
import pytesseract
import requests
import streamlit as st
from PIL import Image, ImageOps

st.set_page_config(page_title="TradeTiger Auto-Tagger", page_icon="🐯", layout="wide")

# ----------------------------------------------------------------------------
# Tunable settings (edit here if your TradeTiger theme/colours differ)
# ----------------------------------------------------------------------------
HEADER_RGB = (250, 248, 215)   # pale-yellow/cream header bar colour
TOL_RG = 12                    # allowed deviation for R and G channels
TOL_B = 20                     # allowed deviation for B channel
SEARCH_FRACTION = 0.5          # only look for the bar in the top 50% of the image
MIN_BAR_HEIGHT = 8             # px, ignore thinner yellow lines
MAX_BAR_HEIGHT = 90            # px, ignore big yellow blocks
UPSCALE = 3                    # crop is enlarged 3x before OCR
TELEGRAM_API = "https://api.telegram.org"

# Tesseract page-segmentation configs tried in order (plain psm 7 first
# because it keeps the spaces that we rely on to isolate the ticker).
WHITELIST = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.@:&-_"
OCR_CONFIGS = [
    "--oem 3 --psm 7",
    "--oem 3 --psm 6",
    f"--oem 3 --psm 7 -c tessedit_char_whitelist={WHITELIST}",
]

# Where is the tesseract binary?  On Streamlit Cloud it is on PATH (packages.txt).
# Locally on Windows you can set TESSERACT_CMD or install to the default path.
if os.environ.get("TESSERACT_CMD"):
    pytesseract.pytesseract.tesseract_cmd = os.environ["TESSERACT_CMD"]
elif os.name == "nt":
    _win = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    if Path(_win).exists():
        pytesseract.pytesseract.tesseract_cmd = _win

# "NSE." / "BSE." prefix (tolerates common OCR slips such as N5E, comma/colon)
PREFIX_START = re.compile(r"^[^A-Z0-9]*(?:N[S5]E|B[S5]E)(?:\s*[.,:;_|]\s*|\s+)")
PREFIX_ANY = re.compile(r"(?:^|[^A-Z0-9])(?:NSE|BSE)\s*[.,:;_|]\s*")


# ----------------------------------------------------------------------------
# Helpers: secrets, tesseract
# ----------------------------------------------------------------------------
def get_secrets():
    """Return (bot_token, chat_id) from st.secrets, or (None, None)."""
    try:
        token = str(st.secrets["BOT_TOKEN"]).strip()
        chat_id = str(st.secrets["CHAT_ID"]).strip()
        if token and chat_id:
            return token, chat_id
    except Exception:
        pass
    return None, None


@st.cache_resource
def tesseract_version():
    try:
        return str(pytesseract.get_tesseract_version())
    except Exception:
        return None


# ----------------------------------------------------------------------------
# Image analysis: header-bar detection (pure NumPy)
# ----------------------------------------------------------------------------
def split_runs(idx, max_gap):
    """Split a sorted 1-D index array into runs separated by gaps > max_gap."""
    if idx.size == 0:
        return []
    breaks = np.where(np.diff(idx) > max_gap + 1)[0] + 1   # [0] -> the array, not the tuple
    return np.split(idx, breaks)


def find_header_bar(rgb_arr):
    """
    Locate the pale-yellow header bar in the upper half of the screenshot.
    Returns (x0, y0, x1, y1) of the *inside* of the bar, or None.
    The left watchlist and the right price axis are ignored automatically
    because they are not yellow.
    """
    h, w, _ = rgb_arr.shape
    limit = max(1, int(h * SEARCH_FRACTION))
    top = rgb_arr[:limit].astype(np.int16)

    mask = (
        (np.abs(top[:, :, 0] - HEADER_RGB[0]) <= TOL_RG)
        & (np.abs(top[:, :, 1] - HEADER_RGB[1]) <= TOL_RG)
        & (np.abs(top[:, :, 2] - HEADER_RGB[2]) <= TOL_B)
    )

    # Rows that contain a decent amount of yellow
    row_counts = mask.sum(axis=1)
    min_row_px = max(80, int(w * 0.12))
    rows = np.where(row_counts >= min_row_px)[0]
    if rows.size == 0:
        return None

    # Group rows into horizontal bands; pick the band with the most yellow
    best, best_score = None, -1
    for run in split_runs(rows, max_gap=2):
        height = int(run[-1] - run[0] + 1)
        if height < MIN_BAR_HEIGHT or height > MAX_BAR_HEIGHT:
            continue
        score = int(row_counts[run].sum())
        if score > best_score:
            best, best_score = run, score
    if best is None:
        return None

    y0, y1 = int(best[0]), int(best[-1]) + 1

    # Inside that band, find the widest continuous yellow stretch (columns)
    col_frac = mask[y0:y1].mean(axis=0)
    cols = np.where(col_frac >= 0.35)[0]
    col_runs = split_runs(cols, max_gap=15)
    if not col_runs:
        return None
    widest = max(col_runs, key=len)
    x0, x1 = int(widest[0]), int(widest[-1]) + 1
    if x1 - x0 < 60:
        return None

    # Crop strictly inside the bar
    dy = 2 if (y1 - y0) >= 12 else 1
    dx = 4
    return (x0 + dx, y0 + dy, x1 - dx, y1 - dy)


def make_ocr_variants(crop_rgb):
    """Upscale 3x and build two OCR-friendly versions (greyscale, black/white)."""
    w, h = crop_rgb.size
    big = crop_rgb.resize((w * UPSCALE, h * UPSCALE), Image.Resampling.LANCZOS)
    gray = ImageOps.autocontrast(big.convert("L"))
    v_gray = ImageOps.expand(gray, border=24, fill=255)

    arr = np.asarray(gray)
    thr = (int(arr.min()) + int(arr.max())) // 2
    bw = Image.fromarray(np.where(arr < thr, 0, 255).astype(np.uint8))
    v_bw = ImageOps.expand(bw, border=24, fill=255)
    return big, [v_gray, v_bw]


# ----------------------------------------------------------------------------
# Ticker parsing
# ----------------------------------------------------------------------------
def extract_symbol(line, require_prefix=True, anywhere=False):
    """
    'NSE.JIOFIN 10@219.70 15:59:52 25'  ->  'JIOFIN'
    Removes NSE./BSE., then keeps everything before the first space or '@'.
    """
    line = (line or "").strip().upper()
    if not line:
        return None

    if anywhere:
        m = PREFIX_ANY.search(line)
    else:
        m = PREFIX_START.match(line)

    if m:
        rest = line[m.end():]
    elif require_prefix:
        return None
    else:
        rest = line

    rest = rest.strip()
    if not rest:
        return None
    token = re.split(r"[\s@]", rest)[0]                    # [0] -> first piece only
    token = re.sub(r"[^A-Z0-9&\-_]", "", token).strip("-_&")
    if 2 <= len(token) <= 20 and re.search(r"[A-Z]", token):
        return token
    return None


def make_hashtag(symbol):
    """Telegram hashtags allow only letters, digits and underscore."""
    tag = symbol.upper().replace("&", "").replace("-", "_")
    tag = re.sub(r"[^A-Z0-9_]", "", tag)
    return f"#{tag}"


def png_bytes(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def analyze_image(data):
    """
    Returns dict: symbol, raw (OCR text seen), crop_png, note.
    symbol is None when OCR failed (-> manual fallback).
    """
    img = Image.open(io.BytesIO(data))
    img.load()
    rgb = img.convert("RGB")
    arr = np.asarray(rgb)

    result = {"symbol": None, "raw": "", "crop_png": None, "note": ""}
    seen = []

    # --- Primary path: yellow header bar ---
    box = find_header_bar(arr)
    if box is not None:
        crop = rgb.crop(box)
        big, variants = make_ocr_variants(crop)
        result["crop_png"] = png_bytes(big)
        for cfg in OCR_CONFIGS:
            for variant in variants:
                text = pytesseract.image_to_string(variant, config=cfg).strip()
                if text:
                    seen.append(text)
                for line in text.splitlines():
                    sym = extract_symbol(line, require_prefix=True)
                    if sym:
                        result.update(symbol=sym, raw=text)
                        return result
        result["note"] = "Header bar found, but no NSE./BSE. ticker could be read."
    else:
        result["note"] = "Yellow header bar not found."

    # --- Fallback: OCR the top-centre strip and search for 'NSE.xxx' anywhere ---
    h, w = rgb.size[1], rgb.size[0]
    strip = rgb.crop((int(w * 0.15), 0, int(w * 0.90), max(40, int(h * 0.20))))
    strip = strip.resize((strip.width * 2, strip.height * 2), Image.Resampling.LANCZOS)
    strip = ImageOps.autocontrast(strip.convert("L"))
    text = pytesseract.image_to_string(strip, config="--oem 3 --psm 6").strip()
    if text:
        seen.append(text)
    for line in text.splitlines():
        sym = extract_symbol(line, require_prefix=True, anywhere=True)
        if sym:
            result.update(symbol=sym, raw=text, note="Detected via fallback strip.")
            return result

    result["raw"] = "\n---\n".join(dict.fromkeys(seen))[:600]
    return result


# ----------------------------------------------------------------------------
# Telegram
# ----------------------------------------------------------------------------
def send_document(token, chat_id, data, filename, caption):
    """Send the original file via sendDocument. Returns (ok, message)."""
    url = f"{TELEGRAM_API}/bot{token}/sendDocument"
    last_err = "Unknown error"
    for _ in range(5):
        try:
            resp = requests.post(
                url,
                data={"chat_id": chat_id, "caption": caption},
                files={"document": (filename, data)},
                timeout=90,
            )
        except requests.RequestException as exc:
            last_err = f"Network error: {exc}"
            time.sleep(2)
            continue

        try:
            payload = resp.json()
        except ValueError:
            payload = {}

        if payload.get("ok"):
            return True, "Sent"
        if resp.status_code == 429:                         # rate limited -> wait & retry
            wait = int(payload.get("parameters", {}).get("retry_after", 5))
            time.sleep(wait + 1)
            last_err = "Rate limited by Telegram"
            continue
        return False, payload.get("description") or resp.text[:200]
    return False, last_err


def telegram_ping(token, chat_id):
    try:
        resp = requests.post(
            f"{TELEGRAM_API}/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": "✅ TradeTiger Auto-Tagger connected."},
            timeout=30,
        )
        payload = resp.json()
        if payload.get("ok"):
            return True, "Test message sent. Check Telegram."
        return False, payload.get("description", "Unknown error")
    except Exception as exc:
        return False, str(exc)


# ----------------------------------------------------------------------------
# Session state
# ----------------------------------------------------------------------------
ss = st.session_state
ss.setdefault("sent_hashes", set())
ss.setdefault("pending", [])
ss.setdefault("results", [])
ss.setdefault("uploader_key", 0)

token, chat_id = get_secrets()

# ----------------------------------------------------------------------------
# Sidebar
# ----------------------------------------------------------------------------
with st.sidebar:
    st.header("⚙️ Settings")

    if token:
        st.success("Telegram secrets loaded")
    else:
        st.error("BOT_TOKEN / CHAT_ID missing in Secrets")

    ver = tesseract_version()
    if ver:
        st.caption(f"Tesseract OCR {ver} ready")
    else:
        st.error("Tesseract not found. Check packages.txt (cloud) or install it (local).")

    dry_run = st.checkbox("Test mode (detect only, don't send)", value=False)
    delay = st.slider("Pause between sends (seconds)", 0.0, 5.0, 1.0, 0.5,
                      help="Telegram limits groups to ~20 messages/minute. "
                           "The app also auto-waits if Telegram asks it to.")

    if st.button("📨 Send test message", disabled=not token):
        ok, msg = telegram_ping(token, chat_id)
        (st.success if ok else st.error)(msg)

    if st.button("🧹 Forget sent history"):
        ss.sent_hashes = set()
        st.info("History cleared. Re-uploading will send again.")

# ----------------------------------------------------------------------------
# Main UI
# ----------------------------------------------------------------------------
st.title("🐯 TradeTiger Chart Auto-Tagger")
st.caption("Upload screenshots → ticker read from the yellow header → sent to Telegram as `#TICKER`.")

files = st.file_uploader(
    "Drop all your TradeTiger screenshots here",
    type=["png", "jpg", "jpeg", "bmp", "webp"],
    accept_multiple_files=True,
    key=f"uploader_{ss.uploader_key}",
)

col_a, col_b, _ = st.columns([1, 1, 4])
run_clicked = col_a.button(
    f"🚀 Process {len(files)} file(s)" if files else "🚀 Process",
    type="primary",
    disabled=not files or (not token and not dry_run) or not tesseract_version(),
)
if col_b.button("🗑️ Clear uploads", disabled=not files):
    ss.uploader_key += 1
    st.rerun()

# ----------------------------------------------------------------------------
# Batch processing
# ----------------------------------------------------------------------------
if run_clicked and files:
    ss.pending = []
    ss.results = []
    total = len(files)
    progress = st.progress(0.0, text="Starting…")

    for i, f in enumerate(files, start=1):
        data = f.getvalue()
        digest = hashlib.md5(data).hexdigest()
        name = f.name
        progress.progress((i - 1) / total, text=f"Processing {i}/{total}: {name}")

        if digest in ss.sent_hashes and not dry_run:
            ss.results.append({"File": name, "Ticker": "", "Status": "Skipped",
                               "Details": "Already sent earlier in this session"})
            continue

        try:
            info = analyze_image(data)
        except Exception as exc:
            info = {"symbol": None, "raw": "", "crop_png": None,
                    "note": f"Could not process image: {exc}"}

        symbol = info["symbol"]
        if symbol:
            tag = make_hashtag(symbol)
            if dry_run:
                ss.results.append({"File": name, "Ticker": symbol, "Status": "Detected (test)",
                                   "Details": f"Would send with caption {tag}"})
                continue

            ok, msg = send_document(token, chat_id, data, f"{symbol}_{Path(name).name}", tag)
            if ok:
                ss.sent_hashes.add(digest)
                ss.results.append({"File": name, "Ticker": symbol, "Status": "Sent",
                                   "Details": f"Caption {tag}"})
                if delay:
                    time.sleep(delay)
                continue

            ss.results.append({"File": name, "Ticker": symbol, "Status": "Needs manual tag",
                               "Details": f"Telegram error: {msg}"})
            reason = f"Ticker read as {symbol}, but Telegram failed: {msg}"
        else:
            ss.results.append({"File": name, "Ticker": "", "Status": "Needs manual tag",
                               "Details": info["note"]})
            reason = info["note"]

        ss.pending.append({
            "id": digest, "name": name, "data": data, "reason": reason,
            "raw": info["raw"], "crop_png": info["crop_png"], "guess": symbol or "",
        })

    progress.progress(1.0, text=f"Done — {total} file(s) processed")

# ----------------------------------------------------------------------------
# Results summary
# ----------------------------------------------------------------------------
for kind, text in ss.pop("flash", []):
    (st.success if kind == "ok" else st.error)(text)

if ss.results:
    n_sent = sum(r["Status"] == "Sent" for r in ss.results)
    n_test = sum(r["Status"] == "Detected (test)" for r in ss.results)
    n_skip = sum(r["Status"] == "Skipped" for r in ss.results)
    n_fail = sum(r["Status"] == "Needs manual tag" for r in ss.results)

    st.subheader("📊 Summary")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Sent to Telegram", n_sent)
    m2.metric("Detected (test mode)", n_test)
    m3.metric("Skipped (duplicates)", n_skip)
    m4.metric("Need manual tag", n_fail)

    if n_fail == 0 and (n_sent or n_test):
        st.success("All images were tagged automatically. 🎉")
    with st.expander("Full results table", expanded=n_fail > 0):
        st.dataframe(ss.results, hide_index=True)

# ----------------------------------------------------------------------------
# Manual fallback tagging
# ----------------------------------------------------------------------------
if ss.pending:
    st.subheader(f"✍️ Manual tagging needed ({len(ss.pending)})")
    st.caption("Type the ticker for each chart (e.g. `JIOFIN`, or paste `NSE.JIOFIN 10@219`) "
               "and send. Leave a box empty to skip that chart for now.")

    with st.form("manual_form"):
        for item in ss.pending:
            c1, c2 = st.columns([2, 3])
            with c1:
                st.image(item["data"], caption=item["name"], width=420)
            with c2:
                st.caption(item["reason"])
                if item["crop_png"]:
                    st.image(item["crop_png"], caption="Header crop the OCR saw", width=420)
                if item["raw"]:
                    st.code(item["raw"], language=None)
                st.text_input("Ticker", value=item["guess"], key=f"manual_{item['id']}",
                              placeholder="e.g. JIOFIN")
            st.divider()
        submitted = st.form_submit_button("📤 Send tagged charts to Telegram", type="primary")

    if submitted:
        if not token:
            st.error("Telegram secrets are missing, so nothing can be sent.")
        else:
            flash, remaining = [], []
            with st.spinner("Sending…"):
                for item in ss.pending:
                    typed = str(ss.get(f"manual_{item['id']}", "")).strip()
                    sym = extract_symbol(typed, require_prefix=False) if typed else None
                    if not sym:
                        if typed:
                            flash.append(("err", f"{item['name']}: “{typed}” is not a valid ticker."))
                        remaining.append(item)
                        continue
                    tag = make_hashtag(sym)
                    ok, msg = send_document(token, chat_id, item["data"],
                                            f"{sym}_{Path(item['name']).name}", tag)
                    if ok:
                        ss.sent_hashes.add(item["id"])
                        flash.append(("ok", f"{item['name']} → {tag}"))
                        if delay:
                            time.sleep(delay)
                    else:
                        flash.append(("err", f"{item['name']}: {msg}"))
                        remaining.append(item)
            ss.pending = remaining
            ss.flash = flash
            st.rerun()
