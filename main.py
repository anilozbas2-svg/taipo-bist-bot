import os
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import quote, urlsplit, urlunsplit, parse_qsl

import requests
import yfinance as yf
import feedparser

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
# EARLY BREAKOUT SETTINGS
# =========================
PICK_START_HOUR = 10
PICK_START_MIN = 0
PICK_END_MIN = 10

# Ana band (bulamazsa otomatik genişleyecek)
EARLY_MIN_PCT = 0.15
EARLY_MAX_PCT = 0.80
PICK_COUNT = 3

# Genişleme adımları (3 hisse bulamazsa sırayla dene)
AUTO_BAND_STEPS = [
    (0.15, 0.80),
    (0.10, 1.00),
    (0.05, 1.25),
    (0.00, 1.50),
    (0.00, 2.00),
]

# Takip mesajları TR: 11:10 ... 17:10
TRACK_HOURS_TR = {11, 12, 13, 14, 15, 16, 17}
TRACK_MINUTE_TR = 10

REPLY_COOLDOWN_SEC = 20
ID_COOLDOWN_SEC = 60

# =========================
# NEWS (RSS) - PRO MODE
# =========================
NEWS_MAX_ITEMS = 3
NEWS_STATE_KEY = "news_seen"  # state.json içinde tutulur (title->ts)

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
            "last_id_reply_ts": 0,
            "day": "",
            "watch": {
                "symbols": [],
                "baseline": {},
                "picked_at": "",
                "band_used": ""
            },
            "sent_pick_message": False,
            "last_track_sent_key": "",
            NEWS_STATE_KEY: {}
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
    # uniq
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

def msg_chat_title(msg: dict):
    chat = msg.get("chat") or {}
    return (chat.get("title") or chat.get("username") or "").strip()

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

def is_weekday_tr():
    return datetime.now(TZ).weekday() < 5

def ensure_today_state(state):
    if NEWS_STATE_KEY not in state:
        state[NEWS_STATE_KEY] = {}

    if state.get("day") != today_str_tr():
        state["day"] = today_str_tr()
        state["watch"] = {"symbols": [], "baseline": {}, "picked_at": "", "band_used": ""}
        state["sent_pick_message"] = False
        state["last_track_sent_key"] = ""
        # news_seen sıfırlanmaz (7 günlük spam engeli)

    return state

def in_pick_window():
    if not is_weekday_tr():
        return False
    n = datetime.now(TZ)
    if n.hour != PICK_START_HOUR:
        return False
    return PICK_START_MIN <= n.minute <= PICK_END_MIN

def is_track_time_now():
    if not is_weekday_tr():
        return False
    n = datetime.now(TZ)
    return (n.hour in TRACK_HOURS_TR) and (n.minute == TRACK_MINUTE_TR)

def should_send_track_now(state):
    key = now_key_minute()
    return state.get("last_track_sent_key", "") != key

# =========================
# DATA
# =========================
def fetch_quote(symbol: str):
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
    out = []
    for sym in symbols:
        q = fetch_quote(sym)
        if q:
            out.append(q)
    return out

def pick_breakouts_with_auto_band(quotes, n=3):
    # adım adım genişle
    for lo, hi in AUTO_BAND_STEPS:
        pool = [q for q in quotes if lo <= float(q["change_pct"]) <= hi]
        pool_sorted = sorted(pool, key=lambda x: x["change_pct"], reverse=True)
        if len(pool_sorted) >= n:
            return pool_sorted[:n], (lo, hi)

    # hala yoksa: 0-2% arası en iyi 3’ü al (en azından “boş kalmasın”)
    fallback = [q for q in quotes if 0.0 <= float(q["change_pct"]) <= 2.0]
    fallback = sorted(fallback, key=lambda x: x["change_pct"], reverse=True)[:n]
    if len(fallback) == n:
        return fallback, (0.0, 2.0)

    return [], None

# =========================
# NEWS (RSS) - HELPERS
# =========================
def _google_news_rss_url(query: str) -> str:
    q = quote(query)
    return f"https://news.google.com/rss/search?q={q}&hl=tr&gl=TR&ceid=TR:tr"

def normalize_url(u: str) -> str:
    # tracking paramlarını temizle (oc, utm, etc.)
    try:
        parts = urlsplit(u)
        q = parse_qsl(parts.query, keep_blank_values=True)
        banned = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "oc"}
        q2 = [(k, v) for (k, v) in q if k not in banned]
        new_query = "&".join([f"{k}={quote(v)}" if v else f"{k}=" for k, v in q2])
        return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))
    except Exception:
        return u

