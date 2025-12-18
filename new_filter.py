import hashlib
from datetime import datetime
from zoneinfo import ZoneInfo

import feedparser

TZ = ZoneInfo("Europe/Istanbul")

# ==========================================================
# RSS HABER KAYNAKLARI (BIST/Finans genel)
# Not: RSS linkleri zaman zaman değişebilir; kod hata vermez,
# sadece o kaynağı pas geçer.
# ==========================================================
RSS_SOURCES = [
    {"name": "KAP", "url": "https://www.kap.org.tr/tr/Rss"},          # KAP genel RSS
    {"name": "Foreks", "url": "https://www.foreks.com/rss"},         # Genel finans RSS
    {"name": "Dunya", "url": "https://www.dunya.com/rss/finans"},    # Finans RSS
    {"name": "BloombergHT", "url": "https://www.bloomberght.com/rss"}# Genel RSS
]

# ==========================================================
# ANAHTAR KELİMELER (BIST GENEL)
# İstersen sonra genişletiriz
# ==========================================================
DEFAULT_KEYWORDS = [
    "bedelsiz",
    "temettü",
    "kar payı",
    "geri alım",
    "pay geri alım",
    "sermaye artırım",
    "sermaye azaltım",
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
    "finansman",
    "borçlanma",
    "tahvil",
    "halka arz",
    "SPK",
    "rekabet kurumu",
    "ceza",
    "vergi",
    "dava",
    "lisans",
    "üretim",
    "kapasite"
]

# ==========================================================
# UTILS
# ==========================================================
def _hash_item(title: str, link: str) -> str:
    raw = (title or "").strip() + "||" + (link or "").strip()
    return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()


def fetch_news(max_items_per_source: int = 12):
    """
    RSS kaynaklarından haberleri çeker.
    Returns list of dict:
      { id, title, link, published, source }
    """
    items = []
    for src in RSS_SOURCES:
        try:
            feed = feedparser.parse(src["url"])
            source_name = src.get("name") or getattr(feed, "feed", {}).get("title", "RSS")

            entries = getattr(feed, "entries", []) or []
            for e in entries[:max_items_per_source]:
                title = (getattr(e, "title", "") or "").strip()
                link = (getattr(e, "link", "") or "").strip()
                published = (getattr(e, "published", "") or getattr(e, "updated", "") or "").strip()

                if not title or not link:
                    continue

                items.append({
                    "id": _hash_item(title, link),
                    "title": title,
                    "link": link,
                    "published": published,
                    "source": source_name
                })
        except Exception:
            # Kaynak patlasa bile bot çökmeyecek
            continue

    return items


def filter_news(items, keywords=None):
    """
    Başlıkta keyword geçenleri seçer.
    """
    if keywords is None:
        keywords = DEFAULT_KEYWORDS

    kw = [k.lower().strip() for k in keywords if k and k.strip()]
    out = []

    for it in items:
        title = (it.get("title") or "").lower()
        if any(k in title for k in kw):
            out.append(it)

    return out


def dedupe_with_state(news_items, state: dict, max_seen_keep: int = 800):
    """
    state['news']['seen'] listesini kullanarak tekrarları engeller.
    Returns: (new_items, updated_state)
    """
    if "news" not in state or not isinstance(state["news"], dict):
        state["news"] = {"seen": [], "last_sent_key": ""}

    seen = state["news"].get("seen", [])
    if not isinstance(seen, list):
        seen = []

    seen_set = set(seen)
    new_items = []

    for it in news_items:
        hid = it.get("id")
        if not hid:
            continue
        if hid in seen_set:
            continue
        new_items.append(it)
        seen_set.add(hid)

    # seen listesini büyütüp şişirmeyelim
    state["news"]["seen"] = list(seen_set)[-max_seen_keep:]

    return new_items, state


def format_news_block(news_items, limit: int = 6) -> str:
    """
    Telegram’a atılacak haber bloğu metni.
    """
    if not news_items:
        return ""

    now_str = datetime.now(TZ).strftime("%d.%m.%Y %H:%M")

    lines = []
    lines.append("📰 TAIPO • BIST HABER RADARI")
    lines.append(f"🕒 {now_str}")
    lines.append("")

    for n in news_items[:limit]:
        title = n.get("title", "").strip()
        link = n.get("link", "").strip()
        source = n.get("source", "").strip()

        if source:
            lines.append(f"• ({source}) {title}")
        else:
            lines.append(f"• {title}")

        lines.append(f"  🔗 {link}")
        lines.append("")

    return "\n".join(lines).strip()


def build_news_message_and_update_state(state: dict, keywords=None, limit: int = 6):
    """
    MAIN.PY burayı çağıracak.
    - RSS çek
    - keyword filtrele
    - state ile dedupe yap
    - mesaj oluştur
    Returns: (message_text_or_empty, updated_state)
    """
    all_items = fetch_news()
    filtered = filter_news(all_items, keywords=keywords)
    new_items, state = dedupe_with_state(filtered, state)

    msg = format_news_block(new_items, limit=limit)
    return msg, state
