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
TARGET_CHAT_ID = os.getenv("CHAT_ID", "").strip()  # group chat id like -5049...
MODE = os.getenv("MODE", "AUTO").strip().upper()   # AUTO or COMMAND

STATE_FILE = "state.json"
DAILY_WATCH_FILE = "daily_watch.json"
SYMBOLS_FILE = "bist100.txt"

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# =========================
# TIME WINDOW (TR)
# =========================
OPEN_HOUR = 9                      # 09:00 açılış seçimi
OPEN_PICK_MINUTE_MAX = 2           # 09:00-09:02 arası "açılış" say
TRACK_START_HOUR = 9               # takip başlar (09:00)
TRACK_END_HOUR = 20                # takip biter (20:00 dahil)
EOD_REPORT_HOUR = 20               # gün sonu raporu
EOD_REPORT_MINUTE = 55             # 20:55 rapor

REPLY_COOLDOWN_SEC = 20            # komut spam'ini yumuşatmak için

# Saatlik mesaj kilidi (aynı saat içinde 1 kez + sadece saat başı 00-02 arası)
HOURLY_LOCK_MINUTE_WINDOW = (0, 2)

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
    # state.json yoksa oluştur
    if not os.path.exists(STATE_FILE):
        save_json(STATE_FILE, {
            "last_update_id": 0,
            "last_command_reply_ts": 0,
            "day": "",
            "watch": {
                "symbols": [],
                "baseline": {},      # {symbol: baseline_price}
                "picked_at": ""      # "dd.mm.yyyy HH:MM"
            },
            "sent_open_message": False,
            "sent_eod_report": False,
            "last_hourly_sent_hour": ""  # "YYYY-MM-DD HH"
        })

    # daily_watch.json yoksa oluştur
    if not os.path.exists(DAILY_WATCH_FILE):
        save_json(DAILY_WATCH_FILE, {
            "day": "",
            "symbols": [],
            "baseline": {},
            "picked_at": ""
        })

def load_symbols():
    # bist100.txt formatı: her satırda bir sembol örn: ASELS.IS
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
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return None
    return msg

def msg_text(msg: dict):
    return (msg.get("text") or "").strip()

def msg_chat_id(msg: dict):
    chat = msg.get("chat") or {}
    return str(chat.get("id", ""))

def is_target_chat(msg: dict):
    cid = msg_chat_id(msg)
    return (TARGET_CHAT_ID and cid == str(TARGET_CHAT_ID))

# =========================
# DATA FETCH
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
            price = fi.get("last_price") or fi.get("lastPrice") or fi.get("last_price")
            prev_close = fi.get("previous_close") or fi.get("previousClose") or fi.get("previous_close")

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
    results = []
    for sym in symbols:
        q = fetch_quote(sym)
        if q:
            results.append(q)
    return results

def pick_strong_weak(quotes, strong_n=3, weak_n=3):
    if not quotes:
        return [], []
    strong = sorted(quotes, key=lambda x: x["change_pct"], reverse=True)[:strong_n]
    weak = sorted(quotes, key=lambda x: x["change_pct"])[:weak_n]
    return strong, weak

# =========================
# WATCH LOGIC + FORMATTING
# =========================
def today_str_tr():
    return datetime.now(TZ).strftime("%Y-%m-%d")

def now_str_tr():
    return datetime.now(TZ).strftime("%d.%m.%Y %H:%M")

def now_hour_key():
    return datetime.now(TZ).strftime("%Y-%m-%d %H")

def is_open_pick_window():
    n = datetime.now(TZ)
    return (n.hour == OPEN_HOUR) and (0 <= n.minute <= OPEN_PICK_MINUTE_MAX)

def in_tracking_window():
    n = datetime.now(TZ)
    if n.hour < TRACK_START_HOUR:
        return False
    if n.hour > TRACK_END_HOUR and not (n.hour == EOD_REPORT_HOUR and n.minute == EOD_REPORT_MINUTE):
        return False
    return True

