"""Daily news digest for an Orange Business Services RU analyst.

Monitors four categories, filters relevance via an LLM, renders a structured
digest (headline / source / gist / impact) and sends it via Gmail SMTP.
Supports CC recipients. All non-deterministic work (network, time, LLM, SMTP)
happens in activities.
"""
from __future__ import annotations

import asyncio
import html
import json
import os
import smtplib
import ssl
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

from pydantic import BaseModel

import mistralai.workflows as workflows

MOSCOW = ZoneInfo("Europe/Moscow")
DEFAULT_RECIPIENT = os.environ.get("GMAIL_ADDRESS", "Avmart1992@gmail.com")

TOPICS: List[Dict[str, Any]] = [
    {
        "id": "direct",
        "emoji": "\U0001F7E0",
        "title": "Прямые упоминания: Orange Business Services, ООО «Оранж Бизнес Сервисез», Эквант, Orange Cyberdefense",
        "queries": [
            "Orange Business Services Россия",
            "Оранж Бизнес Сервисез",
            "Эквант",
            "Orange Cyberdefense Россия",
        ],
    },
    {
        "id": "regulatory",
        "emoji": "\U0001F4DC",
        "title": "Регуляторные изменения в РФ (связь, КИИ, СОРМ, импортозамещение, санкционный комплаенс)",
        "queries": [
            "Минцифры связь закон операторы",
            "Роскомнадзор операторы связи штраф",
            "закон КИИ критическая информационная инфраструктура",
            "СОРМ требования операторы связи",
            "импортозамещение телеком оборудование",
            "подменные номера штрафы операторам",
            "трансграничная передача данных закон",
        ],
    },
    {
        "id": "b2b-market",
        "emoji": "\U0001F4E6",
        "title": "Рынок B2B-телекома и ИТ: конкуренты и корпоративные продукты",
        "queries": [
            "Ростелеком бизнес корпоративный контракт",
            "МТС бизнес облачные сервисы предприятиям",
            "МегаФон корпоративный бизнес",
            "Билайн Бизнес B2B",
            "Яндекс Облако корпоративный",
        ],
    },
    {
        "id": "tech-trends",
        "emoji": "\U0001F9EE",
        "title": "Технологические тренды для B2B: IoT, Big Data, Edge, ИИ в телекоме, кибербезопасность корпоративных сетей",
        "queries": [
            "корпоративный IoT промышленный интернет вещей",
            "SD-WAN корпоративные сети Россия",
            "Edge Computing телеком",
            "искусственный интеллект телеком операторы",
            "кибербезопасность корпоративных сетей атака",
        ],
    },
]


class DigestInput(BaseModel):
    recipient: str = DEFAULT_RECIPIENT
    cc: List[str] = []
    days: int = 2
    max_items_per_topic: int = 7
    dry_run: bool = False


ROLE_PROMPT = """Роль: Ты — профильный ИИ-аналитик в сфере телекома и ИТ, персональный ассистент сотрудника ООО «Оранж Бизнес Сервисез» (международный B2B-провайдер связи и ИТ-решений в России).

Контекст о компании для фильтрации новостей:
- Компания работает строго в сегменте B2B (крупный бизнес, международные корпорации, дипломатические представительства). Новости про потребительские тарифы физлиц, мобильную связь 4G/5G для населения её НЕ касаются.
- Ключевые направления: корпоративные сети (VPN, SD-WAN), спутниковая связь, облачные сервисы, информационная безопасность (включая Orange Cyberdefense), системная интеграция.
- Специфика: материнская компания — французская группа Orange. ООО «Оранж Бизнес Сервисез» продолжает операционную деятельность в РФ, поэтому критически важны комплаенс, контрсанкции и российское регулирование.

Правила:
- Если новость сомнительная или кликбейтная — пометь questionable=true и кратко укажи почему в поле gist.
- Не включай новости про массовый мобильный ритейл или бытовой интернет для физлиц: ставь relevant=false.
- Влияние на Orange формулируй конкретно: риск регуляторного штрафа, угроза оттока клиентов к конкуренту, возможность предложить импортонезависимое решение и т.п. Если влияния нет — relevant=false.

Тебе передадут список новостей (title, source, link). Верни ТОЛЬКО валидный JSON-массив той же длины, где каждый элемент — объект:
{"headline": "смысловой заголовок, отражающий суть влияния", "gist": "что произошло, 1-2 предложения", "impact": "почему это важно для ООО «Оранж Бизнес Сервисез»", "questionable": false, "relevant": true}
Без пояснений и без markdown-обёртки."""


