from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Awaitable, Callable
from urllib.parse import urlsplit

import httpx
from dotenv import load_dotenv

from .extract import DeepSeekMailboxExtractor, ExtractionResult
from .imap_client import ImapMailboxClient, MailMessage, matches_mail_keywords
from .store import MailboxStore


LOGGER = logging.getLogger("hot_gap.mailbox")
THRESHOLDS = ((24, "24h"), (6, "6h"), (1, "1h"))
UTC = timezone.utc


def _drop_privileges() -> None:
    username = os.getenv("MAILBOX_RUN_AS_USER", "").strip()
    if not username or not hasattr(os, "geteuid") or os.geteuid() != 0:
        return
    import pwd

    account = pwd.getpwnam(username)
    os.initgroups(username, account.pw_gid)
    os.setgid(account.pw_gid)
    os.setuid(account.pw_uid)


def _domain(url: str | None) -> str:
    try:
        return urlsplit(url or "").hostname or ""
    except ValueError:
        return ""


async def notify_bark(title: str, message: str) -> bool:
    bark_url = os.getenv("BARK_URL", "").strip().rstrip("/")
    if not bark_url:
        LOGGER.warning("Bark notification skipped: BARK_URL is missing")
        return False
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        for attempt in range(3):
            try:
                response = await client.post(
                    bark_url,
                    json={"title": title, "body": message, "group": "hot-gap-mailbox"},
                )
                response.raise_for_status()
                return True
            except httpx.HTTPError:
                if attempt < 2:
                    await asyncio.sleep(0.5 * (2**attempt))
    LOGGER.warning("Bark notification failed after retries")
    return False


def _review_result(mail: MailMessage) -> ExtractionResult:
    return ExtractionResult(
        is_actionable=True,
        company="待确认",
        type="其他",
        deadline_kind="unknown",
        deadline_hours=None,
        deadline_absolute=None,
        action_url=None,
        summary=f"自动抽取失败，请查看原邮件：{mail.subject}"[:500],
        deadline_at=None,
        needs_manual_review=True,
    )


def _deadline_record(mail: MailMessage, result: ExtractionResult, now: datetime) -> dict[str, object]:
    status = "needs_review" if result.needs_manual_review else "pending"
    if result.deadline_at is not None and result.deadline_at <= now:
        status = "expired"
    return {
        "message_id": mail.message_id,
        "company": result.company,
        "type": result.type,
        "deadline_at": result.deadline_at.astimezone(UTC).isoformat() if result.deadline_at else None,
        "action_url": result.action_url,
        "summary": result.summary,
        "received_at": mail.received_at.astimezone(UTC).isoformat(),
        "status": status,
        "subject": mail.subject[:500],
        "sender": mail.sender[:500],
        "deadline_kind": result.deadline_kind,
    }


async def _notify_new(
    store: MailboxStore,
    record: dict[str, object],
    notifier: Callable[[str, str], Awaitable[bool]],
) -> bool:
    message_id = str(record["message_id"])
    if store.notification_sent(message_id, "new"):
        return False
    deadline = str(record.get("deadline_at") or "待人工确认")
    domain = _domain(str(record.get("action_url") or "")) or "无链接"
    body = (
        f"{record['company']} · {record['type']}\n"
        f"截止：{deadline}\n"
        f"操作域名：{domain}\n"
        f"{record['summary']}\n请登录站点查看专属链接，勿转发。"
    )
    if await notifier("新面试/笔试提醒", body):
        store.record_notification(message_id, "new")
        return True
    return False


async def send_due_notifications(
    store: MailboxStore,
    *,
    now: datetime,
    notifier: Callable[[str, str], Awaitable[bool]] = notify_bark,
) -> int:
    sent = 0
    store.expire_due(now)
    for record in store.pending_with_deadlines():
        deadline = datetime.fromisoformat(str(record["deadline_at"])).astimezone(UTC)
        remaining_hours = (deadline - now.astimezone(UTC)).total_seconds() / 3600
        if remaining_hours <= 0:
            continue
        eligible = [
            (hours, label) for hours, label in THRESHOLDS
            if remaining_hours <= hours
            and not store.notification_sent(str(record["message_id"]), label)
        ]
        if not eligible:
            continue
        # If an old email is first discovered inside several thresholds, send
        # only the most urgent reminder and mark the already-missed wider
        # thresholds so a single cron run cannot flood the phone.
        hours, label = eligible[-1]
        for _, missed_label in eligible[:-1]:
            store.record_notification(str(record["message_id"]), missed_label)
        if remaining_hours <= hours:
            body = (
                f"{record['company']} · {record['type']}\n"
                f"剩余约 {remaining_hours:.1f} 小时\n{record['summary']}\n"
                "请登录站点操作，专属链接勿转发。"
            )
            if await notifier(f"截止倒计时 · {label}", body):
                store.record_notification(str(record["message_id"]), label)
                sent += 1
    return sent


