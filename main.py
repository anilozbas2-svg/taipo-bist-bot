import os
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import yfinance as yf

# =========================
# CONFIG
# =========================
TZ = ZoneInfo("Europe/Istanbul")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
TARGET_CHAT_ID = os.getenv("CHAT_ID", "").strip()  # group chat id like -100...
MODE = os.getenv("MODE", "AUTO").strip().upper()   # AUTO or COMMAND

STATE_FILE = "state.json"
SYMBOLS_FILE = "bist100.txt"

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# =========================
# PICK + TRACK SETTINGS (TR)
# =========================
# 10:00-10:10 arası (TR) yakaladığı an 1 kere gönder, gün boyu sabit
PICK_START_HOUR = 10
PICK_START_MIN = 0
PICK_END_MIN = 10

EARLY_MIN_PCT = 0.15
EARLY_MAX_PCT = 0.80
PICK_COUNT = 3

# takip mesajları TR: 11:10 ... 17:10
TRACK_HOURS_TR = {11, 12, 13, 14, 15, 16, 17}
TRACK_MINUTE_TR = 10

REPLY_COOLDOWN_SEC = 20


# =========================
# IO HELPERS
# =========================
def load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path: str, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def ensure_files():
    if not os.path.exists(STATE_FILE):
        save_json(STATE_FILE, {
            "last_update_id": 0,
            "last_command_reply_ts": 0,
            "day": "",
            "watch": {
                "symbols": [],
                "baseline": {},
                "picked_at": ""
            },
            "sent_pick_message": False,
            "last_track_sent_key": ""  # "YYYY-MM-DD HH:MM"
        })


def load_symbols():
    if not os.path.exists(SYMBOLS_FILE):
        return []
    syms = []
    with open(SYMBOLS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            if not s.endswith(".IS"):
                s = s + ".IS"
            syms.append(s)
    # duplicate temizle
    return list(dict.fromkeys(syms))


# =========================
# TELEGRAM
# =========================
def send_message(text: str, chat_id: str = None):
    if not chat_id:
        chat_id = TARGET_CHAT_ID
    if not BOT_TOKEN or not chat_id:
        return False

    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True
    }
    try:
        r = requests.post(f"{TELEGRAM_API}/sendMessage", json=payload, timeout=20)
        return r.status_code == 200
    except Exception:
        return False


def get_updates(offset: int):
    if not BOT_TOKEN:
        return []
    params = {"timeout": 0, "offset": offset}
    try:
        r = requests.get(f"{TELEGRAM_API}/getUpdates", params=params, timeout=20)
        data = r.json()
        return data.get("result", []) if data.get("ok") else []
    except Exception:
        return []


def extract_message(update: dict):
    return update.get("message") or update.get("edited_message")


def msg_text(msg: dict):
    return (msg.get("text") or "").strip()


def msg_chat_id(msg: dict):
    chat = msg.get("chat") or {}
    return str(chat.get("id", ""))


def is_target_chat(msg: dict):
    cid = msg_chat_id(msg)
    return (TARGET_CHAT_ID and cid == str(TARGET_CHAT_ID))


# =========================
# TIME HELPERS (TR)
# =========================
def today_str_tr():
    return datetime.now(TZ).strftime("%Y-%m-%d")


def now_str_tr():
    return datetime.now(TZ).strftime("%d.%m.%Y %H:%M")


def now_key_minute():
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M")


def ensure_today_state(state):
    if state.get("day") != today_str_tr():
        state["day"] = today_str_tr()
        state["watch"] = {"symbols": [], "baseline": {}, "picked_at": ""}
        state["sent_pick_message"] = False
        state["last_track_sent_key"] = ""
    return state


def in_pick_window():
    n = datetime.now(TZ)
    if n.hour != PICK_START_HOUR:
        return False
    return PICK_START_MIN <= n.minute <= PICK_END_MIN


def is_track_time_now():
    n = datetime.now(TZ)
    return (n.hour in TRACK_HOURS_TR) and (n.minute == TRACK_MINUTE_TR)


def should_send_track_now(state):
    # Aynı dakika içinde 1 kez
    key = now_key_minute()
    return state.get("last_track_sent_key", "") != key


