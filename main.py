import os
import time
import hashlib
import feedparser
import requests
import html
from datetime import datetime, timezone, timedelta
from groq import Groq

# ==================== تنظیمات ====================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")          # @Seven_News_fa
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# منابع معتبر خاورمیانه
FEEDS = [
    "https://www.aljazeera.com/xml/rss/all.xml",
    "https://feeds.bbci.co.uk/news/world/middle_east/rss.xml",
    "https://www.haaretz.com/srv/middle-east-news-rss",
]

# حداکثر تعداد پست در هر اجرا (برای رسیدن به ۳-۴ پست در ساعت)
MAX_POSTS_PER_RUN = 3

# فقط اخبار مربوط به چند ساعت اخیر
MAX_AGE_HOURS = 3

# ==================== توابع ====================

def get_recent_entries():
    """گرفتن اخبار تازه از فیدها"""
    entries = []
    now = datetime.now(timezone.utc)

    for feed_url in FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:8]:  # فقط چند مورد اول هر فید
                # تاریخ انتشار
                published = None
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
                elif hasattr(entry, "updated_parsed") and entry.updated_parsed:
                    published = datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc)

                if published and (now - published) > timedelta(hours=MAX_AGE_HOURS):
                    continue

                title = entry.get("title", "").strip()
                link = entry.get("link", "").strip()
                summary = entry.get("summary", entry.get("description", "")).strip()

                if title and link:
                    entries.append({
                        "title": title,
                        "link": link,
                        "summary": summary[:600],  # محدود کردن طول
                        "published": published
                    })
        except Exception as e:
            print(f"خطا در خواندن فید {feed_url}: {e}")

    # حذف تکراری بر اساس لینک
    unique = {}
    for e in entries:
        unique[e["link"]] = e
    return list(unique.values())

def is_important(entry):
    """فیلتر ساده برای اخبار مهم‌تر خاورمیانه"""
    text = (entry["title"] + " " + entry["summary"]).lower()
    keywords = [
        "gaza", "israel", "iran", "lebanon", "syria", "yemen", "houthi",
        "netanyahu", "hamas", "hezbollah", "tehran", "beirut", "damascus",
        "ceasefire", "airstrike", "nuclear", "oil", "strait of hormuz",
        "غزه", "اسرائیل", "ایران", "لبنان", "سوریه", "یمن", "حماس", "حزب‌الله"
    ]
    return any(k in text for k in keywords)

def rewrite_with_ai(entry):
    """با Groq تیتر جدید و متن تحلیلی-بی‌طرف-رسمی بساز"""
    client = Groq(api_key=GROQ_API_KEY)

    prompt = f"""
تو یک ویراستار خبری حرفه‌ای و بی‌طرف هستی که برای یک کانال تلگرام فارسی درباره خاورمیانه می‌نویسی.

لحن مورد نظر:
- کاملاً بی‌طرف
- تحلیلی
- کمی رسمی و متین
- بدون هیجان‌زدگی یا موضع‌گیری سیاسی

وظیفه تو:
1. یک تیتر جدید، دقیق و جذاب به فارسی بنویس (نه ترجمه تحت‌اللفظی).
2. متن خبر را در ۲ تا ۳ پاراگراف کوتاه و روان به فارسی بازنویسی کن (حداکثر ۲۲۰ کلمه).
3. در انتها منبع و لینک اصلی را ذکر کن.

خبر اصلی:
عنوان: {entry['title']}
خلاصه: {entry['summary']}
لینک: {entry['link']}

خروجی را دقیقاً به این شکل بده:
تیتر: [تیتر جدید]

[متن کامل]

منبع: [نام منبع]
{entry['link']}
"""

    try:
        response = client.chat.completions.create(
            model="openai/gpt-oss-20b",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.4,
            max_tokens=700,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"خطا در هوش مصنوعی: {e}")
        return None

def send_to_telegram(text):
    """ارسال پیام به کانال"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHANNEL_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False
    }
    try:
        r = requests.post(url, json=payload, timeout=15)
        if r.status_code == 200:
            print("✅ پست ارسال شد")
            return True
        else:
            print(f"❌ خطا در ارسال: {r.text}")
            return False
    except Exception as e:
        print(f"❌ خطا در ارتباط با تلگرام: {e}")
        return False

def main():
    print("شروع اجرای ربات اخبار خاورمیانه...")

    if not all([TELEGRAM_TOKEN, CHANNEL_ID, GROQ_API_KEY]):
        print("❌ یکی از متغیرهای محیطی تنظیم نشده است.")
        return

    entries = get_recent_entries()
    print(f"تعداد اخبار پیدا شده: {len(entries)}")

    # فقط اخبار مهم
    important = [e for e in entries if is_important(e)]
    print(f"تعداد اخبار مهم: {len(important)}")

    # مرتب‌سازی بر اساس تازگی
    important.sort(key=lambda x: x["published"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    posted = 0
    for entry in important:
        if posted >= MAX_POSTS_PER_RUN:
            break

        print(f"در حال پردازش: {entry['title'][:60]}...")
        rewritten = rewrite_with_ai(entry)

        if rewritten and "تیتر:" in rewritten:
            # کمی تمیزکاری
            text = html.escape(rewritten)
lines = text.splitlines()

if lines and lines[0].startswith("تیتر:"):
    lines[0] = "<b>" + lines[0].replace("تیتر:", "", 1).strip() + "</b>"

text = "\n".join(lines)
            if send_to_telegram(text):
                posted += 1
                time.sleep(8)  # فاصله بین پست‌ها
        else:
            print("رد شد (خروجی نامعتبر)")

    print(f"پایان. تعداد پست‌های ارسال‌شده: {posted}")

if __name__ == "__main__":
    main()
