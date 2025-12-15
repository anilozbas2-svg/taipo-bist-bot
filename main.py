import os
import json
import time
import requests
from datetime import datetime

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]  # grup chat id (ör: -100xxxx)
STATE_FILE = "state.json"

# Şimdilik sağlam çalışan BIST sembolleri (XU100.IS gibi endeksleri şimdilik çıkar)
DEFAULT_WATCHLIST = [
    "ASELS.IS", "THYAO.IS", "BIMAS.IS", "KCHOL.IS", "SISE.IS",
    "SAHOL.IS", "TUPRS.IS", "AKBNK.IS", "GARAN.IS", "YKBNK.IS"
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

# ---------------- Yahoo Finance ----------------

def fetch_yahoo_quote(symbol: str):
    """
    Yahoo quote endpoint: query1.finance.yahoo.com/v7/finance/quote?symbols=ASELS.IS
    Dönüş: (price, change_percent) veya Exception
    """
    url = "https://query1.finance.yahoo.com/v7/finance/quote"
    params = {"symbols": symbol}

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "en-US,en;q=0.9,tr;q=0.8",
        "Connection": "close",
    }

    r = requests.get(url, params=params, timeout=30, headers=headers)

    # 403/429 vs gibi durumları net görelim diye status'u kontrol ediyoruz
    if r.status_code != 200:
        raise Exception(f"HTTP {r.status_code} - {r.text[:120]}")

    data = r.json()
    result = (((data or {}).get("quoteResponse") or {}).get("result") or [])
    if not result:
        raise Exception("Empty result (symbol not found?)")

    item = result[0]
    price = item.get("regularMarketPrice", None)
    chg_pct = item.get("regularMarketChangePercent", None)

    if price is None:
        raise Exception("regularMarketPrice missing")

    return price, chg_pct

def build_radar_text():
    rows = []
    errors = []

    for sym in DEFAULT_WATCHLIST:
        try:
            price, chg_pct = fetch_yahoo_quote(sym)
            if chg_pct is None:
                chg_str = "n/a"
            else:
                chg_str = f"{chg_pct:.2f}%"
            rows.append((sym, price, chg_str))
        except Exception as e:
            errors.append(f"{sym}: {str(e)}")

    # Başlık
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    text = f"📡 TAIPO-BIST RADAR\n🕒 {now}\n"

    if not rows:
        text += "\n⚠️ Veri çekilemedi."
        if errors:
            text += "\n\nHata örnekleri:\n" + "\n".join([f"• {e}" for e in errors[:8]])
        else:
            text += "\n(Hata detayı gelmedi)"
        return text[:3800]

    # Liste
    text += "\nSemboller:\n"
    for sym, price, chg_str in rows:
        text += f"• {sym} → {price} ({chg_str})\n"

    # Eğer hata varsa en sona ekle (çok uzamasın)
    if errors:
        text += "\n⚠️ Bazı semboller çekilemedi:\n"
        text += "\n".join([f"• {e}" for e in errors[:6]])

    # Komut hatırlatma
    text += "\n\nKomut: /taipo"
    return text[:3800]

# ---------------- Main ----------------

def main():
    state = load_state()
    last_update_id = int(state.get("last_update_id", 0))

    # 1) Telegram komut dinleme (sadece /taipo)
    try:
        data = get_updates(offset=last_update_id + 1)
        updates = data.get("result", []) or []

        max_update_id = last_update_id

        for upd in updates:
            uid = upd.get("update_id", 0)
            if uid > max_update_id:
                max_update_id = uid

            text, msg = extract_text(upd)
            if not text or not msg:
                continue

            # sadece hedef gruptan /taipo gelirse yanıt ver
            if is_from_target_group(msg) and text.lower().startswith("/taipo"):
                send_message(build_radar_text())

        # state güncelle
        if max_update_id != last_update_id:
            state["last_update_id"] = max_update_id
            save_state(state)

    except Exception as e:
        # Komut dinlemede sorun olsa bile scheduled mesajı atmaya devam edelim
        send_message(f"⚠️ TAIPO-BIST RADAR\nTelegram update kontrolünde hata: {str(e)[:250]}")

    # 2) Scheduled çalışmada otomatik mesaj (her 2 saatte bir)
    # GitHub Actions schedule ile burası zaten tetikleniyor.
    # İstersen bunu kapatabiliriz; şimdilik açık:
    try:
        send_message(build_radar_text())
    except Exception as e:
        send_message(f"⚠️ TAIPO-BIST RADAR\nOtomatik gönderimde hata: {str(e)[:250]}")

if __name__ == "__main__":
    main()
