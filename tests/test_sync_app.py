import pytest
from fastapi.testclient import TestClient

from sync.app import PREFS_MAX_BYTES, app, initialize_database
from app.mailbox.store import MailboxStore


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNC_DB_PATH", str(tmp_path / "sync.db"))
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret-with-more-than-32-bytes")
    monkeypatch.setenv("ADMIN_USER", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "admin-pass")
    monkeypatch.setenv("MAX_USERS", "3")
    with TestClient(app, base_url="https://hot.weixincuotiben.top") as test_client:
        yield test_client


def login(client: TestClient, username="admin", password="admin-pass"):
    return client.post("/api/login", json={"username": username, "password": password})


def test_empty_database_requires_bootstrap_admin(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNC_DB_PATH", str(tmp_path / "empty.db"))
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret-with-more-than-32-bytes")
    monkeypatch.delenv("ADMIN_USER", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    with pytest.raises(RuntimeError, match="ADMIN_USER"):
        initialize_database()


def test_login_session_logout_and_auth_check(client):
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/auth-check").status_code == 401
    failed = login(client, password="wrong")
    assert failed.status_code == 401

    response = login(client)
    assert response.status_code == 200
    assert response.json() == {"username": "admin", "is_admin": True}
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=lax" in cookie
    assert "Max-Age=2592000" in cookie
    assert client.get("/api/me").json() == {"username": "admin", "is_admin": True}
    assert client.get("/api/auth-check").status_code == 200

    assert client.post("/api/logout").status_code == 200
    assert client.get("/api/me").status_code == 401


def test_login_rate_limit_after_ten_failures(client):
    for _ in range(10):
        assert login(client, password="bad").status_code == 401
    assert login(client, password="bad").status_code == 429


def test_settings_round_trip_and_size_limit(client):
    assert login(client).status_code == 200
    assert client.get("/api/settings").json() == {"prefs": {}, "updated_at": None}
    prefs = {"theme": "dark", "nested": {"tabs": ["all", "papers"]}}
    saved = client.put("/api/settings", json={"prefs": prefs})
    assert saved.status_code == 200
    assert saved.json()["ok"] is True
    loaded = client.get("/api/settings").json()
    assert loaded["prefs"] == prefs
    assert loaded["updated_at"] == saved.json()["updated_at"]

    oversized = {"value": "x" * PREFS_MAX_BYTES}
    assert client.put("/api/settings", json={"prefs": oversized}).status_code == 413


def test_admin_create_duplicate_limit_reset_and_delete(client):
    assert login(client).status_code == 200
    created = client.post(
        "/api/admin/users", json={"username": "friend", "password": "friend-pass"}
    )
    assert created.status_code == 201
    assert created.json()["username"] == "friend"
    assert "password_hash" not in created.json()
    assert client.post(
        "/api/admin/users", json={"username": "friend", "password": "friend-pass"}
    ).status_code == 409
    assert client.post(
        "/api/admin/users", json={"username": "third", "password": "third-pass"}
    ).status_code == 201
    assert client.post(
        "/api/admin/users", json={"username": "fourth", "password": "fourth-pass"}
    ).status_code == 409

    reset = client.post("/api/admin/users/friend/password", json={"password": "new-pass"})
    assert reset.status_code == 200
    assert client.delete("/api/admin/users/admin").status_code == 400
    assert client.delete("/api/admin/users/friend").status_code == 200
    users = client.get("/api/admin/users").json()["users"]
    assert [user["username"] for user in users] == ["admin", "third"]


def test_non_admin_cannot_access_admin_routes(client):
    assert login(client).status_code == 200
    assert client.post(
        "/api/admin/users", json={"username": "friend", "password": "friend-pass"}
    ).status_code == 201
    client.post("/api/logout")
    assert login(client, "friend", "friend-pass").status_code == 200
    assert client.get("/api/admin/users").status_code == 403
    assert client.delete("/api/admin/users/admin").status_code == 403
    assert client.get("/api/admin/mail-deadlines").status_code == 403
    assert client.post(
        "/api/admin/mail-deadlines/status",
        json={"message_id": "<private@example>", "status": "done"},
    ).status_code == 403


def test_admin_mail_deadlines_are_private_and_done_rows_leave_default_list(client):
    assert login(client).status_code == 200
    store = MailboxStore()
    store.add_deadline({
        "message_id": "<private@example>",
        "company": "三棵树",
        "type": "AI视频面试",
        "deadline_at": "2026-09-13T01:30:00+00:00",
        "action_url": "https://u.hrtps.test/r/private-token",
        "summary": "完成AI视频面试",
        "received_at": "2026-09-10T01:30:00+00:00",
        "status": "pending",
        "subject": "三棵树面试通知",
        "sender": "招聘中心",
        "deadline_kind": "relative_hours",
    }, imap_uid=1)

    listed = client.get("/api/admin/mail-deadlines")
    assert listed.status_code == 200
    assert listed.json()["items"][0]["action_url"].endswith("private-token")
    completed = client.post(
        "/api/admin/mail-deadlines/status",
        json={"message_id": "<private@example>", "status": "done"},
    )
    assert completed.status_code == 200
    assert client.get("/api/admin/mail-deadlines").json()["items"] == []
    history = client.get("/api/admin/mail-deadlines?include_history=true").json()["items"]
    assert history[0]["status"] == "done"
