from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class MailAccount:
    account_id: str
    provider: str
    label: str
    address: str
    host: str = ""
    port: int = 993
    auth_code: str = ""


def load_mail_accounts(max_accounts: int = 12) -> list[MailAccount]:
    accounts: list[MailAccount] = []
    for number in range(1, max_accounts + 1):
        prefix = f"MAIL_ACCOUNT_{number}_"
        provider = os.getenv(f"{prefix}TYPE", "").strip().lower()
        if not provider:
            continue
        if provider not in {"imap", "gmail"}:
            raise RuntimeError(f"{prefix}TYPE must be imap or gmail")
        address = os.getenv(f"{prefix}USER", "").strip()
        label = os.getenv(f"{prefix}LABEL", "").strip() or f"邮箱 {number}"
        host = os.getenv(f"{prefix}HOST", "").strip()
        auth_code = os.getenv(f"{prefix}AUTH_CODE", "").strip()
        try:
            port = int(os.getenv(f"{prefix}PORT", "993"))
        except ValueError as exc:
            raise RuntimeError(f"{prefix}PORT must be an integer") from exc
        if not address:
            raise RuntimeError(f"{prefix}USER is required")
        if provider == "imap" and (not host or not auth_code):
            raise RuntimeError(f"{prefix}HOST and {prefix}AUTH_CODE are required")
        accounts.append(MailAccount(
            account_id=f"mail-{number}", provider=provider, label=label,
            address=address, host=host, port=port, auth_code=auth_code,
        ))
    return accounts


def public_account(account: MailAccount, *, authorized: bool, last_synced_at: str | None,
                   last_error: str | None) -> dict[str, object]:
    return {
        "account_id": account.account_id,
        "provider": account.provider,
        "label": account.label,
        "address_hint": _mask_address(account.address),
        "authorized": authorized,
        "last_synced_at": last_synced_at,
        "last_error": last_error,
    }


def _mask_address(value: str) -> str:
    local, separator, domain = value.partition("@")
    if not separator:
        return "***"
    visible = local[:2] if len(local) > 2 else local[:1]
    return f"{visible}***@{domain}"
