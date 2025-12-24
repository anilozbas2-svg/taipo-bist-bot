# -*- coding: utf-8 -*-
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
import pandas as pd

# =========================================================
# TAIPO-BIST v3 PRO+ (Plan A Ready)
# AUTO:
#   - 10:00 Açılış Raporu (push)
#   - 10:10 Erken Kırılım (push)
#   - 11:00-17:00 Saat Başı Takip (push)
#   - 17:30 Kapanış Hazırlık (push)
#   - 18:10 Gün Sonu Raporu (push)
#
# COMMAND:
#   - /taipo, /taipo pro, /taipo top, /taipo news, /taipo help, /ping, /id
#
# Not:
# - AUTO modunda "HAREKET ALARMI" (alert spam) KAPALI.
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
# TRACK SETTINGS (ISTEDIGIN GIBI SAAT BASI)
# =========================
TRACK_HOURS_TR = {11, 12, 13, 14, 15, 16, 17}
TRACK_MINUTE_TR = 0  # <-- SAAT BASI

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
# PRO: Movers / Cache
# =========================
MOVERS_TOP_N = 5
MOVERS_CACHE_SEC = 120  # 2 min cache

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
        save_json(
            STATE_FILE,
            {
                "last_update_id": 0,
                "last_command_reply_ts": 0,
                "last_id_reply_ts": 0,
                "day": "",
                "watch": {"symbols": [], "baseline": {}, "picked_at": "", "band_used": ""},
                "sent_pick_message": False,
                "last_track_sent_key": "",
                NEWS_STATE_KEY: {},
                "movers_cache": {"ts": 0, "data": None},
                "eod_sent_day": "",
                "last_auto_sent": {},  # key -> {"ts":..., "hash":...}
            },
        )


def _normalize_symbol(s: str) -> str:
    s = (s or "").strip().upper()
    if not s:
        return ""
    if s.startswith("#") or s.startswith("//"):
        return ""
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
    return list(dict.fromkeys(syms))

