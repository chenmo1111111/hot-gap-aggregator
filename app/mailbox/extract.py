from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Mapping
from urllib.parse import urlsplit

import httpx

from .imap_client import MailMessage


CHINA_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")
UTC = timezone.utc
ALLOWED_TYPES = {"AI视频面试", "笔试", "一站式面试", "其他"}
ALLOWED_KINDS = {"relative_hours", "absolute", "unknown"}
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
SYSTEM_PROMPT = """你负责从招聘邮件中提取需要候选人完成的动作。只输出一个 JSON 对象，不要 Markdown。
字段必须完整：
is_actionable: boolean；
company: 公司名，无法确认则为“待确认”；
type: 只能是 AI视频面试、笔试、一站式面试、其他；
deadline_kind: 只能是 relative_hours、absolute、unknown；
deadline_hours: 相对小时数，没有则 null；
deadline_absolute: ISO 8601 时间（带时区），没有则 null；
action_url: 邮件中的专属操作链接，没有则 null；
summary: 一句话说明要做什么，不包含完整专属链接。
“收到邮件后72小时内”属于 relative_hours；明确年月日时间属于 absolute；无法可靠判断必须用 unknown，禁止猜测。"""


@dataclass(frozen=True)
class ExtractionResult:
    is_actionable: bool
    company: str
    type: str
    deadline_kind: str
    deadline_hours: float | None
    deadline_absolute: str | None
    action_url: str | None
    summary: str
    deadline_at: datetime | None
    needs_manual_review: bool


def _json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("model response did not contain a JSON object")
    value = json.loads(cleaned[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError("model response must be a JSON object")
    return value


def _absolute_deadline(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            parsed = datetime.fromisoformat(f"{text}T23:59:59")
        else:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CHINA_TZ)
    return parsed.astimezone(UTC)


def _safe_action_url(value: object, body: str) -> str | None:
    body_candidates = URL_RE.findall(body)
    preferred = str(value or "").strip().rstrip(".,;:!?，。；）)]}")
    candidates = (
        [preferred, *body_candidates]
        if preferred in body_candidates
        else body_candidates
    )
    for candidate in candidates:
        candidate = candidate.rstrip(".,;:!?，。；）)]}")
        try:
            parsed = urlsplit(candidate)
        except ValueError:
            continue
        if parsed.scheme in {"http", "https"} and parsed.hostname:
            return candidate
    return None


def normalize_extraction(
    raw: Mapping[str, Any], mail: MailMessage,
) -> ExtractionResult:
    actionable = raw.get("is_actionable") is True
    company = str(raw.get("company") or "待确认").strip()[:120] or "待确认"
    item_type = str(raw.get("type") or "其他").strip()
    if item_type not in ALLOWED_TYPES:
        item_type = "其他"
    kind = str(raw.get("deadline_kind") or "unknown").strip()
    if kind not in ALLOWED_KINDS:
        kind = "unknown"
    hours: float | None = None
    deadline: datetime | None = None
    if kind == "relative_hours":
        try:
            hours = float(raw.get("deadline_hours"))
        except (TypeError, ValueError):
            hours = None
        if hours is not None and 0 < hours <= 24 * 365:
            deadline = mail.received_at.astimezone(UTC) + timedelta(hours=hours)
        else:
            kind = "unknown"
            hours = None
    elif kind == "absolute":
        deadline = _absolute_deadline(raw.get("deadline_absolute"))
        if deadline is None:
            kind = "unknown"
    summary = " ".join(str(raw.get("summary") or "").split())[:500]
    if not summary:
        summary = f"请查看邮件：{mail.subject}"[:500]
    summary = URL_RE.sub("[专属链接]", summary)
    action_url = _safe_action_url(raw.get("action_url"), mail.body)
    needs_review = actionable and (kind == "unknown" or deadline is None)
    return ExtractionResult(
        is_actionable=actionable,
        company=company,
        type=item_type,
        deadline_kind=kind,
        deadline_hours=hours,
        deadline_absolute=deadline.isoformat() if deadline else None,
        action_url=action_url,
        summary=summary,
        deadline_at=deadline,
        needs_manual_review=needs_review,
    )


class DeepSeekMailboxExtractor:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        chat: Callable[[str, str], Awaitable[str]] | None = None,
    ) -> None:
        self.api_key = (api_key or os.getenv("DEEPSEEK_API_KEY", "")).strip()
        self.base_url = (base_url or os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")).rstrip("/")
        self.model = model or os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
        self._chat_override = chat

    async def _chat(self, system: str, user: str) -> str:
        if self._chat_override is not None:
            return await self._chat_override(system, user)
        if not self.api_key:
            raise RuntimeError("DEEPSEEK_API_KEY is required for mailbox extraction")
        body = {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        last_error: Exception | None = None
        async with httpx.AsyncClient(timeout=45) as client:
            for attempt in range(3):
                try:
                    response = await client.post(
                        f"{self.base_url}/chat/completions", headers=headers, json=body,
                    )
                    response.raise_for_status()
                    return str(response.json()["choices"][0]["message"]["content"])
                except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
                    last_error = exc
                    if attempt < 2:
                        await asyncio.sleep(0.75 * (2**attempt))
        raise RuntimeError(f"DeepSeek mailbox extraction failed: {type(last_error).__name__}")

    async def extract(self, mail: MailMessage) -> ExtractionResult:
        received = mail.received_at.astimezone(CHINA_TZ).isoformat()
        body = mail.body if len(mail.body) <= 12000 else f"{mail.body[:10000]}\n…\n{mail.body[-2000:]}"
        user = (
            f"主题：{mail.subject}\n"
            f"发件人：{mail.sender}\n"
            f"收件时间：{received}\n"
            f"正文：\n{body}"
        )
        response = await self._chat(SYSTEM_PROMPT, user)
        return normalize_extraction(_json_object(response), mail)
