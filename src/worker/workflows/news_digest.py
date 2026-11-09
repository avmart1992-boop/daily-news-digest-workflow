"""Daily news digest: RF telecom law + Orange Business Services RU.

Fetches Google News RSS, builds an HTML digest, sends via Gmail SMTP.
All non-deterministic work (network, time, SMTP) happens in activities.
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
        "id": "telecom-law",
        "emoji": "\U0001F4E1",
        "title": "Телеком-законодательство РФ (связь и телекоммуникации)",
        "queries": [
            "закон о связи Госдума поправки",
            "Минцифры связь операторы закон",
            "телекоммуникации законодательство России",
        ],
    },
    {
        "id": "orange-business",
        "emoji": "\U0001F7E0",
        "title": "ООО «Оранж Бизнес Сервисез» (Orange Business Services в России)",
        "queries": [
            "Оранж Бизнес Сервисез",
            "Orange Business Services Россия",
        ],
    },
]


class DigestInput(BaseModel):
    recipient: str = DEFAULT_RECIPIENT
    days: int = 2
    max_items_per_topic: int = 7
    dry_run: bool = False


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


def _fallback_desc(item: Dict[str, Any]) -> str:
    bits = []
    if item.get("source"):
        bits.append(html.escape(item["source"]))
    if item.get("published"):
        try:
            bits.append(
                datetime.fromisoformat(item["published"]).strftime("%d.%m.%Y")
            )
        except Exception:
            pass
    return " — ".join(bits)


def _describe(items: List[Dict[str, Any]]) -> List[str]:
    """Short descriptions via Mistral API; fallback to source and date."""
    try:
        from mistralai.client import Mistral

        client = Mistral()
        numbered = "\n".join(
            f"{i + 1}. {it['title']}" for i, it in enumerate(items)
        )
        prompt = (
            "Ниже — заголовки российских новостей. Для каждого заголовка напиши "
            "одно краткое описание (1 предложение, максимум 20 слов) на русском. "
            "Верни ТОЛЬКО JSON-массив строк той же длины, без пояснений.\n\n"
            + numbered
        )
        resp = client.chat.complete(
            model="mistral-small-latest",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
        )
        content = (resp.choices[0].message.content or "").strip()
        start, end = content.find("["), content.rfind("]")
        data = json.loads(content[start : end + 1])
        if isinstance(data, list) and len(data) == len(items):
            return [str(x) for x in data]
    except Exception:
        pass
    return [_fallback_desc(it) for it in items]


@workflows.activity()
async def fetch_topic(
    topic: Dict[str, Any], days: int, max_items: int
) -> Dict[str, Any]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    seen = set()
    fresh: List[Dict[str, Any]] = []
    recent: List[Dict[str, Any]] = []
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
            recent.append(item)
    fresh = fresh[:max_items]
    recent = recent[:2]
    return {
        "id": topic["id"],
        "emoji": topic["emoji"],
        "title": topic["title"],
        "fresh": fresh,
        "background": recent,
    }


def _render(topics: List[Dict[str, Any]]) -> str:
    today = datetime.now(MOSCOW).strftime("%d.%m.%Y")
    parts = [f"<h2>Краткая сводка новостей на {today}</h2>"]
    for topic in topics:
        fresh = topic.get("fresh") or []
        parts.append(f"<h3>{topic['emoji']} {html.escape(topic['title'])}</h3>")
        if fresh:
            descs = _describe(fresh)
            parts.append("<ul>")
            for item, desc in zip(fresh, descs):
                desc_html = html.escape(desc) if desc else ""
                parts.append(
                    "<li><b>"
                    + html.escape(item["title"])
                    + "</b>"
                    + ("<br>" + desc_html if desc_html else "")
                    + '<br><a href="'
                    + html.escape(item["link"], quote=True)
                    + '">Читать в источнике</a></li>'
                )
            parts.append("</ul>")
        else:
            parts.append("<p>Значимых новостей за последние 2 дня не найдено.</p>")
            background = topic.get("background") or []
            if background:
                parts.append("<p><b>Фоновые материалы:</b></p><ul>")
                for item in background:
                    parts.append(
                        "<li><b>"
                        + html.escape(item["title"])
                        + '<br><a href="'
                        + html.escape(item["link"], quote=True)
                        + '">Читать в источнике</a></li>'
                    )
                parts.append("</ul>")
    parts.append(
        '<p style="color:#888;font-size:small;">Сводка подготовлена автоматически. '
        "Источники: Google News (RSS). Время: Europe/Moscow.</p>"
    )
    return "\n".join(parts)


@workflows.activity()
async def build_and_send(
    topics: List[Dict[str, Any]], recipient: str, dry_run: bool
) -> Dict[str, Any]:
    """Renders the digest and sends it. Runs outside the deterministic sandbox."""
    today = datetime.now(MOSCOW).strftime("%d.%m.%Y")
    subject = (
        "Новости: телеком-законодательство РФ и Оранж бизнес сервисез — " + today
    )
    html_body = _render(topics)

    if dry_run:
        return {
            "sent": False,
            "dry_run": True,
            "subject": subject,
            "recipient": recipient,
            "topics": [
                {"title": t["title"], "fresh_count": len(t["fresh"])}
                for t in topics
            ],
        }

    sender = os.environ.get("GMAIL_ADDRESS", recipient)
    password = os.environ.get("GMAIL_APP_PASSWORD", "")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
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
        "topics": [
            {"title": t["title"], "fresh_count": len(t["fresh"])}
            for t in topics
        ],
    }


@workflows.workflow.define(
    name="daily-news-digest",
    workflow_display_name="Ежедневная новостная сводка",
    workflow_description=(
        "Ищет новости по телеком-законодательству РФ и Orange Business Services "
        "и отправляет сводку на почту."
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
            dry_run=input.dry_run,
        )
