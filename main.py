import os
import json
import time
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import quote, urlsplit, urlunsplit, parse_qsl, urlencode

import requests
import yfinance as yf
import feedparser

# ✅ NEW: toplu veri için pandas gerekir
import pandas as pd

# =========================================================
# TAIPO-BIST v3 PRO (Plan A Ready)
# - Pick: 10:00–10:10 (TR)
# - Track: 11:30..17:30 (TR) hourly
# - Band target: +0.40% to +0.90% (auto widen)
# - Anti-spam: /id only when requested, cooldown, ignore old commands
# - News: only new items (7d memory) and only if exists
# - Optional: Persist state.json to repo (Plan A) via git commit/push
# =========================================================

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
# PLAN A (state.json persistence)
# =========================
PERSIST_STATE = os.getenv("PERSIST_STATE", "0").strip() == "1"

# =========================
# PICK SETTINGS
# =========================
PICK_START_HOUR = 10
PICK_START_MIN = 0
PICK_END_MIN = 10

PICK_COUNT = 3

AUTO_BAND_STEPS = [
    (0.40, 0.90),
    (0.40, 1.00),
    (0.40, 1.20),
    (0.30, 1.20),
    (0.20, 1.50),
    (0.10, 2.00),
    (0.00, 3.00),
]

# =========================
# TRACK SETTINGS
# =========================
TRACK_HOURS_TR = {11, 12, 13, 14, 15, 16, 17}
TRACK_MINUTE_TR = 30

# =========================
# COMMAND / ANTI-SPAM
# =========================
REPLY_COOLDOWN_SEC = 10
ID_COOLDOWN_SEC = 30

COMMAND_MAX_AGE_SEC = int(os.getenv("COMMAND_MAX_AGE_SEC", "600"))  # 10 min default

# =========================
# NEWS
# =========================
NEWS_MAX_ITEMS = 3
NEWS_STATE_KEY = "news_seen"  # title->ts

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

def _normalize_symbol(s: str) -> str:
    s = (s or "").strip().upper()
    if not s:
        return ""
    # yorum satırı destekle
    if s.startswith("#") or s.startswith("//"):
        return ""
    # AKBNK veya AKBNK.IS ikisi de kabul
    if not s.endswith(".IS"):
        s = s + ".IS"
    return s

