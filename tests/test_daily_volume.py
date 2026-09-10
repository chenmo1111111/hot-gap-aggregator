from __future__ import annotations

import json
from datetime import date

from app.daily_volume import update_daily_volume


def test_daily_volume_tracks_first_seen_and_retains_thirty_days(tmp_path) -> None:
    state = tmp_path / "state.json"
    output = tmp_path / "daily-volume.json"
    output.write_text(json.dumps({"history": [
        {"date": f"2026-08-{day:02d}", "gongkao_new": 1, "qiuzhao_new": 1}
        for day in range(1, 31)
    ]}), encoding="utf-8")
    gongkao = [{
        "url": "https://gov.example/1", "published_at": "2026-09-10",
        "extra": {"id": "g1", "first_seen": "2026-09-10"},
    }]
    qiuzhao = [{
        "company_name": "公司", "position": "岗位", "source_record_id": "q1",
        "updated_at": "2026-09-10",
    }]

    first = update_daily_volume(
        gongkao, qiuzhao, today=date(2026, 9, 10), state_path=state, output_path=output,
    )
    second = update_daily_volume(
        gongkao, qiuzhao, today=date(2026, 9, 11), state_path=state, output_path=output,
    )

    assert first["gongkao_new"] == 1 and first["qiuzhao_new"] == 1
    assert second["gongkao_new"] == 0 and second["qiuzhao_new"] == 0
    assert len(json.loads(output.read_text(encoding="utf-8"))["history"]) == 30
