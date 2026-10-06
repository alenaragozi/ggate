"""Еженедельный дайджест главных новостей из публичного Telegram-канала."""
import os
import sys
import datetime as dt

import requests
from bs4 import BeautifulSoup

CHANNEL = os.environ.get("CHANNEL", "ggm_news")
DAYS = int(os.environ.get("DAYS", "7"))
TOP_N = int(os.environ.get("TOP_N", "10"))
MODEL = os.environ.get("MODEL", "claude-sonnet-5-5")
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
API_KEY = os.environ["ANTHROPIC_API_KEY"]

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ggm-digest/1.0)"}


def fetch_page(before=None):
    params = {"before": before} if before else None
    r = requests.get(f"https://t.me/s/{CHANNEL}", params=params, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text


def parse(page_html):
    soup = BeautifulSoup(page_html, "html.parser")
    posts = []
    for msg in soup.select("div.tgme_widget_message[data-post]"):
        post_id = int(msg["data-post"].split("/")[-1])
        t = msg.select_one("a.tgme_widget_message_date time[datetime]")
        if not t:
            continue
        text_el = msg.select_one("div.tgme_widget_message_text")
        posts.append({
            "id": post_id,
            "date": dt.datetime.fromisoformat(t["datetime"]),
            "text": text_el.get_text("\n", strip=True) if text_el else "",
            "url": f"https://t.me/{CHANNEL}/{post_id}",
        })
    return posts


def collect():
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=DAYS)
    found, before = {}, None
    for page in range(30):
        posts = parse(fetch_page(before))
        if not posts:
            if page == 0:
                raise RuntimeError(
                    f"Не удалось прочитать t.me/s/{CHANNEL}. "
                    "Скорее всего, канал закрытый или у него отключён веб-просмотр."
                )
            break
        for p in posts:
            if p["date"] >= since and p["text"]:
                found[p["id"]] = p
        oldest = min(posts, key=lambda p: p["id"])
        if oldest["date"] < since or (before and oldest["id"] >= before):
            break
        before = oldest["id"]
    return sorted(found.values(), key=lambda p: p["id"]), since


def summarize(posts, since):
    corpus = "\n\n---\n\n".join(
        f"[{p['date']:%d.%m}] {p['url']}\n{p['text'][:1500]}" for p in posts
    )
    period = f"{since:%d.%m}–{dt.datetime.now(dt.timezone.utc):%d.%m.%Y}"
    prompt = f"""Ниже посты Telegram-канала за период {period}.

Выбери {TOP_N} самых важных новостей (если их меньше, бери все). Повторы одной темы объединяй в один пункт. Рекламу, розыгрыши и служебные посты пропускай.

Формат ответа – обычный текст без markdown (без звёздочек и решёток):
Первая строка: Главное за неделю {period}
Пустая строка.
Дальше нумерованный список. Каждый пункт: номер и короткий заголовок; с новой строки 1–2 предложения сути; с новой строки ссылка на исходный пост. Между пунктами пустая строка.

Пиши на языке постов, сухо и по делу. Используй только факты из постов, ничего не додумывай. Длинное тире не используй, только среднее (–).

ПОСТЫ:
{corpus}"""
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={"model": MODEL, "max_tokens": 3000,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=180,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Claude API {r.status_code}: {r.text[:500]}")
    text = "".join(b.get("text", "") for b in r.json()["content"] if b["type"] == "text")
    return text.replace("—", "–").strip()


def send(text):
    chunks, cur = [], ""
    for para in text.split("\n\n"):
        if len(cur) + len(para) + 2 > 4000 and cur:
            chunks.append(cur)
            cur = ""
        cur = f"{cur}\n\n{para}" if cur else para
    if cur:
        chunks.append(cur)
    for chunk in chunks:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": chunk, "disable_web_page_preview": True},
            timeout=30,
        )
        r.raise_for_status()


def main():
    try:
        posts, since = collect()
        if not posts:
            send(f"За последние {DAYS} дней в @{CHANNEL} не нашлось постов с текстом.")
            return
        send(summarize(posts, since))
        print(f"OK: обработано постов – {len(posts)}")
    except Exception as e:
        try:
            send(f"Дайджест @{CHANNEL} не собрался.\nОшибка: {e}")
        finally:
            print(e, file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
