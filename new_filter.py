import hashlib
from datetime import datetime
from zoneinfo import ZoneInfo

import feedparser

TZ = ZoneInfo("Europe/Istanbul")

# BIST genel RSS kaynakları (istersen sonra artırırız)
RSS_SOURCES = [
    # Buraya RSS linkleri gelecek (Aşama A’da netleştiriyoruz)
]

DEFAULT_KEYWORDS = [
    "bedelsiz", "temettü", "kredi", "yatırım", "ihale", "sözleşme",
    "geri alım", "pay geri alım", "kap", "sermaye", "bilanço",
    "ortaklık", "satın alma", "birleşme", "finansman"
]

def _hash_item(title: str, link: str) -> str:
    raw = f"{title}|{link}".encode("utf-8", errors="ignore")
    return hashlib.sha256(raw).hexdigest()[:16]

def fetch_news(max_items_per_source: int = 8):
    """
    Returns list of dict: {title, link, published, source}
    """
    items = []
    for url in RSS_SOURCES:
        d = feedparser.parse(url)
        src = getattr(d.feed, "title", "") or "RSS"
        for e in (d.entries or [])[:max_items_per_source]:
            title = (getattr(e, "title", "") or "").strip()
            link = (getattr(e, "link", "") or "").strip()
            published = (getattr(e, "published", "") or "").strip()
            if title and link:
                items.append({
                    "id": _hash_item(title, link),
                    "title": title,
                    "link": link,
                    "published": published,
                    "source": src
                })
    return items

def filter_news(items, keywords=None):
    """
    Keyword contains match (case-insensitive).
    """
    if keywords is None:
        keywords = DEFAULT_KEYWORDS

    kw = [k.lower().strip() for k in keywords if k.strip()]
    out = []
    for it in items:
        t = it["title"].lower()
        if any(k in t for k in kw):
            out.append(it)
    return out

def format_news_block(news_items, title="📢 Haber Radar"):
    """
    Builds a short block to append to Telegram message.
    """
    if not news_items:
        return ""

    lines = []
    lines.append("")
    lines.append("──────────────────────────────")
    lines.append(f"{title} • {datetime.now(TZ).strftime('%d.%m.%Y %H:%M')}")
    for n in news_items[:5]:
        lines.append(f"• {n['title']}")
        lines.append(f"  🔗 {n['link']}")
    return "\n".join(lines)
