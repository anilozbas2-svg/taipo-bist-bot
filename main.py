import os
import json
import requests

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]  # grup chat id (ör: -100xxxx)

STATE_FILE = "state.json"


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
    return (msg.get("text") or "").strip(), msg


def is_from_target_group(msg):
    chat = msg.get("chat") or {}
    return str(chat.get("id")) == str(CHAT_ID)


def build_radar_text():
    # Şimdilik örnek cevap. Sonra buraya TAIPO-BIST radarını koyacağız.
    return "📡 TAİPO-BİST RADAR\n\n✅ Sistem aktif.\n🟢 Komut: /taipo\n⏱ Otomatik: 2 saatte bir çalışır."


def main():
    state = load_state()
    last_update_id = int(state.get("last_update_id", 0))

    # 1) Telegram'dan yeni mesajları çek (komut var mı bak)
    data = get_updates(offset=last_update_id + 1)
    updates = data.get("result", [])

    responded = False
    max_update_id = last_update_id

    for upd in updates:
        uid = upd.get("update_id", 0)
        if uid > max_update_id:
            max_update_id = uid

        text, msg = extract_text(upd)
        if not msg:
            continue

        # sadece bizim gruptan gelen komutlara cevap ver
        if is_from_target_group(msg) and text.lower().startswith("/taipo"):
            send_message(build_radar_text())
            responded = True

    # state güncelle
    if max_update_id != last_update_id:
        state["last_update_id"] = max_update_id
        save_state(state)

    # 2) Otomatik mesaj (schedule çalışınca atsın)
    # GitHub Actions her 2 saatte bir çalıştığı için burada otomatik gönderiyoruz.
    # Not: Eğer sadece /taipo ile cevap isteseydik bunu kapatırdık.
    send_message("✅ TAİPO-BİST bot çalıştı. GitHub Actions OK!")

    # İstersen burada rapor da atabiliriz:
    # send_message(build_radar_text())


if __name__ == "__main__":
    main()
