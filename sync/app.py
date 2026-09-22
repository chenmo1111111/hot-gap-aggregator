from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, Signer, TimestampSigner
from passlib.context import CryptContext
from pydantic import BaseModel, Field
from dotenv import load_dotenv

from app.mailbox.store import MailboxStore, initialize_mailbox_database
from app.mailbox.reminder_url import (
    CHINA_TZ,
    ReminderUrlError,
    extract_reminder_from_text,
    extract_reminder_from_url,
)
try:
    from app.mailbox.accounts import load_mail_accounts, public_account
    from app.mailbox.gmail_client import (
        GmailReadonlyClient,
        authorization_url,
        exchange_authorization_code,
    )
    from app.mailbox.inbox_store import InboxStore, TokenCipher, initialize_inbox_database
except ModuleNotFoundError as exc:
    optional_mail_modules = {
        "app.mailbox.accounts", "app.mailbox.gmail_client",
        "app.mailbox.inbox_store", "cryptography",
    }
    if exc.name not in optional_mail_modules:
        raise
    load_mail_accounts = public_account = None
    GmailReadonlyClient = authorization_url = exchange_authorization_code = None
    InboxStore = TokenCipher = initialize_inbox_database = None

local_env = Path(".env")
if local_env.is_file() and os.access(local_env, os.R_OK):
    load_dotenv(local_env)

logger = logging.getLogger("hot_gap.sync")

COOKIE_NAME = "session"
COOKIE_MAX_AGE = 30 * 24 * 60 * 60
PREFS_MAX_BYTES = 64 * 1024
LOGIN_WINDOW_SECONDS = 60
LOGIN_FAILURE_LIMIT = 10

password_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
login_failures: dict[str, deque[float]] = defaultdict(deque)
login_failures_lock = threading.Lock()


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class SettingsBody(BaseModel):
    prefs: dict[str, Any]


class CreateUserBody(LoginBody):
    is_admin: bool = False


