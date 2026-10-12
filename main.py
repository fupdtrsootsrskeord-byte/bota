import os
import re
import json
import time
import html
import logging
import hashlib
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

import feedparser
import requests
import trafilatura
from groq import Groq

# -------------------- Configuration --------------------
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")

MAX_POSTS_PER_RUN = int(os.getenv("MAX_POSTS_PER_RUN", "3"))
MIN_SCORE = int(os.getenv("MIN_NEWS_SCORE", "48"))
MAX_AGE_HOURS = int(os.getenv("MAX_AGE_HOURS", "24"))
SEEN_FILE = os.getenv("SEEN_FILE", "data/seen_articles.json")

# Image reuse is disabled by default. Set ALLOW_SOURCE_IMAGES=true only if
# you have permission/license to republish images from the relevant publishers.
ALLOW_SOURCE_IMAGES = os.getenv("ALLOW_SOURCE_IMAGES", "false").lower() == "true"
MAX_ARTICLE_CHARS = 14000
USER_AGENT = "Mozilla/5.0 (compatible; MiddleEastNewsBot/1.0; +https://github.com/)"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("mideast-news")

# Official or commonly used RSS feeds where available; Google News RSS is used
# as a fallback/discovery feed for publishers that no longer provide public RSS.
FEEDS = [
    ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml", "aljazeera.com"),
    ("BBC", "https://feeds.bbci.co.uk/news/world/middle_east/rss.xml", "bbc.co.uk"),
    ("Haaretz", "https://news.google.com/rss/search?q=site%3Ahaaretz.com+Middle+East&hl=en-US&gl=US&ceid=US%3Aen", "haaretz.com"),
    ("Jerusalem Post", "https://www.jpost.com/rss/rssallnews", "jpost.com"),
    ("Associated Press", "https://news.google.com/rss/search?q=site%3Aapnews.com+Middle+East&hl=en-US&gl=US&ceid=US%3Aen", "apnews.com"),
    ("Reuters", "https://news.google.com/rss/search?q=site%3Areuters.com+Middle+East&hl=en-US&gl=US&ceid=US%3Aen", "reuters.com"),
]

# High-impact topics; this is a ranking heuristic, not a fact-checking system.
IMPACT_TERMS = {
    "war": 12, "attack": 12, "strike": 12, "missile": 12, "drone": 10,
    "killed": 10, "dead": 10, "injured": 7, "explosion": 10, "bomb": 10,
    "ceasefire": 10, "hostage": 10, "nuclear": 10, "iran": 7, "israel": 7,
    "gaza": 8, "lebanon": 7, "syria": 6, "iraq": 6, "yemen": 6,
    "houthi": 8, "hezbollah": 8, "hamas": 8, "palestinian": 7,
    "netanyahu": 5, "sanctions": 7, "negotiations": 6, "talks": 4,
    "military": 6, "army": 5, "airstrike": 10, "border": 4, "strait of hormuz": 9,
    "diplomatic": 5, "minister": 3, "president": 3, "emergency": 7,
}
URGENT_TERMS = [
    "breaking", "just in", "live:", "urgent", "launches", "launched",
    "attacks", "attack", "strikes", "strike", "killed", "explosion",
    "missile", "airstrike", "ceasefire", "evacuate", "retaliation",
]
REGION_TERMS = [
    "middle east", "iran", "israel", "palestine", "palestinian", "gaza",
    "west bank", "lebanon", "syria", "iraq", "yemen", "saudi", "riyadh", "qatar",
    "uae", "united arab emirates", "bahrain", "kuwait", "oman", "jordan",
    "egypt", "hormuz", "hezbollah", "hamas", "houthi",
]
LOW_VALUE_TERMS = [
    "celebrity", "fashion", "recipe", "horoscope", "sports", "football",
    "soccer", "opinion", "review", "travel guide", "entertainment",
]

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT})
groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None


def clean_text(value):
    if not value:
        return ""
    value = re.sub(r"<[^>]+>", " ", str(value))
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def normalize_url(url):
    if not url:
        return ""
    try:
        p = urlparse(url.strip())
        query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                 if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid", "ocid"}]
        return urlunparse((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), "", urlencode(query), ""))
    except Exception:
        return url.strip()