# =========================
# TELEGRAM
# =========================
def _escape_html(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _hash_text(s: str) -> str:
    # basit, hızlı dedupe
    return str(abs(hash(s)))


def send_message(text: str, chat_id: str = None) -> bool:
    if not chat_id:
        chat_id = TARGET_CHAT_ID
    if not BOT_TOKEN or not chat_id:
        return False
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(f"{TELEGRAM_API}/sendMessage", json=payload, timeout=20)
        return r.status_code == 200
    except Exception:
        return False


def send_auto_deduped(state, key: str, text: str, chat_id: str = None, cooldown_sec: int = 180) -> dict:
    """
    AUTO mesajlarında aynı içeriği tekrar tekrar basmayı engeller.
    key: 'open'|'pick'|'track'|'preclose'|'close' gibi
    """
    if not chat_id:
        chat_id = TARGET_CHAT_ID

    if "last_auto_sent" not in state:
        state["last_auto_sent"] = {}

    now_ts = int(time.time())
    h = _hash_text(text)
    prev = state["last_auto_sent"].get(key, {}) or {}

    prev_ts = int(prev.get("ts", 0) or 0)
    prev_h = prev.get("hash", "")

    # aynı içerik + cooldown içinde ise basma
    if prev_h == h and (now_ts - prev_ts) < cooldown_sec:
        return state

    ok = send_message(text, chat_id=chat_id)
    if ok:
        state["last_auto_sent"][key] = {"ts": now_ts, "hash": h}
    return state


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
    return TARGET_CHAT_ID and cid == str(TARGET_CHAT_ID)


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
    if "movers_cache" not in state:
        state["movers_cache"] = {"ts": 0, "data": None}
    if "eod_sent_day" not in state:
        state["eod_sent_day"] = ""
    if "last_auto_sent" not in state:
        state["last_auto_sent"] = {}

    if state.get("day") != today_str_tr():
        state["day"] = today_str_tr()
        state["watch"] = {"symbols": [], "baseline": {}, "picked_at": "", "band_used": ""}
        state["sent_pick_message"] = False
        state["last_track_sent_key"] = ""
        state["movers_cache"] = {"ts": 0, "data": None}
        state["eod_sent_day"] = ""
        state["last_auto_sent"] = {}
    return state


def is_open_report_time_now():
    # 10:00 TR
    if not is_weekday_tr():
        return False
    n = datetime.now(TZ)
    return n.hour == 10 and n.minute == 0


def is_pick_trigger_time_now():
    # 10:10 TR (10:00–10:10 aralığının SONU -> erken kırılım seçimi burada)
    if not is_weekday_tr():
        return False
    n = datetime.now(TZ)
    return n.hour == 10 and n.minute == 10


def is_track_time_now():
    if not is_weekday_tr():
        return False
    n = datetime.now(TZ)
    return (n.hour in TRACK_HOURS_TR) and (n.minute == TRACK_MINUTE_TR)


def should_send_track_now(state):
    key = now_key_minute()
    return state.get("last_track_sent_key", "") != key


def is_preclose_time_now():
    # 17:30 TR
    if not is_weekday_tr():
        return False
    n = datetime.now(TZ)
    return n.hour == 17 and n.minute == 30


def is_close_report_time_now():
    # 18:10 TR
    if not is_weekday_tr():
        return False
    n = datetime.now(TZ)
    return n.hour == 18 and n.minute == 10

# =========================
# DATA
# =========================
def fetch_quote(symbol: str):
    """Single symbol quote for watchlist tracking."""
    try:
        t = yf.Ticker(symbol)
        fi = getattr(t, "fast_info", None)
        price = None
        prev_close = None

        if fi:
            price = fi.get("last_price") or fi.get("lastPrice")
            prev_close = fi.get("previous_close") or fi.get("previousClose")

        if price is None or prev_close is None:
            hist2 = t.history(period="2d", interval="1d")
            if hist2 is not None and len(hist2) >= 2:
                prev_close = float(hist2["Close"].iloc[-2])
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


def scan_quotes_bulk_intraday(symbols):
    """Bulk scan during pick window (intraday 1m + daily)."""
    if not symbols:
        return []

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
                    df_i = intraday.dropna()
                    if not df_i.empty and "Close" in df_i.columns:
                        last_price = float(df_i["Close"].iloc[-1])
                        if "Volume" in df_i.columns:
                            last_vol = float(df_i["Volume"].iloc[-1])

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
            if last_vol is not None and avg_vol and avg_vol > 0:
                q["avg_volume"] = float(avg_vol)
                q["vol_ratio"] = round(float(last_vol / avg_vol), 2)

            out.append(q)
        except Exception:
            continue

    return out


def scan_daily_movers(symbols):
    """PRO movers (top/bottom) için hafif tarama + Radar Score."""
    if not symbols:
        return []

    try:
        daily2 = yf.download(
            tickers=symbols,
            period="5d",
            interval="1d",
            group_by="ticker",
            threads=True,
            auto_adjust=False,
            progress=False,
        )
    except Exception:
        daily2 = None

    out = []
    for sym in symbols:
        try:
            df = None
            if isinstance(daily2, pd.DataFrame) and not daily2.empty:
                if isinstance(daily2.columns, pd.MultiIndex):
                    if sym in daily2.columns.get_level_values(0):
                        df = daily2[sym].dropna()
                else:
                    df = daily2.dropna()

            if df is None or df.empty or "Close" not in df.columns:
                continue
            if len(df) < 2:
                continue

            prev_close = float(df["Close"].iloc[-2])
            last_close = float(df["Close"].iloc[-1])
            if prev_close == 0:
                continue

            change_pct = ((last_close - prev_close) / prev_close) * 100.0

            vol_ratio = None
            if "Volume" in df.columns and len(df) >= 3:
                last_vol = float(df["Volume"].iloc[-1])
                avg_vol = float(df["Volume"].tail(5).mean()) if len(df) >= 5 else float(df["Volume"].mean())
                if avg_vol > 0:
                    vol_ratio = last_vol / avg_vol

            score = float(change_pct)
            if vol_ratio is not None:
                score += float(vol_ratio) * 0.35

            out.append(
                {
                    "symbol": sym,
                    "price": round(last_close, 2),
                    "change_pct": round(change_pct, 2),
                    "vol_ratio": round(vol_ratio, 2) if vol_ratio is not None else None,
                    "score": round(score, 2),
                }
            )
        except Exception:
            continue

    return out


def pick_breakouts_with_auto_band(quotes, n=3):
    quotes_pos = [q for q in quotes if float(q.get("change_pct", 0)) > 0]

    def _rank_score(q: dict) -> float:
        vr = float(q.get("vol_ratio", 0.0) or 0.0)
        cp = float(q.get("change_pct", 0.0) or 0.0)
        return vr * 10.0 + cp

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
                items.append({"title": title, "link": normalize_url(link)})

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
    lines = ["📰 <b>Haber Radar</b> (max 3 • yeni)"]
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
# FORMAT HELPERS
# =========================
def clean_sym(sym: str):
    return sym.replace(".IS", "")


def trend_emoji(pct: float):
    return "🟢⬆️" if pct >= 0 else "🔴⬇️"


def pct_str(pct: float):
    return f"+{pct:.2f}%" if pct >= 0 else f"{pct:.2f}%"


def score_str(score: float):
    return f"{score:.2f}"


def build_movers_block(movers, top_n=5):
    if not movers:
        return "⚠️ Movers verisi alınamadı."

    pos = sum(1 for m in movers if float(m.get("change_pct", 0)) > 0)
    neg = sum(1 for m in movers if float(m.get("change_pct", 0)) < 0)
    flat = len(movers) - pos - neg

    movers_sorted = sorted(movers, key=lambda x: float(x.get("change_pct", 0)), reverse=True)
    top = movers_sorted[:top_n]
    bottom = list(reversed(movers_sorted[-top_n:]))

    lines = []
    lines.append("┌──────────────────────────────")
    lines.append("│ 📌 <b>MARKET ÖZET</b>")
    lines.append(f"│ 🟢 Artıda: <b>{pos}</b>  🔴 Ekside: <b>{neg}</b>  ⚪️ Yatay: <b>{flat}</b>")
    lines.append("└──────────────────────────────")
    lines.append("")
    lines.append("📈 <b>En Çok Yükselen 5</b>")
    for m in top:
        sym = clean_sym(m["symbol"])
        vr = m.get("vol_ratio")
        vr_txt = f" • hacim x{vr:.2f}" if isinstance(vr, (int, float)) else ""
        lines.append(
            f"• <code>{sym}</code> {m['price']:.2f}  {trend_emoji(m['change_pct'])} {pct_str(m['change_pct'])}  | Skor {score_str(m['score'])}{vr_txt}"
        )

    lines.append("")
    lines.append("📉 <b>En Çok Düşen 5</b>")
    for m in bottom:
        sym = clean_sym(m["symbol"])
        vr = m.get("vol_ratio")
        vr_txt = f" • hacim x{vr:.2f}" if isinstance(vr, (int, float)) else ""
        lines.append(
            f"• <code>{sym}</code> {m['price']:.2f}  {trend_emoji(m['change_pct'])} {pct_str(m['change_pct'])}  | Skor {score_str(m['score'])}{vr_txt}"
        )

    return "\n".join(lines)


def build_open_message():
    lines = []
    lines.append("🟦 <b>AÇILIŞ RAPORU</b> – TAIPO BIST v3 PRO+")
    lines.append(f"🕒 {now_str_tr()}")
    lines.append("")
    lines.append("ℹ️ Erken kırılım listesi 10:00–10:10 arasında oluşur, <b>10:10'da</b> otomatik paylaşılır.")
    lines.append("⌨️ <code>/taipo</code> | <code>/taipo pro</code> | <code>/taipo top</code> | <code>/taipo news</code> | <code>/ping</code> | <code>/id</code>")
    return "\n".join(lines)


def build_pick_message(picks, picked_at, band_used):
    lo, hi = band_used
    lines = []
    lines.append("✅ <b>10:00–10:10 Erken Kırılım</b> – TAIPO BIST v3 PRO+")
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
        lines.append(f"• <code>{sym}</code>  {q['price']:.2f}   {trend_emoji(q['change_pct'])}  {pct_str(q['change_pct'])}{vr_txt}")
    lines.append("")
    lines.append("🕒 Takip: 11:00 • 12:00 • 13:00 • 14:00 • 15:00 • 16:00 • 17:00")
    lines.append("⌨️ <code>/taipo</code> | <code>/taipo pro</code> | <code>/taipo top</code> | <code>/taipo news</code> | <code>/ping</code> | <code>/id</code>")
    return "\n".join(lines)


def build_track_message(state, title="✅ <b>Saatlik Takip</b>"):
    watch = state.get("watch", {})
    symbols = watch.get("symbols", [])
    baseline = watch.get("baseline", {})
    picked_at = watch.get("picked_at", "")
    band_used = watch.get("band_used", "")

    lines = []
    lines.append(f"{title} – TAIPO BIST v3 PRO+ (aynı 3 hisse)")
    lines.append("")
    lines.append("┌──────────────────────────────")
    lines.append("│ 🕒 <b>TAKİP</b>")
    lines.append(f"│ {now_str_tr()}")
    lines.append("└──────────────────────────────")
    lines.append("")
    if picked_at:
        lines.append(f"🎯 Seçim Zamanı: {picked_at}")
    if band_used:
        lines.append(f"🎚️ Band: {band_used}")
    lines.append("")

    if not symbols:
        lines.append("⚠️ Bugün için takip listesi yok. (10:10'da oluşur)")
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
            f"<code>{clean_sym(sym)}</code>  {float(base):.2f} → {q['price']:.2f}   {trend_emoji(pct_from_base)}  {pct_str(pct_from_base)}"
        )

    lines.append("")
    lines.append("⌨️ <code>/taipo</code>")
    return "\n".join(lines)


