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
            "sent_eod_report": False    
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
            # Çok güvenli: .IS yoksa ekle    
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
    # message or edited_message    
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
    # Eğer CHAT_ID set ise sadece o gruptan geleni dinle    
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
            # fast_info anahtarları farklı gelebilir, çoklu fallback:    
            price = fi.get("last_price") or fi.get("lastPrice") or fi.get("last_price")    
            prev_close = fi.get("previous_close") or fi.get("previousClose") or fi.get("previous_close")    

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
    results = []    
    for sym in symbols:    
        q = fetch_quote(sym)    
        if q:    
            results.append(q)    
    return results    

def pick_strong_weak(quotes, strong_n=3, weak_n=3):    
    if not quotes:    
        return [], []    
    # güçlü: en yüksek değişim    
    strong = sorted(quotes, key=lambda x: x["change_pct"], reverse=True)[:strong_n]    
    # zayıf: en düşük değişim    
    weak = sorted(quotes, key=lambda x: x["change_pct"])[:weak_n]    
    return strong, weak    

# =========================    
# WATCH LOGIC + FORMATTING    
# =========================    
def today_str_tr():    
    return datetime.now(TZ).strftime("%Y-%m-%d")    

def now_str_tr():    
    return datetime.now(TZ).strftime("%d.%m.%Y %H:%M")    

def now_hm():    
    return datetime.now(TZ).strftime("%H:%M")    

def is_open_pick_window():    
    n = datetime.now(TZ)    
    return (n.hour == OPEN_HOUR) and (0 <= n.minute <= OPEN_PICK_MINUTE_MAX)    

def in_tracking_window():    
    n = datetime.now(TZ)    
    # 7 gün açık — hafta sonu kısıt yok    
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
    # ok + renkli nokta: elit premium    
    if pct >= 0:    
        return "🟢⬆️"    
    return "🔴⬇️"    

def pct_str(pct: float):    
    # +/-    
    if pct >= 0:    
        return f"+{pct:.2f}%"    
    return f"{pct:.2f}%"    

# =========================    
# MESAJ ZAMAN KONTROLÜ    
# =========================    
def send_hourly_update(state):    
    current_time = datetime.datetime.now().strftime('%H:%M')    
    last_sent_time = state.get("last_sent_time", "")    

    if current_time != last_sent_time:  # Eğer bir önceki gönderim zamanı ile şu anki zaman farklıysa    
        # Mesaj gönder    
        state["last_sent_time"] = current_time  # Şu anki zamanı kaydet    
        send_message(build_status_message(state))  # Mesajı gönder    
        save_json(STATE_FILE, state)    

# =========================    
# SABAH SEÇİLEN HİSSELERİ KAYDETME    
# =========================    
def pick_daily_watch_if_needed(state, symbols):    
    """    
    Açılış penceresinde ve seçilmemişse 6 hisse seç.    
    3 güçlü + 3 zayıf    
    """    
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

    # Sembol listesi: güçlüler + zayıflar    
    watch_syms = [q["symbol"] for q in strong] + [q["symbol"] for q in weak]    

    # baseline: o anki fiyat (seçim anı)    
    baseline = {q["symbol"]: q["price"] for q in (strong + weak)}    

    state["watch"]["symbols"] = watch_syms    
    state["watch"]["baseline"] = baseline    
    state["watch"]["picked_at"] = now_str_tr()    
    state["sent_open_message"] = True    
    state["sent_eod_report"] = False    

    persist_daily_watch(state)    
    return state, strong, weak    

# =========================    
# YÜKSELEN VE DÜŞEN HİSSELERİ GÖNDERME    
# =========================    
def build_status_message(state):    
    watch_syms = state.get("watch", {}).get("symbols", [])    
    if not watch_syms:    
        return "Takip listesi boş."    

    results = []    
    # Yükselen hisseler    
    rising_stocks = [sym for sym in watch_syms if sym["change_pct"] > 0]    
    falling_stocks = [sym for sym in watch_syms if sym["change_pct"] < 0]    

    if rising_stocks:    
        results.append("Yükselen Hisseler:")    
        for stock in rising_stocks:    
            results.append(f"🟢 {stock['symbol']} - {stock['price']} ({stock['change_pct']}%)")    
    if falling_stocks:    
        results.append("Düşen Hisseler:")    
        for stock in falling_stocks:    
            results.append(f"🔴 {stock['symbol']} - {stock['price']} ({stock['change_pct']}%)")    

    return "\n".join(results)    

# =========================    
# MAIN RUN MODES    
# =========================    
def run_auto():    
    ensure_files()    
    state = load_json(STATE_FILE, {})    
    state = ensure_today_state(state)    

    # takip penceresi dışında sessiz kal    
    if not in_tracking_window() and not is_eod_report_time():    
        save_json(STATE_FILE, state)    
        return    

    symbols = load_symbols()    
    if not symbols:    
        send_message(f"⚠️ bist100.txt bulunamadı veya boş.\n🕒 {now_str_tr()}")    
        save_json(STATE_FILE, state)    
        return    

    # 1) Açılış seçimi gerekiyorsa    
    state, strong, weak = pick_daily_watch_if_needed(state, symbols)    
    if strong and weak:    
        msg = build_open_message(strong, weak, state["watch"]["picked_at"])    
        send_message(msg)    
        save_json(STATE_FILE, state)    
        return    

    # 2) Gün sonu raporu (20:55) — günde 1 kez    
    if is_eod_report_time():    
        if not state.get("sent_eod_report"):    
            if state.get("watch", {}).get("symbols"):    
                send_message(build_eod_report(state))    
                state["sent_eod_report"] = True    
            save_json(STATE_FILE, state)    
        return    

    # 3) Saatlik takip mesajı (seçim varsa)    
    if state.get("watch", {}).get("symbols"):    
        send_message(build_hourly_message(state))    

    save_json(STATE_FILE, state)    

def main():    
    if MODE == "COMMAND":    
        run_command_listener()    
    else:    
        run_auto()    

if __name__ == "__main__":    
    main()