def fetch_bist_news_items():
    queries = [
        'BIST OR "Borsa İstanbul" OR "Borsa Istanbul"',
        '"BIST 100" OR BIST100',
        'KAP OR "Kamuyu Aydınlatma Platformu"',
        'SPK OR "Sermaye Piyasası Kurulu"',
        'temettü OR bedelsiz OR "pay geri alım" OR "sermaye artırımı"',
        'ihale OR sözleşme OR anlaşma OR yatırım'
    ]

    items = []
    for q in queries:
        url = _google_news_rss_url(q)
        feed = feedparser.parse(url)
        for e in feed.entries[:10]:
            title = (e.get("title") or "").strip()
            link = (e.get("link") or "").strip()
            published = (e.get("published") or "").strip()
            if title and link:
                items.append({
                    "title": title,
                    "link": normalize_url(link),
                    "published": published
                })

    # tekrarları temizle (title bazlı daha stabil)
    uniq = []
    seen_titles = set()
    for it in items:
        key = it["title"].lower()
        if key in seen_titles:
            continue
        seen_titles.add(key)
        uniq.append(it)

    return uniq

def pick_new_news_for_message(state, items, max_items=NEWS_MAX_ITEMS):
    now_ts = int(time.time())
    seen_map = state.get(NEWS_STATE_KEY, {}) or {}

    # 7 gün dışını sil
    cutoff = now_ts - 7 * 24 * 3600
    seen_map = {k: v for k, v in seen_map.items() if int(v) >= cutoff}

    selected = []
    for it in items:
        key = it["title"].lower()
        if key in seen_map:
            continue
        selected.append(it)
        if len(selected) >= max_items:
            break

    for it in selected:
        seen_map[it["title"].lower()] = now_ts

    state[NEWS_STATE_KEY] = seen_map
    return state, selected

def build_news_block(selected_items):
    if not selected_items:
        return ""  # ✅ yeni haber yoksa hiçbir şey ekleme (spam engeli)

    lines = []
    lines.append("📰 Haber Radar (max 3 • 🔥 seçilir)")
    for it in selected_items:
        lines.append(f"• 🔥 {it['title']}\n  {it['link']}")
    return "\n".join(lines)

def append_news_to_text(state, base_text: str):
    try:
        items = fetch_bist_news_items()
        state, selected = pick_new_news_for_message(state, items, NEWS_MAX_ITEMS)
        news_block = build_news_block(selected)
        if not news_block:
            return state, base_text
        return state, f"{base_text}\n\n{news_block}"
    except Exception:
        return state, base_text

# =========================
# FORMAT
# =========================
def clean_sym(sym: str):
    return sym.replace(".IS", "")

def trend_emoji(pct: float):
    return "🟢⬆️" if pct >= 0 else "🔴⬇️"

def pct_str(pct: float):
    return f"+{pct:.2f}%" if pct >= 0 else f"{pct:.2f}%"

def build_pick_message(picks, picked_at, band_used):
    lo, hi = band_used
    lines = []
    lines.append("✅ 10:00–10:10 Erken Kırılım – PREMIUM v2")
    lines.append("")
    lines.append("┌──────────────────────────────")
    lines.append("│ 📊 TAIPO • ERKEN KIRILIM RADAR")
    lines.append(f"│ {picked_at}")
    lines.append("└──────────────────────────────")
    lines.append("")
    lines.append(f"🎯 Band (auto): {lo:.2f}% – {hi:.2f}%")
    lines.append("")
    lines.append("🟢 3 ERKEN KIRILIM (Bugün sabit takip)")
    for q in picks:
        sym = clean_sym(q["symbol"])
        lines.append(f"{sym:<6} {q['price']:>8}   {trend_emoji(q['change_pct'])}  {pct_str(q['change_pct'])}")
    lines.append("")
    lines.append("🕒 Takip: 11:10 • 12:10 • 13:10 • 14:10 • 15:10 • 16:10 • 17:10")
    lines.append("⌨️ Komut: /taipo")
    return "\n".join(lines)

def build_track_message(state):
    watch = state.get("watch", {})
    symbols = watch.get("symbols", [])
    baseline = watch.get("baseline", {})
    picked_at = watch.get("picked_at", "")
    band_used = watch.get("band_used", "")

    lines = []
    lines.append("✅ Saatlik Takip – PREMIUM v2 (Aynı 3 Hisse)")
    lines.append("")
    lines.append("┌──────────────────────────────")
    lines.append("│ 🕒 TAIPO • TAKİP ÇİZELGESİ")
    lines.append(f"│ {now_str_tr()}")
    lines.append("└──────────────────────────────")
    lines.append("")
    if picked_at:
        lines.append(f"🎯 Seçim Zamanı: {picked_at}")
    if band_used:
        lines.append(f"🎚️ Band: {band_used}")
    lines.append("")

    if not symbols:
        lines.append("⚠️ Bugün için takip listesi yok. (10:00–10:10 arası oluşur)")
        lines.append("⌨️ /taipo")
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
        lines.append(
            f"{clean_sym(sym):<6} {float(base):>8.2f} → {q['price']:>8.2f}   "
            f"{trend_emoji(pct_from_base)}  {pct_str(pct_from_base)}"
        )

    lines.append("")
    lines.append("⌨️ /taipo")
    return "\n".join(lines)

