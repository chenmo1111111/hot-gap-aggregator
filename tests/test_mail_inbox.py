from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import pytest
from cryptography.fernet import Fernet

from app.mailbox.accounts import load_mail_accounts, public_account
from app.mailbox.classify import MailClassifier
from app.mailbox.gmail_client import GMAIL_SCOPE, _gmail_message
from app.mailbox.imap_client import ImapMailboxClient, MailAttachment, MailMessage
from app.mailbox.inbox_store import InboxStore, TokenCipher


UTC = timezone.utc


def test_loads_six_private_accounts_without_exposing_credentials(monkeypatch) -> None:
    for number in range(1, 7):
        provider = "imap" if number <= 3 else "gmail"
        monkeypatch.setenv(f"MAIL_ACCOUNT_{number}_TYPE", provider)
        monkeypatch.setenv(f"MAIL_ACCOUNT_{number}_LABEL", f"账号 {number}")
        monkeypatch.setenv(f"MAIL_ACCOUNT_{number}_USER", f"person{number}@example.test")
        if provider == "imap":
            monkeypatch.setenv(f"MAIL_ACCOUNT_{number}_HOST", "imap.example.test")
            monkeypatch.setenv(f"MAIL_ACCOUNT_{number}_AUTH_CODE", f"secret-{number}")
    accounts = load_mail_accounts()
    assert len(accounts) == 6
    public = public_account(accounts[0], authorized=True, last_synced_at=None, last_error=None)
    assert public["address_hint"] == "pe***@example.test"
    assert "auth_code" not in public


def test_refresh_token_is_encrypted_in_separate_private_database(tmp_path) -> None:
    store = InboxStore(tmp_path / "inbox.db")
    store.initialize()
    store.upsert_account("mail-4", "gmail", "Gmail 1", "person@gmail.test")
    key = Fernet.generate_key().decode("ascii")
    cipher = TokenCipher(key)
    encrypted = cipher.encrypt("refresh-token-private")
    store.save_refresh_token("mail-4", encrypted)
    state = store.account_state("mail-4")
    assert state["refresh_token_encrypted"] != "refresh-token-private"
    assert cipher.decrypt(state["refresh_token_encrypted"]) == "refresh-token-private"
    assert b"refresh-token-private" not in (tmp_path / "inbox.db").read_bytes()


def test_store_filters_searches_reads_body_and_prunes_after_90_days(tmp_path) -> None:
    store = InboxStore(tmp_path / "inbox.db")
    store.initialize()
    store.upsert_account("mail-1", "imap", "QQ主邮箱", "person@qq.test")
    now = datetime(2026, 9, 14, tzinfo=UTC)
    common = {
        "account_id": "mail-1", "provider_uid": "1", "sender": "招聘中心 <job@example.test>",
        "category": "面试笔试类", "deadline_linked": True,
        "attachments": [{"filename": "安排.pdf", "size": 2048, "content_type": "application/pdf"}],
    }
    assert store.add_message({**common, "message_key": "new", "message_id": "<new>",
        "subject": "视频面试安排", "body": "请在周五前完成视频面试", "received_at": now.isoformat()})
    assert store.add_message({**common, "message_key": "old", "message_id": "<old>",
        "subject": "历史邮件", "body": "旧正文", "received_at": (now - timedelta(days=91)).isoformat()})
    items, total = store.list_messages(category="面试笔试类", query="周五")
    assert total == 1 and items[0]["subject"] == "视频面试安排"
    detail = store.get_message("mail-1", "new")
    assert detail and detail["body"] == "请在周五前完成视频面试"
    assert detail["attachments"][0]["filename"] == "安排.pdf"
    assert store.prune(90, now=now) == 1


@pytest.mark.asyncio
async def test_deadline_result_reuses_classification_without_calling_deepseek() -> None:
    called = False

    async def chat(_system: str, _user: str) -> str:
        nonlocal called
        called = True
        return '{"category":"其他"}'

    classifier = MailClassifier(chat=chat)
    mail = MailMessage(1, "<one>", "笔试", "招聘中心", datetime.now(UTC), "正文")
    assert await classifier.classify(mail, deadline_outcome="deadline") == "面试笔试类"
    assert called is False


