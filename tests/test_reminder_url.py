from __future__ import annotations

import socket

import pytest

from app.mailbox.reminder_url import (
    ReminderUrlError,
    _html_document,
    _public_http_url,
    normalize_preview,
)


def test_notice_text_fallback_extracts_registration_window_without_guessing() -> None:
    text = (
        "天津师范大学公开招聘公告\n"
        "报名时间：2026年10月8日9:00至2026年10月14日14:00。\n"
        "报考人员登录天津市人才服务中心网进行报名。"
    )
    preview = normalize_preview(
        {}, page_title="天津师范大学公开招聘公告", text=text,
        links=["https://www.tjtalents.com.cn/"],
        source_url="https://www.tjnu.edu.cn/notice",
    )
    assert preview.start_at == "2026-10-08T09:00:00+08:00"
    assert preview.deadline_at == "2026-10-14T14:00:00+08:00"
    assert preview.action_url == "https://www.tjnu.edu.cn/notice"
    assert preview.confidence == "low"


def test_html_reader_keeps_real_links_and_drops_script_text() -> None:
    title, text, links = _html_document(
        b"<html><head><title>Test notice</title><script>private()</script></head>"
        b"<body><h1>Recruitment</h1><p>Registration starts soon.</p>"
        b"<a href='/apply'>Apply</a></body></html>",
        "utf-8", "https://example.test/notice",
    )
    assert title == "Recruitment"
    assert "private" not in text
    assert links == ["https://example.test/apply"]


def test_url_reader_rejects_private_network_targets(monkeypatch) -> None:
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *_args, **_kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))],
    )
    with pytest.raises(ReminderUrlError, match="内网"):
        _public_http_url("http://example.test/internal")