def is_eod_report_time():
    n = datetime.now(TZ)
    return (n.hour == EOD_REPORT_HOUR and n.minute == EOD_REPORT_MINUTE)

def reset_for_new_day(state):
    state["day"] = today_str_tr()
    state["sent_open_message"] = False
    state["sent_eod_report"] = False
    state["last_hourly_sent_hour"] = ""
    state["watch"] = {"symbols": [], "baseline": {}, "picked_at": ""}
    return state

def ensure_today_state(state):
    if state.get("day") != today_str_tr():
        state = reset_for_new_day(state)
    return state

def persist_daily_watch(state):
    dw = {
        "day": state.get("day", ""),
        "symbols": state.get("watch", {}).get("symbols", []),
        "baseline": state.get("watch", {}).get("baseline", {}),
        "picked_at": state.get("watch", {}).get("picked_at", ""),
    }
    save_json(DAILY_WATCH_FILE, dw)

def clean_sym(sym):
    return sym.replace(".IS", "")

def trend_emoji(pct: float):
    return "🟢⬆️" if pct >= 0 else "🔴⬇️"

def pct_str(pct: float):
    return f"+{pct:.2f}%" if pct >= 0 else f"{pct:.2f}%"

def build_open_message(strong, weak, picked_at):
    lines = []
    lines.append("✅ 09:00 Açılış Mesajı – PREMIUM v4")
    lines.append("")
    lines.append("┌──────────────────────────────")
    lines.append("│ 📊 TAIPO • BIST RADAR v4")
    lines.append(f"│ {now_str_tr()}")
    lines.append("└──────────────────────────────")
    lines.append("")
    lines.append("🟢 GÜÇLÜ ALANLAR  (Takipte)")
    for q in strong:
        sym = clean_sym(q["symbol"])
        e = trend_emoji(q["change_pct"])
        lines.append(f"{sym:<6} {q['price']:>8}   {e}  {pct_str(q['change_pct'])}")
    lines.append("")
    lines.append("🔴 ZAYIF ALANLAR  (Riskli)")
    for q in weak:
        sym = clean_sym(q["symbol"])
        e = trend_emoji(q["change_pct"])
        lines.append(f"{sym:<6} {q['price']:>8}   {e}  {pct_str(q['change_pct'])}")
    lines.append("")
    lines.append("🕘 Saatlik takip: AÇIK")
    lines.append("⌨️ Komut: /taipo")
    return "\n".join(lines)

def build_hourly_message(state):
    watch = state.get("watch", {})
    symbols = watch.get("symbols", [])
    baseline = watch.get("baseline", {})
    picked_at = watch.get("picked_at", "")

    lines = []
    lines.append("✅ Saatlik Takip Mesajı – PREMIUM v4 (Aynı 6 Hisse)")
    lines.append("")
    lines.append("┌──────────────────────────────")
    lines.append("│ 🕒 TAIPO • SAATLİK TAKİP v4")
    lines.append(f"│ {now_str_tr()}")
    lines.append("└──────────────────────────────")
    lines.append("")
    lines.append("📌 Günün Takip Listesi (6 Hisse)")
    if picked_at:
        lines.append(f"🎯 Seçim: {picked_at}")
    lines.append("")

    strong_syms = symbols[:3]
    weak_syms = symbols[3:6]

    def line_for(sym):
        q = fetch_quote(sym)
        if not q:
            return f"{clean_sym(sym)}  → veri yok"
        base = baseline.get(sym)
        if base is None or float(base) == 0:
            base = q["prev_close"]
        pct_from_base = ((float(q["price"]) - float(base)) / float(base)) * 100.0
        e = trend_emoji(pct_from_base)
        return f"{clean_sym(sym):<6} {float(base):>8.2f} → {q['price']:>8.2f}   {e}  {pct_str(pct_from_base)}"

    lines.append("🟢 GÜÇLÜ (Takip)")
    for sym in strong_syms:
        lines.append(line_for(sym))
    lines.append("")
    lines.append("🔴 ZAYIF (Risk)")
    for sym in weak_syms:
        lines.append(line_for(sym))
    lines.append("")
    lines.append("🔄 Gün içi takip sürüyor")
    lines.append("⌨️ /taipo")
    return "\n".join(lines)

