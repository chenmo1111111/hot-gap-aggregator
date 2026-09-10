from __future__ import annotations

import json
import os

import pytest

from app.collect_gongkao import _write_atomic, collect_gongkao


def test_atomic_snapshot_is_world_readable_for_the_web_server(tmp_path) -> None:
    target = tmp_path / "gongkao.json"
    _write_atomic(target, {"items": [{"title": "公告"}]})
    assert json.loads(target.read_text(encoding="utf-8"))["items"][0]["title"] == "公告"
    if os.name != "nt":
        assert target.stat().st_mode & 0o777 == 0o644


@pytest.mark.asyncio
async def test_server_owned_mode_preserves_existing_snapshot(monkeypatch, tmp_path) -> None:
    target = tmp_path / "gongkao.json"
    payload = {"source": "gongkao", "items": [{"title": "S1 authoritative"}]}
    target.write_text(json.dumps(payload), encoding="utf-8")
    before = target.read_bytes()
    monkeypatch.setenv("GONGKAO_ON_SERVER", "true")
    returned = await collect_gongkao(tmp_path)
    assert returned == payload
    assert target.read_bytes() == before
