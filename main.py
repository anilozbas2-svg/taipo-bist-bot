import os
import json
import math
import requests
import yfinance as yf
from datetime import datetime
from zoneinfo import ZoneInfo

BOT_TOKEN = os.environ["BOT_TOKEN"]
DEFAULT_CHAT_ID = os.environ.get("CHAT_ID", "")  # otomatik mesajın gideceği grup
API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

STATE_FILE = "state.json"
BIST_FILE = "bist100.txt"
WATCH_FILE = "daily_watch.json"
IST = ZoneInfo("Europe/Istanbul")


def tg_send(chat_id: str, text: str):
    url = f"{API_BASE}/sendMessage"
    return requests.post(url, data={"chat_id": chat_id, "text": text})


def load_json(path: str, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default
    return default


def save_json(path: str, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_symbols():
    with open(BIST_FILE, "r", encoding="utf-8") as f:
        return [line.strip().upper() for line in f if line.strip()]


def today_str_ist():
    return datetime.now(IST).strftime("%Y-%m-%d")


def now_str_ist():
    return datetime.now(IST).strftime("%d.%m.%Y %H:%M")


def fetch_last_close_change_and_volume_score(sym: str):
    """
    Hızlı ve stabil seçim skoru:
    - Son 2 gün kapanış değişimi (%)
    - Son gün hacmi / son 10 gün ortalama hacim oranı
    """
    try:
        t = yf.Ticker(sym + ".IS")
        hist = t.history(period="15d", interval="1d")
        if hist is None or len(hist) < 3:
            return None

        prev = hist.iloc[-2]
        last = hist.iloc[-1]

        prev_close = float(prev["Close"])
        last_close = float(last["Close"])
        if prev_close <= 0:
            return None

        pct = ((last_close - prev_close) / prev_close) * 100.0

        vols = hist["Volume"].dropna().tolist()
        last_vol = float(last.get("Volume", 0.0) or 0.0)
        avg_vol = float(sum(vols[-10:]) / max(1, len(vols[-10:]))) if vols else 0.0

        vol_ratio = (last_vol / avg_vol) if avg_vol > 0 else 1.0
        # Skor: momentum ağırlıklı + hacim bonusu (log ile yumuşat)
        score = (pct * 1.0) + (math.log(max(vol_ratio, 0.1)) * 1.2)

        return {
            "symbol": sym,
            "score": round(score, 4),
            "pct_close": round(pct, 2),
        }
    except Exception:
        return None


def fetch_current_price(sym: str):
    """
    Gün içi anlık fiyat için hızlı çekim (yfinance gecikmeli olabilir ama stabil).
    """
    try:
        t = yf.Ticker(sym + ".IS")
        h = t.history(period="1d", interval="1m")
        if h is not None and len(h) >= 1:
            return float(h.iloc[-1]["Close"])
        # fallback
        h2 = t.history(period="2d", interval="1d")
        if h2 is not None and len(h2) >= 1:
            return float(h2.iloc[-1]["Close"])
    except Exception:
        pass
    return None


def fetch_today_open(sym: str):
    """
    Açılış fiyatı (günlük open).
    """
    try:
        t = yf.Ticker(sym + ".IS")
        h = t.history(period="1d", interval="1d")
        if h is not None and len(h) >= 1:
            return float(h.iloc[0]["Open"])
    except Exception:
        pass
    return None


def choose_daily_top3(symbols):
    scored = []
    for s in symbols:
        d = fetch_last_close_change_and_volume_score(s)
        if d:
            scored.append(d)

    if not scored:
        return []

    scored.sort(key=lambda x: x["score"], reverse=True)
    return [x["symbol"] for x in scored[:3]]


def ensure_daily_watchlist():
    """
    10:01'de seçilen 3 hisseyi (ve açılış fiyatlarını) günlük dosyaya yazar.
    Gün içinde aynı 3 hisse sabit kalır.
    """
    today = today_str_ist()
    watch = load_json(WATCH_FILE, default={})

    if watch.get("date") == today and watch.get("symbols"):
        return watch  # bugünün listesi zaten var

    symbols = load_symbols()
    top3 = choose_daily_top3(symbols)
    if not top3:
        return {"date": today, "symbols": [], "open_prices": {}}

    open_prices = {}
    for s in top3:
        op = fetch_today_open(s)
        if op is None:
            # fallback: current
            op = fetch_current_price(s)
        if op is not None:
            open_prices[s] = round(float(op), 2)

    watch = {
        "date": today,
        "symbols": top3,
        "open_prices": open_prices,
    }
    save_json(WATCH_FILE, watch)
    return watch


def build_opening_message(watch):
    now = now_str_ist()
    syms = watch.get("symbols", [])
    if not syms:
        return f"📌 AÇILIŞ SEÇİMİ (10:01)\n⏱ {now}\n\n⚠️ Bugün seçim oluşturulamadı."

    lines = [f"📌 GÜNÜN AÇILIŞ SEÇİMİ\n⏱ {now}\n",
             "🟢 GÜÇLÜ 3 (GÜN BOYU TAKİP)"]
    for s in syms:
        op = watch.get("open_prices", {}).get(s, None)
        if op is None:
            lines.append(f"• {s}")
        else:
            lines.append(f"• {s}  | Açılış: {op}")
    lines.append("\nNot: Gün içinde bu 3 hisse değişmez. Komut: /taipo")
    return "\n".join(lines)


def build_tracking_message(watch, title="⏱ SAATLİK TAKİP"):
    now = now_str_ist()
    syms = watch.get("symbols", [])
    if not syms:
        return f"{title}\n⏱ {now}\n\n⚠️ Bugün takip listesi yok."

    lines = [f"{title}\n⏱ {now}\n",
             "📍 Aynı 3 hisse gün boyu izleniyor:"]
    for s in syms:
        op = watch.get("open_prices", {}).get(s)
        cur = fetch_current_price(s)

        if op is None and cur is not None:
            op = cur

        if cur is None or op is None:
            lines.append(f"• {s} → veri yok")
            continue

        pct = ((cur - op) / op) * 100.0 if op != 0 else 0.0
        pct_r = round(pct, 2)
        cur_r = round(cur, 2)

        tag = "🟢" if pct_r >= 0.8 else ("🟡" if pct_r >= -0.2 else "🔴")
        lines.append(f"• {tag} {s} → {cur_r}  | Açılış: {op}  | %{pct_r}")

    lines.append("\nKomut: /taipo")
    return "\n".join(lines)


def load_state():
    return load_json(STATE_FILE, default={})


def save_state(state):
    save_json(STATE_FILE, state)


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
    /taipo ve /start komutlarına cevap verir.
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
            watch = ensure_daily_watchlist()
            tg_send(chat_id, build_tracking_message(watch, title="📡 TAIPO-BIST /taipo TAKİP"))
            continue

    save_state(state)


def send_scheduled_messages():
    """
    main.yml saatlik çalışır.
    - 10:00 tetiklenince 10:01 için kısa bekleme yapar ve açılış seçimini gönderir.
    - 11:00-18:00 arası saatlik takip mesajı gönderir.
    """
    if not DEFAULT_CHAT_ID:
        return

    now = datetime.now(IST)
    h = now.hour
    m = now.minute

    # BIST seans saatleri: 10-18
    if h < 10 or h > 18:
        return

    # 10:00 tetiklemesi geldiğinde: 10:01'e kadar bekle (en geç 10:02)
    if h == 10 and m == 0:
        # 70 saniye bekle -> yaklaşık 10:01
        import time
        time.sleep(70)

        watch = ensure_daily_watchlist()
        tg_send(DEFAULT_CHAT_ID, build_opening_message(watch))
        return

    # 11-18 saatlik takip
    if 11 <= h <= 18:
        watch = ensure_daily_watchlist()
        tg_send(DEFAULT_CHAT_ID, build_tracking_message(watch, title="⏱ SAATLİK TAKİP"))
        return


def main():
    # 1) Komutları yakala (command.yml bunu sık çalıştıracak)
    handle_commands()

    # 2) Saatlik otomatik mesajlar (main.yml bunu saat başı çalıştıracak)
    send_scheduled_messages()


if __name__ == "__main__":
    main()
