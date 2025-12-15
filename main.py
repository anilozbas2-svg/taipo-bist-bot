import os
import json
import requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]  # grup id: -100xxxxxx veya özel chat id

STATE_FILE = "state.json"
BIST_LIST_FILE = "bist100.txt"   # repo ana dizininde olacak

IST_TZ = ZoneInfo("Europe/Istanbul")


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


def read_bist100_symbols():
    """
    bist100.txt satır satır: ASELS, THYAO, BIMAS ... (sadece kod)
    """
    if not os.path.exists(BIST_LIST_FILE):
        raise FileNotFoundError(f"{BIST_LIST_FILE} bulunamadı. Repo ana dizinine ekle.")

    symbols = []
    with open(BIST_LIST_FILE, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip().upper()
            if not s or s.startswith("#"):
                continue
            symbols.append(s)

    # 100 olması ideal ama az/çok olursa da çalışır
    return symbols


def yahoo_quote(symbols):
    """
    Yahoo Finance public quote endpoint.
    symbols: ['ASELS','THYAO',...]
    returns: list of dict {symbol, price, chg, chg_pct}
    """
    # Yahoo BIST sembol formatı: ASELS.IS
    yahoo_syms = [f"{s}.IS" for s in symbols]
    url = "https://query1.finance.yahoo.com/v7/finance/quote"
    params = {"symbols": ",".join(yahoo_syms)}

    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()

    results = []
    for item in data.get("quoteResponse", {}).get("result", []):
        ysym = item.get("symbol", "")
        base = ysym.replace(".IS", "")
        price = item.get("regularMarketPrice")
        chg = item.get("regularMarketChange")
        chg_pct = item.get("regularMarketChangePercent")

        if price is None or chg is None or chg_pct is None:
            continue

        results.append({
            "symbol": base,
            "price": float(price),
            "chg": float(chg),
            "chg_pct": float(chg_pct),
        })

    return results


def build_radar_message(rows, now_tr: datetime):
    rows_sorted = sorted(rows, key=lambda x: x["chg_pct"], reverse=True)
    top3 = rows_sorted[:3]
    bot3 = rows_sorted[-3:][::-1]  # en kötüden iyiye doğru

    header = f"📡 TAIPO-BIST RADAR\n🕒 {now_tr.strftime('%d.%m.%Y %H:%M')}"

    lines = [header, ""]
    lines.append("🔥 En Güçlü 3 (ALIM yapılabilir ✅):")
    for r in top3:
        lines.append(f"• {r['symbol']} → {r['price']:.2f} ({r['chg_pct']:.2f}%, Δ {r['chg']:.2f})")

    lines.append("")
    lines.append("🧊 En Zayıf 3 (İZLE ⚠️):")
    for r in bot3:
        lines.append(f"• {r['symbol']} → {r['price']:.2f} ({r['chg_pct']:.2f}%, Δ {r['chg']:.2f})")

    lines.append("")
    lines.append("Komut: /taipo")
    return "\n".join(lines)


def get_updates(offset: int):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    params = {
        "timeout": 0,
        "offset": offset,
        "allowed_updates": ["message", "edited_message"],
    }
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def extract_message(update):
    msg = update.get("message") or update.get("edited_message") or {}
    text = (msg.get("text") or "").strip()
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    return text, chat_id


def main():
    now_tr = datetime.now(timezone.utc).astimezone(IST_TZ)

    # 1) Komutla cevap (/taipo)
    state = load_state()
    last_update_id = int(state.get("last_update_id", 0))

    try:
        updates = get_updates(last_update_id + 1).get("result", [])
    except Exception:
        updates = []

    responded = False
    max_update_id = last_update_id

    for upd in updates:
        uid = upd.get("update_id", 0)
        if uid > max_update_id:
            max_update_id = uid

        text, chat_id = extract_message(upd)
        if not text:
            continue

        # sadece aynı chat'ten gelen komutlara cevap
        if str(chat_id) == str(CHAT_ID) and text.lower().startswith("/taipo"):
            try:
                symbols = read_bist100_symbols()
                rows = yahoo_quote(symbols)
                if not rows:
                    send_message("⚠️ TAIPO-BIST RADAR\nVeri çekilemedi (boş sonuç).")
                else:
                    send_message(build_radar_message(rows, now_tr))
                responded = True
            except Exception as e:
                send_message(f"⚠️ TAIPO-BIST RADAR\nVeri çekilemedi.\nHata: {e}")

    # state güncelle
    if max_update_id != last_update_id:
        state["last_update_id"] = max_update_id
        save_state(state)

    # 2) Otomatik saatlik mesaj (schedule ile)
    # Komutla cevap verdiyse bile sorun değil: istersen burada otomatiği kapatırız.
    if not responded:
        try:
            symbols = read_bist100_symbols()
            rows = yahoo_quote(symbols)
            if not rows:
                send_message("⚠️ TAIPO-BIST RADAR\nVeri çekilemedi (boş sonuç).")
            else:
                send_message(build_radar_message(rows, now_tr))
        except Exception as e:
            send_message(f"⚠️ TAIPO-BIST RADAR\nVeri çekilemedi.\nHata: {e}")


if __name__ == "__main__":
    main()
