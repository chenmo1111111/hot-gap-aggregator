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

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from itsdangerous import BadSignature, Signer
from passlib.context import CryptContext
from pydantic import BaseModel, Field
from dotenv import load_dotenv

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
        if count:
            return
        username = os.getenv("ADMIN_USER", "").strip()
        password = os.getenv("ADMIN_PASSWORD", "")
        if not username or not password:
            logger.critical("users table is empty; ADMIN_USER and ADMIN_PASSWORD are required")
            raise RuntimeError("ADMIN_USER and ADMIN_PASSWORD are required for first startup")
        connection.execute(
            "INSERT INTO users(username, password_hash, is_admin, created_at) VALUES (?, ?, 1, ?)",
            (username, hash_password(password), utc_now()),
        )


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