async def run_once(
    *,
    store: MailboxStore | None = None,
    client: ImapMailboxClient | None = None,
    extractor: DeepSeekMailboxExtractor | None = None,
    notifier: Callable[[str, str], Awaitable[bool]] = notify_bark,
    now: datetime | None = None,
    rescan_prefilter_misses: bool = False,
) -> dict[str, int]:
    current = (now or datetime.now(UTC)).astimezone(UTC)
    store = store or MailboxStore()
    store.initialize()
    client = client or ImapMailboxClient()
    extractor = extractor or DeepSeekMailboxExtractor()
    last_uid_raw = store.get_state("last_uid")
    last_uid = int(last_uid_raw) if last_uid_raw and last_uid_raw.isdigit() else None
    known_validity = store.get_state("uid_validity")
    uid_validity, messages, high_water_uid = client.fetch_messages(
        last_uid=None if rescan_prefilter_misses else last_uid,
        known_uid_validity=known_validity,
        initial_days=30,
    )
    stats = {
        "fetched": len(messages), "prefiltered": 0, "new_deadlines": 0,
        "needs_review": 0, "non_actionable": 0, "deduplicated": 0,
        "new_notifications": 0, "due_notifications": 0,
        "reprocessed_prefilter_misses": 0,
    }
    if known_validity and known_validity != uid_validity:
        last_uid = None
    highest_uid = last_uid or 0
    for mail in messages:
        keyword_match = matches_mail_keywords(mail.subject, mail.sender)
        previous_outcome = store.processed_outcome(mail.message_id)
        reconsidering = (
            rescan_prefilter_misses
            and previous_outcome == "prefilter_miss"
            and keyword_match
        )
        if mail.uid <= highest_uid and not reconsidering:
            continue
        if previous_outcome is not None and not reconsidering:
            stats["deduplicated"] += 1
            highest_uid = max(highest_uid, mail.uid)
            store.set_state("last_uid", str(highest_uid))
            continue
        if not keyword_match:
            store.mark_processed(mail.message_id, mail.uid, "prefilter_miss")
            highest_uid = max(highest_uid, mail.uid)
            store.set_state("last_uid", str(highest_uid))
            continue
        if reconsidering:
            stats["reprocessed_prefilter_misses"] += 1
        stats["prefiltered"] += 1
        try:
            result = await extractor.extract(mail)
        except Exception as exc:  # body/model response must never enter logs
            LOGGER.warning("mail extraction degraded for UID %d: %s", mail.uid, type(exc).__name__)
            result = _review_result(mail)
        if not result.is_actionable:
            store.mark_processed(mail.message_id, mail.uid, "non_actionable")
            stats["non_actionable"] += 1
        else:
            record = _deadline_record(mail, result, current)
            if store.add_deadline(record, imap_uid=mail.uid):
                stats["new_deadlines"] += 1
                if record["status"] == "needs_review":
                    stats["needs_review"] += 1
                if record["status"] != "expired" and await _notify_new(store, record, notifier):
                    stats["new_notifications"] += 1
        highest_uid = max(highest_uid, mail.uid)
        store.set_state("last_uid", str(highest_uid))
    store.set_state("uid_validity", uid_validity)
    store.set_state("last_uid", str(max(highest_uid, high_water_uid)))
    stats["due_notifications"] = await send_due_notifications(
        store, now=current, notifier=notifier,
    )
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pull private mailbox recruitment deadlines")
    parser.add_argument("--no-drop-privileges", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--report", action="store_true",
        help="print a private masked deadline report without reading IMAP",
    )
    parser.add_argument(
        "--rescan-prefilter-misses",
        action="store_true",
        help="reconsider recent messages previously rejected by the metadata prefilter",
    )
    arguments = parser.parse_args(argv)
    load_dotenv()
    os.umask(0o077)
    if not arguments.no_drop_privileges:
        _drop_privileges()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    if arguments.report:
        store = MailboxStore()
        store.initialize()
        rows = [{
            "company": row["company"],
            "type": row["type"],
            "deadline_at": row["deadline_at"],
            "received_at": row["received_at"],
            "status": row["status"],
            "action_domain": _domain(row.get("action_url")),
            "summary": row["summary"],
        } for row in store.list_deadlines(include_history=True)]
        print(json.dumps({"count": len(rows), "items": rows}, ensure_ascii=False, indent=2))
        return 0
    try:
        stats = asyncio.run(
            run_once(rescan_prefilter_misses=arguments.rescan_prefilter_misses)
        )
    except Exception as exc:
        LOGGER.error("mailbox reminder run failed: %s", type(exc).__name__)
        try:
            asyncio.run(notify_bark("邮箱提醒任务失败", f"S1 邮箱提醒任务异常：{type(exc).__name__}"))
        except Exception:
            LOGGER.error("mailbox failure notification also failed")
        return 1
    print(json.dumps({"event": "mailbox_deadlines", **stats}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