def load_symbols():
    if not os.path.exists(SYMBOLS_FILE):
        return []
    syms = []
    with open(SYMBOLS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            s = _normalize_symbol(line)
            if s:
                syms.append(s)
    # dedupe preserve order
    return list(dict.fromkeys(syms))

# =========================
# TELEGRAM
# =========================
def _escape_html(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def send_message(text: str, chat_id: str = None) -> bool:
    if not chat_id:
        chat_id = TARGET_CHAT_ID
    if not BOT_TOKEN or not chat_id:
        return False

    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
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

def is_fresh_command(msg: dict) -> bool:
    d = msg.get("date")
    if not isinstance(d, int):
        return True
    return (int(time.time()) - d) <= COMMAND_MAX_AGE_SEC

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
# DATA (FAST / BULK)
# =========================
def fetch_quote(symbol: str):
    """
    Single symbol quote (used for tracking 3 symbols)
    Returns dict:
      symbol, price, prev_close, change_pct, volume(optional)
    """
    try:
        t = yf.Ticker(symbol)

        fi = getattr(t, "fast_info", None)
        price = None
        prev_close = None
        vol = None

        if fi:
            price = fi.get("last_price") or fi.get("lastPrice")
            prev_close = fi.get("previous_close") or fi.get("previousClose")
            vol = fi.get("last_volume") or fi.get("lastVolume") or fi.get("volume")

        if price is None or prev_close is None:
            hist2 = t.history(period="2d", interval="1d")
            if hist2 is not None and len(hist2) >= 2:
                prev_close = float(hist2["Close"].iloc[-2])
                price = float(hist2["Close"].iloc[-1])

        if price is None or prev_close in (None, 0):
            return None

        change_pct = ((float(price) - float(prev_close)) / float(prev_close)) * 100.0

        out = {
            "symbol": symbol,
            "price": round(float(price), 2),
            "prev_close": round(float(prev_close), 2),
            "change_pct": round(float(change_pct), 2),
        }
        if vol is not None:
            out["volume"] = float(vol)

        return out
    except Exception:
        return None

def scan_quotes_bulk(symbols):
    """
    ✅ Bulk fetch for pick window (fast)
    Uses:
      - intraday 1m for last price & last volume
      - daily for prev close + avg volume
    """
    if not symbols:
        return []

    # yfinance bazı ortamlarda uyarı basar, sessiz kalsın
    try:
        intraday = yf.download(
            tickers=symbols,
            period="1d",
            interval="1m",
            group_by="ticker",
            threads=True,
            auto_adjust=False,
            progress=False,
        )
    except Exception:
        intraday = None

    try:
        daily = yf.download(
            tickers=symbols,
            period="10d",
            interval="1d",
            group_by="ticker",
            threads=True,
            auto_adjust=False,
            progress=False,
        )
    except Exception:
        daily = None

    out = []

    for sym in symbols:
        try:
            # intraday parse
            last_price = None
            last_vol = None

            if isinstance(intraday, pd.DataFrame) and not intraday.empty:
                if isinstance(intraday.columns, pd.MultiIndex):
                    if sym in intraday.columns.get_level_values(0):
                        df_i = intraday[sym].dropna()
                        if not df_i.empty:
                            last_price = float(df_i["Close"].iloc[-1])
                            if "Volume" in df_i.columns:
                                last_vol = float(df_i["Volume"].iloc[-1])
                else:
                    # tek sembol gibi döndüyse
                    df_i = intraday.dropna()
                    if not df_i.empty and "Close" in df_i.columns:
                        last_price = float(df_i["Close"].iloc[-1])
                        if "Volume" in df_i.columns:
                            last_vol = float(df_i["Volume"].iloc[-1])

            # daily parse
            prev_close = None
            avg_vol = None

            if isinstance(daily, pd.DataFrame) and not daily.empty:
                if isinstance(daily.columns, pd.MultiIndex):
                    if sym in daily.columns.get_level_values(0):
                        df_d = daily[sym].dropna()
                        if len(df_d) >= 2 and "Close" in df_d.columns:
                            prev_close = float(df_d["Close"].iloc[-2])
                        if "Volume" in df_d.columns and len(df_d) >= 5:
                            avg_vol = float(df_d["Volume"].tail(10).mean())
                else:
                    df_d = daily.dropna()
                    if len(df_d) >= 2 and "Close" in df_d.columns:
                        prev_close = float(df_d["Close"].iloc[-2])
                    if "Volume" in df_d.columns and len(df_d) >= 5:
                        avg_vol = float(df_d["Volume"].tail(10).mean())

            if last_price is None or prev_close in (None, 0):
                continue

            change_pct = ((last_price - prev_close) / prev_close) * 100.0

            q = {
                "symbol": sym,
                "price": round(float(last_price), 2),
                "prev_close": round(float(prev_close), 2),
                "change_pct": round(float(change_pct), 2),
            }

            if last_vol is not None:
                q["volume"] = float(last_vol)

            # basit vol ratio: anlık volume / avg daily volume
            if last_vol is not None and avg_vol and avg_vol > 0:
                q["avg_volume"] = float(avg_vol)
                q["vol_ratio"] = round(float(last_vol / avg_vol), 2)

            out.append(q)
        except Exception:
            continue

    return out

def _rank_score(q: dict) -> float:
    vr = float(q.get("vol_ratio", 0.0) or 0.0)
    cp = float(q.get("change_pct", 0.0) or 0.0)
    return vr * 10.0 + cp

def pick_breakouts_with_auto_band(quotes, n=3):
    quotes_pos = [q for q in quotes if float(q.get("change_pct", 0)) > 0]

    for lo, hi in AUTO_BAND_STEPS:
        pool = [q for q in quotes_pos if lo <= float(q["change_pct"]) <= hi]
        if not pool:
            continue
        pool_sorted = sorted(pool, key=_rank_score, reverse=True)
        if len(pool_sorted) >= n:
            return pool_sorted[:n], (lo, hi)

    fallback = [q for q in quotes_pos if 0.0 <= float(q["change_pct"]) <= 3.0]
    fallback = sorted(fallback, key=_rank_score, reverse=True)[:n]
    if len(fallback) == n:
        return fallback, (0.0, 3.0)

    return [], None

# =========================
# NEWS (RSS)
# =========================
def _google_news_rss_url(query: str) -> str:
    q = quote(query)
    return f"https://news.google.com/rss/search?q={q}&hl=tr&gl=TR&ceid=TR:tr"

def normalize_url(u: str) -> str:
    try:
        parts = urlsplit(u)
        q = parse_qsl(parts.query, keep_blank_values=True)
        banned = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "oc"}
        q2 = [(k, v) for (k, v) in q if k not in banned]
        new_query = urlencode(q2, doseq=True)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))
    except Exception:
        return u

