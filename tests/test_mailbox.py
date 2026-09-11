from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

import pytest

from app.mailbox.__main__ import run_once, send_due_notifications
from app.mailbox.extract import DeepSeekMailboxExtractor, normalize_extraction
from app.mailbox.imap_client import MailMessage, _message_body, matches_mail_keywords
from app.mailbox.store import MailboxStore


def mail(
    uid: int,
    message_id: str,
    subject: str,
    *,
    received_at: datetime | None = None,
) -> MailMessage:
    return MailMessage(
        uid=uid,
        message_id=message_id,
        subject=subject,
        sender="校园招聘 <jobs@example.test>",
        received_at=received_at or datetime(2026, 9, 10, 1, 30, tzinfo=UTC),
        body="请在规定时间内完成，操作地址 https://u.hrtps.test/r/private-token",
    )


def test_keyword_prefilter_uses_only_subject_and_sender() -> None:
    assert matches_mail_keywords("三棵树 AI视频面试邀请", "招聘中心")
    assert matches_mail_keywords("普通通知", "校招服务 <jobs@example.test>")
    assert not matches_mail_keywords("普通通知", "系统消息 <notice@example.test>")


def test_html_mail_keeps_private_anchor_target_for_extraction() -> None:
    message = EmailMessage()
    message.add_alternative(
        '<html><body><p>请完成测评</p><a href="https://u.hrtps.test/r/private-token">立即参加</a></body></html>',
        subtype="html",
    )
    body = _message_body(message)
    assert "请完成测评" in body
    assert "https://u.hrtps.test/r/private-token" in body


def test_relative_absolute_and_unknown_deadlines_are_normalized() -> None:
    sample = mail(1, "<relative@example>", "三棵树 AI视频面试")
    relative = normalize_extraction({
        "is_actionable": True,
        "company": "三棵树",
        "type": "AI视频面试",
        "deadline_kind": "relative_hours",
        "deadline_hours": 72,
        "deadline_absolute": None,
        "action_url": "https://u.hrtps.test/r/private-token",
        "summary": "收到邮件后72小时内完成AI视频面试 https://u.hrtps.test/r/private-token",
    }, sample)
    assert relative.deadline_at == datetime(2026, 9, 13, 1, 30, tzinfo=UTC)
    assert relative.needs_manual_review is False
    assert "private-token" not in relative.summary

    absolute = normalize_extraction({
        "is_actionable": True,
        "company": "信锐网科",
        "type": "笔试",
        "deadline_kind": "absolute",
        "deadline_absolute": "2026-09-12T20:00:00+08:00",
        "summary": "参加线上笔试",
    }, sample)
    assert absolute.deadline_at == datetime(2026, 9, 12, 12, 0, tzinfo=UTC)

    unknown = normalize_extraction({
        "is_actionable": True,
        "company": "信锐网科",
        "type": "其他",
        "deadline_kind": "unknown",
        "summary": "邮件未说明明确截止时间",
    }, sample)
    assert unknown.deadline_at is None
    assert unknown.needs_manual_review is True


@pytest.mark.asyncio
async def test_extractor_requires_json_but_accepts_fenced_response() -> None:
    async def chat(_system: str, _user: str) -> str:
        return """```json
        {"is_actionable": true, "company": "三棵树", "type": "AI视频面试",
         "deadline_kind": "relative_hours", "deadline_hours": 72,
         "deadline_absolute": null, "action_url": null, "summary": "完成视频面试"}
        ```"""

    result = await DeepSeekMailboxExtractor(chat=chat).extract(
        mail(1, "<json@example>", "三棵树面试")
    )
    assert result.company == "三棵树"
    assert result.deadline_at == datetime(2026, 9, 13, 1, 30, tzinfo=UTC)


class FakeClient:
    def __init__(self, messages: list[MailMessage], validity: str = "123") -> None:
        self.messages = messages
        self.validity = validity
        self.calls: list[tuple[int | None, str | None, int]] = []

    def fetch_messages(self, *, last_uid, known_uid_validity, initial_days):
        self.calls.append((last_uid, known_uid_validity, initial_days))
        high_water = max((message.uid for message in self.messages), default=last_uid or 0)
        return self.validity, self.messages, high_water


class FakeExtractor:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def extract(self, message: MailMessage):
        self.calls.append(message.message_id)
        if "无法解析" in message.subject:
            raise ValueError("do not log private model output")
        return normalize_extraction({
            "is_actionable": True,
            "company": "三棵树",
            "type": "AI视频面试",
            "deadline_kind": "relative_hours",
            "deadline_hours": 72,
            "action_url": "https://u.hrtps.test/r/private-token",
            "summary": "完成AI视频面试",
        }, message)


