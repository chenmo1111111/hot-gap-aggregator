from __future__ import annotations

import base64
import os
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from html import unescape
from typing import Any
from urllib.parse import urlencode

import httpx
from selectolax.parser import HTMLParser

from .imap_client import MailAttachment, MailMessage


UTC = timezone.utc
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"


def google_client_settings() -> tuple[str, str, str]:
    client_id = os.getenv("MAIL_GOOGLE_CLIENT_ID", "").strip()
    client_secret = os.getenv("MAIL_GOOGLE_CLIENT_SECRET", "").strip()
    redirect_uri = os.getenv(
        "MAIL_GOOGLE_REDIRECT_URI",
        "https://hot.weixincuotiben.top/api/admin/mail-inbox/oauth/google/callback",
    ).strip()
    if not client_id or not client_secret or not redirect_uri:
        raise RuntimeError("Google OAuth client settings are incomplete")
    return client_id, client_secret, redirect_uri


def authorization_url(state: str, *, login_hint: str = "") -> str:
    client_id, _, redirect_uri = google_client_settings()
    query = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": GMAIL_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    if login_hint:
        query["login_hint"] = login_hint
    return f"{GOOGLE_AUTH_URL}?{urlencode(query)}"


async def exchange_authorization_code(code: str) -> dict[str, Any]:
    client_id, client_secret, redirect_uri = google_client_settings()
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(GOOGLE_TOKEN_URL, data={
            "code": code, "client_id": client_id, "client_secret": client_secret,
            "redirect_uri": redirect_uri, "grant_type": "authorization_code",
        })
        response.raise_for_status()
        value = response.json()
    if not value.get("refresh_token"):
        raise RuntimeError("Google did not return a refresh token; revoke access and retry")
    return value


async def refresh_access_token(refresh_token: str) -> str:
    client_id, client_secret, _ = google_client_settings()
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(GOOGLE_TOKEN_URL, data={
            "refresh_token": refresh_token, "client_id": client_id,
            "client_secret": client_secret, "grant_type": "refresh_token",
        })
        response.raise_for_status()
        value = response.json()
    token = str(value.get("access_token") or "")
    if not token:
        raise RuntimeError("Google token refresh returned no access token")
    return token


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _header(headers: list[dict[str, str]], name: str) -> str:
    value = next((item.get("value", "") for item in headers if item.get("name", "").lower() == name.lower()), "")
    try:
        return str(make_header(decode_header(value))).strip()
    except (LookupError, UnicodeError):
        return value.strip()


def _payload_content(payload: dict[str, Any]) -> tuple[str, tuple[MailAttachment, ...]]:
    plain: list[str] = []
    html: list[str] = []
    attachments: list[MailAttachment] = []

    def visit(part: dict[str, Any]) -> None:
        filename = str(part.get("filename") or "").strip()
        mime = str(part.get("mimeType") or "application/octet-stream")
        body = part.get("body") or {}
        if filename:
            attachments.append(MailAttachment(filename[:500], int(body.get("size") or 0), mime[:120]))
            return
        data = body.get("data")
        if data and mime in {"text/plain", "text/html"}:
            text = _decode(str(data)).decode("utf-8", errors="replace")
            (plain if mime == "text/plain" else html).append(text)
        for child in part.get("parts") or []:
            visit(child)

    visit(payload)
    if plain:
        text = "\n".join(plain)
    elif html:
        tree = HTMLParser("\n".join(html))
        text = tree.text(separator="\n")
        links = []
        for node in tree.css("a[href]"):
            href = str(node.attributes.get("href") or "").strip()
            if href.startswith(("http://", "https://")) and href not in links:
                links.append(href)
        if links:
            text += "\n邮件链接：\n" + "\n".join(links[:30])
    else:
        text = ""
    cleaned = unescape(text).replace("\x00", "")
    return "\n".join(line.strip() for line in cleaned.splitlines() if line.strip()), tuple(attachments)


def _gmail_message(value: dict[str, Any]) -> MailMessage:
    payload = value.get("payload") or {}
    headers = payload.get("headers") or []
    received = datetime.fromtimestamp(int(value.get("internalDate") or 0) / 1000, UTC)
    if received.year < 2000:
        try:
            parsed = parsedate_to_datetime(_header(headers, "Date"))
            received = (parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed).astimezone(UTC)
        except (TypeError, ValueError, OverflowError):
            received = datetime.now(UTC)
    body, attachments = _payload_content(payload)
    gmail_id = str(value["id"])
    return MailMessage(
        uid=0, message_id=_header(headers, "Message-ID") or f"gmail:{gmail_id}",
        subject=_header(headers, "Subject"), sender=_header(headers, "From"),
        received_at=received, body=body, attachments=attachments,
    )


class GmailReadonlyClient:
    def __init__(self, refresh_token: str, *, timeout: float = 30) -> None:
        self.refresh_token = refresh_token
        self.timeout = timeout

    async def profile(self) -> dict[str, Any]:
        token = await refresh_access_token(self.refresh_token)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(
                "https://gmail.googleapis.com/gmail/v1/users/me/profile",
                headers={"Authorization": f"Bearer {token}"},
            )
            response.raise_for_status()
            return response.json()

    async def fetch_messages(self, *, after_at: datetime | None = None,
                             initial_days: int = 90) -> list[tuple[str, MailMessage]]:
        token = await refresh_access_token(self.refresh_token)
        headers = {"Authorization": f"Bearer {token}"}
        after_time = after_at or (datetime.now(UTC) - timedelta(days=initial_days))
        after = int(after_time.timestamp())
        params: dict[str, Any] = {"q": f"after:{after}", "maxResults": 500}
        ids: list[str] = []
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for _ in range(20):
                response = await client.get(
                    "https://gmail.googleapis.com/gmail/v1/users/me/messages",
                    headers=headers, params=params,
                )
                response.raise_for_status()
                value = response.json()
                ids.extend(str(item["id"]) for item in value.get("messages") or [])
                token_page = value.get("nextPageToken")
                if not token_page:
                    break
                params["pageToken"] = token_page
            result: list[tuple[str, MailMessage]] = []
            for gmail_id in ids:
                response = await client.get(
                    f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{gmail_id}",
                    headers=headers, params={"format": "full"},
                )
                response.raise_for_status()
                result.append((gmail_id, _gmail_message(response.json())))
        return result
