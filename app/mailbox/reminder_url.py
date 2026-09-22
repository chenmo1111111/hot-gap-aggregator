"""Read a public recruitment URL and extract a confirmable manual reminder preview."""

from __future__ import annotations

import io
import ipaddress
import json
import re
import socket
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Mapping
from urllib.parse import urljoin, urlsplit

import httpx
from selectolax.parser import HTMLParser

from .extract import CHINA_TZ, DeepSeekMailboxExtractor, _absolute_deadline, _json_object


MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024
MAX_TEXT_CHARS = 30_000
DATE_TIME_RE = re.compile(
    r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"
    r"(?:\s*(\d{1,2})(?:\s*[:：时]\s*(\d{1,2}))?\s*分?)?"
)
EVENT_TIME_RE = re.compile(
    r"(?:(20\d{2})\s*年\s*)?(\d{1,2})\s*月\s*(\d{1,2})\s*日"
    r"(?:\s*(?:周|星期)[一二三四五六日天])?\s*"
    r"(\d{1,2})\s*[:：]\s*(\d{2})"
    r"(?:\s*(?:-|—|–|至|~)\s*(\d{1,2})\s*[:：]\s*(\d{2}))?"
)
SCHEDULED_EVENT_WORDS = ("面试", "笔试", "测评", "会议", "宣讲", "考试")
URL_RE = re.compile(r"https?://[^\s<>\"'（）()]+", re.I)
SYSTEM_PROMPT = """你负责从中国招聘公告正文中提取报名提醒。只输出一个 JSON 对象，不要 Markdown。
字段必须完整：
title: 招聘事项的简短名称；
type: 如“公考报名”“秋招投递”“资格审查”，无法细分用“报名”；
start_at: 报名、面试、笔试、测评或会议的开始时间，ISO 8601 且必须带 +08:00；没有则 null；
deadline_at: 报名截止或确认参加的截止时间，ISO 8601 且必须带 +08:00；没有则 null；
action_url: 公告中真实报名入口，没有则使用原公告网址；
summary: 一句话说明需要做什么；
confidence: high、medium 或 low。
必须区分公告发布日期、报名时间、资格审查时间和考试时间。禁止把公告发布日期当报名时间；禁止猜测不存在的日期。
对于已经安排好时间的面试、笔试、测评或会议：start_at 填活动开始时间；结束时间只写进 summary，不得当成 deadline_at。"""


class ReminderUrlError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReminderPreview:
    title: str
    type: str
    start_at: str | None
    deadline_at: str | None
    action_url: str
    summary: str
    confidence: str
    source_url: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _public_http_url(url: str) -> str:
    value = url.strip()
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise ReminderUrlError("网址格式不正确") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ReminderUrlError("只支持公开的 http/https 网址")
    host = parsed.hostname.casefold().rstrip(".")
    if host == "localhost" or host.endswith(".local"):
        raise ReminderUrlError("不允许读取本机或内网地址")
    try:
        default_port = 443 if parsed.scheme == "https" else 80
        addresses = {item[4][0] for item in socket.getaddrinfo(host, parsed.port or default_port)}
    except socket.gaierror as exc:
        raise ReminderUrlError("网址域名无法解析") from exc
    for raw in addresses:
        address = ipaddress.ip_address(raw)
        if not address.is_global:
            raise ReminderUrlError("不允许读取本机、内网或保留地址")
    return value


def _html_document(content: bytes, encoding: str, base_url: str) -> tuple[str, str, list[str]]:
    text = content.decode(encoding or "utf-8", errors="replace")
    tree = HTMLParser(text)
    for selector in ("script", "style", "noscript", "svg", "nav", "footer"):
        for node in tree.css(selector):
            node.decompose()
    title_node = tree.css_first("h1") or tree.css_first("title")
    title = " ".join((title_node.text() if title_node else "").split())[:200]
    body = tree.body or tree.root
    plain = "\n".join(line.strip() for line in body.text(separator="\n").splitlines() if line.strip())
    links: list[str] = []
    for node in tree.css("a[href]"):
        href = urljoin(base_url, str(node.attributes.get("href") or "").strip())
        if href.startswith(("http://", "https://")) and href not in links:
            links.append(href)
    for href in URL_RE.findall(plain):
        if href not in links:
            links.append(href)
    return title, plain[:MAX_TEXT_CHARS], links[:100]