@pytest.mark.asyncio
async def test_run_is_incremental_deduplicated_and_preserves_failed_extraction(tmp_path) -> None:
    store = MailboxStore(tmp_path / "mail.db")
    extractor = FakeExtractor()
    notifications: list[tuple[str, str]] = []

    async def notify(title: str, body: str) -> bool:
        notifications.append((title, body))
        assert "private-token" not in body
        return True

    messages = [
        mail(1, "<tree@example>", "三棵树 AI视频面试"),
        mail(2, "<review@example>", "无法解析的笔试通知"),
        mail(3, "<noise@example>", "会员积分到账"),
    ]
    stats = await run_once(
        store=store, client=FakeClient(messages), extractor=extractor,
        notifier=notify, now=datetime(2026, 9, 10, 2, 0, tzinfo=UTC),
    )
    assert stats["fetched"] == 3
    assert stats["prefiltered"] == 2
    assert stats["new_deadlines"] == 2
    assert stats["needs_review"] == 1
    assert extractor.calls == ["<tree@example>", "<review@example>"]
    rows = store.list_deadlines()
    assert {row["status"] for row in rows} == {"pending", "needs_review"}
    assert next(row for row in rows if row["message_id"] == "<tree@example>")["deadline_at"] == (
        datetime(2026, 9, 13, 1, 30, tzinfo=UTC).isoformat()
    )

    duplicate = mail(4, "<tree@example>", "三棵树 AI视频面试")
    second = await run_once(
        store=store, client=FakeClient([duplicate]), extractor=extractor,
        notifier=notify, now=datetime(2026, 9, 10, 3, 0, tzinfo=UTC),
    )
    assert second["deduplicated"] == 1
    assert extractor.calls == ["<tree@example>", "<review@example>"]


@pytest.mark.asyncio
async def test_uid_validity_change_does_not_skip_lower_new_uids(tmp_path) -> None:
    store = MailboxStore(tmp_path / "mail.db")
    store.initialize()
    store.set_state("uid_validity", "old")
    store.set_state("last_uid", "100")
    extractor = FakeExtractor()

    async def notify(_title: str, _body: str) -> bool:
        return True

    stats = await run_once(
        store=store,
        client=FakeClient([mail(1, "<new-validity@example>", "三棵树 AI视频面试")], "new"),
        extractor=extractor,
        notifier=notify,
        now=datetime(2026, 9, 10, 2, 0, tzinfo=UTC),
    )
    assert stats["new_deadlines"] == 1
    assert extractor.calls == ["<new-validity@example>"]
    assert store.get_state("last_uid") == "1"


@pytest.mark.asyncio
async def test_done_item_is_hidden_and_gets_no_due_notifications(tmp_path) -> None:
    store = MailboxStore(tmp_path / "mail.db")
    store.initialize()
    now = datetime(2026, 9, 10, 0, 0, tzinfo=UTC)
    store.add_deadline({
        "message_id": "<done@example>", "company": "信锐网科", "type": "笔试",
        "deadline_at": (now + timedelta(hours=5)).isoformat(),
        "action_url": None, "summary": "完成线上笔试", "received_at": now.isoformat(),
        "status": "pending", "subject": "笔试通知", "sender": "招聘中心",
        "deadline_kind": "absolute",
    }, imap_uid=9)
    assert store.set_status("<done@example>", "done")
    assert store.list_deadlines() == []
    sent: list[str] = []

    async def notify(title: str, _body: str) -> bool:
        sent.append(title)
        return True

    assert await send_due_notifications(store, now=now, notifier=notify) == 0
    assert sent == []
    assert store.list_deadlines(include_history=True)[0]["status"] == "done"


@pytest.mark.asyncio
async def test_due_notifications_fire_once_per_crossed_threshold_without_burst(tmp_path) -> None:
    store = MailboxStore(tmp_path / "mail.db")
    store.initialize()
    deadline = datetime(2026, 9, 11, 0, 0, tzinfo=UTC)
    store.add_deadline({
        "message_id": "<threshold@example>", "company": "三棵树", "type": "笔试",
        "deadline_at": deadline.isoformat(), "action_url": None,
        "summary": "完成线上笔试", "received_at": "2026-09-09T00:00:00+00:00",
        "status": "pending", "subject": "笔试通知", "sender": "招聘中心",
        "deadline_kind": "absolute",
    }, imap_uid=10)
    sent: list[str] = []

    async def notify(title: str, _body: str) -> bool:
        sent.append(title)
        return True

    assert await send_due_notifications(
        store, now=deadline - timedelta(hours=23), notifier=notify,
    ) == 1
    assert await send_due_notifications(
        store, now=deadline - timedelta(hours=5), notifier=notify,
    ) == 1
    assert await send_due_notifications(
        store, now=deadline - timedelta(minutes=30), notifier=notify,
    ) == 1
    assert await send_due_notifications(
        store, now=deadline - timedelta(minutes=20), notifier=notify,
    ) == 0
    assert sent == ["截止倒计时 · 24h", "截止倒计时 · 6h", "截止倒计时 · 1h"]


def test_mailbox_cron_is_independent_from_public_refresh() -> None:
    cron = (Path(__file__).parents[1] / "deploy/server/hot-gap-mailbox.cron").read_text(
        encoding="utf-8"
    )
    assert "*/20 * * * * root" in cron
    assert "-m app.mailbox" in cron
    assert "hot-gap-mailbox.lock" in cron
    schedule = next(line for line in cron.splitlines() if line.startswith("*/20"))
    assert "hot-gap-feishu-refresh" not in schedule
    assert "sync_feishu" not in schedule


def test_mailbox_installer_checks_service_user_import_and_stable_startup() -> None:
    installer = (
        Path(__file__).parents[1] / "deploy/server/install-hot-gap-mailbox.sh"
    ).read_text(encoding="utf-8")

    assert 'chmod 755 "$project/app/mailbox"' in installer
    assert "runuser -u www-data -- .venv/bin/python -c 'import sync.app'" in installer
    assert "service_ready=0" in installer
    assert "ActiveState --value" in installer
    assert "SubState --value" in installer
    assert "did not become stably active" in installer
