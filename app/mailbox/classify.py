from __future__ import annotations

import json

from .extract import DeepSeekMailboxExtractor, _json_object
from .imap_client import MailMessage


CATEGORIES = {"面试笔试类", "工作相关", "广告推广", "其他"}
CLASSIFY_PROMPT = """你负责给私人收件箱邮件做粗分类。只输出 JSON 对象 {\"category\": \"分类\"}。
分类只能是：面试笔试类、工作相关、广告推广、其他。
招聘流程中的面试、笔试、测评、offer 归面试笔试类；工作通知和职业事务归工作相关；营销促销归广告推广。无法确认归其他。"""
BATCH_PROMPT = """你负责给私人收件箱邮件做粗分类。只输出 JSON 对象，格式为
{\"items\":[{\"id\":\"输入ID\",\"category\":\"分类\"}]}。
每个输入 ID 必须恰好返回一次。分类只能是：面试笔试类、工作相关、广告推广、其他。
招聘流程中的面试、笔试、测评、offer 归面试笔试类；工作通知和职业事务归工作相关；营销促销归广告推广；无法确认归其他。"""


class MailClassifier:
    def __init__(self, chat=None) -> None:
        self.client = DeepSeekMailboxExtractor(chat=chat)

    async def classify(self, mail: MailMessage, *, deadline_outcome: str | None = None) -> str | None:
        if deadline_outcome == "deadline":
            return "面试笔试类"
        body = mail.body[:6000]
        try:
            response = await self.client._chat(
                CLASSIFY_PROMPT,
                f"主题：{mail.subject}\n发件人：{mail.sender}\n正文：\n{body}",
            )
            category = str(_json_object(response).get("category") or "").strip()
            return category if category in CATEGORIES else None
        except Exception:
            return None

    async def classify_many(
        self, entries: list[tuple[str, MailMessage, str | None]], *, batch_size: int = 12,
    ) -> dict[str, str | None]:
        results: dict[str, str | None] = {}
        pending: list[tuple[str, MailMessage]] = []
        for identifier, mail, outcome in entries:
            if outcome == "deadline":
                results[identifier] = "面试笔试类"
            else:
                pending.append((identifier, mail))
        for start in range(0, len(pending), max(1, batch_size)):
            batch = pending[start:start + max(1, batch_size)]
            payload = [{
                "id": identifier, "subject": mail.subject,
                "sender": mail.sender, "body": mail.body[:1800],
            } for identifier, mail in batch]
            try:
                response = await self.client._chat(BATCH_PROMPT, json.dumps(payload, ensure_ascii=False))
                raw = _json_object(response).get("items")
                mapped = {
                    str(item.get("id")): str(item.get("category"))
                    for item in raw if isinstance(item, dict)
                } if isinstance(raw, list) else {}
                for identifier, _ in batch:
                    category = mapped.get(identifier)
                    results[identifier] = category if category in CATEGORIES else None
            except Exception:
                for identifier, _ in batch:
                    results[identifier] = None
        return results
