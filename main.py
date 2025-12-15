import os
import json
import requests
from datetime import datetime

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]  # grup chat id (ör: -100xxxx)
STATE_FILE = "state.json"

# BIST sembolleri (TradingView formatı: BIST:ASELS)
DEFAULT_WATCHLIST = [
    "ASELS", "THYAO", "BIMAS", "KCHOL", "SISE",
    "SAHOL", "TUPRS", "AKBNK", "GARAN", "YKBNK"
]

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"last_update_id": 0}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def send_message(text: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    r = requests.post(url, data={"chat_id": CHAT_ID, "text": text})
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
    text = (msg.get("text") or "").strip()
    return text, msg

def is_from_target_group(msg):
    chat = msg.get("chat") or {}
    return str(chat.get("id")) == str(CHAT_ID)

# ---------------- TradingView (BIST Scanner) ----------------

def fetch_tradingview_quotes(symbols):
    """
    TradingView scanner:
    POST https://scanner.tradingview.com/turkey/scan
    Döndürür: { "data": [ { "s":"BIST:ASELS", "d":[close, chg, chg_pct] }, ... ] }
    """
    url = "https://scanner.tradingview.com/turkey/scan"

    tv_symbols = [f"BIST:{s}" for s in symbols]

    payload = {
        "symbols": {"tickers": tv_symbols},
        "columns": [
            "close",                     # son fiyat
            "change",                    # değişim
            "change_abs",                # bazen farklı gelebilir (opsiyon)
            "change_percent"             # yüzde değişim
        ]
    }

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Content-Type": "application/json"
    }

    r = requests.post(url, json=payload, headers=headers, timeout=30)
    if r.status_code != 200:
        raise Exception(f"TradingView HTTP {r.status_code} - {r.text[:120]}")

    data = r.json()
    rows = data.get("data") or []
    out = {}

    for row in rows:
        sym = row.get("s", "")
        d = row.get("d") or []
        # d[0]=close, d[1]=change, d[3]=change_percent (bazı durumlarda index kayabilir)
        close = d[0] if len(d) > 0 else None
        chg = d[1] if len(d) > 1 else None
        chg_pct = d[3] if len(d) > 3 else None

        out[sym.replace("BIST:", "")] = (close, chg_pct, chg)

    return out

def build_radar_text():
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    text = f"📡 TAIPO-BIST RADAR\n🕒 {now}\n"

    try:
        quotes = fetch_tradingview_quotes(DEFAULT_WATCHLIST)
    except Exception as e:
        return (text + f"\n⚠️ Veri çekilemedi (TradingView).\nHata: {str(e)[:400]}")[:3800]

    rows = []
    missing = []

    for s in DEFAULT_WATCHLIST:
        if s in quotes and quotes[s][0] is not None:
            price, chg_pct, _chg = quotes[s]
            chg_str = "n/a" if chg_pct is None else f"{float(chg_pct):.2f}%"
            rows.append((s, price, chg_str))
        else:
            missing.append(s)

    if not rows:
        text += "\n⚠️ Veri geldi ama fiyat yok."
        if missing:
            text += "\nEksik: " + ", ".join(missing)
        return text[:3800]

    text += "\nSemboller:\n"
    for sym, price, chg_str in rows:
        text += f"• {sym} → {price} ({chg_str})\n"

    if missing:
        text += "\n⚠️ Eksik kalanlar: " + ", ".join(missing)

    text += "\n\nKomut: /taipo"
    return text[:3800]

# ---------------- Main ----------------

def main():
    state = load_state()
    last_update_id = int(state.get("last_update_id", 0))

    # 1) /taipo komutu dinle
    try:
        data = get_updates(offset=last_update_id + 1)
        updates = data.get("result", []) or []
        max_update_id = last_update_id

        for upd in updates:
            uid = upd.get("update_id", 0)
            max_update_id = max(max_update_id, uid)

            text, msg = extract_text(upd)
            if not text or not msg:
                continue

            if is_from_target_group(msg) and text.lower().startswith("/taipo"):
                send_message(build_radar_text())

        if max_update_id != last_update_id:
            state["last_update_id"] = max_update_id
            save_state(state)

    except Exception as e:
        send_message(f"⚠️ TAIPO-BIST RADAR\nTelegram update kontrolünde hata: {str(e)[:250]}")

    # 2) Otomatik (schedule) mesaj
    try:
        send_message(build_radar_text())
    except Exception as e:
        send_message(f"⚠️ TAIPO-BIST RADAR\nOtomatik gönderimde hata: {str(e)[:250]}")

if __name__ == "__main__":
    main()
