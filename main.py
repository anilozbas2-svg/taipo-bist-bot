import os
import json
import time
import requests

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]  # grup id (ör: -100xxxxxx)

STATE_FILE = "state.json"
DEFAULT_WATCHLIST = [
    "XU100.IS",
    "ASELS.IS", "THYAO.IS", "BIMAS.IS", "KCHOL.IS", "SISE.IS",
    "SAHOL.IS", "TUPRS.IS", "AKBNK.IS", "GARAN.IS", "YKBNK.IS"
]

# İstersen GitHub Secrets'a WATCHLIST ekleyebilirsin:
# Örn: "ASELS.IS,THYAO.IS,BIMAS.IS,KCHOL.IS"
WATCHLIST_ENV = os.getenv("WATCHLIST", "").strip()


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"last_update_id": 0}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)


def send_message(text: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    r = requests.post(url, data={"chat_id": CHAT_ID, "text": text}, timeout=30)
    r.raise_for_status()


def get_updates(offset: int):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    params = {
        "timeout": 0,
        "offset": offset,
        "allowed_updates": ["message", "edited_message"],
    }
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def extract_text(update):
    msg = update.get("message") or update.get("edited_message") or {}
    return (msg.get("text") or "").strip(), msg


def is_from_target_group(msg):
    chat = msg.get("chat") or {}
    return str(chat.get("id")) == str(CHAT_ID)


def get_watchlist():
    if WATCHLIST_ENV:
        items = [x.strip() for x in WATCHLIST_ENV.split(",") if x.strip()]
        return items if items else DEFAULT_WATCHLIST
    return DEFAULT_WATCHLIST


def fetch_yahoo_quote(symbol: str):
    # Yahoo chart endpoint
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"range": "2d", "interval": "1d"}  # kapanış / önceki kapanış için yeterli
    r = requests.get(url, params=params, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    data = r.json()

    chart = (data.get("chart") or {})
    err = chart.get("error")
    if err:
        raise RuntimeError(f"{symbol} error: {err}")

    result = (chart.get("result") or [None])[0] or {}
    meta = result.get("meta") or {}

    price = meta.get("regularMarketPrice")
    prev_close = meta.get("previousClose")
    currency = meta.get("currency", "TRY")

    if price is None or prev_close in (None, 0):
        raise RuntimeError(f"{symbol} missing price/prev_close")

    chg = price - prev_close
    chg_pct = (chg / prev_close) * 100.0

    return {
        "symbol": symbol,
        "price": float(price),
        "prev_close": float(prev_close),
        "chg": float(chg),
        "chg_pct": float(chg_pct),
        "currency": currency,
    }


def build_radar_text():
    wl = get_watchlist()
    rows = []
    errors = []

    for sym in wl:
        try:
            q = fetch_yahoo_quote(sym)
            rows.append(q)
            time.sleep(0.25)  # nazik olalım (rate-limit ihtimalini azaltır)
        except Exception as e:
            errors.append(f"{sym}: {e}")

    if not rows:
        return "⚠️ TAIPO-BIST RADAR\nVeri çekilemedi. (Yahoo erişim/isim hatası olabilir)"

    # en çok yükselen/düşen
    rows_sorted = sorted(rows, key=lambda x: x["chg_pct"], reverse=True)
    top_up = rows_sorted[:3]
    top_down = list(reversed(rows_sorted[-3:]))

    def fmt_line(x):
        sign = "+" if x["chg_pct"] >= 0 else ""
        # fiyat formatı basit
        return f"{x['symbol']}  {x['price']:.2f}  ({sign}{x['chg_pct']:.2f}%)"

    lines = []
    lines.append("📊 TAIPO-BIST RADAR (Yahoo Finance)")
    lines.append("—")
    lines.append("🔥 En çok yükselenler:")
    for x in top_up:
        lines.append("• " + fmt_line(x))
    lines.append("—")
    lines.append("🧊 En çok düşenler:")
    for x in top_down:
        lines.append("• " + fmt_line(x))
    lines.append("—")
    lines.append("📌 İzleme listesi (özet):")
    for x in rows_sorted:
        lines.append("• " + fmt_line(x))

    if errors:
        lines.append("—")
        lines.append("⚠️ Çekilemeyenler:")
        for e in errors[:5]:
            lines.append("• " + e)

    lines.append("—")
    lines.append("Komut: /taipo  |  Otomatik: 2 saatte bir")

    # Telegram mesaj limiti için çok uzarsa kırpalım
    text = "\n".join(lines)
    return text[:3800]


def main():
    state = load_state()
    last_update_id = int(state.get("last_update_id", 0))

    # 1) Komut var mı? (gruptan /taipo gelirse cevap ver)
    try:
        data = get_updates(offset=last_update_id + 1)
        updates = data.get("result", [])
    except Exception:
        updates = []

    max_update_id = last_update_id
    responded = False

    for upd in updates:
        uid = upd.get("update_id", 0)
        if uid > max_update_id:
            max_update_id = uid

        text, msg = extract_text(upd)
        if not msg:
            continue

        if is_from_target_group(msg) and text.lower().startswith("/taipo"):
            send_message(build_radar_text())
            responded = True

    state["last_update_id"] = max_update_id
    save_state(state)

    # 2) Otomatik gönderim (Actions her 2 saatte bir çalıştığında)
    # Not: Eğer az önce /taipo cevapladıysa yine de otomatik atmak istersen responded kontrolünü kaldır.
    if not responded:
        send_message(build_radar_text())


if __name__ == "__main__":
    main()