def item_datetime(entry):
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(key)
        if value:
            try:
                return datetime.fromtimestamp(time.mktime(value), tz=timezone.utc)
            except Exception:
                pass
    return None


def source_for_url(url, fallback="Unknown"):
    host = urlparse(url or "").netloc.lower()
    for name, _, domain in FEEDS:
        if domain in host:
            return name
    # Google News wrapper entries may include publisher name in source field.
    return fallback


def load_seen():
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_seen(seen):
    os.makedirs(os.path.dirname(SEEN_FILE) or ".", exist_ok=True)
    # Retain the latest 1,500 records to keep the state file small.
    trimmed = dict(sorted(seen.items(), key=lambda kv: kv[1], reverse=True)[:1500])
    tmp = SEEN_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(trimmed, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SEEN_FILE)


def entry_image(entry):
    if not ALLOW_SOURCE_IMAGES:
        return None
    candidates = []
    for link in entry.get("links", []) or []:
        if link.get("type", "").startswith("image/"):
            candidates.append(link.get("href"))
    for key in ("media_content", "media_thumbnail"):
        for item in entry.get(key, []) or []:
            candidates.append(item.get("url"))
    for enc in entry.get("enclosures", []) or []:
        if (enc.get("type") or "").startswith("image/"):
            candidates.append(enc.get("href") or enc.get("url"))
    for url in candidates:
        if url and url.startswith("https://"):
            return url
    return None


def fetch_feeds():
    items = []
    for publisher, feed_url, domain in FEEDS:
        try:
            response = session.get(feed_url, timeout=20)
            response.raise_for_status()
            parsed = feedparser.parse(response.content)
            if parsed.bozo and not parsed.entries:
                log.warning("RSS parse failed for %s: %s", publisher, parsed.bozo_exception)
                continue
            log.info("%s: %d RSS items", publisher, len(parsed.entries))
            for entry in parsed.entries[:40]:
                title = clean_text(entry.get("title"))
                link = normalize_url(entry.get("link"))
                summary = clean_text(entry.get("summary") or entry.get("description"))
                if not title or not link:
                    continue
                # Google News RSS sometimes wraps the original link; use the feed's
                # link as provided, which remains a valid article/discovery URL.
                dt = item_datetime(entry)
                if dt and datetime.now(timezone.utc) - dt > timedelta(hours=MAX_AGE_HOURS):
                    continue
                source = source_for_url(link, publisher)
                if source == "Unknown":
                    source = publisher
                items.append({
                    "title": title,
                    "link": link,
                    "summary": summary,
                    "published": dt.isoformat() if dt else "",
                    "published_dt": dt,
                    "source": source,
                    "feed_source": publisher,
                    "image": entry_image(entry),
                })
        except Exception as exc:
            log.warning("Could not read feed %s: %s", publisher, exc)
    return items


def score_item(item, all_items):
    text = (item["title"] + " " + item["summary"]).lower()

    # Geographic relevance: reject items that have no clear Middle East signal.
    region_hits = sum(1 for term in REGION_TERMS if term in text)
    if region_hits == 0:
        return 0, {"region": 0, "importance": 0, "urgency": 0, "evidence": 0, "freshness": 0}

    importance_raw = sum(points for term, points in IMPACT_TERMS.items() if term in text)
    importance = min(35, importance_raw)

    urgency_hits = sum(1 for term in URGENT_TERMS if term in text)
    urgency = min(25, urgency_hits * 6)

    # Base source credibility is not proof of the individual claim.
    known = item["feed_source"] in {
        "Al Jazeera", "BBC", "Haaretz", "Jerusalem Post", "Associated Press", "Reuters"
    }
    evidence = 12 if known else 7
    # Corroboration heuristic: a similar title/summary from a different publisher.
    words = set(re.findall(r"[a-z]{3,}", text))
    corroborating_sources = set()
    for other in all_items:
        if other is item or other["feed_source"] == item["feed_source"]:
            continue
        other_text = (other["title"] + " " + other["summary"]).lower()
        other_words = set(re.findall(r"[a-z]{3,}", other_text))
        if len(words) >= 4 and len(other_words) >= 4:
            overlap = len(words & other_words) / max(1, len(words | other_words))
            if overlap >= 0.16:
                corroborating_sources.add(other["feed_source"])
    evidence += min(13, len(corroborating_sources) * 6)

    dt = item.get("published_dt")
    if dt:
        age = max(0, (datetime.now(timezone.utc) - dt).total_seconds() / 3600)
        freshness = max(0, min(15, round(15 * (1 - age / MAX_AGE_HOURS))))
    else:
        freshness = 7

    if any(term in text for term in LOW_VALUE_TERMS):
        importance = max(0, importance - 12)

    total = min(100, importance + urgency + min(25, evidence) + freshness)
    return total, {
        "region": min(10, region_hits),
        "importance": importance,
        "urgency": urgency,
        "evidence": min(25, evidence),
        "freshness": freshness,
        "corroborating_sources": sorted(corroborating_sources),
    }