def _pdf_document(content: bytes) -> tuple[str, str, list[str]]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ReminderUrlError("服务器尚未安装 PDF 文本读取组件") from exc
    try:
        reader = PdfReader(io.BytesIO(content))
        pages = [(page.extract_text() or "") for page in reader.pages[:80]]
    except Exception as exc:
        raise ReminderUrlError("PDF 正文读取失败，可能是扫描件或已加密") from exc
    plain = "\n".join(pages).replace("\x00", "")[:MAX_TEXT_CHARS]
    if not plain.strip():
        raise ReminderUrlError("PDF 没有可读取文字，扫描件暂不支持自动识别")
    links = list(dict.fromkeys(URL_RE.findall(plain)))[:100]
    return "", plain, links


async def read_public_document(url: str) -> tuple[str, str, list[str], str]:
    current = _public_http_url(url)
    headers = {"User-Agent": "Mozilla/5.0 (compatible; HotGapReminder/1.0)"}
    async with httpx.AsyncClient(timeout=30, headers=headers, follow_redirects=False) as client:
        for _ in range(6):
            response = await client.get(current)
            if response.is_redirect:
                location = response.headers.get("location", "")
                if not location:
                    raise ReminderUrlError("公告网址重定向缺少目标地址")
                current = _public_http_url(urljoin(current, location))
                continue
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise ReminderUrlError(f"公告网址返回 HTTP {response.status_code}") from exc
            content = response.content
            if len(content) > MAX_DOWNLOAD_BYTES:
                raise ReminderUrlError("公告页面超过10MB，无法自动读取")
            content_type = response.headers.get("content-type", "").casefold()
            if "pdf" in content_type or current.casefold().split("?", 1)[0].endswith(".pdf"):
                title, text, links = _pdf_document(content)
            elif "html" in content_type or not content_type:
                title, text, links = _html_document(content, response.encoding or "utf-8", current)
            else:
                raise ReminderUrlError("该网址不是可读取的网页或PDF")
            if len(text.strip()) < 80:
                raise ReminderUrlError("页面正文过短，可能需要登录或由 JavaScript 动态加载")
            return title, text, links, current
    raise ReminderUrlError("公告网址重定向次数过多")


def _fallback_dates(text: str) -> tuple[str | None, str | None]:
    section_match = re.search(r"报名时间[：:]?(.{0,260})", text, re.S)
    area = section_match.group(1) if section_match else text
    values: list[str] = []
    for match in DATE_TIME_RE.finditer(area):
        year, month, day, hour, minute = match.groups()
        parsed = datetime(
            int(year), int(month), int(day), int(hour or 0), int(minute or 0), tzinfo=CHINA_TZ,
        )
        value = parsed.isoformat()
        if value not in values:
            values.append(value)
        if len(values) == 2:
            break
    if len(values) >= 2:
        return values[0], values[1]
    return (values[0], None) if values else (None, None)


def _is_scheduled_event(item_type: str) -> bool:
    return any(word in item_type for word in SCHEDULED_EVENT_WORDS)


def _fallback_type(text: str) -> str:
    return next((word for word in SCHEDULED_EVENT_WORDS if word in text), "报名")


def _fallback_event_start(text: str, *, now: datetime) -> str | None:
    match = EVENT_TIME_RE.search(text)
    if not match:
        return None
    year, month, day, hour, minute, _, _ = match.groups()
    parsed = datetime(
        int(year or now.year), int(month), int(day), int(hour), int(minute), tzinfo=CHINA_TZ,
    )
    if year is None and parsed < now - timedelta(days=30):
        parsed = parsed.replace(year=parsed.year + 1)
    return parsed.isoformat()