def fetch_bist_news_items():
    queries = [
        '"Borsa İstanbul" OR BIST OR "BIST 100"',
        'KAP OR "Kamuyu Aydınlatma Platformu"',
        'SPK OR "Sermaye Piyasası Kurulu"',
        'temettü OR bedelsiz OR "pay geri alım" OR "sermaye artırımı"',
    ]

    items = []
    for q in queries:
        url = _google_news_rss_url(q)
        feed = feedparser.parse(url)
        for e in feed.entries[:10]:
            title = (e.get("title") or "").strip()
            link = (e.get("link") or "").strip()
            if title and link:
                items.append({
                    "title": title,
                    "link": normalize_url(link),
                })

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
        return ""

    lines = []
    lines.append("📰 <b>Haber Radar</b> (max 3 • yeni)")
    for it in selected_items:
        title = _escape_html(it["title"])
        link = it["link"]
        lines.append(f"• 🔥 {title} — <a href=\"{link}\">Haberi aç</a>")
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
    lines.append("✅ <b>10:00–10:10 Erken Kırılım</b> – TAIPO BIST v3 PRO")
    lines.append("")
    lines.append("┌──────────────────────────────")
    lines.append("│ 📊 <b>ERKEN KIRILIM RADAR</b>")
    lines.append(f"│ {picked_at}")
    lines.append("└──────────────────────────────")
    lines.append("")
    lines.append(f"🎯 <b>Band (auto):</b> {lo:.2f}% – {hi:.2f}%")
    lines.append("")
    lines.append("🟢 <b>Seçilen 3 Hisse</b> (takip listesi)")
    for q in picks:
        sym = clean_sym(q["symbol"])
        vr = q.get("vol_ratio")
        vr_txt = f" • hacim x{vr:.2f}" if isinstance(vr, (int, float)) else ""
        lines.append(
            f"<code>{sym}</code>  {q['price']:.2f}   {trend_emoji(q['change_pct'])}  {pct_str(q['change_pct'])}{vr_txt}"
        )
    lines.append("")
    lines.append("🕒 Takip: 11:30 • 12:30 • 13:30 • 14:30 • 15:30 • 16:30 • 17:30")
    lines.append("⌨️ Komut: <code>/taipo</code>  | Test: <code>/ping</code>  | ID: <code>/id</code>")
    return "\n".join(lines)

def build_track_message(state):
    watch = state.get("watch", {})
    symbols = watch.get("symbols", [])
    baseline = watch.get("baseline", {})
    picked_at = watch.get("picked_at", "")
    band_used = watch.get("band_used", "")

    lines = []
    lines.append("✅ <b>Saatlik Takip</b> – TAIPO BIST v3 PRO (aynı 3 hisse)")
    lines.append("")
    lines.append("┌──────────────────────────────")
    lines.append("│ 🕒 <b>TAKİP ÇİZELGESİ</b>")
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
        lines.append("⌨️ <code>/taipo</code>")
        return "\n".join(lines)

    for sym in symbols:
        q = fetch_quote(sym)
        if not q:
            lines.append(f"<code>{clean_sym(sym)}</code> → veri yok")
            continue

        base = baseline.get(sym)
        if base is None or float(base) == 0:
            base = q["prev_close"]

        pct_from_base = ((float(q["price"]) - float(base)) / float(base)) * 100.0
        lines.append(
            f"<code>{clean_sym(sym)}</code>  {float(base):.2f} → {q['price']:.2f}   "
            f"{trend_emoji(pct_from_base)}  {pct_str(pct_from_base)}"
        )

    lines.append("")
    lines.append("⌨️ <code>/taipo</code>")
    return "\n".join(lines)

