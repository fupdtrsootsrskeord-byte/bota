import os
import re
import json
import time
import hashlib
import logging
import html
from datetime import datetime, timezone, timedelta
from urllib.parse import quote, urlsplit, urlunsplit

import feedparser
import requests
from groq import Groq


# ==================== تنظیمات قبلی ====================

TELEGRAM_TOKEN = (
    os.getenv("TELEGRAM_TOKEN")
    or os.getenv("TELEGRAM_BOT_TOKEN")
    or os.getenv("BOT_TOKEN")
)

CHANNEL_ID = (
    os.getenv("CHANNEL_ID")
    or os.getenv("TELEGRAM_CHAT_ID")
    or os.getenv("CHAT_ID")
    or "@Seven_News_fa"
)

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")

MAX_POSTS_PER_RUN = 3
MAX_AGE_HOURS = 24
MIN_SCORE = 45
REQUEST_TIMEOUT = 20
SEEN_FILE = os.path.join("data", "seen_articles.json")

HEADERS = {
    "User-Agent": "MiddleEastNewsBot/2.0 (RSS news reader)"
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

session = requests.Session()
session.headers.update(HEADERS)


# ==================== منابع خبری ====================

FEEDS = [
    {
        "name": "Al Jazeera",
        "url": "https://www.aljazeera.com/xml/rss/all.xml",
        "weight": 10,
    },
    {
        "name": "BBC",
        "url": "https://feeds.bbci.co.uk/news/world/middle_east/rss.xml",
        "weight": 10,
    },
    {
        "name": "Haaretz",
        "url": "https://www.haaretz.com/srv/middle-east-news-rss",
        "weight": 8,
    },
    {
        "name": "The Jerusalem Post",
        "url": "https://www.jpost.com/rss/rssallnews",
        "weight": 8,
    },
    {
        "name": "Associated Press",
        "url": (
            "https://news.google.com/rss/search?q="
            + quote('Middle East when:1d site:apnews.com')
            + "&hl=en-US&gl=US&ceid=US:en"
        ),
        "weight": 10,
    },
    {
        "name": "Reuters",
        "url": (
            "https://news.google.com/rss/search?q="
            + quote('Middle East when:1d site:reuters.com')
            + "&hl=en-US&gl=US&ceid=US:en"
        ),
        "weight": 10,
    },
]


# ==================== کلمات کلیدی ====================

REGION_KEYWORDS = [
    "middle east", "iran", "israel", "gaza", "palestine",
    "lebanon", "syria", "iraq", "yemen", "jordan",
    "saudi arabia", "qatar", "bahrain", "uae",
    "united arab emirates", "west bank", "tehran",
    "beirut", "damascus", "baghdad", "sanaa",
    "houthi", "hezbollah", "hamas", "netanyahu",
    "persian gulf", "strait of hormuz", "red sea",
    "ایران", "اسرائیل", "غزه", "فلسطین", "لبنان",
    "سوریه", "عراق", "یمن", "اردن", "عربستان",
    "حماس", "حزب‌الله", "حوثی", "تهران", "بیروت",
    "تنگه هرمز", "کرانه باختری",
]

URGENT_KEYWORDS = [
    "breaking", "urgent", "just in", "live updates",
    "airstrike", "air strike", "missile", "ballistic missile",
    "invasion", "attack", "explosion", "killed",
    "ceasefire", "truce", "war", "retaliation",
    "nuclear", "sanctions", "emergency", "evacuation",
    "military operation", "drone strike", "hostage",
    "حمله", "حملات", "موشک", "فوری", "آتش‌بس",
    "انفجار", "جنگ", "تحریم", "هسته‌ای", "تلافی",
    "عملیات نظامی", "پهپاد", "گروگان",
]

HIGH_IMPORTANCE_KEYWORDS = [
    "president", "prime minister", "foreign minister",
    "supreme leader", "military", "army", "government",
    "united nations", "nato", "nuclear deal",
    "oil prices", "oil production", "strait of hormuz",
    "peace talks", "diplomacy", "election",
    "رئیس‌جمهور", "نخست‌وزیر", "وزیر خارجه",
    "رهبر", "ارتش", "دولت", "سازمان ملل",
    "مذاکرات", "دیپلماسی", "انتخابات",
]


# ==================== ابزارهای کمکی ====================

def clean_text(value):
    """پاک‌سازی HTML و فاصله‌های اضافی متن RSS."""
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def normalize_url(url):
    """حذف پارامترهای رهگیری از لینک."""
    try:
        parts = urlsplit(url.strip())
        return urlunsplit((
            parts.scheme,
            parts.netloc.lower(),
            parts.path.rstrip("/"),
            "",
            ""
        ))
    except Exception:
        return url.strip()


def article_id(entry):
    """شناسه پایدار برای جلوگیری از ارسال مجدد."""
    basis = (
        normalize_url(entry.get("link", ""))
        or entry.get("title", "").lower()
    )
    return hashlib.sha256(
        basis.encode("utf-8")
    ).hexdigest()


def load_seen():
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_seen(seen):
    os.makedirs(os.path.dirname(SEEN_FILE), exist_ok=True)

    # نگهداری حداکثر ۳۰۰۰ شناسه اخیر
    if len(seen) > 3000:
        ordered = sorted(
            seen.items(),
            key=lambda item: item[1],
            reverse=True
        )
        seen = dict(ordered[:2500])

    temporary_file = SEEN_FILE + ".tmp"

    with open(temporary_file, "w", encoding="utf-8") as file:
        json.dump(seen, file, ensure_ascii=False, indent=2)

    os.replace(temporary_file, SEEN_FILE)


def parse_published(entry):
    for key in ("published_parsed", "updated_parsed"):
        value = entry.get(key)
        if value:
            try:
                return datetime(
                    *value[:6],
                    tzinfo=timezone.utc
                )
            except (TypeError, ValueError):
                pass

    return None


# ==================== دریافت خبرها ====================

def get_recent_entries():
    entries = []
    now = datetime.now(timezone.utc)

    for source in FEEDS:
        try:
            response = session.get(
                source["url"],
                timeout=REQUEST_TIMEOUT
            )
            response.raise_for_status()

            feed = feedparser.parse(response.content)

            if getattr(feed, "bozo", False):
                logging.warning(
                    "RSS may be malformed: %s",
                    source["name"]
                )

            for item in feed.entries[:25]:
                title = clean_text(item.get("title", ""))
                link = item.get("link", "").strip()

                summary = clean_text(
                    item.get("summary", "")
                    or item.get("description", "")
                )

                published = parse_published(item)

                if not title or not link:
                    continue

                # لینک‌های غیرقابل استفاده را کنار بگذار
                if not link.startswith(("https://", "http://")):
                    continue

                # خبرهای قدیمی را حذف کن
                if published:
                    age = now - published

                    if age > timedelta(hours=MAX_AGE_HOURS):
                        continue

                    # تاریخ‌های آینده معمولاً ناشی از خطای RSS هستند
                    if age < timedelta(hours=-2):
                        continue

                # اگر تاریخ انتشار وجود ندارد، خبر را نگه می‌داریم؛
                # اما در امتیاز تازگی امتیاز کامل نمی‌گیرد.
                entries.append({
                    "title": title,
                    "link": normalize_url(link),
                    "summary": summary[:1800],
                    "published": published,
                    "source": source["name"],
                    "source_weight": source["weight"],
                })

        except Exception as exc:
            logging.warning(
                "Could not read %s: %s",
                source["name"],
                exc
            )

    # حذف لینک‌های تکراری بین فیدها
    unique = {}

    for entry in entries:
        key = entry["link"]

        if (
            key not in unique
            or entry["source_weight"]
            > unique[key]["source_weight"]
        ):
            unique[key] = entry

    return list(unique.values())


# ==================== اهمیت و فوریت ====================

def score_article(entry, all_entries):
    text = (
        entry["title"] + " " + entry["summary"]
    ).lower()

    region_hits = sum(
        1 for keyword in REGION_KEYWORDS
        if keyword in text
    )

    if region_hits == 0:
        return 0

    # اهمیت: حداکثر 40
    importance = min(40, 15 + region_hits * 5)

    high_hits = sum(
        1 for keyword in HIGH_IMPORTANCE_KEYWORDS
        if keyword in text
    )
    importance = min(40, importance + high_hits * 2)

    # فوریت: حداکثر 25
    urgent_hits = sum(
        1 for keyword in URGENT_KEYWORDS
        if keyword in text
    )
    urgency = min(25, urgent_hits * 9)

    # تازگی: حداکثر 20
    freshness = 5
    published = entry.get("published")

    if published:
        age_hours = max(
            0,
            (datetime.now(timezone.utc) - published)
            .total_seconds() / 3600
        )

        if age_hours <= 1:
            freshness = 20
        elif age_hours <= 3:
            freshness = 16
        elif age_hours <= 6:
            freshness = 12
        elif age_hours <= 12:
            freshness = 8
        else:
            freshness = 4

    # هم‌پوشانی با خبرهای مستقل: حداکثر 15
    # این بخش فقط نشانه هم‌پوشانی موضوعی است، نه اثبات صحت خبر.
    corroboration = 0
    title_words = set(
        re.findall(r"[a-zA-Z0-9\u0600-\u06FF]+",
                   entry["title"].lower())
    )

    for other in all_entries:
        if other is entry or other["source"] == entry["source"]:
            continue

        other_words = set(
            re.findall(
                r"[a-zA-Z0-9\u0600-\u06FF]+",
                other["title"].lower()
            )
        )

        common = title_words & other_words
        meaningful = {
            word for word in common
            if len(word) >= 4
        }

        if len(meaningful) >= 2:
            corroboration = 15
            break

    # وزن منبع فقط نقش محدود دارد
    source_bonus = min(
        3,
        max(0, entry.get("source_weight", 5) - 7)
    )

    total = min(
        100,
        importance + urgency + freshness
        + corroboration + source_bonus
    )

    return total


def select_important(entries, seen):
    candidates = []

    for entry in entries:
        if article_id(entry) in seen:
            continue

        score = score_article(entry, entries)

        if score >= MIN_SCORE:
            entry["score"] = score
            candidates.append(entry)

    # اولویت با امتیاز بالاتر؛ سپس خبر تازه‌تر
    candidates.sort(
        key=lambda item: (
            item["score"],
            item["published"] or datetime.min.replace(
                tzinfo=timezone.utc
            )
        ),
        reverse=True
    )

    return candidates


# ==================== بازنویسی با هوش مصنوعی ====================

def rewrite_with_ai(entry):
    client = Groq(api_key=GROQ_API_KEY)

    prompt = f"""
تو دبیر خبری یک کانال فارسی‌زبان درباره خاورمیانه هستی.

قواعد:
- بی‌طرف، دقیق و حرفه‌ای بنویس.
- فقط بر اساس اطلاعات ارائه‌شده بنویس.
- جزئیات، اعداد، نام‌ها و نقل‌قول‌های تأییدنشده نساز.
- اگر اطلاعات ناقص است، عدم قطعیت را روشن بیان کن.
- خلاصه RSS را با متن کامل مقاله اشتباه نگیر.
- اگر خبر حاوی ادعای یک طرف درگیری است، آن را به همان طرف نسبت بده.
- تیتر فارسی روشن و غیرهیجانی بنویس.
- متن را در ۲ یا ۳ پاراگراف کوتاه آماده انتشار بنویس.
- نام رسانه را در متن خروجی ننویس.
- هیچ توضیحی درباره وظیفه خودت اضافه نکن.

قالب:
تیتر: ...

متن خبر...

لینک: {entry['link']}

عنوان خبر اصلی:
{entry['title']}

خلاصه موجود از RSS:
{entry['summary']}

تاریخ انتشار:
{entry['published'] or 'نامشخص'}

توجه: اگر خلاصه RSS جزئیات کافی ندارد، خبر را کوتاه‌تر بنویس
و اطلاعاتی را که در دسترس نیستند حدس نزن.
"""

    try:
        response = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "تو یک دبیر خبری فارسی‌زبان هستی. "
                        "دقت و بی‌طرفی از جذابیت مهم‌تر است."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
            max_tokens=700,
        )

        result = response.choices[0].message.content

        return result.strip() if result else None

    except Exception:
        logging.exception("AI rewriting failed")
        return None