@pytest.mark.asyncio
async def test_batch_classification_reuses_deadline_and_batches_other_messages() -> None:
    calls = 0

    async def chat(_system: str, _user: str) -> str:
        nonlocal calls
        calls += 1
        return '{"items":[{"id":"b","category":"工作相关"},{"id":"c","category":"广告推广"}]}'

    classifier = MailClassifier(chat=chat)
    sample = MailMessage(1, "<one>", "主题", "发件人", datetime.now(UTC), "正文")
    result = await classifier.classify_many([
        ("a", sample, "deadline"), ("b", sample, None), ("c", sample, None),
    ])
    assert result == {"a": "面试笔试类", "b": "工作相关", "c": "广告推广"}
    assert calls == 1


def test_inbox_cron_is_independent_and_does_not_modify_deadline_or_refresh_jobs() -> None:
    root = __import__("pathlib").Path(__file__).parents[1]
    inbox_cron = (root / "deploy/server/hot-gap-mail-inbox.cron").read_text(encoding="utf-8")
    deadline_cron = (root / "deploy/server/hot-gap-mailbox.cron").read_text(encoding="utf-8")
    assert "17 * * * *" in inbox_cron
    assert "app.mailbox.inbox_sync" in inbox_cron
    assert "app.mailbox.inbox_sync" not in deadline_cron
    assert "/usr/local/sbin/hot-gap-feishu-refresh" not in inbox_cron
    installer = (root / "deploy/server/install-hot-gap-mail-inbox.sh").read_text(encoding="utf-8")
    assert "No inbox sync, collector, refresh, or container was started" in installer
    assert "systemctl restart hot-gap-sync.service" in installer
    assert "hot-gap-mailbox.cron" not in installer


def test_gmail_parser_keeps_body_and_attachment_metadata_without_attachment_content() -> None:
    encoded = "5L2g5aW977yM5LiL5ZGo6Z2i6K-V"  # 你好，下周面试
    parsed = _gmail_message({
        "id": "gmail-id", "internalDate": "1789315200000",
        "payload": {"mimeType": "multipart/mixed", "headers": [
            {"name": "Subject", "value": "面试安排"},
            {"name": "From", "value": "Recruiter <jobs@example.test>"},
        ], "parts": [
            {"mimeType": "text/plain", "body": {"data": encoded}},
            {"mimeType": "application/pdf", "filename": "interview.pdf", "body": {"attachmentId": "private", "size": 12345}},
        ]},
    })
    assert "下周面试" in parsed.body
    assert parsed.attachments == (MailAttachment("interview.pdf", 12345, "application/pdf"),)
    assert "private" not in parsed.body
    assert GMAIL_SCOPE.endswith("gmail.readonly")


def test_imap_sync_selects_readonly_and_uses_peek_without_store(monkeypatch) -> None:
    source = EmailMessage()
    source["Subject"] = "只读测试"
    source["From"] = "Sender <sender@example.test>"
    source["Message-ID"] = "<readonly@example.test>"
    source.set_content("正文")

    class FakeImap:
        instance = None

        def __init__(self, *_args, **_kwargs):
            self.calls = []
            FakeImap.instance = self

        def login(self, *_args):
            return "OK", []

        def select(self, mailbox, readonly=False):
            self.calls.append(("select", mailbox, readonly))
            return "OK", [b"1"]

        def response(self, _name):
            return "UIDVALIDITY", [b"123"]

        def uid(self, command, *args):
            self.calls.append(("uid", command, *args))
            if command == "search":
                return "OK", [b"1"]
            return "OK", [(b'1 (UID 1 INTERNALDATE "14-Sep-2026 09:00:00 +0000")', source.as_bytes())]

        def logout(self):
            return "BYE", []

    monkeypatch.setattr("app.mailbox.imap_client.imaplib.IMAP4_SSL", FakeImap)
    _, messages, _ = ImapMailboxClient("imap.example.test", 993, "user", "code").fetch_messages(
        last_uid=None, known_uid_validity=None, initial_days=90,
    )
    assert messages[0].message_id == "<readonly@example.test>"
    assert ("select", "INBOX", True) in FakeImap.instance.calls
    commands = [call for call in FakeImap.instance.calls if call[0] == "uid"]
    assert any(call[1:] == ("fetch", "1", "(BODY.PEEK[] INTERNALDATE)") for call in commands)
    assert all(call[1].lower() != "store" for call in commands)