def _fetch_rss(query: str, limit: int = 15) -> List[Dict[str, Any]]:
    url = (
        "https://news.google.com/rss/search?q="
        + urllib.parse.quote(query)
        + "&hl=ru&gl=RU&ceid=RU:ru"
    )
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; news-digest-workflow/1.0)"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    root = ET.fromstring(raw)
    items: List[Dict[str, Any]] = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        source = (item.findtext("source") or "").strip()
        published = None
        pub_raw = item.findtext("pubDate")
        if pub_raw:
            try:
                published = parsedate_to_datetime(pub_raw)
            except Exception:
                published = None
        if not title or not link:
            continue
        clean_title = title
        if source and title.endswith(" - " + source):
            clean_title = title[: -(len(source) + 3)].strip()
        items.append(
            {
                "title": clean_title,
                "link": link,
                "source": source,
                "published": published.isoformat() if published else None,
            }
        )
        if len(items) >= limit:
            break
    return items


def _fallback_analysis(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "headline": item["title"],
        "gist": "",
        "impact": "",
        "questionable": False,
        "relevant": True,
    }


def _analyze(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """LLM analysis: headline, gist, impact, relevance. Falls back gracefully."""
    try:
        from mistralai.client import Mistral

        client = Mistral()
        payload = json.dumps(
            [
                {"title": it["title"], "source": it.get("source", ""), "link": it["link"]}
                for it in items
            ],
            ensure_ascii=False,
        )
        user_msg = (
            "Новости для анализа (последовательность важна, порядок сохраняй):\n" + payload
        )
        resp = client.chat.complete(
            model="mistral-medium-latest",
            messages=[
                {"role": "system", "content": ROLE_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.2,
        )
        content = (resp.choices[0].message.content or "").strip()
        start, end = content.find("["), content.rfind("]")
        data = json.loads(content[start : end + 1])
        if isinstance(data, list) and len(data) == len(items):
            out = []
            for entry in data:
                if not isinstance(entry, dict):
                    return [_fallback_analysis(it) for it in items]
                out.append(
                    {
                        "headline": str(entry.get("headline") or ""),
                        "gist": str(entry.get("gist") or ""),
                        "impact": str(entry.get("impact") or ""),
                        "questionable": bool(entry.get("questionable", False)),
                        "relevant": bool(entry.get("relevant", True)),
                    }
                )
            return out
    except Exception:
        pass
    return [_fallback_analysis(it) for it in items]


@workflows.activity()
async def fetch_topic(
    topic: Dict[str, Any], days: int, max_items: int
) -> Dict[str, Any]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    seen = set()
    fresh: List[Dict[str, Any]] = []
    for query in topic["queries"]:
        for item in _fetch_rss(query, limit=15):
            key = item["title"].lower()
            if key in seen:
                continue
            seen.add(key)
            published = None
            if item.get("published"):
                try:
                    published = datetime.fromisoformat(item["published"])
                except Exception:
                    published = None
            if published is not None and published >= cutoff:
                fresh.append(item)
    fresh = fresh[:max_items]
    analyses = _analyze(fresh) if fresh else []
    for item, analysis in zip(fresh, analyses):
        item["analysis"] = analysis
    return {
        "id": topic["id"],
        "emoji": topic["emoji"],
        "title": topic["title"],
        "fresh": fresh,
    }


def _render_item(item: Dict[str, Any]) -> str:
    analysis = item.get("analysis") or {}
    headline = analysis.get("headline") or item["title"]
    gist = analysis.get("gist") or ""
    impact = analysis.get("impact") or ""
    questionable = analysis.get("questionable", False)
    source = item.get("source") or ""
    link = item.get("link") or ""
    date_str = ""
    if item.get("published"):
        try:
            date_str = datetime.fromisoformat(item["published"]).strftime("%d.%m.%Y")
        except Exception:
            date_str = ""
    parts = ["<li>"]
    parts.append("\U0001F4CC <b>" + html.escape(headline) + "</b>")
    source_line = "\U0001F4F0 Источник: "
    if source:
        source_line += html.escape(source)
    if link:
        source_line += ' — <a href="' + html.escape(link, quote=True) + '">ссылка</a>'
    if date_str:
        source_line += " (" + date_str + ")"
    parts.append(source_line)
    if gist:
        parts.append("\U0001F4DD Суть: " + html.escape(gist))
    if impact:
        parts.append("\U0001F4A1 Влияние на Orange: " + html.escape(impact))
    if questionable:
        parts.append("\u26A0\uFE0F Новость может быть сомнительной или кликбейтной — проверьте первоисточник.")
    parts.append("</li>")
    return "\n".join(parts)


def _render(topics: List[Dict[str, Any]]) -> str:
    today = datetime.now(MOSCOW).strftime("%d.%m.%Y")
    parts = [f"<h2>Краткая сводка новостей на {today}</h2>"]
    for topic in topics:
        fresh = topic.get("fresh") or []
        relevant_items = [
            it for it in fresh if (it.get("analysis") or {}).get("relevant", True)
        ]
        parts.append(f"<h3>{topic['emoji']} {html.escape(topic['title'])}</h3>")
        if relevant_items:
            parts.append("<ul>")
            for item in relevant_items:
                parts.append(_render_item(item))
            parts.append("</ul>")
        else:
            parts.append("<p>Значимых новостей за последние 2 дня не найдено.</p>")
    total = sum(
        1
        for t in topics
        for it in (t.get("fresh") or [])
        if (it.get("analysis") or {}).get("relevant", True)
    )
    parts.append(
        '<p style="color:#888;font-size:small;">Сводка подготовлена автоматически. '
        "Отобрано новостей: " + str(total) + ". Источники: Google News (RSS). "
        "Время: Europe/Moscow.</p>"
    )
    return "\n".join(parts)


@workflows.activity()
async def build_and_send(
    topics: List[Dict[str, Any]], recipient: str, cc: List[str], dry_run: bool
) -> Dict[str, Any]:
    """Renders the digest and sends it. Runs outside the deterministic sandbox."""
    today = datetime.now(MOSCOW).strftime("%d.%m.%Y")
    subject = (
        "Новости: телеком-законодательство РФ и Оранж бизнес сервисез — " + today
    )
    html_body = _render(topics)

    summary = [
        {
            "title": t["title"],
            "fresh_count": len(t["fresh"]),
            "relevant_count": sum(
                1
                for it in t["fresh"]
                if (it.get("analysis") or {}).get("relevant", True)
            ),
        }
        for t in topics
    ]

    if dry_run:
        return {
            "sent": False,
            "dry_run": True,
            "subject": subject,
            "recipient": recipient,
            "cc": cc,
            "topics": summary,
        }

    sender = os.environ.get("GMAIL_ADDRESS", recipient)
    password = os.environ.get("GMAIL_APP_PASSWORD", "")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg.set_content("Откройте HTML-версию письма, чтобы увидеть сводку новостей.")
    msg.add_alternative(html_body, subtype="html")
    context = ssl.create_default_context()
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=60) as server:
        server.starttls(context=context)
        server.login(sender, password)
        server.send_message(msg)
    return {
        "sent": True,
        "dry_run": False,
        "subject": subject,
        "recipient": recipient,
        "cc": cc,
        "topics": summary,
    }


@workflows.workflow.define(
    name="daily-news-digest",
    workflow_display_name="Ежедневная новостная сводка",
    workflow_description=(
        "Мониторит новости для ООО «Оранж Бизнес Сервисез»: прямые упоминания, "
        "регуляторика РФ, B2B-рынок и технологические тренды; отправляет "
        "структурированную выжимку на почту (с поддержкой копий CC)."
    ),
)
class DailyNewsDigest:
    @workflows.workflow.entrypoint
    async def run(self, input: DigestInput) -> Dict[str, Any]:
        results = await asyncio.gather(
            *[
                fetch_topic(
                    topic=topic,
                    days=input.days,
                    max_items=input.max_items_per_topic,
                )
                for topic in TOPICS
            ]
        )
        return await build_and_send(
            topics=list(results),
            recipient=input.recipient,
            cc=list(input.cc),
            dry_run=input.dry_run,
        )
