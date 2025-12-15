import os
import json
import time
from datetime import datetime, timedelta

import requests
import yfinance as yf


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
TARGET_CHAT_ID = os.getenv("CHAT_ID", "").strip()  # grup chat id: -5049...
MODE = os.getenv("MODE", "AUTO").strip().upper()   # AUTO veya LISTEN

STATE_FILE = "state.json"
DAILY_FILE = "daily_watch.json"
BIST_FILE = "bist100.txt"

TZ_OFFSET_HOURS = 3  # Türkiye UTC+3


# -------------------------
# Utilities
# -------------------------
def now_tr():
    return datetime.utcnow() + timedelta(hours=TZ_OFFSET_HOURS)

def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def load_state():
    return load_json(STATE_FILE, {"last_update_id": 0})

def save_state(state):
    save_json(STATE_FILE, state)

def read_bist_symbols():
    # bist100.txt içinde örnek: ASELS, BIMAS, ASTOR...
    symbols = []
    try:
        with open(BIST_FILE, "r", encoding="utf-8") as f:
            for line in f:
                s = line.strip().upper()
                if not s:
                    continue
                # zaten .IS ise dokunma
                if s.endswith(".IS"):
                    symbols.append(s)
                else:
                    symbols.append(s + ".IS")
    except Exception:
        pass
    return list(dict.fromkeys(symbols))  # uniq, order preserved


# -------------------------
# Telegram
# -------------------------
def tg_api(method):
    return f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"

def get_updates(offset):
    try:
        r = requests.get(
            tg_api("getUpdates"),
            params={"offset": offset, "timeout": 20, "allowed_updates": ["message"]},
            timeout=25
        )
        return r.json() if r.ok else {"ok": False, "result": []}
    except Exception:
        return {"ok": False, "result": []}

def send_message(chat_id, text):
    if not BOT_TOKEN or not chat_id:
        return
    try:
        requests.post(
            tg_api("sendMessage"),
            data={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            },
            timeout=20
        )
    except Exception:
        pass

def extract_text_and_chat(update):
    msg = update.get("message") or {}
    text = (msg.get("text") or "").strip()
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    return text, chat_id, msg

def is_command(text, cmd):
    # /taipo veya /taipo@BotUser
    t = (text or "").strip()
    if not t.startswith("/"):
        return False
    first = t.split()[0]  # sadece ilk token
    first = first.split("@")[0].lower()
    return first == cmd.lower()


# -------------------------
# Market Data
# -------------------------
def batch_download_last_prices(tickers):
    """
    100 hisseyi tek tek çağırmak yerine batch indir.
    1 dakikalık veri: bugünkü son fiyatı yakalar.
    """
    if not tickers:
        return {}

    try:
        df = yf.download(
            tickers=tickers,
            period="1d",
            interval="1m",
            group_by="ticker",
            threads=True,
            progress=False
        )
    except Exception:
        return {}

    prices = {}

    # Tek ticker gelirse kolon yapısı farklı olabiliyor
    if isinstance(df.columns, pd.MultiIndex) if "pd" in globals() else False:
        pass

    # yfinance dönüşleri bazen karışık: iki durumu da handle edelim
    try:
        # Çoklu ticker: df['Close'][TICKER] veya df[(TICKER,'Close')]
        if hasattr(df.columns, "levels") and len(df.columns.levels) == 2:
            # MultiIndex: (PriceType, Ticker) ya da (Ticker, PriceType) olabiliyor
            # En sağlamı: her ticker için close kolonunu bul.
            for t in tickers:
                close_series = None
                # olası 2 düzen
                if (t, "Close") in df.columns:
                    close_series = df[(t, "Close")]
                elif ("Close", t) in df.columns:
                    close_series = df[("Close", t)]
                if close_series is not None and len(close_series.dropna()) > 0:
                    prices[t] = float(close_series.dropna().iloc[-1])
        else:
            # Tek ticker: df['Close']
            if "Close" in df.columns and len(df["Close"].dropna()) > 0 and len(tickers) == 1:
                prices[tickers[0]] = float(df["Close"].dropna().iloc[-1])
    except Exception:
        return {}

    # Eğer batch boş kaldıysa fast_info fallback (yavaş ama çalışır)
    if not prices:
        for t in tickers[:20]:  # limit koyuyoruz, yoksa 100 tane çok uzar
            try:
                ti = yf.Ticker(t)
                lp = ti.fast_info.get("last_price")
                if lp:
                    prices[t] = float(lp)
            except Exception:
                continue

    return prices