# =========================
# CORE LOGIC
# =========================
def try_pick_once(state, symbols):
    if state.get("sent_pick_message"):
        return state, None, None

    if not in_pick_window():
        return state, None, None

    quotes = scan_quotes(symbols)
    if not quotes:
        return state, None, None

    picks, band = pick_breakouts_with_auto_band(quotes, n=PICK_COUNT)
    if len(picks) < PICK_COUNT:
        return state, None, None

    watch_syms = [q["symbol"] for q in picks]
    baseline = {q["symbol"]: q["price"] for q in picks}

    state["watch"]["symbols"] = watch_syms
    state["watch"]["baseline"] = baseline
    state["watch"]["picked_at"] = now_str_tr()
    state["watch"]["band_used"] = f"{band[0]:.2f}%–{band[1]:.2f}%"
    state["sent_pick_message"] = True

    return state, picks, band

# =========================
# MODES
# =========================
def run_auto(state):
    symbols = load_symbols()
    if not symbols:
        send_message(f"⚠️ bist100.txt bulunamadı veya boş.\n🕒 {now_str_tr()}")
        return state

    # 1) 10:00–10:10 arası yakalarsa 1 kere gönder
    state, picks, band = try_pick_once(state, symbols)
    if picks:
        text = build_pick_message(picks, state["watch"]["picked_at"], band)
        state, text = append_news_to_text(state, text)
        send_message(text)
        return state

    # 2) Takip saatleri (11:10–17:10)
    if is_track_time_now():
        if state.get("watch", {}).get("symbols"):
            if should_send_track_now(state):
                text = build_track_message(state)
                state, text = append_news_to_text(state, text)
                send_message(text)
                state["last_track_sent_key"] = now_key_minute()

    return state

def run_command_listener(state):
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

        low = text.lower()

        # /id: sadece isteyince (spam yok)
        if low.startswith("/id"):
            now_ts = int(time.time())
            last_ts = int(state.get("last_id_reply_ts", 0))
            if now_ts - last_ts >= ID_COOLDOWN_SEC:
                cid = msg_chat_id(msg)
                title = msg_chat_title(msg)
                reply = f"🆔 Chat ID: {cid}"
                if title:
                    reply += f"\n👥 Grup: {title}"
                send_message(reply, chat_id=cid)
                state["last_id_reply_ts"] = now_ts
            continue

        # /taipo
        if low.startswith("/taipo"):
            now_ts = int(time.time())
            last_ts = int(state.get("last_command_reply_ts", 0))
            if now_ts - last_ts < REPLY_COOLDOWN_SEC:
                continue

            if state.get("watch", {}).get("symbols"):
                reply = build_track_message(state)
                state, reply = append_news_to_text(state, reply)
                send_message(reply, chat_id=msg_chat_id(msg))
            else:
                base = (
                    f"📡 TAIPO • ERKEN KIRILIM RADAR\n🕒 {now_str_tr()}\n\n"
                    f"⚠️ Bugün liste henüz oluşmadı.\n"
                    f"⏰ Seçim aralığı: 10:00–10:10 (hafta içi)\n"
                    f"🎯 Band (auto): {EARLY_MIN_PCT:.2f}% – {EARLY_MAX_PCT:.2f}%\n"
                )
                state, base = append_news_to_text(state, base)
                send_message(base, chat_id=msg_chat_id(msg))

            state["last_command_reply_ts"] = now_ts

    state["last_update_id"] = max_uid
    return state

def main():
    ensure_files()
    state = load_json(STATE_FILE, {})
    state = ensure_today_state(state)

    # ✅ Her çalışmada önce komutları yakala (workflow 5 dk'da bir çalışsa bile)
    state = run_command_listener(state)

    # COMMAND moddaysa sadece komut dinle
    if MODE == "COMMAND":
        save_json(STATE_FILE, state)
        return

    # AUTO moddaysa otomatik radarları da çalıştır
    state = run_auto(state)
    save_json(STATE_FILE, state)

if __name__ == "__main__":
    main()
