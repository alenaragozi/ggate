"""Еженедельный дайджест главных новостей канала ggm_news.

Версия 2: отбор по баллам (тема + упоминания в контрольных каналах + просмотры),
правила разнообразия, служебная справка с баллами.
"""
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
CONTROL_CHANNELS = [c.strip().lstrip("@").replace("https://t.me/", "").replace("s/", "")
                    for c in os.environ.get("CONTROL_CHANNELS", "").split(",") if c.strip()]
DAYS = int(os.environ.get("DAYS", "7"))
TOP_N = int(os.environ.get("TOP_N", "7"))
MAX_PER_COUNTRY = int(os.environ.get("MAX_PER_COUNTRY", "2"))
MAX_PER_CATEGORY = int(os.environ.get("MAX_PER_CATEGORY", "3"))
SEND_REPORT = os.environ.get("SEND_REPORT", "1") == "1"
MODEL = os.environ.get("MODEL", "claude-sonnet-5-5")
HEADER = os.environ.get("HEADER", "Дайджест новостей за прошедшую неделю 📰")
INTRO = os.environ.get("INTRO", "Собрали для вас главные новости индустрии за неделю.")
LINK_TEXT = os.environ.get("LINK_TEXT", "Читать полный материал")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Баллы
CATEGORY_POINTS = {"regulation": 5, "sanctions": 4, "deals": 4,
                   "market_data": 3, "events": 2, "company": 1}
CATEGORY_NAMES = {"regulation": "регулирование", "sanctions": "санкции", "deals": "сделки",
                  "market_data": "цифры рынка", "events": "мероприятия", "company": "компании"}
REPOST_POINTS = 2      # за каждый канал с репостом/ссылкой на наш пост
MENTION_POINTS = 1     # за каждый канал, написавший о том же своими словами
CONTROL_CAP = 5        # максимум за контрольные каналы
VIEWS_POINTS = 1       # за просмотры в час в верхней трети недели

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ggm-digest/2.0)"}
OUR_LINK = re.compile(rf"t\.me/(?:s/)?{re.escape(CHANNEL)}/(\d+)", re.I)


# ---------- 1. Сбор постов ----------

def parse_views(s):
    s = (s or "").strip().upper().replace(",", ".")
    if not s:
        return 0
    mult = 1
    if s.endswith("K"):
        mult, s = 1_000, s[:-1]
    elif s.endswith("M"):
        mult, s = 1_000_000, s[:-1]
    try:
        return int(float(s) * mult)
    except ValueError:
        return 0


