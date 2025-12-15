import os
import json
import yfinance as yf
from datetime import datetime
import requests

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

STATE_FILE = "state.json"
BIST_FILE = "bist100.txt"


def send_message(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": CHAT_ID, "text": text})


def load_symbols():
    with open(BIST_FILE, "r") as f:
        return [line.strip() for line in f if line.strip()]


def fetch_data(symbol):
    try:
        stock = yf.Ticker(symbol + ".IS")
        data = stock.history(period="2d")
        if len(data) < 2:
            return None
        prev, last = data.iloc[-2], data.iloc[-1]
        change_pct = ((last["Close"] - prev["Close"]) / prev["Close"]) * 100
        return {
            "symbol": symbol,
            "price": round(last["Close"], 2),
            "change_pct": round(change_pct, 2),
            "change": round(last["Close"] - prev["Close"], 2),
        }
    except:
        return None


def build_radar():
    symbols = load_symbols()
    results = []

    for sym in symbols:
        data = fetch_data(sym)
        if data:
            results.append(data)

    strongest = sorted(results, key=lambda x: x["change_pct"], reverse=True)[:3]
    weakest = sorted(results, key=lambda x: x["change_pct"])[:3]

    now = datetime.now().strftime("%d.%m.%Y %H:%M")

    text = f"📡 TAIPO-BIST RADAR\n⏰ {now}\n\n"

    text += "🟢 EN GÜÇLÜ 3 (AL TAKİP)\n"
    for s in strongest:
        text += f"• {s['symbol']} → {s['price']} (%{s['change_pct']}, Δ{s['change']})\n"

    text += "\n🔴 EN ZAYIF 3 (İZLE / RİSK)\n"
    for s in weakest:
        text += f"• {s['symbol']} → {s['price']} (%{s['change_pct']}, Δ{s['change']})\n"

    text += "\nKomut: /taipo"

    return text


def main():
    radar_text = build_radar()
    send_message(radar_text)


if __name__ == "__main__":
    main()