def is_duplicate(item, seen, prior_selected):
    url = normalize_url(item["link"])
    if url in seen:
        return True
    title_hash = hashlib.sha256(item["title"].lower().encode("utf-8")).hexdigest()
    if title_hash in seen:
        return True

    # Lightweight near-duplicate check against already selected items.
    words = set(re.findall(r"[a-z]{3,}", item["title"].lower()))
    for other in prior_selected:
        other_words = set(re.findall(r"[a-z]{3,}", other["title"].lower()))
        if words and other_words:
            overlap = len(words & other_words) / max(1, len(words | other_words))
            if overlap >= 0.55:
                return True
    return False


def extract_article(url):
    try:
        response = session.get(url, timeout=25, allow_redirects=True)
        response.raise_for_status()
        extracted = trafilatura.extract(
            response.text,
            url=response.url,
            include_comments=False,
            include_tables=False,
            favor_precision=True,
        )
        if extracted and len(extracted.strip()) >= 500:
            return extracted.strip()[:MAX_ARTICLE_CHARS], True
        log.warning("Full article text unavailable or too short: %s", url)
    except Exception as exc:
        log.warning("Article extraction failed for %s: %s", url, exc)
    return "", False


def rewrite_with_ai(item, article_text, full_text_ok, score, score_details):
    if not groq_client:
        raise RuntimeError("GROQ_API_KEY is missing")

    if full_text_ok:
        evidence_text = article_text
        mode_note = "متن اصلی مقاله استخراج شده است؛ فقط از اطلاعات همین متن استفاده کن."
    else:
        evidence_text = item["summary"]
        mode_note = (
            "متن کامل مقاله در دسترس نبود. فقط خلاصه RSS در اختیار توست؛ "
            "گزارش را کوتاه و محتاطانه بنویس و هرگز ادعا نکن متن کامل را خوانده‌ای."
        )

    prompt = f"""
تو دبیر ارشد سرویس بین‌الملل یک رسانه فارسی‌زبان حرفه‌ای هستی.
یک گزارش خبری دقیق و طبیعی برای انتشار در تلگرام بنویس.

قواعد الزامی:
- فقط از اطلاعات موجود در متن زیر استفاده کن؛ هیچ عدد، نقل‌قول، علت، جزئیات یا زمینه‌ای را جعل نکن.
- میان واقعیت تأییدشده، ادعای مقام‌ها/گروه‌ها و اطلاعات نامشخص تفاوت بگذار.
- اگر یک ادعا به یک طرف نسبت داده شده، آن را به همان طرف نسبت بده؛ آن را واقعیت قطعی معرفی نکن.
- اگر گزارش‌ها هنوز مستقلانه تأیید نشده‌اند، صریح و کوتاه بگو «این ادعا هنوز به‌طور مستقل تأیید نشده است» فقط اگر متن مبنایی برای این احتیاط دارد.
- تیتر روشن، دقیق و خبری باشد؛ اغراق، کلیک‌بیت و زبان تبلیغاتی ممنوع.
- فارسی معیار و روان بنویس؛ ترجمه تحت‌اللفظی و عبارت‌های ماشینی ممنوع.
- نام‌ها، مکان‌ها، تاریخ‌ها و اعداد را دقیق حفظ کن.
- از ذکر نام رسانه، منبع، لینک، اعتباردهی یا عبارت «منبع:» در خروجی خودداری کن.
- اگر متن کافی نیست، گزارش را کوتاه نگه دار؛ کمبود اطلاعات را با حدس پر نکن.
- متن را در 2 تا 5 پاراگراف کوتاه تنظیم کن.
- هیچ Markdown یا HTML استفاده نکن؛ خروجی ساده باشد تا کد آن را امن قالب‌بندی کند.
- {mode_note}

عنوان اصلی: {item['title']}
خلاصه RSS: {item['summary']}
متن در دسترس:
{evidence_text}

امتیاز داخلی اهمیت: {score}/100
اطلاعات داخلی درباره شواهد: {json.dumps(score_details, ensure_ascii=False)}
این امتیاز و اطلاعات داخلی را در خروجی چاپ نکن.

خروجی دقیقاً با این ساختار باشد:
تیتر: [تیتر خبری]

[متن گزارش]
"""
    result = groq_client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {"role": "system", "content": "تو ویراستار دقیق خبر فارسی هستی و هرگز واقعیت جعل نمی‌کنی."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.25,
        max_tokens=900,
    )
    return (result.choices[0].message.content or "").strip()


