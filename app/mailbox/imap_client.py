from __future__ import annotations

import imaplib
import os
import re
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email import policy
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from html import unescape

from selectolax.parser import HTMLParser


MAIL_KEYWORDS = re.compile(
    r"面试|笔试|测评|AI\s*视频面试|一站式面试|校招|网申|offer",
    re.IGNORECASE,
)
INTERNALDATE_RE = re.compile(rb'INTERNALDATE "([^"]+)"')


@dataclass(frozen=True)
class MailMessage:
    uid: int
    message_id: str
    subject: str
    sender: str
    received_at: datetime
    body: str


def _decoded_header(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value))).strip()
    except (LookupError, UnicodeError):
        return value.strip()


def _message_body(message: EmailMessage) -> str:
    plain: list[str] = []
    html: list[str] = []
    parts = message.walk() if message.is_multipart() else [message]
    for part in parts:
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        content_type = part.get_content_type()
        if content_type not in {"text/plain", "text/html"}:
            continue
        try:
            content = part.get_content()
        except (LookupError, UnicodeError):
            payload = part.get_payload(decode=True) or b""
            content = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        text = str(content)
        if content_type == "text/plain":
            plain.append(text)
        else:
            html.append(text)
    if plain:
        text = "\n".join(plain)
    elif html:
        tree = HTMLParser("\n".join(html))
        text = "\n".join(tree.text(separator="\n").splitlines())
        links = []
        for node in tree.css("a[href]"):
            href = str(node.attributes.get("href") or "").strip()
            if href.startswith(("http://", "https://")) and href not in links:
                links.append(href)
        if links:
            text = f"{text}\n邮件链接：\n" + "\n".join(links[:30])
    else:
        text = ""
    text = unescape(text).replace("\x00", "")
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def _received_at(metadata: bytes, message: EmailMessage) -> datetime:
    match = INTERNALDATE_RE.search(metadata)
    raw = match.group(1).decode("ascii", errors="ignore") if match else message.get("Date")
    try:
        parsed = parsedate_to_datetime(raw) if raw else None
    except (TypeError, ValueError, OverflowError):
        parsed = None
    if parsed is None:
        return datetime.now(UTC)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def matches_mail_keywords(subject: str, sender: str) -> bool:
    return bool(MAIL_KEYWORDS.search(f"{subject}\n{sender}"))


class ImapMailboxClient:
    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        username: str | None = None,
        auth_code: str | None = None,
        *,
        timeout: float = 30,
    ) -> None:
        self.host = (host or os.getenv("MAIL_IMAP_HOST", "")).strip()
        self.port = int(port or os.getenv("MAIL_IMAP_PORT", "993"))
        self.username = (username or os.getenv("MAIL_IMAP_USER", "")).strip()
        self.auth_code = (auth_code or os.getenv("MAIL_IMAP_AUTH_CODE", "")).strip()
        self.timeout = timeout
        if not self.host or not self.username or not self.auth_code:
            raise RuntimeError("MAIL_IMAP_HOST, MAIL_IMAP_USER and MAIL_IMAP_AUTH_CODE are required")

    def fetch_messages(
        self,
        *,
        last_uid: int | None,
        known_uid_validity: str | None,
        initial_days: int = 30,
    ) -> tuple[str, list[MailMessage], int]:
        context = ssl.create_default_context()
        connection = imaplib.IMAP4_SSL(
            self.host, self.port, ssl_context=context, timeout=self.timeout,
        )
        try:
            connection.login(self.username, self.auth_code)
            status, _ = connection.select("INBOX", readonly=True)
            if status != "OK":
                raise RuntimeError("cannot select IMAP INBOX")
            validity_response = connection.response("UIDVALIDITY")[1]
            uid_validity = (
                validity_response[0].decode("ascii", errors="ignore")
                if validity_response and validity_response[0]
                else "unknown"
            )
            if known_uid_validity and known_uid_validity != uid_validity:
                last_uid = None
            if last_uid is None:
                since = (datetime.now(UTC) - timedelta(days=initial_days)).strftime("%d-%b-%Y")
                status, data = connection.uid("search", None, "SINCE", since)
            else:
                status, data = connection.uid("search", None, f"UID {last_uid + 1}:*")
            if status != "OK":
                raise RuntimeError("IMAP UID search failed")
            raw_uids = data[0].split() if data and data[0] else []
            numeric_uids = sorted(int(raw_uid) for raw_uid in raw_uids)
            high_water_uid = max(numeric_uids, default=last_uid or 0)
            if last_uid is None and not numeric_uids:
                all_status, all_data = connection.uid("search", None, "ALL")
                if all_status == "OK" and all_data and all_data[0]:
                    high_water_uid = max(int(raw_uid) for raw_uid in all_data[0].split())
            messages: list[MailMessage] = []
            for uid in numeric_uids:
                if last_uid is not None and uid <= last_uid:
                    continue
                fetch_status, fetched = connection.uid(
                    "fetch", str(uid), "(BODY.PEEK[] INTERNALDATE)",
                )
                if fetch_status != "OK" or not fetched:
                    raise RuntimeError(f"IMAP fetch failed for UID {uid}")
                metadata = b""
                body = b""
                for entry in fetched:
                    if isinstance(entry, tuple):
                        metadata = bytes(entry[0])
                        body = bytes(entry[1])
                        break
                if not body:
                    raise RuntimeError(f"IMAP message body missing for UID {uid}")
                parsed = BytesParser(policy=policy.default).parsebytes(body)
                if not isinstance(parsed, EmailMessage):
                    parsed = EmailMessage(policy=policy.default)
                    parsed.set_content(body.decode("utf-8", errors="replace"))
                subject = _decoded_header(parsed.get("Subject"))
                sender = _decoded_header(parsed.get("From"))
                message_id = str(parsed.get("Message-ID") or "").strip()
                if not message_id:
                    message_id = f"imap:{uid_validity}:{uid}"
                messages.append(MailMessage(
                    uid=uid,
                    message_id=message_id,
                    subject=subject,
                    sender=sender,
                    received_at=_received_at(metadata, parsed),
                    body=_message_body(parsed),
                ))
            return uid_validity, messages, high_water_uid
        finally:
            try:
                connection.logout()
            except Exception:
                pass
