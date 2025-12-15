import os
import time
import requests
from datetime import datetime, timezone, timedelta

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

def send_message(text: str) -> None:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "disable_web_page_preview": True
    }
    r = requests.post(url, data=payload, timeout=20)
    r.raise_for_status()

def now_tr_str() -> str:
    tr_tz = timezone(timedelta(hours=3))
    return datetime.now(tr_tz).strftime("%d.%m.%Y %H:%M")

if __name__ == "__main__":
    # GitHub Actions tetik türü: schedule / workflow_dispatch
    event_name = os.getenv("GITHUB_EVENT_NAME", "unknown")
    if event_name == "schedule":
        trigger = "⏰ Otomatik (2 saatte bir)"
    elif event_name == "workflow_dispatch":
        trigger = "🖐️ Manuel (Run workflow)"
    else:
        trigger = f"⚙️ Tetik: {event_name}"

    msg = (
        f"✅ TAIPO-BIST bot çalıştı.\n"
        f"🕒 TR Saat: {now_tr_str()}\n"
        f"{trigger}"
    )

    # Küçük bir güvenlik: Telegram bazen anlık rate limit atabilir
    for i in range(3):
        try:
            send_message(msg)
            break
        except Exception as e:
            if i == 2:
                raise
            time.sleep(2)
