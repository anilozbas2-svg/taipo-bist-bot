import os
import json
import requests
from datetime import datetime
import yfinance as yf

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

STATE_FILE = "state.json"
BIST_FILE = "bist100.txt"


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"last_update_id": 0}


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)


def send_message(text: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": CHAT_ID, "text": text})
    

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


def load_symbols():
    # bist100.txt içindeki tekrarları otomatik temizler
    with open(BIST_FILE, "r", encoding="utf-8") as f:
        raw = [line.strip().upper() for line in f if line.strip()]
    # sırayı bozmadan unique
    unique = list(dict.fromkeys(raw))
    return unique


def fetch_data(symbol: str):
    # yfinance: BIST sembolleri ".IS" ile okunur
    try:
        ticker = yf.Ticker(symbol + ".IS")
        data = ticker.history(period="2d")
        if data is None or len(data) < 2:
            return None

        prev_close = float(data["Close"].iloc[-2])
        last_close = float(data["Close"].iloc[-1])

        if prev_close == 0:
            return None

        change_pct = ((last_close - prev_close) / prev_close) * 100.0
        change = last_close - prev_close

        return {
            "symbol": symbol,
            "price": round(last_close, 2),
            "change_pct": round(change_pct, 2),
            "change": round(change, 2),
        }
    except Exception:
        return None


def build_radar():
    symbols = load_symbols()
    results = []

    for sym in symbols:
        d = fetch_data(sym)
        if d:
            results.append(d)

    now = datetime.now().strftime("%d.%m.%Y %H:%M")

    if not results:
        return f"📡 TAIPO-BIST RADAR\n🕒 {now}\n\n⚠️ Veri çekilemedi."

    strongest = sorted(results, key=lambda x: x["change_pct"], reverse=True)[:3]
    weakest = sorted(results, key=lambda x: x["change_pct"])[:3]

    text = f"📡 TAIPO-BIST RADAR\n🕒 {now}\n\n"
    text += "🟢 EN GÜÇLÜ 3 (AL TAKİP)\n"
    for s in strongest:
        text += f"• {s['symbol']} → {s['price']} (%{s['change_pct']}, Δ{s['change']})\n"

    text += "\n🔴 EN ZAYIF 3 (İZLE / RİSK)\n"
    for w in weakest:
        text += f"• {w['symbol']} → {w['price']} (%{w['change_pct']}, Δ{w['change']})\n"

    text += "\nKomut: /taipo"
    return text


def main():
    state = load_state()
    last_update_id = int(state.get("last_update_id", 0))

    # 1) Telegram'dan /taipo var mı? varsa EXTRA mesaj at
    try:
        updates = get_updates(last_update_id + 1)
        updates = updates.get("result", [])
    except Exception:
        updates = []

    max_update_id = last_update_id

    for upd in updates:
        uid = upd.get("update_id", 0)
        if uid > max_update_id:
            max_update_id = uid

        text, msg = extract_text(upd)
        if not text:
            continue

        # sadece hedef gruptan gelen komutları dinle
        if is_from_target_group(msg) and text.lower().startswith("/taipo"):
            # EXTRA radar (komuta özel)
            send_message(build_radar())

    # state kaydet
    if max_update_id != last_update_id:
        state["last_update_id"] = max_update_id
        save_state(state)

    # 2) Her workflow çalıştığında OTOMATİK radar mesajı at (her saat)
    send_message(build_radar())


if __name__ == "__main__":
    main()
