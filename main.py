import os
import json
import time
from datetime import datetime, date
from zoneinfo import ZoneInfo

import requests
import yfinance as yf


# =========================
#   CONFIG
# =========================
TZ = ZoneInfo("Europe/Istanbul")
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
TARGET_CHAT_ID = os.getenv("CHAT_ID", "").strip()  # group chat id like -5049...
MODE = os.getenv("MODE", "AUTO").strip().upper()   # AUTO or COMMAND

STATE_FILE = "state.json"
DAILY_WATCH_FILE = "daily_watch.json"
SYMBOLS_FILE = "bist100.txt"

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Market time window (TR)
OPEN_HOUR = 10
CLOSE_HOUR = 18
OPEN_PICK_MINUTE_MAX = 2   # 10:00–10:02 arası "açılış" say
REPLY_COOLDOWN_SEC = 20    # komut spam'ini yumuşatmak için


# =========================
#   IO HELPERS
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
                "baseline": {},   # {symbol: baseline_price}
                "picked_at": "",
            },
            "sent_open_message": False
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
#   TELEGRAM
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
#   DATA FETCH
# =========================
def fetch_quote(symbol: str):
    """
    Returns dict:
      {
        symbol, price, prev_close, change_pct
      }
    """
    try:
        t = yf.Ticker(symbol)
        # fast_info genelde daha hızlı
        fi = getattr(t, "fast_info", None)
        price = None
        prev_close = None

        if fi:
            price = fi.get("last_price") or fi.get("lastPrice") or fi.get("last_price")
            prev_close = fi.get("previous_close") or fi.get("previousClose")

        # fallback
        if price is None or prev_close is None:
            hist = t.history(period="2d", interval="1d")
            if hist is not None and len(hist) >= 1:
                prev_close = float(hist["Close"].iloc[-1])
            hist2 = t.history(period="1d", interval="1m")
            if hist2 is not None and len(hist2) >= 1:
                price = float(hist2["Close"].iloc[-1])

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


def scan_top3(symbols):
    results = []
    for sym in symbols:
        q = fetch_quote(sym)
        if q:
            results.append(q)

    if not results:
        return []

    # “momentum”: o anki % değişim yüksek olanları al
    results.sort(key=lambda x: x["change_pct"], reverse=True)
    return results[:3]


# =========================
#   WATCH LOGIC
# =========================
def today_str_tr():
    return datetime.now(TZ).strftime("%Y-%m-%d")


def now_str_tr():
    return datetime.now(TZ).strftime("%d.%m.%Y %H:%M")


def in_market_hours_tr():
    n = datetime.now(TZ)
    if n.weekday() >= 5:  # 5=Sat, 6=Sun
        return False
    return OPEN_HOUR <= n.hour <= CLOSE_HOUR


def is_open_pick_window():
    n = datetime.now(TZ)
    return (n.hour == OPEN_HOUR) and (0 <= n.minute <= OPEN_PICK_MINUTE_MAX)


def reset_for_new_day(state):
    state["day"] = today_str_tr()
    state["sent_open_message"] = False
    state["watch"] = {"symbols": [], "baseline": {}, "picked_at": ""}
    return state


def ensure_today_state(state):
    if state.get("day") != today_str_tr():
        state = reset_for_new_day(state)
    return state


def persist_daily_watch(state):
    # daily_watch.json: bilgilendirme amaçlı
    dw = {
        "day": state.get("day", ""),
        "symbols": state.get("watch", {}).get("symbols", []),
        "baseline": state.get("watch", {}).get("baseline", {}),
        "picked_at": state.get("watch", {}).get("picked_at", "")
    }
    save_json(DAILY_WATCH_FILE, dw)


def build_status_message(state):
    watch = state.get("watch", {})
    symbols = watch.get("symbols", [])
    baseline = watch.get("baseline", {})
    picked_at = watch.get("picked_at", "")

    if not symbols:
        return f"📡 TAİPO-BIST RADAR\n🕒 {now_str_tr()}\n\n⚠️ Bugün için takip listesi yok.\n➡️ Açılışta (10:00–10:02) otomatik seçer veya /taipo ile anlık tarar."

    lines = []
    lines.append(f"📡 TAİPO-BIST TAKİP\n🕒 {now_str_tr()}")
    if picked_at:
        lines.append(f"🎯 Seçim: {picked_at}")

    lines.append("\n🟢 GÜNÜN TAKİP 3'LÜSÜ")

    for sym in symbols:
        q = fetch_quote(sym)
        if not q:
            lines.append(f"• {sym.replace('.IS','')} → veri yok")
            continue

        b = baseline.get(sym)
        # baz fiyat yoksa prev_close baz al
        if b is None:
            b = q["prev_close"]

        # sadece yüzde; TL farkı yazmıyoruz (senin isteğin)
        pct_from_base = ((q["price"] - float(b)) / float(b)) * 100.0 if b else q["change_pct"]

        lines.append(
            f"• {sym.replace('.IS','')} → {q['price']}  ({pct_from_base:+.2f}%)"
        )

    lines.append("\nKomut: /taipo")
    return "\n".join(lines)


