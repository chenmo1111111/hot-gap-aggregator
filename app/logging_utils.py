from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from typing import Any


_FEISHU_HOOK_RE = re.compile(
    r"(https?://(?:open\.)?feishu\.cn/open-apis/bot/v2/hook/)[^\s?'\"/]+",
    re.I,
)
_BEARER_RE = re.compile(r"(?i)(authorization[\s'\":=]+bearer\s+)[^\s,'\"}]+")
_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:access_token|api_key|key|secret|sign|token)=)[^&\s'\"]+"
)
_TELEGRAM_BOT_RE = re.compile(r"(https?://api\.telegram\.org/bot)[^/\s]+", re.I)
_SERVERCHAN_RE = re.compile(r"(https?://sctapi\.ftqq\.com/)[^/.\s]+(?=\.send)", re.I)
_SECRET_ENV_NAMES = (
    "FEISHU_WEBHOOK", "FEISHU_SIGN_SECRET", "BARK_URL", "ZHIPU_API_KEY",
    "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "YOUTUBE_API_KEY", "NCBI_API_KEY",
    "TG_BOT_TOKEN", "SERVERCHAN_KEY", "SESSION_SECRET", "RSSHUB_KEY",
    "WEWERSS_CODE", "GITHUB_TOKEN",
)


def redact_sensitive(value: Any) -> str:
    """Return a log-safe representation without configured credentials."""
    text = str(value)
    text = _FEISHU_HOOK_RE.sub(r"\1***", text)
    text = _BEARER_RE.sub(r"\1***", text)
    text = _QUERY_SECRET_RE.sub(r"\1***", text)
    text = _TELEGRAM_BOT_RE.sub(r"\1***", text)
    text = _SERVERCHAN_RE.sub(r"\1***", text)
    for name in _SECRET_ENV_NAMES:
        secret = os.getenv(name, "")
        if secret and len(secret) >= 6:
            text = text.replace(secret, "***")
    return text


def redact_payload(value: Any) -> Any:
    """Recursively redact secrets if a request payload is ever logged."""
    if isinstance(value, Mapping):
        return {key: redact_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_payload(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_payload(item) for item in value)
    return redact_sensitive(value) if isinstance(value, str) else value


def install_sensitive_log_redaction() -> None:
    """Install a process-wide LogRecord factory that redacts before handlers run."""
    current = logging.getLogRecordFactory()
    if getattr(current, "_hotgap_redacts_secrets", False):
        return

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = current(*args, **kwargs)
        try:
            record.msg = redact_sensitive(record.getMessage())
            record.args = ()
        except Exception:
            # Logging must never break the worker because redaction failed.
            pass
        return record

    setattr(factory, "_hotgap_redacts_secrets", True)
    logging.setLogRecordFactory(factory)
