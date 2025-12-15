import os
import json
import time
import requests
from pathlib import Path

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ.get("CHAT_ID")  # otomatik mesaj için
RUN_MODE = os.environ.get("RUN_MODE", "cron")  # cron | poll

STATE_FILE = Path("state.json")


def tg(method: str, data: dict | None = None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    r = requests.post(url, data=data or {}, timeout=30)
    r.raise_for_status()
    return r.json()


def send_message(chat_id: str, text: str):
    tg("sendMessage", {"chat_id": chat_id, "text": text})


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"last_update_id": 0}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def run_cron():
    if not CHAT_ID:
        raise RuntimeError("CHAT_ID env yok. GitHub Secrets'e CHAT_ID ekli olmalı.")
    send_message(CHAT_ID, "✅ TAIPO-BIST bot çalıştı. GitHub Actions OK!")


def run_poll():
    """
    5 dakikada bir çalışır.
    /taipo komutunu görürse cevap yazar.
    last_update_id state.json içinde tutulur (dupe olmasın diye).
    """
    state = load_state()
    offset = state.get("last_update_id", 0) + 1

    res = tg("getUpdates", {"offset": offset, "limit": 50, "timeout": 0})
    updates = res.get("result", [])

    if not updates:
        return

    for upd in updates:
        state["last_update_id"] = max(state["last_update_id"], upd.get("update_id", 0))

        msg = upd.get("message") or upd.get("edited_message")
        if not msg:
            continue

        text = (msg.get("text") or "").strip()
        chat_id = str(msg["chat"]["id"])

        # Komutlar
        if text.startswith("/taipo"):
            reply = (
                "📡 TAIPO-BIST RADAR\n"
                "Komut alındı ✅\n\n"
                "Şimdilik test mesajı gönderiyorum.\n"
                "Bir sonraki adım: buraya gerçek radar listesini koyacağız."
            )
            send_message(chat_id, reply)

    save_state(state)


if __name__ == "__main__":
    if RUN_MODE == "poll":
        run_poll()
    else:
        run_cron()