def build_help_message():
    return (
        "🧭 <b>TAIPO Komutlar</b>\n\n"
        "• <code>/taipo</code> → PRO özet (breadth + +5/-5 + haber)\n"
        "• <code>/taipo pro</code> → PRO detay (watchlist + movers + haber)\n"
        "• <code>/taipo top</code> → sadece movers (+5/-5)\n"
        "• <code>/taipo news</code> → sadece haber radar\n"
        "• <code>/ping</code> → canlı test\n"
        "• <code>/id</code> → chat id\n"
    )

# =========================
# PRO: Movers cache
# =========================
def get_movers_cached(state, symbols):
    now_ts = int(time.time())
    cache = state.get("movers_cache", {}) or {}
    if cache.get("data") and (now_ts - int(cache.get("ts", 0))) <= MOVERS_CACHE_SEC:
        return state, cache["data"], True

    movers = scan_daily_movers(symbols)
    state["movers_cache"] = {"ts": now_ts, "data": movers}
    return state, movers, False

# =========================
# CORE LOGIC
# =========================
def try_pick_once(state, symbols):
    # sadece 10:10 TR tetiklenince seç
    if state.get("sent_pick_message"):
        return state, None, None
    if not is_pick_trigger_time_now():
        return state, None, None

    quotes = scan_quotes_bulk_intraday(symbols)
    if not quotes:
        return state, None, None

    picks, band = pick_breakouts_with_auto_band(quotes, n=PICK_COUNT)
    if not band or len(picks) < PICK_COUNT:
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
        text = f"⚠️ <b>bist100.txt</b> bulunamadı veya boş.\n🕒 {now_str_tr()}"
        state = send_auto_deduped(state, "err", text, cooldown_sec=300)
        return state

    # movers (her AUTO tetikte çekilir, cache var)
    state, movers, _ = get_movers_cached(state, symbols)

    # 10:00 açılış
    if is_open_report_time_now():
        text = build_open_message() + "\n\n" + build_movers_block(movers, MOVERS_TOP_N)
        state, text = append_news_to_text(state, text)
        state = send_auto_deduped(state, "open", text, cooldown_sec=1800)
        return state

    # 10:10 pick
    state, picks, band = try_pick_once(state, symbols)
    if picks:
        text = build_pick_message(picks, state["watch"]["picked_at"], band)
        text += "\n\n" + build_movers_block(movers, MOVERS_TOP_N)
        state, text = append_news_to_text(state, text)
        state = send_auto_deduped(state, "pick", text, cooldown_sec=1800)
        return state

    # saat başı takip (11-17)
    if is_track_time_now():
        if state.get("watch", {}).get("symbols"):
            if should_send_track_now(state):
                text = build_track_message(state, title="✅ <b>Saatlik Takip (Saat Başı)</b>")
                text += "\n\n" + build_movers_block(movers, MOVERS_TOP_N)
                state, text = append_news_to_text(state, text)
                state = send_auto_deduped(state, "track", text, cooldown_sec=1500)
                state["last_track_sent_key"] = now_key_minute()
        else:
            # liste yoksa saat başı spam atma, sadece sessiz geç
            pass
        return state

    # 17:30 kapanış hazırlık
    if is_preclose_time_now():
        text = build_track_message(state, title="🟨 <b>Kapanış Hazırlık Raporu</b>")
        text += "\n\n" + build_movers_block(movers, MOVERS_TOP_N)
        state, text = append_news_to_text(state, text)
        state = send_auto_deduped(state, "preclose", text, cooldown_sec=1800)
        return state

    # 18:10 gün sonu
    if is_close_report_time_now():
        if state.get("eod_sent_day") == today_str_tr():
            return state

        watch = state.get("watch", {})
        symbols_w = watch.get("symbols", [])
        baseline = watch.get("baseline", {})
        picked_at = watch.get("picked_at", "")

        lines = []
        lines.append("🏁 <b>GÜN SONU RAPORU</b> – TAIPO BIST")
        lines.append(f"🕒 {now_str_tr()}")
        if picked_at:
            lines.append(f"🎯 Seçim: {picked_at}")
        lines.append("")

        if not symbols_w:
            lines.append("⚠️ Bugün watchlist oluşmadı.")
        else:
            hits = 0
            for sym in symbols_w:
                q = fetch_quote(sym)
                if not q:
                    lines.append(f"• <code>{clean_sym(sym)}</code> → veri yok")
                    continue
                base = float(baseline.get(sym, q["prev_close"]) or q["prev_close"])
                pct_from_base = ((float(q["price"]) - base) / base) * 100.0
                if pct_from_base >= 0:
                    hits += 1
                lines.append(
                    f"• <code>{clean_sym(sym)}</code>  {base:.2f} → {q['price']:.2f}  {trend_emoji(pct_from_base)} {pct_str(pct_from_base)}"
                )
            lines.append("")
            lines.append(f"✅ Gün sonu durum: <b>{hits}/{len(symbols_w)}</b> artıda")

        text = "\n".join(lines) + "\n\n" + build_movers_block(movers, MOVERS_TOP_N)
        state, text = append_news_to_text(state, text)
        state = send_auto_deduped(state, "close", text, cooldown_sec=1800)
        state["eod_sent_day"] = today_str_tr()
        return state

    # AUTO çalıştı ama saat değil -> hiçbir şey atma
    return state


