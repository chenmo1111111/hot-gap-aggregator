from pathlib import Path

import pytest

from app.store.database import Database
from app.watchers.city_subsidy import CitySubsidyWatcher, extract_main_text


FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_extract_main_text_ignores_navigation() -> None:
    text = extract_main_text(fixture("city_subsidy_old.html"))
    assert "三万元" in text
    assert "导航" not in text
    assert "备案信息" not in text


@pytest.mark.asyncio
async def test_city_watcher_baselines_then_pushes_once(monkeypatch, tmp_path) -> None:
    config = tmp_path / "city.yaml"
    config.write_text("pages:\n  - city: 杭州\n    name: 应届生生活补贴\n    url: https://example.test/policy\n", encoding="utf-8")
    database = Database(tmp_path / "watch.db")
    pushed: list[str] = []

    async def judge(prompt: str) -> str:
        assert "三万元" in prompt and "两万元" in prompt
        return "现在仍可申领，硕士补贴降至两万元，截止2026年10月31日。"

    async def notify(text: str, _title: str) -> dict[str, str]:
        pushed.append(text)
        return {"feishu": "ok"}

    watcher = CitySubsidyWatcher(database, config, judge=judge, notifier=notify)
    monkeypatch.setattr(watcher, "_fetch", lambda _url: _async_value(fixture("city_subsidy_old.html")))
    assert (await watcher.run())[0]["status"] == "baseline"
    monkeypatch.setattr(watcher, "_fetch", lambda _url: _async_value(fixture("city_subsidy_new.html")))
    assert (await watcher.run())[0]["status"] == "pushed"
    assert pushed[0].startswith("【补贴变动】杭州")
    assert (await watcher.run())[0]["status"] == "unchanged"
    assert len(pushed) == 1
    database.close()


async def _async_value(value: str) -> str:
    return value
