import logging

from app.logging_utils import redact_payload, redact_sensitive


def test_redacts_webhook_tokens_authorization_and_query_secrets(monkeypatch) -> None:
    webhook = "https://open.feishu.cn/open-apis/bot/v2/hook/super-secret-token"
    monkeypatch.setenv("FEISHU_WEBHOOK", webhook)
    value = (
        f"POST {webhook}?access_token=another-secret "
        "Authorization: Bearer bearer-secret"
    )
    redacted = redact_sensitive(value)
    assert "super-secret-token" not in redacted
    assert "another-secret" not in redacted
    assert "bearer-secret" not in redacted
    assert "/hook/***" in redacted


def test_redacts_configured_bark_url_inside_nested_request_body(monkeypatch) -> None:
    bark = "https://bark.example/very-secret-device-key"
    monkeypatch.setenv("BARK_URL", bark)
    payload = {"webhook": bark, "items": [f"callback={bark}"]}
    redacted = redact_payload(payload)
    assert redacted == {"webhook": "***", "items": ["callback=***"]}


def test_process_wide_log_record_factory_redacts_httpx_style_message(monkeypatch) -> None:
    webhook = "https://open.feishu.cn/open-apis/bot/v2/hook/do-not-log-me"
    monkeypatch.setenv("FEISHU_WEBHOOK", webhook)
    record = logging.getLogger("httpx").makeRecord(
        "httpx", logging.INFO, __file__, 1, "HTTP Request: POST %s", (webhook,), None,
    )
    assert "do-not-log-me" not in record.getMessage()
    assert "/hook/***" in record.getMessage()
