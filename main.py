import os
import json
import yfinance as yf
import requests
from datetime import datetime

BOT_TOKEN = os.environ["BOT_TOKEN"]
DEFAULT_CHAT_ID = os.environ.get("CHAT_ID", "")  # otomatik mesajın gideceği chat/grup
STATE_FILE = "state.json"
BIST_FILE = "bist100.txt"

API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"


def tg_send(chat_id: str, text: str):
    url = f"{API_BASE}/sendMessage"
    r = requests.post(url, data={"chat_id": chat_id, "text": text})
    return r


def load_symbols():
    with open(BIST_FILE, "r", encoding="utf-8") as f:
        return [line.strip().upper() for line in f if line.strip()]


def fetch_data(symbol: str):
    try:
        stock = yf.Ticker(symbol + ".IS")
        data = stock.history(period="2d")
        if len(data) < 2:
            return None
        prev, last = data.iloc[-2], data.iloc[-1]
        change_pct = ((last["Close"] - prev["Close"]) / prev["Close"]) * 100
        return {
            "symbol": symbol,
            "price": round(float(last["Close"]), 2),
            "change_pct": round(float(change_pct), 2),
            "change": round(float(last["Close"] - prev["Close"]), 2),
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
        return f"📡 TAIPO-BIST RADAR\n⏱ {now}\n\n⚠️ Veri çekilemedi."

    strongest = sorted(results, key=lambda x: x["change_pct"], reverse=True)[:3]
    weakest = sorted(results, key=lambda x: x["change_pct"])[:3]

    text = f"📡 TAIPO-BIST RADAR\n⏱ {now}\n\n"
    text += "🟢 EN GÜÇLÜ 3 (AL TAKİP)\n"
    for s in strongest:
        text += f"• {s['symbol']} → {s['price']} (%{s['change_pct']}, Δ{s['change']})\n"

    text += "\n🔴 EN ZAYIF 3 (İZLE / RİSK)\n"
    for s in weakest:
        text += f"• {s['symbol']} → {s['price']} (%{s['change_pct']}, Δ{s['change']})\n"

    text += "\nKomut: /taipo"
    return text


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)


def get_updates(offset=None):
    params = {"timeout": 0}
    if offset is not None:
        params["offset"] = offset
    url = f"{API_BASE}/getUpdates"
    r = requests.get(url, params=params)
    return r.json()


def handle_commands():
    """
    Workflow çalıştığında Telegram mesajlarını tarar.
    /start ve /taipo komutlarına, komutun geldiği chate cevap verir.
    """
    state = load_state()
    last_update_id = state.get("last_update_id")

    data = get_updates(offset=(last_update_id + 1) if last_update_id is not None else None)
    if not data.get("ok"):
        return

    updates = data.get("result", [])
    if not updates:
        return

    for upd in updates:
        state["last_update_id"] = upd.get("update_id", state.get("last_update_id"))

        msg = upd.get("message") or upd.get("edited_message")
        if not msg:
            continue

        chat_id = str((msg.get("chat") or {}).get("id"))
        text = (msg.get("text") or "").strip()

        if not text:
            continue

        if text.startswith("/start"):
            tg_send(chat_id, "✅ TAIPO-BIST hazır.\nKomut: /taipo")
            continue

        if text.startswith("/taipo"):
            tg_send(chat_id, build_radar())
            continue

    save_state(state)


def send_hourly_auto():
    """
    Cron tetiklenince otomatik mesaj atsın.
    """
    if not DEFAULT_CHAT_ID:
        return
    tg_send(DEFAULT_CHAT_ID, build_radar())


def main():
    # 1) Komutları yakala ve cevapla
    handle_commands()

    # 2) Otomatik mesaj
    send_hourly_auto()


if __name__ == "__main__":
    main()