def build_eod_report(state):
    watch = state.get("watch", {})
    symbols = watch.get("symbols", [])
    baseline = watch.get("baseline", {})
    picked_at = watch.get("picked_at", "")

    lines = []
    lines.append("🏁 20:55 Gün Sonu Raporu – PREMIUM v4")
    lines.append("")
    lines.append("┌──────────────────────────────")
    lines.append("│ 🏁 TAIPO • GÜN SONU RAPORU v4")
    lines.append(f"│ {now_str_tr()}")
    lines.append("└──────────────────────────────")
    lines.append("")
    lines.append("📌 Günlük Takip Özeti (6 Hisse)")
    if picked_at:
        lines.append(f"🎯 Seçim: {picked_at}")
    lines.append("")

    perf = []
    for sym in symbols:
        q = fetch_quote(sym)
        if not q:
            continue
        base = baseline.get(sym)
        if base is None or float(base) == 0:
            base = q["prev_close"]
        pct_from_base = ((float(q["price"]) - float(base)) / float(base)) * 100.0
        perf.append((sym, float(base), float(q["price"]), float(pct_from_base)))

    for sym, base, price, pct_ in perf:
        e = trend_emoji(pct_)
        lines.append(f"{clean_sym(sym):<6} {base:>8.2f} → {price:>8.2f}   {e}  {pct_str(pct_)}")

    if perf:
        best = max(perf, key=lambda x: x[3])
        worst = min(perf, key=lambda x: x[3])
        lines.append("")
        lines.append(f"🏆 Günün Kazananı: {clean_sym(best[0])}   {trend_emoji(best[3])}  {pct_str(best[3])}")
        lines.append(f"🧊 Günün Kaybedeni: {clean_sym(worst[0])}  {trend_emoji(worst[3])}  {pct_str(worst[3])}")

    lines.append("")
    lines.append("✅ Takip kapandı (Yarın reset)")
    lines.append("⌨️ /taipo")
    return "\n".join(lines)

def build_no_watch_message():
    lines = []
    lines.append("📡 TAIPO • BIST RADAR")
    lines.append(f"🕒 {now_str_tr()}")
    lines.append("")
    lines.append("⚠️ Bugün için takip listesi henüz yok.")
    lines.append("🟩 Açılış seçimi: 09:00–09:02 arası otomatik yapılır.")
    lines.append("⌨️ /taipo")
    return "\n".join(lines)

def pick_daily_watch_if_needed(state, symbols):
    if state.get("sent_open_message"):
        return state, None, None

    if not is_open_pick_window():
        return state, None, None

    quotes = scan_quotes(symbols)
    if not quotes:
        return state, None, None

    strong, weak = pick_strong_weak(quotes, strong_n=3, weak_n=3)
    if not strong or not weak:
        return state, None, None

    watch_syms = [q["symbol"] for q in strong] + [q["symbol"] for q in weak]
    baseline = {q["symbol"]: q["price"] for q in (strong + weak)}

    state["watch"]["symbols"] = watch_syms
    state["watch"]["baseline"] = baseline
    state["watch"]["picked_at"] = now_str_tr()
    state["sent_open_message"] = True
    state["sent_eod_report"] = False

    persist_daily_watch(state)
    return state, strong, weak

def should_send_hourly_now(state):
    n = datetime.now(TZ)
    if not (HOURLY_LOCK_MINUTE_WINDOW[0] <= n.minute <= HOURLY_LOCK_MINUTE_WINDOW[1]):
        return False
    key = now_hour_key()
    return state.get("last_hourly_sent_hour", "") != key