def fetch_page(channel, before=None):
    params = {"before": before} if before else None
    r = requests.get(f"https://t.me/s/{channel}", params=params, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text


def parse(page_html, channel):
    soup = BeautifulSoup(page_html, "html.parser")
    posts = []
    for msg in soup.select("div.tgme_widget_message[data-post]"):
        post_id = int(msg["data-post"].split("/")[-1])
        t = msg.select_one("a.tgme_widget_message_date time[datetime]")
        if not t:
            continue
        text_el = msg.select_one("div.tgme_widget_message_text")
        views_el = msg.select_one("span.tgme_widget_message_views")
        # ссылки на наши посты: репост из нашего канала или ссылка в тексте
        our_refs = set()
        for a in msg.select("a.tgme_widget_message_forwarded_from_name[href], "
                            "div.tgme_widget_message_text a[href]"):
            m = OUR_LINK.search(a["href"])
            if m:
                our_refs.add(int(m.group(1)))
        posts.append({
            "id": post_id,
            "channel": channel,
            "date": dt.datetime.fromisoformat(t["datetime"]),
            "text": text_el.get_text("\n", strip=True) if text_el else "",
            "views": parse_views(views_el.get_text() if views_el else ""),
            "url": f"https://t.me/{channel}/{post_id}",
            "our_refs": our_refs,
        })
    return posts


def collect(channel, since, max_pages=30):
    found, before = {}, None
    for page in range(max_pages):
        posts = parse(fetch_page(channel, before), channel)
        if not posts:
            if page == 0:
                raise RuntimeError(f"Не удалось прочитать t.me/s/{channel} "
                                   "(канал закрытый или отключён веб-просмотр)")
            break
        for p in posts:
            if p["date"] >= since:
                found[p["id"]] = p
        oldest = min(posts, key=lambda p: p["id"])
        if oldest["date"] < since or (before and oldest["id"] >= before):
            break
        before = oldest["id"]
    return found


# ---------- 2. Claude ----------

def call_claude(prompt, max_tokens):
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": API_KEY, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": MODEL, "max_tokens": max_tokens, "temperature": 0,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=600,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Claude API {r.status_code}: {r.text[:500]}")
    raw = "".join(b.get("text", "") for b in r.json()["content"] if b["type"] == "text").strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError(f"Claude вернул не JSON: {raw[:300]}")


CLASSIFY_PROMPT = """Ты помогаешь редакции отобрать новости iGaming-индустрии для еженедельного дайджеста.

Ниже блок ПОСТЫ НАШЕГО КАНАЛА (у каждого есть ID) – это кандидаты в дайджест. Может быть ещё блок ПОСТЫ КОНТРОЛЬНЫХ КАНАЛОВ, сгруппированный по каналам, – это только справочный материал.

Для КАЖДОГО поста нашего канала верни объект с полями:
- id – ID поста;
- exclude – true, если это реклама или партнёрский материал, розыгрыш или конкурс, анонс мероприятия (будущего), служебный пост (опрос, приветствие, объявление о канале) или пост без новости; иначе false;
- duplicate_of – если пост о том же событии, что и другой пост нашего канала, укажи ID самого полного поста об этом событии; у самого полного поста и у уникальных постов – null;
- category – одна из:
  regulation – законы, запреты рекламы, лицензирование, налоги, решения и позиция регуляторов;
  sanctions – штрафы, отзыв лицензий, блокировки;
  deals – слияния, поглощения, крупные партнёрства, выход компаний на рынки и уход с них;
  market_data – цифры рынка, финансовые отчёты, статистика регуляторов, рекорды;
  events – итоги прошедших мероприятий, награды;
  company – прочие новости компаний и продуктов;
- country – страна, к которой относится новость, на английском (например "Canada"); если новость глобальная или страна неясна – null;
- mentions – список контрольных каналов (названия как в заголовках блока), где есть новость о том же самом событии. Считай только явное совпадение события, общей темы недостаточно. Если совпадений нет или блока нет – пустой список.

Для постов с exclude = true достаточно полей id и exclude.

Ответь только JSON-массивом без пояснений и без markdown-обёртки.

{data}"""

WRITE_PROMPT = """Ниже посты Telegram-канала с новостями iGaming-индустрии. Редакция уже отобрала их для дайджеста, порядок не меняй, ничего не добавляй и не убирай.

Для каждого поста напиши:
- emoji: один эмодзи. Если новость про страну или регион – флаг этой страны. Иначе тематический (🏆 награды, 📊 цифры и отчёты, ⚖️ регулирование, 🤝 сделки, 💰 финансы и т. п.).
- title: заголовок на русском, 6–14 слов, с главной цифрой или фактом, если они есть. Без точки в конце.
- summary: одно предложение (максимум два) на русском с сутью новости. Самое важное (название компании, мероприятия, ключевую цифру или вывод) выдели двойными звёздочками: **так**. Выделений 1–2 на пункт.

Пиши сухо и по делу, только факты из поста, ничего не додумывай. Длинное тире не используй, только среднее (–).

Ответь только JSON-массивом в том же порядке, без пояснений и без markdown-обёртки:
[{{"id": 123, "emoji": "🇨🇦", "title": "...", "summary": "..."}}]

ПОСТЫ:
{corpus}"""


def build_classify_data(ours, controls):
    parts = ["ПОСТЫ НАШЕГО КАНАЛА:"]
    for p in sorted(ours.values(), key=lambda p: p["id"]):
        parts.append(f"ID {p['id']} ({p['date']:%d.%m})\n{p['text'][:800]}\n---")
    if controls:
        parts.append("\nПОСТЫ КОНТРОЛЬНЫХ КАНАЛОВ:")
        for ch, posts in controls.items():
            parts.append(f"\n=== {ch} ===")
            for p in sorted(posts.values(), key=lambda p: p["id"]):
                if p["text"]:
                    parts.append("- " + p["text"][:250].replace("\n", " "))
    return "\n".join(parts)


# ---------- 3. Баллы и отбор ----------

def score(ours, controls, classes):
    """Возвращает список кандидатов с баллами и расшифровкой."""
    now = dt.datetime.now(dt.timezone.utc)
    by_id = {int(c["id"]): c for c in classes if "id" in c}

    # кто из контрольных каналов сделал репост/ссылку на наш пост (проверяет код)
    reposts = {}
    for ch, posts in controls.items():
        for p in posts.values():
            for ref in p["our_refs"]:
                reposts.setdefault(ref, set()).add(ch)

    # группы: каноничный пост + его дубли
    groups = {}
    for pid in ours:
        c = by_id.get(pid)
        if not c or c.get("exclude"):
            continue
        try:
            canon = int(c.get("duplicate_of") or pid)
        except (TypeError, ValueError):
            canon = pid
        if canon not in ours or canon not in by_id or by_id[canon].get("exclude"):
            canon = pid
        groups.setdefault(canon, []).append(pid)

    cands = []
    for canon, members in groups.items():
        c = by_id[canon]
        cat = c.get("category") if c.get("category") in CATEGORY_POINTS else "company"
        rep_ch, men_ch, vph = set(), set(), 0.0
        for pid in members:
            p = ours[pid]
            rep_ch |= reposts.get(pid, set())
            men_ch |= {str(m).lstrip("@") for m in (by_id[pid].get("mentions") or [])} & set(controls)
            hours = max((now - p["date"]).total_seconds() / 3600, 1)
            vph = max(vph, p["views"] / hours)
        men_ch -= rep_ch
        control_pts = min(len(rep_ch) * REPOST_POINTS + len(men_ch) * MENTION_POINTS, CONTROL_CAP)
        cands.append({
            "id": canon, "category": cat, "country": (c.get("country") or "").strip().lower() or None,
            "cat_pts": CATEGORY_POINTS[cat], "reposts": sorted(rep_ch), "mentions": sorted(men_ch),
            "control_pts": control_pts, "vph": vph, "views_pts": 0, "dupes": len(members) - 1,
        })

    # просмотры в час: верхняя треть недели
    if cands:
        ranked = sorted(cands, key=lambda x: x["vph"], reverse=True)
        for x in ranked[:max(1, len(ranked) // 3)]:
            if x["vph"] > 0:
                x["views_pts"] = VIEWS_POINTS
    for x in cands:
        x["total"] = x["cat_pts"] + x["control_pts"] + x["views_pts"]
    return cands


def select(cands):
    picked, by_country, by_cat = [], {}, {}
    for x in sorted(cands, key=lambda x: (x["total"], x["vph"]), reverse=True):
        if x["country"] and by_country.get(x["country"], 0) >= MAX_PER_COUNTRY:
            continue
        if by_cat.get(x["category"], 0) >= MAX_PER_CATEGORY:
            continue
        picked.append(x)
        if x["country"]:
            by_country[x["country"]] = by_country.get(x["country"], 0) + 1
        by_cat[x["category"]] = by_cat.get(x["category"], 0) + 1
        if len(picked) == TOP_N:
            break
    return picked


# ---------- 4. Сборка поста и справки ----------

def fmt(text):
    text = html.escape(str(text).replace("—", "–"))
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)


def build_blocks(written, ours):
    blocks = [f"<b>{fmt(HEADER)}</b>\n\n{fmt(INTRO)}"]
    for i in written:
        url = html.escape(ours[int(i["id"])]["url"], quote=True)
        blocks.append(f"{i.get('emoji', '📰')} <b>{fmt(i['title'])}</b>\n"
                      f"<blockquote>{fmt(i['summary'])}</blockquote>\n"
                      f'<a href="{url}">{fmt(LINK_TEXT)}</a>')
    return blocks


def build_report(picked, written, stats):
    titles = {int(w["id"]): w["title"] for w in written}
    lines = ["Справка: почему выбраны эти новости", "",
             f"Постов за неделю: {stats['total']}, отсеяно: {stats['excluded']}, "
             f"дублей объединено: {stats['dupes']}, кандидатов: {stats['cands']}"]
    if CONTROL_CHANNELS:
        lines.append(f"Контрольные каналы прочитаны: {stats['ctrl_ok']} из {len(CONTROL_CHANNELS)}")
        for err in stats["ctrl_err"]:
            lines.append(f"  не прочитан: {err}")
    else:
        lines.append("Контрольные каналы не заданы")
    lines.append("")
    for n, x in enumerate(picked, 1):
        parts = [f"тема «{CATEGORY_NAMES[x['category']]}» +{x['cat_pts']}"]
        if x["control_pts"]:
            parts.append(f"репосты {len(x['reposts'])}, упоминания {len(x['mentions'])} – +{x['control_pts']}")
        if x["views_pts"]:
            parts.append(f"просмотры +{x['views_pts']}")
        lines.append(f"{n}. {titles.get(x['id'], '')} – {x['total']} б.")
        lines.append(f"   {'; '.join(parts)}; {round(x['vph'])} просм./час")
        lines.append(f"   https://t.me/{CHANNEL}/{x['id']}")
    return "\n".join(lines).replace("—", "–")


# ---------- 5. Отправка ----------

def send(text, parse_html=False):
    payload = {"chat_id": CHAT_ID, "text": text, "link_preview_options": {"is_disabled": True}}
    if parse_html:
        payload["parse_mode"] = "HTML"
    r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json=payload, timeout=30)
    if not r.ok:
        raise RuntimeError(f"Telegram {r.status_code}: {r.text[:300]}")


def send_chunks(blocks, parse_html):
    msg = ""
    for b in blocks:
        if msg and len(msg) + len(b) + 2 > 3800:
            send(msg, parse_html)
            msg = ""
        msg = f"{msg}\n\n{b}" if msg else b
    if msg:
        send(msg, parse_html)


# ---------- Запуск ----------

def main():
    missing = [n for n, v in [("TELEGRAM_BOT_TOKEN", BOT_TOKEN), ("TELEGRAM_CHAT_ID", CHAT_ID),
                              ("ANTHROPIC_API_KEY", API_KEY)] if not v]
    if missing:
        print("Не заданы секреты: " + ", ".join(missing), file=sys.stderr)
        sys.exit(1)

    try:
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=DAYS)

        print(f"1/5 Читаю @{CHANNEL}")
        raw = collect(CHANNEL, since)
        ours = {k: p for k, p in raw.items()
                if p["text"] and not p["text"].lower().startswith("дайджест")}
        print(f"    постов: {len(raw)}, с текстом: {len(ours)}")
        if not ours:
            send(f"За последние {DAYS} дней в @{CHANNEL} не нашлось постов с текстом.")
            return

        print("2/5 Читаю контрольные каналы")
        controls, ctrl_err = {}, []
        for ch in CONTROL_CHANNELS:
            try:
                controls[ch] = collect(ch, since, max_pages=15)
                print(f"    @{ch}: {len(controls[ch])}")
            except Exception as e:
                ctrl_err.append(f"@{ch} ({e})")
                print(f"    @{ch}: ошибка {e}")

        print("3/5 Классификация (Claude)")
        classes = call_claude(CLASSIFY_PROMPT.format(data=build_classify_data(ours, controls)), 16000)

        print("4/5 Баллы и отбор")
        cands = score(ours, controls, classes)
        picked = select(cands)
        if not picked:
            send(f"За неделю в @{CHANNEL} не нашлось новостей для дайджеста после фильтров.")
            return

        print("5/5 Тексты (Claude) и отправка")
        corpus = "\n\n---\n\n".join(f"ID {x['id']}\n{ours[x['id']]['text'][:1500]}" for x in picked)
        written = call_claude(WRITE_PROMPT.format(corpus=corpus), 4000)
        order = {x["id"]: n for n, x in enumerate(picked)}
        written = sorted([w for w in written if int(w["id"]) in order], key=lambda w: order[int(w["id"])])

        send_chunks(build_blocks(written, ours), parse_html=True)
        if SEND_REPORT:
            stats = {"total": len(ours),
                     "excluded": sum(1 for c in classes if c.get("exclude")),
                     "dupes": sum(x["dupes"] for x in cands), "cands": len(cands),
                     "ctrl_ok": len(controls), "ctrl_err": ctrl_err}
            send_chunks(build_report(picked, written, stats).split("\n\n"), parse_html=False)
        print(f"Готово: в дайджесте {len(written)} новостей")
    except Exception as e:
        traceback.print_exc()
        try:
            send(f"Дайджест @{CHANNEL} не собрался.\nОшибка: {e}")
        except Exception as e2:
            print(f"Не удалось отправить ошибку в Telegram: {e2}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