def get_prev_close(ticker):
    try:
        h = yf.Ticker(ticker).history(period="5d", interval="1d")
        h = h.dropna()
        if len(h) >= 2:
            return float(h["Close"].iloc[-2])
        elif len(h) == 1:
            return float(h["Close"].iloc[-1])
    except Exception:
        return None
    return None


# -------------------------
# Daily Watch Logic
# -------------------------
def load_daily():
    return load_json(DAILY_FILE, {})

def reset_daily_if_new_day():
    d = load_daily()
    today = now_tr().strftime("%Y-%m-%d")
    if d.get("date") and d.get("date") != today:
        save_json(DAILY_FILE, {})  # reset

def pick_daily_top3():
    """
    10:01 civarı çalışır:
    - BIST100 tarar
    - anlık fiyat / önceki kapanış ile % değişime göre en güçlü 3 seçer
    - daily_watch.json içine kaydeder
    """
    symbols = read_bist_symbols()
    if not symbols:
        return None

    # Anlık fiyatları batch çek
    last_prices = batch_download_last_prices(symbols)
    if not last_prices:
        return None

    scored = []
    for sym, lastp in last_prices.items():
        pc = get_prev_close(sym)
        if not pc or pc <= 0:
            continue
        chg = ((lastp - pc) / pc) * 100.0
        scored.append((sym, lastp, pc, chg))

    scored.sort(key=lambda x: x[3], reverse=True)
    top3 = scored[:3]
    if not top3:
        return None

    payload = {
        "date": now_tr().strftime("%Y-%m-%d"),
        "picked_at": now_tr().strftime("%d.%m.%Y %H:%M"),
        "watch": [
            {
                "symbol": s.replace(".IS", ""),
                "ticker": s,
                "base_price": round(lp, 2),      # 10:01 anlık
                "prev_close": round(pc, 2),
            }
            for (s, lp, pc, _) in top3
        ]
    }
    save_json(DAILY_FILE, payload)
    return payload

def build_watch_message(daily):
    """
    Saatlik takip mesajı (hep aynı 3 hisse)
    - base -> % değişim
    """
    watch = daily.get("watch") or []
    if not watch:
        return None

    tickers = [w["ticker"] for w in watch]
    last_prices = batch_download_last_prices(tickers)

    lines = []
    header = (
        f"📡 <b>TAIPO-BIST WATCH</b>\n"
        f"🕒 {now_tr().strftime('%d.%m.%Y %H:%M')}\n"
        f"🎯 Günün 3'lüsü (10:01 seçimi)\n"
        f"Seçim zamanı: {daily.get('picked_at','-')}\n"
        f"\n"
    )
    for w in watch:
        t = w["ticker"]
        sym = w["symbol"]
        base = float(w["base_price"])
        lastp = float(last_prices.get(t, base))
        pct = ((lastp - base) / base) * 100.0 if base > 0 else 0.0
        lines.append(f"• {sym} → {round(lastp,2)}  (<b>%{round(pct,2)}</b>)")

    footer = "\n\nKomut: /taipo"
    return header + "\n".join(lines) + footer


