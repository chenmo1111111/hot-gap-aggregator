from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_0600_runner_contains_the_three_daily_purchased_tables() -> None:
    script = (ROOT / "scripts" / "run_wanqing_sync.ps1").read_text(encoding="utf-8")

    assert script.count("Invoke-Capture \"") == 3
    assert '"app.capture_wanqing"' in script
    assert '"app.capture_gongkao_sheet"' in script
    assert '"app.capture_shasha"' in script
    assert "capture_xiaozhaoya" not in script
    assert "xiaozhaoyaSnapshot" not in script


def test_0600_task_replaces_the_old_0730_schedule() -> None:
    script = (ROOT / "scripts" / "install_wanqing_task.ps1").read_text(encoding="utf-8")

    assert "capture Wanqing, Shasha, and Gongkao Sheet" in script
    assert "Xiaozhaoya" not in script
    assert '[string]$TaskName = "HotGap-Purchased-Tables-0600"' in script
    assert '[string]$RunAt = "06:00"' in script
    assert '"HotGap-Purchased-Tables-0730"' in script
