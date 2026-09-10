from __future__ import annotations

import json

import pytest

from app.capture_monitor import main, notify_feishu_file, update_state, validate_and_commit


def _write(path, count: int, prefix: str = "row") -> None:
    path.write_text(json.dumps({"items": [
        {"company_name": "公司", "position": f"岗位{i}", "source_record_id": f"{prefix}{i}"}
        for i in range(count)
    ]}, ensure_ascii=False), encoding="utf-8")


def test_capture_drop_below_ninety_percent_keeps_previous_snapshot(tmp_path) -> None:
    stable = tmp_path / "stable.json"
    candidate = tmp_path / "candidate.json"
    _write(stable, 100, "old")
    previous = stable.read_text(encoding="utf-8")
    _write(candidate, 89, "new")

    with pytest.raises(ValueError, match="90%"):
        validate_and_commit(
            candidate, stable, kind="qiuzhao",
            disappearance_log=tmp_path / "missing.jsonl",
        )

    assert stable.read_text(encoding="utf-8") == previous
    assert candidate.exists()


def test_xiaozhaoya_uses_eighty_percent_safety_gate(tmp_path) -> None:
    stable = tmp_path / "xiaozhaoya.json"
    candidate = tmp_path / "xiaozhaoya.candidate.json"
    stable.write_text(
        json.dumps({"items": [{"recruitmentId": index} for index in range(100)]}),
        encoding="utf-8",
    )
    candidate.write_text(
        json.dumps({"items": [{"recruitmentId": index} for index in range(79)]}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="80%"):
        validate_and_commit(
            candidate,
            stable,
            kind="xiaozhaoya",
            disappearance_log=tmp_path / "missing.jsonl",
        )


def test_capture_success_logs_disappeared_items_and_commits(tmp_path) -> None:
    stable = tmp_path / "stable.json"
    candidate = tmp_path / "candidate.json"
    _write(stable, 10, "old")
    _write(candidate, 9, "new")
    log = tmp_path / "missing.jsonl"

    result = validate_and_commit(candidate, stable, kind="qiuzhao", disappearance_log=log)

    assert result == {"previous": 10, "current": 9, "missing": 10}
    assert not candidate.exists()
    assert json.loads(log.read_text(encoding="utf-8"))["missing_count"] == 10


def test_capture_failure_state_escalates_and_success_resets(tmp_path) -> None:
    path = tmp_path / "state.json"
    first = update_state(path, success=False, reason="one")
    second = update_state(path, success=False, reason="two")
    restored = update_state(path, success=True)
    assert first["consecutive_failures"] == 1
    assert second["consecutive_failures"] == 2
    assert restored["consecutive_failures"] == 0
    assert restored["last_success"]


def test_base64_alert_decodes_utf8(monkeypatch, capsys) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "app.capture_monitor.notify_feishu",
        lambda title, message: calls.append((title, message)) or True,
    )
    assert main([
        "alert-b64", "--title-b64", "5rWL6K+V", "--message-b64", "5peg6ZyA5aSE55CG",
    ]) == 0
    assert calls == [("测试", "无需处理")]
    assert json.loads(capsys.readouterr().out)["feishu_sent"] is True


def test_alert_file_validates_and_sends(monkeypatch, tmp_path) -> None:
    alert = tmp_path / "alert.json"
    alert.write_text(json.dumps({"title": "test", "message": "detail"}), encoding="utf-8")
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "app.capture_monitor.notify_feishu",
        lambda title, message: calls.append((title, message)) or True,
    )
    assert notify_feishu_file(alert) is True
    assert calls == [("test", "detail")]