# =========================
# DATA
# =========================
def fetch_quote(symbol: str):
    """
    Returns dict:
      { symbol, price, prev_close, change_pct }
    """
    try:
        t = yf.Ticker(symbol)

        fi = getattr(t, "fast_info", None)
        price = None
        prev_close = None

        if fi:
            price = fi.get("last_price") or fi.get("lastPrice")
            prev_close = fi.get("previous_close") or fi.get("previousClose")

        # fallback: history
        if price is None or prev_close is None:
            hist2 = t.history(period="2d", interval="1d")
            if hist2 is not None and len(hist2) >= 2:
                prev_close = float(hist2["Close"].iloc[-2])
                price = float(hist2["Close"].iloc[-1])
            else:
                hist1 = t.history(period="1d", interval="1m")
                if hist1 is not None and len(hist1) >= 1:
                    price = float(hist1["Close"].iloc[-1])
                hist_close = t.history(period="2d", interval="1d")
                if hist_close is not None and len(hist_close) >= 2:
                    prev_close = float(hist_close["Close"].iloc[-2])

        if price is None or prev_close in (None, 0):
            return None

        change_pct = ((float(price) - float(prev_close)) / float(prev_close)) * 100.0
        return {
            "symbol": symbol,
            "price": round(float(price), 2),
            "prev_close": round(float(prev_close), 2),
            "change_pct": round(float(change_pct), 2),
        }
    except Exception:
        return None


def scan_quotes(symbols):
    out = []
    for sym in symbols:
        q = fetch_quote(sym)
        if q:
            out.append(q)
    return out


def pick_early_breakouts(quotes, n=3, lo=0.15, hi=0.80):
    # sadece erken kırılım bandı
    pool = [q for q in quotes if lo <= float(q["change_pct"]) <= hi]
    # band içinde en güçlüden seç
    pool_sorted = sorted(pool, key=lambda x: x["change_pct"], reverse=True)
    return pool_sorted[:n]


# =========================
# FORMAT
# =========================
def clean_sym(sym: str):
    return sym.replace(".IS", "")


def trend_emoji(pct: float):
    return "🟢⬆️" if pct >= 0 else "🔴⬇️"


def pct_str(pct: float):
    return f"+{pct:.2f}%" if pct >= 0 else f"{pct:.2f}%"


def build_pick_message(picks, picked_at):
    lines = []
    lines.append("✅ 10:00–10:10 Erken Kırılım – PREMIUM v1")
    lines.append("")
    lines.append("┌──────────────────────────────")
    lines.append("│ 📊 TAIPO • ERKEN KIRILIM RADAR")
    lines.append(f"│ {picked_at}")
    lines.append("└──────────────────────────────")
    lines.append("")
    lines.append(f"🎯 Kriter: {EARLY_MIN_PCT:.2f}% – {EARLY_MAX_PCT:.2f}% (henüz tren kaçmadan)")
    lines.append("")
    lines.append("🟢 3 ERKEN KIRILIM (Bugün sabit takip)")
    for q in picks:
        sym = clean_sym(q["symbol"])
        lines.append(f"{sym:<6} {q['price']:>8}   {trend_emoji(q['change_pct'])}  {pct_str(q['change_pct'])}")
    lines.append("")
    lines.append("🕒 Takip mesajları: 11:10 • 12:10 • 13:10 • 14:10 • 15:10 • 16:10 • 17:10")
    lines.append("⌨️ Komut: /taipo")
    return "\n".join(lines)


def build_track_message(state):
    watch = state.get("watch", {})
    symbols = watch.get("symbols", [])
    baseline = watch.get("baseline", {})
    picked_at = watch.get("picked_at", "")

    lines = []
    lines.append("✅ Saatlik Takip – PREMIUM v1 (Aynı 3 Hisse)")
    lines.append("")
    lines.append("┌──────────────────────────────")
    lines.append("│ 🕒 TAIPO • TAKİP ÇİZELGESİ")
    lines.append(f"│ {now_str_tr()}")
    lines.append("└──────────────────────────────")
    lines.append("")
    if picked_at:
        lines.append(f"🎯 Seçim Zamanı: {picked_at}")
    lines.append("")

    if not symbols:
        lines.append("⚠️ Bugün için takip listesi yok. (10:00–10:10 arası oluşur)")
        return "\n".join(lines)

    for sym in symbols:
        q = fetch_quote(sym)
        if not q:
            lines.append(f"{clean_sym(sym):<6}  → veri yok")
            continue

        base = baseline.get(sym)
        if base is None or float(base) == 0:
            base = q["prev_close"]

        pct_from_base = ((float(q["price"]) - float(base)) / float(base)) * 100.0
        lines.append(f"{clean_sym(sym):<6} {float(base):>8.2f} → {q['price']:>8.2f}   {trend_emoji(pct_from_base)}  {pct_str(pct_from_base)}")

    lines.append("")
    lines.append("⌨️ /taipo")
    return "\n".join(lines)


