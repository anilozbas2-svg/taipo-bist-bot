import hashlib
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import feedparser

TZ = ZoneInfo("Europe/Istanbul")

# ============================================================
# RSS KAYNAKLARI (BIST + ekonomi genel)
# Not: Kaynakları artırabiliriz; şimdilik stabil + hızlı olanlar
# ============================================================
RSS_FEEDS = [
    # Investing.com Türkiye - Borsa
    "https://tr.investing.com/rss/news_301.rss",
    # Investing.com Türkiye - Ekonomi
    "https://tr.investing.com/rss/news_285.rss",
    # Reuters (genel) - bazı RSS'ler bölgesel çalışır; feedparser tolere eder
    "https://feeds.reuters.com/reuters/businessNews",
]

# ============================================================
# ÖNEMLİ HABER ANAHTARLARI (puanlama)
# ============================================================
IMPORTANT_KEYWORDS = [
    # Şirket / KAP tipi kritikler
    "bedelsiz", "temettü", "geri alım", "pay geri alım", "sermaye", "sermaye artırımı",
    "ihale", "sözleşme", "anlaşma", "ortaklık", "yatırım", "kap", "spk",
    "bilanço", "finansal sonuç", "kâr", "zarar",
    "ceza", "soruşturma", "dava", "iflas", "konkordato",
    # Makro
    "tcmb", "merkez bankası", "faiz", "enflasyon", "kur", "cds"
]

# Genel BIST/Ekonomi kelimeleri (daha düşük puan)
GENERAL_KEYWORDS = [
    "bist", "borsa istanbul", "endeks", "hisse", "hisseler", "piyasa",
    "dolar", "euro", "altın", "petrol"
]


# ============================================================
# Yardımcılar
# ============================================================
def _now_tr() -> datetime:
    return datetime.now(TZ)

def _norm_text(s: str) -> str:
    s = (s or "").strip()
    s = s.lower()
    s = re.sub(r"\s+", " ", s)
    return s

def _hash_id(title: str, link: str) -> str:
    base = (_norm_text(title) + "|" + (link or "")).encode("utf-8")
    return hashlib.sha1(base).hexdigest()  # kısa ve stabil

def _score_item(title: str, summary: str) -> int:
    text = _norm_text(title) + " " + _norm_text(summary)

    score = 0

    # Önemli kelimeler: +3
    for kw in IMPORTANT_KEYWORDS:
        if kw in text:
            score += 3

    # Genel kelimeler: +1
    for kw in GENERAL_KEYWORDS:
        if kw in text:
            score += 1

    return score

def _parse_published_dt(entry) -> datetime | None:
    """
    RSS entry published/parsing: feedparser bazen struct_time verir.
    Yoksa None döner.
    """
    # feedparser: entry.get("published_parsed")
    pp = entry.get("published_parsed")
    if pp:
        # struct_time -> datetime (UTC varsayılır gibi davranabilir)
        # biz TR'ye çevirme yerine "now - age" kontrolünü çok katı yapmıyoruz
        try:
            dt_utc = datetime(*pp[:6])
            # tz-naive; TR'ye "yaklaşık" kabul edelim
            return dt_utc.replace(tzinfo=TZ)
        except Exception:
            pass
    return None

def _within_window(dt: datetime | None, start: datetime, end: datetime) -> bool:
    """
    dt yoksa: 'dupe' kontrolüne güvenip serbest bırakırız.
    dt varsa: pencere içinde mi bakarız.
    """
    if dt is None:
        return True
    return start <= dt <= end


# ============================================================
# Ana API: 3 bülten için haber çıkar
# ============================================================
def collect_news_items(
    seen_ids: list[str],
    window_start: datetime,
    window_end: datetime,
    max_items: int = 3
) -> tuple[list[dict], list[str]]:
    """
    - RSS'lerden haberleri çek
    - seen_ids içinde olmayanları al
    - zaman penceresine uyanları seç
    - puanlayıp en iyi max_items döndür
    Dönen:
      items: [{title, link, score, id}]
      updated_seen_ids
    """
    items = []

    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:50]:
                title = entry.get("title", "") or ""
                link = entry.get("link", "") or ""
                summary = entry.get("summary", "") or entry.get("description", "") or ""

                hid = _hash_id(title, link)
                if hid in seen_ids:
                    continue

                published_dt = _parse_published_dt(entry)
                if not _within_window(published_dt, window_start, window_end):
                    continue

                score = _score_item(title, summary)

                # Çok alakasızları ele (hiç anahtar yoksa 0 olabilir, yine de bırakabiliriz)
                # Burada kalsın, sonra sıralamada alta düşer.

                items.append({
                    "id": hid,
                    "title": title.strip(),
                    "link": link.strip(),
                    "score": score
                })
        except Exception:
            continue

    # Skora göre sırala, eşitlikte en yeni yoksa link/title stabil olsun
    items_sorted = sorted(items, key=lambda x: (x["score"], x["title"]), reverse=True)

    picked = items_sorted[:max_items]

    # picked'leri seen_ids'e ekle
    for it in picked:
        seen_ids.append(it["id"])

    # seen_ids şişmesin: son 200 id tut (rolling)
    if len(seen_ids) > 200:
        seen_ids = seen_ids[-200:]

    return picked, seen_ids


# ============================================================
# 3 BÜLTEN PENCERELERİ
# ============================================================
def get_news_window(slot_name: str) -> tuple[datetime, datetime]:
    """
    slot_name:
      - "yesterday" : dün 17:10 sonrası -> bugün 09:30
      - "midday"    : bugün 09:30 -> bugün 10:30
      - "close"     : bugün 10:30 -> bugün 17:40
    """
    now = _now_tr()
    today = now.date()
    start = end = now

    if slot_name == "yesterday":
        # Dün 17:10
        yday = today - timedelta(days=1)
        start = datetime(yday.year, yday.month, yday.day, 17, 10, tzinfo=TZ)
        end = datetime(today.year, today.month, today.day, 9, 30, tzinfo=TZ)

    elif slot_name == "midday":
        start = datetime(today.year, today.month, today.day, 9, 30, tzinfo=TZ)
        end = datetime(today.year, today.month, today.day, 10, 30, tzinfo=TZ)

    elif slot_name == "close":
        start = datetime(today.year, today.month, today.day, 10, 30, tzinfo=TZ)
        end = datetime(today.year, today.month, today.day, 17, 40, tzinfo=TZ)

    else:
        # fallback: son 24 saat
        start = now - timedelta(hours=24)
        end = now

    return start, end


# ============================================================
# Mesaj formatı
# ============================================================
def format_news_message(slot_name: str, items: list[dict]) -> str:
    now = _now_tr().strftime("%d.%m.%Y %H:%M")

    title_map = {
        "yesterday": "🕘 DÜNKÜ HABERLER (17:10 sonrası)",
        "midday": "🕥 GÜNDÜZ HABERLERİ",
        "close": "🕔 KAPANIŞ HABERLERİ"
    }
    header = title_map.get(slot_name, "📰 HABER BÜLTENİ")

    lines = []
    lines.append("📌 TAIPO • BIST HABER RADAR")
    lines.append(f"{header} — {now}")
    lines.append("")
    if not items:
        lines.append("🔥 Önemli Haber: Yok (bu aralıkta filtreye takılan haber çıkmadı)")
        return "\n".join(lines)

    lines.append("🔥 ÖNEMLİ (Max 3)")
    for i, it in enumerate(items, 1):
        lines.append(f"{i}) {it['title']}")
        if it.get("link"):
            lines.append(f"🔗 {it['link']}")
        lines.append("")

    return "\n".join(lines).strip()