def build_open_message(picks):
    # picks: list of quote dict
    lines = []
    lines.append(f"🚀 TAİPO-BIST AÇILIŞ SEÇİMİ (Takip 3)\n🕒 {now_str_tr()}")
    lines.append("\n🟢 EN GÜÇLÜ 3 (GÜN BOYU TAKİP)")
    for p in picks:
        lines.append(f"• {p['symbol'].replace('.IS','')} → {p['price']}  ({p['change_pct']:+.2f}%)")
    lines.append("\n📌 Not: Bugün kapanışa kadar aynı 3 hisse izlenecek.")
    lines.append("Komut: /taipo")
    return "\n".join(lines)


def pick_daily_watch_if_needed(state, symbols):
    """
    Açılış penceresinde ve seçilmemişse 3 hisse seç.
    """
    if state.get("sent_open_message"):
        return state, None

    if not is_open_pick_window():
        return state, None

    picks = scan_top3(symbols)
    if not picks:
        return state, None

    watch_syms = [p["symbol"] for p in picks]
    baseline = {p["symbol"]: p["price"] for p in picks}

    state["watch"]["symbols"] = watch_syms
    state["watch"]["baseline"] = baseline
    state["watch"]["picked_at"] = now_str_tr()
    state["sent_open_message"] = True

    persist_daily_watch(state)
    return state, picks


# =========================
#   MAIN RUN MODES
# =========================
def run_auto():
    """
    main.yml ile saatlik tetiklenir.
    - 10:00-10:02 arası ilk defa yakalarsa: seçim yap + açılış mesajı at
    - diğer saatler: takip mesajı at (eğer takip listesi varsa)
    """
    ensure_files()
    state = load_json(STATE_FILE, {})
    state = ensure_today_state(state)

    if not in_market_hours_tr():
        # Piyasa dışıysa sessiz kalalım
        save_json(STATE_FILE, state)
        return

    symbols = load_symbols()
    if not symbols:
        send_message(f"⚠️ bist100.txt bulunamadı veya boş.\n🕒 {now_str_tr()}")
        save_json(STATE_FILE, state)
        return

    # Açılış seçimi gerekiyorsa
    state, picks = pick_daily_watch_if_needed(state, symbols)
    if picks:
        send_message(build_open_message(picks))
        save_json(STATE_FILE, state)
        return

    # Saatlik takip mesajı (takip listesi varsa)
    msg = build_status_message(state)
    # Eğer takip listesi yoksa her saat spam olmasın: sadece 10:00-10:02 yakalayamadıysa sessiz kalabiliriz
    # Ama sen "saat başı takip" istedin, bu yüzden yine yolluyoruz.
    send_message(msg)

    save_json(STATE_FILE, state)


def run_command_listener():
    """
    command.yml ile 2 dakikada bir tetiklenir.
    Telegram getUpdates ile /taipo komutu arar ve cevap verir.
    """
    ensure_files()
    state = load_json(STATE_FILE, {})
    state = ensure_today_state(state)

    last_update_id = int(state.get("last_update_id", 0))
    updates = get_updates(last_update_id + 1)

    max_uid = last_update_id
    did_reply = False

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

        # sadece hedef gruptan komut dinle
        if TARGET_CHAT_ID and not is_target_chat(msg):
            continue

        if text.lower().startswith("/taipo"):
            # cooldown
            now_ts = int(time.time())
            last_ts = int(state.get("last_command_reply_ts", 0))
            if now_ts - last_ts < REPLY_COOLDOWN_SEC:
                continue

            # Eğer bugün henüz seçim yapılmadıysa: anlık tarayıp gösterelim (ama günün takip listesi olarak yazmayalım)
            watch_syms = state.get("watch", {}).get("symbols", [])
            if not watch_syms:
                symbols = load_symbols()
                picks = scan_top3(symbols) if symbols else []
                if picks:
                    temp_lines = []
                    temp_lines.append(f"📡 TAİPO-BIST ANLIK RADAR\n🕒 {now_str_tr()}")
                    temp_lines.append("\n🟢 ANLIK EN GÜÇLÜ 3")
                    for p in picks:
                        temp_lines.append(f"• {p['symbol'].replace('.IS','')} → {p['price']}  ({p['change_pct']:+.2f}%)")
                    temp_lines.append("\n📌 Not: Günlük takip listesi açılışta (10:00–10:02) sabitlenir.")
                    temp_lines.append("Komut: /taipo")
                    send_message("\n".join(temp_lines), chat_id=msg_chat_id(msg))
                else:
                    send_message(f"⚠️ Veri çekilemedi.\n🕒 {now_str_tr()}", chat_id=msg_chat_id(msg))
            else:
                send_message(build_status_message(state), chat_id=msg_chat_id(msg))

            state["last_command_reply_ts"] = int(time.time())
            did_reply = True

    state["last_update_id"] = max_uid
    save_json(STATE_FILE, state)


def main():
    if MODE == "COMMAND":
        run_command_listener()
    else:
        run_auto()


if __name__ == "__main__":
    main()
