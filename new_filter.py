import hashlib
from datetime import datetime
from zoneinfo import ZoneInfo
import feedparser

TZ = ZoneInfo("Europe/Istanbul")

# =========================
# RSS HABER KAYNAKLARI (BIST)
# =========================
RSS_SOURCES = [
    "https://www.kap.org.tr/tr/rss/company",
    "https://www.foreks.com/rss",
    "https://www.dunya.com/rss?d=finans",
    "https://www.bloomberght.com/rss"
]

# =========================
# ANAHTAR KELİMELER (BIST GENEL)
# =========================
DEFAULT_KEYWORDS = [
    "bedelsiz",
    "temettü",
    "kâr payı",
    "geri alım",
    "pay geri alım",
    "sermaye artırımı",
    "sermaye azaltımı",
    "bilanço",
    "finansal sonuç",
    "kredi",
    "ihale",
    "sözleşme",
    "yatırım",
    "ortaklık",
    "satın alma",
    "birleşme",
    "kap bildirimi",
    "finansman"
]

def _hash_item(title: str, link: str) -> str:
    raw = f"{title}|{link}".encode("utf-8", errors="ignore")
    return hashlib.sha256(raw).hexdigest()[:16]

def fetch_news(max_items_per_source: int = 10):
    """
    Returns list of dict:
    { id, title, link, published, source }
    """
    items = []
    for url in RSS_SOURCES:
        d = feedparser.parse(url)
        source = getattr(d.feed, "title", "RSS")
        for e in (d.entries or [])[:max_items_per_source]:
            title = (getattr(e, "title", "") or "").strip()
            link = (getattr(e, "link", "") or "").strip()
            published = (getattr(e, "published", "") or "").strip()

            if not title or not link:
                continue

            items.append({
                "id": _hash_item(title, link),
                "title": title,
                "link": link,
                "published": published,
                "source": source
            })
    return items

def filter_news(items, keywords=None):
    if keywords is None:
        keywords = DEFAULT_KEYWORDS

    keywords = [k.lower() for k in keywords]
    out = []

    for it in items:
        title = it["title"].lower()
        if any(k in title for k in keywords):
            out.append(it)

    return out

def format_news_block(news_items, limit=4):
    if not news_items:
        return ""

    lines = []
    lines.append("")
    lines.append("📰 BIST HABER RADARI")
    lines.append(f"🕒 {datetime.now(TZ).strftime('%d.%m.%Y %H:%M')}")
    lines.append("──────────────────────────────")

    for n in news_items[:limit]:
        lines.append(f"• {n['title']}")
        lines.append(f"  🔗 {n['link']}")

    return "\n".join(lines)
