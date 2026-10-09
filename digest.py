"""Еженедельный дайджест главных новостей канала ggm_news."""
import os
import re
import sys
import json
import html
import datetime as dt
import traceback

import requests
from bs4 import BeautifulSoup

# ---------- Настройки (меняются в digest.yml) ----------
CHANNEL = os.environ.get("CHANNEL", "ggm_news")
DAYS = int(os.environ.get("DAYS", "7"))
TOP_N = int(os.environ.get("TOP_N", "7"))
MODEL = os.environ.get("MODEL", "claude-sonnet-5-5")
HEADER = os.environ.get("HEADER", "Дайджест новостей за прошедшую неделю 📰")
INTRO = os.environ.get("INTRO", "Собрали для вас главные новости индустрии за неделю.")
LINK_TEXT = os.environ.get("LINK_TEXT", "Читать полный материал")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ggm-digest/1.0)"}


# ---------- 1. Сбор постов ----------

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
                raise RuntimeError(f"Не удалось прочитать t.me/s/{CHANNEL} "
                                   "(канал закрытый или отключён веб-просмотр)")
            break
        for p in posts:
            is_digest = p["text"].lower().startswith("дайджест")
            if p["date"] >= since and p["text"] and not is_digest:
                found[p["id"]] = p
        oldest = min(posts, key=lambda p: p["id"])
        if oldest["date"] < since or (before and oldest["id"] >= before):
            break
        before = oldest["id"]
    return found


# ---------- 2. Отбор и тексты через Claude ----------

PROMPT = """Ниже посты Telegram-канала с новостями iGaming-индустрии за последнюю неделю. У каждого поста есть ID.

Выбери {top_n} самых важных новостей недели на твой взгляд (если их меньше, бери все). Отдавай предпочтение бизнесовым новостям: сделки и инвестиции, финансовые результаты и цифры рынка, изменения регулирования, которые влияют на бизнес операторов и партнёров, крупные игроки рынка. Повторы одной темы объединяй, бери один ID. Рекламу, розыгрыши, анонсы мероприятий и служебные посты пропускай.

Для каждой новости напиши:
- emoji: один эмодзи. Если новость про страну или регион – флаг этой страны. Иначе тематический (🏆 награды, 📊 цифры и отчёты, ⚖️ регулирование, 🤝 сделки, 💰 финансы и т. п.).
- title: заголовок на русском, 6–14 слов, с главной цифрой или фактом, если они есть. Без точки в конце.
- summary: одно предложение (максимум два) на русском с сутью новости. Самое важное (название компании, мероприятия, ключевую цифру или вывод) выдели двойными звёздочками: **так**. Выделений 1–2 на пункт.

Пиши сухо и по делу, только факты из поста, ничего не додумывай. Длинное тире не используй, только среднее (–).

Ответь только JSON-массивом без пояснений и без markdown-обёртки, в порядке важности:
[{{"id": 123, "emoji": "🇨🇦", "title": "...", "summary": "..."}}]

ПОСТЫ:
{corpus}"""


def summarize(posts):
    corpus = "\n\n---\n\n".join(
        f"ID {p['id']}\n{p['text'][:1500]}" for p in sorted(posts.values(), key=lambda p: p["id"]))
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": API_KEY, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": MODEL, "max_tokens": 4000,
              "messages": [{"role": "user", "content": PROMPT.format(top_n=TOP_N, corpus=corpus)}]},
        timeout=300,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Claude API {r.status_code}: {r.text[:500]}")
    raw = "".join(b.get("text", "") for b in r.json()["content"] if b["type"] == "text").strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw).strip()
    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError(f"Claude вернул не JSON: {raw[:300]}")
    result, seen = [], set()
    for i in items:
        try:
            pid = int(i["id"])
        except (KeyError, TypeError, ValueError):
            continue
        if pid in posts and pid not in seen and i.get("title") and i.get("summary"):
            seen.add(pid)
            result.append(i)
    return result[:TOP_N]


# ---------- 3. Сборка поста ----------

def fmt(text):
    text = html.escape(str(text).replace("—", "–"))
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)


def build_blocks(items, posts):
    blocks = [f"<b>{fmt(HEADER)}</b>\n\n{fmt(INTRO)}"]
    for i in items:
        url = html.escape(posts[int(i["id"])]["url"], quote=True)
        blocks.append(f"{i.get('emoji', '📰')} <b>{fmt(i['title'])}</b>\n"
                      f"<blockquote>{fmt(i['summary'])}</blockquote>\n"
                      f'<a href="{url}">{fmt(LINK_TEXT)}</a>')
    return blocks


# ---------- 4. Отправка ----------

def send(text, parse_html=False):
    payload = {"chat_id": CHAT_ID, "text": text, "link_preview_options": {"is_disabled": True}}
    if parse_html:
        payload["parse_mode"] = "HTML"
    r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json=payload, timeout=30)
    if not r.ok:
        raise RuntimeError(f"Telegram {r.status_code}: {r.text[:300]}")


def send_blocks(blocks):
    msg = ""
    for b in blocks:
        if msg and len(msg) + len(b) + 2 > 3800:
            send(msg, parse_html=True)
            msg = ""
        msg = f"{msg}\n\n{b}" if msg else b
    if msg:
        send(msg, parse_html=True)


def main():
    missing = [n for n, v in [("TELEGRAM_BOT_TOKEN", BOT_TOKEN), ("TELEGRAM_CHAT_ID", CHAT_ID),
                              ("ANTHROPIC_API_KEY", API_KEY)] if not v]
    if missing:
        print("Не заданы секреты: " + ", ".join(missing), file=sys.stderr)
        sys.exit(1)
    try:
        print(f"1/3 Читаю @{CHANNEL}")
        posts = collect()
        print(f"    постов за {DAYS} дней: {len(posts)}")
        if not posts:
            send(f"За последние {DAYS} дней в @{CHANNEL} не нашлось постов с текстом.")
            return
        print("2/3 Отбор и тексты (Claude)")
        items = summarize(posts)
        if not items:
            raise RuntimeError("Claude не выбрал ни одной новости")
        print("3/3 Отправка")
        send_blocks(build_blocks(items, posts))
        print(f"Готово: в дайджесте {len(items)} новостей")
    except Exception as e:
        traceback.print_exc()
        try:
            send(f"Дайджест @{CHANNEL} не собрался.\nОшибка: {e}")
        except Exception as e2:
            print(f"Не удалось отправить ошибку в Telegram: {e2}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
