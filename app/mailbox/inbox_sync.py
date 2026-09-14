from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

from .accounts import MailAccount, load_mail_accounts
from .classify import MailClassifier
from .gmail_client import GmailReadonlyClient
from .imap_client import ImapMailboxClient, MailMessage
from .inbox_store import InboxStore, TokenCipher
from .store import MailboxStore


UTC = timezone.utc
LOGGER = logging.getLogger("hot_gap.mail_inbox")


def _drop_privileges() -> None:
    username = os.getenv("MAILBOX_RUN_AS_USER", "").strip()
    if not username or not hasattr(os, "geteuid") or os.geteuid() != 0:
        return
    import pwd

    account = pwd.getpwnam(username)
    os.setgid(account.pw_gid)
    os.initgroups(username, account.pw_gid)
    os.setuid(account.pw_uid)


def _message_key(provider_uid: str, mail: MailMessage) -> str:
    stable = mail.message_id.strip() or provider_uid
    return hashlib.sha256(stable.encode("utf-8", errors="replace")).hexdigest()


def _safe_error(exc: Exception) -> str:
    return type(exc).__name__


def _attachment_rows(mail: MailMessage) -> list[dict[str, object]]:
    return [
        {"filename": item.filename, "size": item.size, "content_type": item.content_type}
        for item in mail.attachments
    ]


async def _store_messages(store: InboxStore, deadline_store: MailboxStore,
                          classifier: MailClassifier, account: MailAccount,
                          incoming: list[tuple[str, MailMessage]]) -> tuple[int, int]:
    added = classified = 0
    candidates: list[tuple[str, str, MailMessage, str | None]] = []
    for provider_uid, mail in incoming:
        key = _message_key(provider_uid, mail)
        if store.has_message(account.account_id, key):
            continue
        outcome = deadline_store.processed_outcome(mail.message_id)
        candidates.append((provider_uid, key, mail, outcome))
    categories = await classifier.classify_many([
        (key, mail, outcome) for _, key, mail, outcome in candidates
    ])
    for provider_uid, key, mail, outcome in candidates:
        category = categories.get(key)
        classified += int(category is not None)
        if store.add_message({
            "account_id": account.account_id, "message_key": key,
            "provider_uid": provider_uid, "message_id": mail.message_id,
            "sender": mail.sender, "subject": mail.subject, "body": mail.body,
            "received_at": mail.received_at.astimezone(UTC).isoformat(),
            "attachments": _attachment_rows(mail), "category": category,
            "deadline_linked": outcome == "deadline",
        }):
            added += 1
    return added, classified


async def sync_account(store: InboxStore, deadline_store: MailboxStore,
                       classifier: MailClassifier, account: MailAccount,
                       initial_days: int) -> dict[str, object]:
    store.upsert_account(account.account_id, account.provider, account.label, account.address)
    state = store.account_state(account.account_id)
    try:
        if account.provider == "imap":
            client = ImapMailboxClient(
                account.host, account.port, account.address, account.auth_code,
            )
            uid_validity, messages, high_water = await asyncio.to_thread(
                client.fetch_messages,
                last_uid=state.get("last_uid"),
                known_uid_validity=state.get("uid_validity"),
                initial_days=initial_days,
            )
            incoming = [(str(mail.uid), mail) for mail in messages]
            added, classified = await _store_messages(
                store, deadline_store, classifier, account, incoming,
            )
            store.update_imap_state(account.account_id, uid_validity, high_water)
        else:
            encrypted = str(state.get("refresh_token_encrypted") or "")
            if not encrypted:
                return {"account_id": account.account_id, "status": "authorization_required", "added": 0}
            refresh_token = TokenCipher().decrypt(encrypted)
            client = GmailReadonlyClient(refresh_token)
            profile = await client.profile()
            actual = str(profile.get("emailAddress") or "").lower()
            if actual and actual != account.address.lower():
                raise RuntimeError("authorized Gmail address does not match configured account")
            last_synced = state.get("last_synced_at")
            after_at = None
            if last_synced:
                try:
                    after_at = datetime.fromisoformat(str(last_synced)) - timedelta(days=1)
                except ValueError:
                    after_at = None
            incoming = await client.fetch_messages(after_at=after_at, initial_days=initial_days)
            added, classified = await _store_messages(
                store, deadline_store, classifier, account, incoming,
            )
            store.record_sync(account.account_id)
        return {"account_id": account.account_id, "status": "ok", "added": added,
                "classified": classified}
    except Exception as exc:
        error = _safe_error(exc)
        store.record_sync(account.account_id, error)
        return {"account_id": account.account_id, "status": "failed", "error": error, "added": 0}


async def run_once() -> dict[str, object]:
    load_dotenv()
    store = InboxStore()
    store.initialize()
    deadline_store = MailboxStore()
    deadline_store.initialize()
    accounts = load_mail_accounts()
    classifier = MailClassifier()
    initial_days = max(1, int(os.getenv("MAIL_INBOX_RETENTION_DAYS", "90")))
    results = []
    for account in accounts:
        results.append(await sync_account(
            store, deadline_store, classifier, account, initial_days,
        ))
    pruned = store.prune(initial_days)
    return {
        "event": "mail_inbox_sync", "accounts": len(accounts),
        "added": sum(int(item.get("added", 0)) for item in results),
        "pruned": pruned, "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Synchronize private read-only mail inboxes")
    parser.parse_args()
    load_dotenv()
    os.umask(0o077)
    _drop_privileges()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        result = asyncio.run(run_once())
    except Exception as exc:
        LOGGER.error("mail inbox sync failed: %s", type(exc).__name__)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 1 if any(item.get("status") == "failed" for item in result["results"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