# =========================
# COMMAND LISTENER
# =========================
def run_command_listener():
    ensure_files()
    state = load_json(STATE_FILE, {})
    state = ensure_today_state(state)

    last_update_id = int(state.get("last_update_id", 0))
    updates = get_updates(last_update_id + 1)
    max_uid = last_update_id

    for upd in updates:
        uid = int(upd.get("update_id", 0))
        if uid > max_uid:
            max_uid = uid

        msg = extract_message(upd)
        if not msg:
            continue

        text = msg_text(msg)
        if not text:
            continue

        if TARGET_CHAT_ID and not is_target_chat(msg):
            continue

        if text.lower().startswith("/taipo"):
            now_ts = int(time.time())
            last_ts = int(state.get("last_command_reply_ts", 0))
            if now_ts - last_ts < REPLY_COOLDOWN_SEC:
                continue

            watch_syms = state.get("watch", {}).get("symbols", [])

            if not watch_syms:
                symbols = load_symbols()
                if symbols:
                    quotes = scan_quotes(symbols)
                    strong, weak = pick_strong_weak(quotes, 3, 3)
                    if strong and weak:
                        temp = []
                        temp.append("📡 TAIPO • ANLIK RADAR")
                        temp.append(f"🕒 {now_str_tr()}")
                        temp.append("")
                        temp.append("🟢 ANLIK GÜÇLÜ 3")
                        for q in strong:
                            temp.append(f"{clean_sym(q['symbol']):<6} {q['price']:>8}   {trend_emoji(q['change_pct'])}  {pct_str(q['change_pct'])}")
                        temp.append("")
                        temp.append("🔴 ANLIK ZAYIF 3")
                        for q in weak:
                            temp.append(f"{clean_sym(q['symbol']):<6} {q['price']:>8}   {trend_emoji(q['change_pct'])}  {pct_str(q['change_pct'])}")
                        temp.append("")
                        temp.append("🟩 Not: Günlük takip listesi 09:00–09:02 arası sabitlenir.")
                        temp.append("⌨️ /taipo")
                        send_message("\n".join(temp), chat_id=msg_chat_id(msg))
                    else:
                        send_message(f"⚠️ Veri çekilemedi.\n🕒 {now_str_tr()}", chat_id=msg_chat_id(msg))
                else:
                    send_message(build_no_watch_message(), chat_id=msg_chat_id(msg))
            else:
                send_message(build_hourly_message(state), chat_id=msg_chat_id(msg))

            state["last_command_reply_ts"] = int(time.time())

    state["last_update_id"] = max_uid
    save_json(STATE_FILE, state)

# =========================
# AUTO MODE
# =========================
def run_auto():
    ensure_files()
    state = load_json(STATE_FILE, {})
    state = ensure_today_state(state)

    if not in_tracking_window() and not is_eod_report_time():
        save_json(STATE_FILE, state)
        return

    symbols = load_symbols()
    if not symbols:
        send_message(f"⚠️ bist100.txt bulunamadı veya boş.\n🕒 {now_str_tr()}")
        save_json(STATE_FILE, state)
        return

    # 1) Açılış seçimi
    state, strong, weak = pick_daily_watch_if_needed(state, symbols)
    if strong and weak:
        msg = build_open_message(strong, weak, state["watch"]["picked_at"])
        send_message(msg)
        save_json(STATE_FILE, state)
        return

    # 2) Gün sonu raporu
    if is_eod_report_time():
        if not state.get("sent_eod_report"):
            if state.get("watch", {}).get("symbols"):
                send_message(build_eod_report(state))
                state["sent_eod_report"] = True
            save_json(STATE_FILE, state)
        return

    # 3) Saatlik takip (kilitli)
    if state.get("watch", {}).get("symbols"):
        if should_send_hourly_now(state):
            send_message(build_hourly_message(state))
            state["last_hourly_sent_hour"] = now_hour_key()

    save_json(STATE_FILE, state)

def main():
    if MODE == "COMMAND":
        run_command_listener()
    else:
        run_auto()

if __name__ == "__main__":
    main()