class PasswordBody(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class MailDeadlineStatusBody(BaseModel):
    message_id: str = Field(min_length=1, max_length=998)
    status: str = Field(pattern="^(done|pending)$")


class MailDeadlinePreviewBody(BaseModel):
    url: str = Field(min_length=8, max_length=2048)


class MailDeadlineTextPreviewBody(BaseModel):
    text: str = Field(min_length=5, max_length=20_000)


class ManualMailDeadlineBody(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    type: str = Field(default="报名", min_length=1, max_length=40)
    start_at: datetime | None = None
    deadline_at: datetime | None = None
    action_url: str = Field(default="", max_length=2048)
    summary: str = Field(default="", max_length=500)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def database_path() -> Path:
    return Path(os.getenv("SYNC_DB_PATH", "./sync.db"))


def connect() -> sqlite3.Connection:
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def session_signer() -> Signer:
    secret = os.getenv("SESSION_SECRET", "")
    if len(secret.encode("utf-8")) < 32:
        raise RuntimeError("SESSION_SECRET must contain at least 32 bytes")
    return Signer(secret, salt="hot-gap-session")


def hash_password(password: str) -> str:
    if len(password.encode("utf-8")) > 72:
        raise HTTPException(status_code=422, detail="密码不能超过 72 字节")
    return password_context.hash(password)


def initialize_database() -> None:
    # Fail at application startup instead of silently running with an unsafe key.
    session_signer()
    with connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (
                user_id INTEGER PRIMARY KEY,
                prefs_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            """
        )
        count = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if not count:
            username = os.getenv("ADMIN_USER", "").strip()
            password = os.getenv("ADMIN_PASSWORD", "")
            if not username or not password:
                logger.critical("users table is empty; ADMIN_USER and ADMIN_PASSWORD are required")
                raise RuntimeError("ADMIN_USER and ADMIN_PASSWORD are required for first startup")
            connection.execute(
                "INSERT INTO users(username, password_hash, is_admin, created_at) VALUES (?, ?, 1, ?)",
                (username, hash_password(password), utc_now()),
            )
    initialize_mailbox_database()
    if initialize_inbox_database is not None:
        initialize_inbox_database()


def client_ip(request: Request) -> str:
    # The service only listens on loopback and Nginx overwrites X-Real-IP.
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "unknown")


def prune_failures(ip: str, now: float) -> deque[float]:
    attempts = login_failures[ip]
    cutoff = now - LOGIN_WINDOW_SECONDS
    while attempts and attempts[0] <= cutoff:
        attempts.popleft()
    return attempts


def failure_limit_reached(ip: str) -> bool:
    with login_failures_lock:
        return len(prune_failures(ip, time.monotonic())) >= LOGIN_FAILURE_LIMIT


def record_login_failure(ip: str) -> None:
    with login_failures_lock:
        prune_failures(ip, time.monotonic()).append(time.monotonic())


def clear_login_failures(ip: str) -> None:
    with login_failures_lock:
        login_failures.pop(ip, None)


def row_to_user(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "username": row["username"],
        "is_admin": bool(row["is_admin"]),
        "created_at": row["created_at"],
    }


def current_user(request: Request) -> dict[str, Any]:
    cookie = request.cookies.get(COOKIE_NAME)
    if not cookie:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录")
    try:
        raw_user_id = session_signer().unsign(cookie).decode("utf-8")
        user_id = int(raw_user_id)
    except (BadSignature, UnicodeDecodeError, ValueError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="会话无效") from None
    with connect() as connection:
        row = connection.execute(
            "SELECT id, username, is_admin, created_at FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户不存在")
    return row_to_user(row)


def admin_user(user: Annotated[dict[str, Any], Depends(current_user)]) -> dict[str, Any]:
    if not user["is_admin"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
    return user


@asynccontextmanager
async def lifespan(_: FastAPI):
    with login_failures_lock:
        login_failures.clear()
    initialize_database()
    yield


app = FastAPI(title="Hot Gap Sync", docs_url=None, redoc_url=None, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://hot.weixincuotiben.top"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type"],
)


@app.post("/api/login")
def login(body: LoginBody, request: Request, response: Response) -> dict[str, Any]:
    ip = client_ip(request)
    if failure_limit_reached(ip):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="登录失败次数过多，请稍后再试")
    with connect() as connection:
        row = connection.execute(
            "SELECT id, username, password_hash, is_admin, created_at FROM users WHERE username = ?",
            (body.username.strip(),),
        ).fetchone()
    if row is None or not password_context.verify(body.password, row["password_hash"]):
        record_login_failure(ip)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
    clear_login_failures(ip)
    cookie = session_signer().sign(str(row["id"]).encode("utf-8")).decode("utf-8")
    response.set_cookie(
        COOKIE_NAME,
        cookie,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    return {"username": row["username"], "is_admin": bool(row["is_admin"])}


@app.post("/api/logout")
def logout(response: Response) -> dict[str, bool]:
    response.delete_cookie(COOKIE_NAME, path="/", secure=True, httponly=True, samesite="lax")
    return {"ok": True}


@app.get("/api/me")
def me(user: Annotated[dict[str, Any], Depends(current_user)]) -> dict[str, Any]:
    return {"username": user["username"], "is_admin": user["is_admin"]}


@app.get("/api/auth-check")
def auth_check(_: Annotated[dict[str, Any], Depends(current_user)]) -> Response:
    return Response(status_code=status.HTTP_200_OK)


@app.get("/api/settings")
def get_settings(user: Annotated[dict[str, Any], Depends(current_user)]) -> dict[str, Any]:
    with connect() as connection:
        row = connection.execute(
            "SELECT prefs_json, updated_at FROM settings WHERE user_id = ?", (user["id"],)
        ).fetchone()
    if row is None:
        return {"prefs": {}, "updated_at": None}
    try:
        prefs = json.loads(row["prefs_json"])
    except json.JSONDecodeError:
        prefs = {}
    return {"prefs": prefs, "updated_at": row["updated_at"]}


@app.put("/api/settings")
def put_settings(body: SettingsBody, user: Annotated[dict[str, Any], Depends(current_user)]) -> dict[str, Any]:
    encoded = json.dumps(body.prefs, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > PREFS_MAX_BYTES:
        raise HTTPException(status_code=413, detail="偏好设置不能超过 64KB")
    updated_at = utc_now()
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO settings(user_id, prefs_json, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET prefs_json = excluded.prefs_json, updated_at = excluded.updated_at
            """,
            (user["id"], encoded, updated_at),
        )
    return {"ok": True, "updated_at": updated_at}


@app.get("/api/admin/users")
def list_users(_: Annotated[dict[str, Any], Depends(admin_user)]) -> dict[str, Any]:
    with connect() as connection:
        rows = connection.execute(
            "SELECT id, username, is_admin, created_at FROM users ORDER BY id"
        ).fetchall()
    return {"users": [row_to_user(row) for row in rows]}


@app.post("/api/admin/users", status_code=status.HTTP_201_CREATED)
def create_user(body: CreateUserBody, _: Annotated[dict[str, Any], Depends(admin_user)]) -> dict[str, Any]:
    username = body.username.strip()
    if not username:
        raise HTTPException(status_code=422, detail="用户名不能为空")
    password_hash = hash_password(body.password)
    max_users = max(1, int(os.getenv("MAX_USERS", "8")))
    try:
        with connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            count = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            if count >= max_users:
                raise HTTPException(status_code=409, detail=f"用户数量已达到上限 {max_users}")
            cursor = connection.execute(
                "INSERT INTO users(username, password_hash, is_admin, created_at) VALUES (?, ?, ?, ?)",
                (username, password_hash, int(body.is_admin), utc_now()),
            )
            row = connection.execute(
                "SELECT id, username, is_admin, created_at FROM users WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="用户名已存在") from None
    return row_to_user(row)


@app.delete("/api/admin/users/{username}")
def delete_user(
    username: str,
    user: Annotated[dict[str, Any], Depends(admin_user)],
) -> dict[str, bool]:
    if username == user["username"]:
        raise HTTPException(status_code=400, detail="不能删除当前登录账号")
    with connect() as connection:
        cursor = connection.execute("DELETE FROM users WHERE username = ?", (username,))
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="用户不存在")
    return {"ok": True}


@app.post("/api/admin/users/{username}/password")
def reset_password(
    username: str,
    body: PasswordBody,
    _: Annotated[dict[str, Any], Depends(admin_user)],
) -> dict[str, bool]:
    with connect() as connection:
        cursor = connection.execute(
            "UPDATE users SET password_hash = ? WHERE username = ?",
            (hash_password(body.password), username),
        )
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="用户不存在")
    return {"ok": True}


@app.get("/api/admin/mail-deadlines")
def list_mail_deadlines(
    _: Annotated[dict[str, Any], Depends(admin_user)],
    include_history: bool = False,
) -> dict[str, Any]:
    items = MailboxStore().list_deadlines(include_history=include_history)
    return {"items": items, "count": len(items)}


@app.post("/api/admin/mail-deadlines/status")
def update_mail_deadline_status(
    body: MailDeadlineStatusBody,
    _: Annotated[dict[str, Any], Depends(admin_user)],
) -> dict[str, bool]:
    if not MailboxStore().set_status(body.message_id, body.status):
        raise HTTPException(status_code=404, detail="提醒不存在")
    return {"ok": True}


@app.post("/api/admin/mail-deadlines/preview")
async def preview_manual_mail_deadline(
    body: MailDeadlinePreviewBody,
    _: Annotated[dict[str, Any], Depends(admin_user)],
) -> dict[str, Any]:
    try:
        preview = await extract_reminder_from_url(body.url)
    except ReminderUrlError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("manual reminder URL extraction failed: %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="公告读取失败，请稍后重试") from exc
    return preview.to_dict()


@app.post("/api/admin/mail-deadlines/preview-text")
async def preview_manual_mail_deadline_text(
    body: MailDeadlineTextPreviewBody,
    _: Annotated[dict[str, Any], Depends(admin_user)],
) -> dict[str, Any]:
    try:
        preview = await extract_reminder_from_text(body.text)
    except ReminderUrlError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("manual reminder text extraction failed: %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="邀请文字识别失败，请稍后重试") from exc
    return preview.to_dict()


def _utc_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=CHINA_TZ)
    return value.astimezone(timezone.utc)


@app.post("/api/admin/mail-deadlines/manual")
def create_manual_mail_deadline(
    body: ManualMailDeadlineBody,
    _: Annotated[dict[str, Any], Depends(admin_user)],
) -> dict[str, Any]:
    start_value, deadline_value = _utc_datetime(body.start_at), _utc_datetime(body.deadline_at)
    start_at = start_value.isoformat() if start_value else None
    deadline_at = deadline_value.isoformat() if deadline_value else None
    if not start_at and not deadline_at:
        raise HTTPException(status_code=422, detail="报名开始时间和截止时间至少填写一个")
    if start_value and deadline_value and deadline_value <= start_value:
        raise HTTPException(status_code=422, detail="截止时间必须晚于报名开始时间")
    action_url = body.action_url.strip()
    if action_url:
        try:
            parsed = urlsplit(action_url)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="操作网址格式不正确") from exc
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise HTTPException(status_code=422, detail="操作网址必须是 http/https 地址")
    message_id = f"manual:{uuid4().hex}"
    store = MailboxStore()
    store.add_manual_deadline({
        "message_id": message_id,
        "company": body.title.strip(),
        "type": body.type.strip(),
        "start_at": start_at,
        "deadline_at": deadline_at,
        "action_url": action_url or None,
        "summary": body.summary.strip() or f"按时处理：{body.title.strip()}",
    })
    item = next(row for row in store.list_deadlines() if row["message_id"] == message_id)
    return {"ok": True, "item": item}


@app.delete("/api/admin/mail-deadlines/manual/{message_id}")
def delete_manual_mail_deadline(
    message_id: str,
    _: Annotated[dict[str, Any], Depends(admin_user)],
) -> dict[str, bool]:
    if not MailboxStore().delete_manual_deadline(message_id):
        raise HTTPException(status_code=404, detail="手动提醒不存在")
    return {"ok": True}


def oauth_state_signer() -> TimestampSigner:
    return TimestampSigner(os.getenv("SESSION_SECRET", ""), salt="hot-gap-mail-oauth")


def require_mail_inbox() -> None:
    if any(component is None for component in (
        load_mail_accounts, public_account, GmailReadonlyClient,
        authorization_url, exchange_authorization_code,
        InboxStore, TokenCipher, initialize_inbox_database,
    )):
        raise HTTPException(status_code=503, detail="多邮箱管理功能尚未安装")


def configured_mail_account(account_id: str):
    require_mail_inbox()
    account = next((item for item in load_mail_accounts() if item.account_id == account_id), None)
    if account is None:
        raise HTTPException(status_code=404, detail="邮箱账号不存在")
    return account


@app.get("/api/admin/mail-inbox/accounts")
def list_mail_accounts(
    _: Annotated[dict[str, Any], Depends(admin_user)],
) -> dict[str, Any]:
    require_mail_inbox()
    store = InboxStore()
    items = []
    for account in load_mail_accounts():
        store.upsert_account(account.account_id, account.provider, account.label, account.address)
        state = store.account_state(account.account_id)
        items.append(public_account(
            account,
            authorized=account.provider == "imap" or bool(state.get("refresh_token_encrypted")),
            last_synced_at=state.get("last_synced_at"),
            last_error=state.get("last_error"),
        ))
    return {"items": items, "count": len(items)}


@app.get("/api/admin/mail-inbox")
def list_mail_inbox(
    _: Annotated[dict[str, Any], Depends(admin_user)],
    account_id: str = "", category: str = "", q: str = "",
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    require_mail_inbox()
    items, total = InboxStore().list_messages(
        account_id=account_id, category=category, query=q[:200],
        limit=limit, offset=offset,
    )
    return {"items": items, "count": len(items), "total": total}


@app.get("/api/admin/mail-inbox/message/{account_id}/{message_key}")
def get_mail_inbox_message(
    account_id: str, message_key: str,
    _: Annotated[dict[str, Any], Depends(admin_user)],
) -> dict[str, Any]:
    require_mail_inbox()
    item = InboxStore().get_message(account_id, message_key)
    if item is None:
        raise HTTPException(status_code=404, detail="邮件不存在")
    return item


@app.post("/api/admin/mail-inbox/oauth/google/{account_id}/start")
def start_google_mail_oauth(
    account_id: str,
    user: Annotated[dict[str, Any], Depends(admin_user)],
) -> dict[str, str]:
    account = configured_mail_account(account_id)
    if account.provider != "gmail":
        raise HTTPException(status_code=400, detail="该账号不是 Gmail")
    state = oauth_state_signer().sign(f"{user['id']}:{account_id}".encode("utf-8")).decode("utf-8")
    return {"authorization_url": authorization_url(state, login_hint=account.address)}


@app.get("/api/admin/mail-inbox/oauth/google/callback")
async def finish_google_mail_oauth(
    code: str, state: str,
    user: Annotated[dict[str, Any], Depends(admin_user)],
) -> RedirectResponse:
    try:
        unsigned = oauth_state_signer().unsign(state, max_age=600).decode("utf-8")
        raw_user_id, account_id = unsigned.split(":", 1)
        if int(raw_user_id) != user["id"]:
            raise ValueError("OAuth state user mismatch")
    except (BadSignature, UnicodeDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="Gmail 授权状态无效或已过期") from None
    account = configured_mail_account(account_id)
    if account.provider != "gmail":
        raise HTTPException(status_code=400, detail="该账号不是 Gmail")
    try:
        token = await exchange_authorization_code(code)
        refresh_token = str(token["refresh_token"])
        profile = await GmailReadonlyClient(refresh_token).profile()
        actual_address = str(profile.get("emailAddress") or "").lower()
        if actual_address != account.address.lower():
            raise HTTPException(status_code=400, detail="授权的 Gmail 账号与配置不一致")
        store = InboxStore()
        store.upsert_account(account.account_id, account.provider, account.label, account.address)
        store.save_refresh_token(account.account_id, TokenCipher().encrypt(refresh_token))
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Gmail OAuth failed for account %s: %s", account.account_id, type(exc).__name__)
        raise HTTPException(status_code=502, detail="Gmail 授权交换失败") from None
    return RedirectResponse(url="/?mailbox_oauth=success", status_code=303)