def normalize_preview(
    raw: Mapping[str, Any], *, page_title: str, text: str,
    links: list[str], source_url: str, now: datetime | None = None,
) -> ReminderPreview:
    current = (now or datetime.now(CHINA_TZ)).astimezone(CHINA_TZ)
    title = " ".join(str(raw.get("title") or page_title or "报名提醒").split())[:160]
    item_type = " ".join(str(raw.get("type") or "报名").split())[:40]
    start = _absolute_deadline(raw.get("start_at"))
    deadline = _absolute_deadline(raw.get("deadline_at"))
    fallback_start, fallback_end = _fallback_dates(text)
    start_at = start.astimezone(CHINA_TZ).isoformat() if start else fallback_start
    deadline_at = deadline.astimezone(CHINA_TZ).isoformat() if deadline else fallback_end
    if _is_scheduled_event(item_type):
        start_at = start_at or _fallback_event_start(text, now=current)
        if start_at and deadline_at:
            start_value = datetime.fromisoformat(start_at)
            deadline_value = datetime.fromisoformat(deadline_at)
            if start_value < deadline_value <= start_value + timedelta(hours=12):
                deadline_at = None
    allowed = [source_url, *links]
    requested_url = str(raw.get("action_url") or "").strip()
    default_url = source_url or (links[0] if links else "")
    action_url = next((value for value in allowed if value and value == requested_url), default_url)
    summary = " ".join(str(raw.get("summary") or f"按时处理：{title}").split())[:500]
    confidence = str(raw.get("confidence") or "low").strip().casefold()
    if confidence not in {"high", "medium", "low"}:
        confidence = "low"
    if not start_at and not deadline_at:
        confidence = "low"
    return ReminderPreview(
        title=title, type=item_type, start_at=start_at, deadline_at=deadline_at,
        action_url=action_url, summary=summary, confidence=confidence, source_url=source_url,
    )


async def extract_reminder_from_url(
    url: str,
    *,
    chat: Callable[[str, str], Awaitable[str]] | None = None,
) -> ReminderPreview:
    page_title, text, links, final_url = await read_public_document(url)
    body = text if len(text) <= 18_000 else f"{text[:14_000]}\n…\n{text[-4_000:]}"
    link_text = "\n".join(links[:50])
    user = (
        f"当前北京时间：{datetime.now(CHINA_TZ).isoformat()}\n"
        f"原公告网址：{final_url}\n页面标题：{page_title}\n"
        f"页面内链接：\n{link_text}\n公告正文：\n{body}"
    )
    extractor = DeepSeekMailboxExtractor(chat=chat)
    try:
        response = await extractor._chat(SYSTEM_PROMPT, user)
        raw = _json_object(response)
    except (RuntimeError, ValueError, json.JSONDecodeError):
        # Deterministic date parsing still provides an editable preview when
        # the model is temporarily unavailable; nothing is saved implicitly.
        raw = {}
    return normalize_preview(
        raw, page_title=page_title, text=text,
        links=links, source_url=final_url,
    )


async def extract_reminder_from_text(
    text: str,
    *,
    chat: Callable[[str, str], Awaitable[str]] | None = None,
) -> ReminderPreview:
    plain = text.strip()
    if len(plain) < 5:
        raise ReminderUrlError("请粘贴完整的邀请或通知文字")
    if len(plain) > 20_000:
        raise ReminderUrlError("粘贴文字不能超过20000字")
    links = list(dict.fromkeys(
        value.rstrip(".,;:!?，。；）)]}") for value in URL_RE.findall(plain)
    ))[:100]
    page_title = next((line.strip() for line in plain.splitlines() if line.strip()), "事项提醒")[:200]
    user = (
        f"当前北京时间：{datetime.now(CHINA_TZ).isoformat()}\n"
        "以下是用户直接粘贴的邀请或通知文字。请提取需要提醒的时间；没有年份时，"
        "结合当前日期选择最近的合理未来日期。\n"
        f"正文：\n{plain}"
    )
    extractor = DeepSeekMailboxExtractor(chat=chat)
    try:
        response = await extractor._chat(SYSTEM_PROMPT, user)
        raw = _json_object(response)
    except (RuntimeError, ValueError, json.JSONDecodeError):
        raw = {}
    if not raw.get("type"):
        raw = {**raw, "type": _fallback_type(plain)}
    return normalize_preview(
        raw, page_title=page_title, text=plain, links=links,
        source_url="", now=datetime.now(CHINA_TZ),
    )


__all__ = [
    "ReminderPreview", "ReminderUrlError", "extract_reminder_from_text",
    "extract_reminder_from_url",
    "normalize_preview", "read_public_document",
]