# =========================
# CORE LOGIC
# =========================
def try_pick_once(state, symbols):
    if state.get("sent_pick_message"):
        return state, None, None

    if not in_pick_window():
        return state, None, None

    # ✅ bulk scan
    quotes = scan_quotes_bulk(symbols)
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
        send_message(f"⚠️ <b>bist100.txt</b> bulunamadı veya boş.\n🕒 {now_str_tr()}")
        return state

    # 1) 10:00–10:10 pick once
    state, picks, band = try_pick_once(state, symbols)
    if picks:
        text = build_pick_message(picks, state["watch"]["picked_at"], band)
        state, text = append_news_to_text(state, text)
        send_message(text)
        return state

    # 2) Track at 11:30..17:30
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

        if TARGET_CHAT_ID and not is_target_chat(msg):
            continue

        if not is_fresh_command(msg):
            continue

        low = text.lower()

        # /ping
        if low.startswith("/ping"):
            cid = msg_chat_id(msg)
            title = msg_chat_title(msg)
            reply = f"🏓 <b>PONG</b>\n🕒 {now_str_tr()}"
            if title:
                reply += f"\n👥 <b>Grup:</b> {_escape_html(title)}"
            send_message(reply, chat_id=cid)
            continue

        # /id
        if low.startswith("/id"):
            now_ts = int(time.time())
            last_ts = int(state.get("last_id_reply_ts", 0))
            if now_ts - last_ts >= ID_COOLDOWN_SEC:
                cid = msg_chat_id(msg)
                title = msg_chat_title(msg)
                reply = f"🆔 <b>Chat ID:</b> <code>{cid}</code>"
                if title:
                    reply += f"\n👥 <b>Grup:</b> {_escape_html(title)}"
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
                    f"📡 <b>TAIPO • ERKEN KIRILIM RADAR</b>\n"
                    f"🕒 {now_str_tr()}\n\n"
                    f"⚠️ Bugün liste henüz oluşmadı.\n"
                    f"⏰ Seçim aralığı: 10:00–10:10 (hafta içi)\n"
                    f"🎯 Band hedef: 0.40% – 0.90% (auto genişler)\n"
                    f"🕒 Takip: 11:30–17:30 saat başı\n"
                )
                state, base = append_news_to_text(state, base)
                send_message(base, chat_id=msg_chat_id(msg))

            state["last_command_reply_ts"] = now_ts

    state["last_update_id"] = max_uid
    return state

# =========================
# PLAN A: Git persist state.json
# =========================
def _git_has_changes(path: str) -> bool:
    try:
        r = subprocess.run(["git", "status", "--porcelain", path], capture_output=True, text=True, check=False)
        return bool(r.stdout.strip())
    except Exception:
        return False

def persist_state_if_enabled():
    if not PERSIST_STATE:
        return
    if not os.path.exists(".git"):
        return
    if not _git_has_changes(STATE_FILE):
        return

    try:
        subprocess.run(["git", "config", "user.email", "actions@github.com"], check=False)
        subprocess.run(["git", "config", "user.name", "github-actions"], check=False)

        subprocess.run(["git", "add", STATE_FILE], check=False)
        subprocess.run(["git", "commit", "-m", "chore: update state"], check=False)
        subprocess.run(["git", "push"], check=False)
    except Exception:
        pass

def main():
    ensure_files()
    state = load_json(STATE_FILE, {})
    state = ensure_today_state(state)

    # Always listen commands first (AUTO mode too)
    state = run_command_listener(state)

    if MODE == "COMMAND":
        save_json(STATE_FILE, state)
        persist_state_if_enabled()
        return

    state = run_auto(state)
    save_json(STATE_FILE, state)
    persist_state_if_enabled()

if __name__ == "__main__":
    main()