# -------------------------
# Radar (mevcut gibi kalsın: top gainers/losers)
# -------------------------
def build_radar():
    symbols = read_bist_symbols()
    if not symbols:
        return "📡 TAIPO-BIST RADAR\nVeri yok (bist100.txt bulunamadı)."

    last_prices = batch_download_last_prices(symbols)
    if not last_prices:
        return "📡 TAIPO-BIST RADAR\nVeri çekilemedi (yfinance)."

    results = []
    for sym, lastp in last_prices.items():
        pc = get_prev_close(sym)
        if not pc or pc <= 0:
            continue
        pct = ((lastp - pc) / pc) * 100.0
        results.append((sym, lastp, pc, pct))

    if not results:
        return "📡 TAIPO-BIST RADAR\nVeri işlenemedi."

    strongest = sorted(results, key=lambda x: x[3], reverse=True)[:3]
    weakest = sorted(results, key=lambda x: x[3])[:3]

    text = f"📡 <b>TAIPO-BIST RADAR</b>\n🕒 {now_tr().strftime('%d.%m.%Y %H:%M')}\n\n"
    text += "🟢 <b>EN GÜÇLÜ 3 (AL TAKİP)</b>\n"
    for s, lp, pc, pct in strongest:
        text += f"• {s.replace('.IS','')} → {round(lp,2)}  (<b>%{round(pct,2)}</b>)\n"

    text += "\n🔴 <b>EN ZAYIF 3 (İZLE / RİSK)</b>\n"
    for s, lp, pc, pct in weakest:
        text += f"• {s.replace('.IS','')} → {round(lp,2)}  (<b>%{round(pct,2)}</b>)\n"

    text += "\nKomut: /taipo"
    return text


# -------------------------
# AUTO (schedule) runner
# -------------------------
def auto_run():
    """
    Bu sadece main.yml (MODE=AUTO) için:
    - 10:01'de günlük 3'lü seçer (daily_watch.json yazar)
    - 10:00-18:00 arası saat başı bu 3'lüye takip mesajı atar
    """
    reset_daily_if_new_day()

    t = now_tr()
    hm = t.strftime("%H:%M")

    # 10:01 seçimi (en geç 10:02 gelsin diye)
    if hm in ["10:01", "10:02"]:
        daily = pick_daily_top3()
        if daily and TARGET_CHAT_ID:
            msg = build_watch_message(daily)
            if msg:
                send_message(TARGET_CHAT_ID, msg)
        return

    # Saat başı takip (10:00-18:00)
    if t.hour >= 10 and t.hour <= 18 and t.minute == 0:
        daily = load_daily()
        if daily and daily.get("date") == t.strftime("%Y-%m-%d") and TARGET_CHAT_ID:
            msg = build_watch_message(daily)
            if msg:
                send_message(TARGET_CHAT_ID, msg)


# -------------------------
# LISTEN (command) runner
# -------------------------
def listen_run():
    """
    Bu sadece command.yml (MODE=LISTEN) için:
    - Telegram update'leri okur
    - /taipo gelirse yanıt verir (private veya grup fark etmez)
    """
    state = load_state()
    last_update_id = int(state.get("last_update_id", 0))
    resp = get_updates(last_update_id + 1)

    updates = resp.get("result", []) if isinstance(resp, dict) else []
    max_update_id = last_update_id

    for upd in updates:
        uid = int(upd.get("update_id", 0))
        if uid > max_update_id:
            max_update_id = uid

        text, chat_id, msg = extract_text_and_chat(upd)
        if not text or not chat_id:
            continue

        if is_command(text, "/start"):
            send_message(chat_id, "✅ TAIPO-BIST hazır.\nKomut: /taipo")
            continue

        if is_command(text, "/taipo"):
            # Eğer daily_watch varsa günün 3'lüsüyle cevap ver, yoksa radar gönder
            reset_daily_if_new_day()
            daily = load_daily()
            today = now_tr().strftime("%Y-%m-%d")

            if daily and daily.get("date") == today and daily.get("watch"):
                msg_text = build_watch_message(daily)
            else:
                msg_text = build_radar()

            send_message(chat_id, msg_text)
            continue

    if max_update_id != last_update_id:
        state["last_update_id"] = max_update_id
        save_state(state)


def main():
    if not BOT_TOKEN:
        return

    if MODE == "LISTEN":
        listen_run()
    else:
        # AUTO default
        auto_run()


if __name__ == "__main__":
    main()