def run_command_listener(state):
    last_update_id = int(state.get("last_update_id", 0))
    updates = get_updates(last_update_id + 1)
    max_uid = last_update_id

    symbols = None  # lazy load

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

        low = text.lower().strip()
        cid = msg_chat_id(msg)

        # /ping
        if low.startswith("/ping"):
            title = msg_chat_title(msg)
            reply = f"🏓 <b>PONG</b>\n🕒 {now_str_tr()}"
            if title:
                reply += f"\n👥 <b>Grup:</b> {_escape_html(title)}"
            send_message(reply, chat_id=cid)
            continue

        # /help
        if low.startswith("/help") or low.startswith("/taipohelp") or low.startswith("/taipo help"):
            send_message(build_help_message(), chat_id=cid)
            continue

        # /id
        if low.startswith("/id"):
            now_ts = int(time.time())
            last_ts = int(state.get("last_id_reply_ts", 0))
            if now_ts - last_ts >= ID_COOLDOWN_SEC:
                title = msg_chat_title(msg)
                reply = f"🆔 <b>Chat ID:</b> <code>{cid}</code>"
                if title:
                    reply += f"\n👥 <b>Grup:</b> {_escape_html(title)}"
                send_message(reply, chat_id=cid)
                state["last_id_reply_ts"] = now_ts
            continue

        # /taipo*
        if low.startswith("/taipo"):
            now_ts = int(time.time())
            last_ts = int(state.get("last_command_reply_ts", 0))
            if now_ts - last_ts < REPLY_COOLDOWN_SEC:
                continue

            if symbols is None:
                symbols = load_symbols()

            parts = low.split()
            mode = "default"
            if len(parts) >= 2:
                mode = parts[1].strip()

            movers = []
            if symbols:
                state, movers, _ = get_movers_cached(state, symbols)

            header = f"🛰️ <b>TAIPO • BIST RADAR</b>\n🕒 {now_str_tr()}\n"

            if mode in ("news",):
                base = header + "\n📰 <b>Haber Modu</b>"
                state, base = append_news_to_text(state, base)
                send_message(base, chat_id=cid)

            elif mode in ("top",):
                base = header + "\n\n" + build_movers_block(movers, MOVERS_TOP_N)
                send_message(base, chat_id=cid)

            elif mode in ("pro",):
                blocks = [header]
                if state.get("watch", {}).get("symbols"):
                    blocks.append(build_track_message(state, title="✅ <b>Watchlist Detay</b>"))
                else:
                    blocks.append(
                        "⚠️ Bugün liste henüz oluşmadı.\n"
                        "⏰ Seçim: <b>10:10</b> (10:00–10:10 aralığının sonucu)\n"
                        "🎯 Band hedef: 0.40% – 0.90% (auto genişler)\n"
                        "🕒 Saatlik takip: 11:00–17:00 saat başı\n"
                        "🟨 17:30 kapanış hazırlık • 🏁 18:10 gün sonu\n"
                    )
                blocks.append(build_movers_block(movers, MOVERS_TOP_N))
                blocks.append("⌨️ <code>/taipo top</code> | <code>/taipo news</code> | <code>/taipo</code>")
                text_out = "\n\n".join(blocks)
                state, text_out = append_news_to_text(state, text_out)
                send_message(text_out, chat_id=cid)

            else:
                blocks = [header]
                if state.get("watch", {}).get("symbols"):
                    blocks.append("✅ <b>Watchlist</b>: bugün seçilen 3 hisse mevcut. (Detay: <code>/taipo pro</code>)")
                else:
                    blocks.append("⚠️ Bugün liste henüz oluşmadı. (10:10'da oluşur)")
                blocks.append(build_movers_block(movers, MOVERS_TOP_N))
                blocks.append("⌨️ <code>/taipo pro</code> | <code>/taipo top</code> | <code>/taipo news</code> | <code>/taipo help</code>")
                text_out = "\n\n".join(blocks)
                state, text_out = append_news_to_text(state, text_out)
                send_message(text_out, chat_id=cid)

            state["last_command_reply_ts"] = now_ts
            continue

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

# =========================
# MAIN
# =========================
def main():
    ensure_files()
    state = load_json(STATE_FILE, {})
    state = ensure_today_state(state)

    # COMMAND her zaman dinlenebilir (AUTO çalışsa bile)
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