# =========================
# CORE LOGIC
# =========================
def try_pick_once(state, symbols):
    """
    10:00–10:10 arasında uygun 3 hisse bulunursa:
    - listeyi kilitle
    - baseline kaydet
    - 1 kere mesaj at
    """
    if state.get("sent_pick_message"):
        return state, None

    if not in_pick_window():
        return state, None

    quotes = scan_quotes(symbols)
    if not quotes:
        return state, None

    picks = pick_early_breakouts(quotes, n=PICK_COUNT, lo=EARLY_MIN_PCT, hi=EARLY_MAX_PCT)
    if len(picks) < PICK_COUNT:
        return state, None

    watch_syms = [q["symbol"] for q in picks]
    baseline = {q["symbol"]: q["price"] for q in picks}

    state["watch"]["symbols"] = watch_syms
    state["watch"]["baseline"] = baseline
    state["watch"]["picked_at"] = now_str_tr()
    state["sent_pick_message"] = True

    return state, picks


# =========================
# RUN MODES
# =========================
def run_auto():
    ensure_files()
    state = load_json(STATE_FILE, {})
    state = ensure_today_state(state)

    symbols = load_symbols()
    if not symbols:
        send_message(f"⚠️ bist100.txt bulunamadı veya boş.\n🕒 {now_str_tr()}")
        save_json(STATE_FILE, state)
        return

    # 1) 10:00–10:10 arası yakalarsa anında gönder
    state, picks = try_pick_once(state, symbols)
    if picks:
        send_message(build_pick_message(picks, state["watch"]["picked_at"]))
        save_json(STATE_FILE, state)
        return

    # 2) Takip saatleri (11:10–17:10)
    if is_track_time_now():
        if state.get("watch", {}).get("symbols"):
            if should_send_track_now(state):
                send_message(build_track_message(state))
                state["last_track_sent_key"] = now_key_minute()

    save_json(STATE_FILE, state)


def run_command_listener():
    ensure_files()
    state = load_json(STATE_FILE, {})
    state = ensure_today_state(state)

    last_update_id = int(state.get("last_update_id", 0))
    updates = get_updates(last_update_id + 1)
    max_uid = last_update_id

    for upd in updates:
        uid = int(upd.get("update_id", 0))
        max_uid = max(max_uid, uid)

        msg = extract_message(upd)
        if not msg:
            continue

        text = msg_text(msg)
        if not text:
            continue

        # sadece hedef gruptan dinle
        if TARGET_CHAT_ID and not is_target_chat(msg):
            continue

        if text.lower().startswith("/taipo"):
            now_ts = int(time.time())
            last_ts = int(state.get("last_command_reply_ts", 0))
            if now_ts - last_ts < REPLY_COOLDOWN_SEC:
                continue

            # varsa takip mesajını döndür
            if state.get("watch", {}).get("symbols"):
                send_message(build_track_message(state), chat_id=msg_chat_id(msg))
            else:
                send_message(
                    f"📡 TAIPO • ERKEN KIRILIM RADAR\n🕒 {now_str_tr()}\n\n"
                    f"⚠️ Bugün liste henüz oluşmadı.\n"
                    f"⏰ Seçim aralığı: 10:00–10:10\n"
                    f"🎯 Kriter: {EARLY_MIN_PCT:.2f}% – {EARLY_MAX_PCT:.2f}%\n",
                    chat_id=msg_chat_id(msg)
                )

            state["last_command_reply_ts"] = now_ts

    state["last_update_id"] = max_uid
    save_json(STATE_FILE, state)


def main():
    if MODE == "COMMAND":
        run_command_listener()
    else:
        run_auto()


if __name__ == "__main__":
    main()
