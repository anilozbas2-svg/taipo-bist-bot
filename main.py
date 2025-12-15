import os
import requests

BOT_TOKEN = os.environ["BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

def send_message(text: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    r = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text})
    r.raise_for_status()

if __name__ == "__main__":
    send_message("✅ TAIPO-BIST bot çalıştı. GitHub Actions OK!")