def telegram_send(text, photo_url=None):
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("TELEGRAM_BOT_TOKEN/BOT_TOKEN or TELEGRAM_CHAT_ID/CHAT_ID is missing")

    # Use plain text to avoid malformed Telegram HTML tags.
    if photo_url and ALLOW_SOURCE_IMAGES:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
        payload = {
            "chat_id": CHAT_ID,
            "photo": photo_url,
            "caption": text[:1024],
        }
    else:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": CHAT_ID,
            "text": text[:4096],
            "disable_web_page_preview": True,
        }
    response = session.post(url, data=payload, timeout=30)
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data}")
    return data


def main():
    log.info("Starting Middle East news bot")
    missing = []
    if not (BOT_TOKEN and CHAT_ID):
        missing.append("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
    if not GROQ_API_KEY:
        missing.append("GROQ_API_KEY")
    if missing:
        raise RuntimeError("Missing GitHub Secrets: " + ", ".join(missing))

    seen = load_seen()
    items = fetch_feeds()
    log.info("Collected %d candidate items", len(items))
    if not items:
        log.warning("No feed items found; ending without posting")
        return

    scored = []
    for item in items:
        score, details = score_item(item, items)
        if score >= MIN_SCORE:
            item["score"] = score
            item["score_details"] = details
            scored.append(item)

    scored.sort(key=lambda x: (x["score"], x.get("published_dt") or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
    log.info("%d candidates passed score threshold %d", len(scored), MIN_SCORE)

    chosen = []
    for item in scored:
        if is_duplicate(item, seen, chosen):
            continue
        chosen.append(item)
        if len(chosen) >= MAX_POSTS_PER_RUN:
            break

    sent = 0
    for item in chosen:
        log.info("Processing (score %s): %s", item["score"], item["title"][:120])
        article_text, full_text_ok = extract_article(item["link"])
        try:
            post_text = rewrite_with_ai(
                item, article_text, full_text_ok,
                item["score"], item["score_details"]
            )
            if not post_text or "تیتر:" not in post_text:
                log.warning("AI output did not match expected format; skipping")
                continue

            photo_url = item.get("image") if ALLOW_SOURCE_IMAGES else None
            telegram_send(post_text, photo_url)
            sent += 1

            # Store URL and title hash only after a successful Telegram post.
            seen[normalize_url(item["link"])] = datetime.now(timezone.utc).isoformat()
            seen[hashlib.sha256(item["title"].lower().encode("utf-8")).hexdigest()] = datetime.now(timezone.utc).isoformat()
            log.info("Posted successfully")
        except Exception as exc:
            log.exception("Failed to publish item: %s", exc)

        time.sleep(1)

    save_seen(seen)
    log.info("Finished. Posts sent: %d", sent)


if __name__ == "__main__":
    main()