# ==================== ارسال به تلگرام ====================

def send_to_telegram(message):
    if not TELEGRAM_TOKEN or not CHANNEL_ID:
        logging.error("Telegram settings are missing")
        return False

    url = (
        "https://api.telegram.org/bot"
        + TELEGRAM_TOKEN
        + "/sendMessage"
    )

    # استفاده از متن ساده برای جلوگیری از خطاهای HTML
    # و نمایش ناخواسته تگ‌ها در تلگرام
    payload = {
        "chat_id": CHANNEL_ID,
        "text": message[:4000],
        "disable_web_page_preview": False,
    }

    try:
        response = session.post(
            url,
            json=payload,
            timeout=REQUEST_TIMEOUT
        )

        if response.ok:
            logging.info("Telegram post sent successfully")
            return True

        logging.error(
            "Telegram error %s: %s",
            response.status_code,
            response.text[:1000]
        )
        return False

    except requests.RequestException:
        logging.exception("Telegram connection failed")
        return False


# ==================== اجرای اصلی ====================

def main():
    logging.info("Starting Middle East news bot")

    missing = []

    if not TELEGRAM_TOKEN:
        missing.append("TELEGRAM_TOKEN")
    if not CHANNEL_ID:
        missing.append("CHANNEL_ID")
    if not GROQ_API_KEY:
        missing.append("GROQ_API_KEY")

    if missing:
        logging.error(
            "Missing environment variables: %s",
            ", ".join(missing)
        )
        return

    seen = load_seen()
    entries = get_recent_entries()

    logging.info("Collected %s RSS entries", len(entries))

    candidates = select_important(entries, seen)

    logging.info(
        "%s new candidate articles passed the score threshold",
        len(candidates)
    )

    posted = 0

    for entry in candidates:
        if posted >= MAX_POSTS_PER_RUN:
            break

        logging.info(
            "Processing score=%s source=%s title=%s",
            entry["score"],
            entry["source"],
            entry["title"][:100]
        )

        rewritten = rewrite_with_ai(entry)

        if not rewritten or "تیتر:" not in rewritten:
            logging.warning("Invalid AI output; skipping article")
            continue

        # اگر مدل لینک را نیاورد، لینک اصلی را اضافه کن.
        if entry["link"] not in rewritten:
            rewritten = rewritten.rstrip() + "\n\nلینک: " + entry["link"]

        # متن ساده ارسال می‌شود؛ بنابراین نیازی به HTML escaping نیست.
        if send_to_telegram(rewritten):
            seen[article_id(entry)] = (
                datetime.now(timezone.utc).isoformat()
            )
            posted += 1
            save_seen(seen)
            time.sleep(3)

    # ذخیره شناسه‌های قبلی حتی اگر پستی ارسال نشده باشد
    save_seen(seen)

    logging.info(
        "Finished. Successfully sent %s posts",
        posted
    )


if __name__ == "__main__":
    main()
