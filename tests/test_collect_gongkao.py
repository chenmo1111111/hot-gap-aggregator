from __future__ import annotations

import json
import os

from app.collect_gongkao import _write_atomic


def test_atomic_snapshot_is_world_readable_for_the_web_server(tmp_path) -> None:
    target = tmp_path / "gongkao.json"
    _write_atomic(target, {"items": [{"title": "公告"}]})
    assert json.loads(target.read_text(encoding="utf-8"))["items"][0]["title"] == "公告"
    if os.name != "nt":
        assert target.stat().st_mode & 0o777 == 0o644
