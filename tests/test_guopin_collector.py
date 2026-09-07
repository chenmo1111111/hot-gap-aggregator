import json
from pathlib import Path

from app.collectors.guopin import parse_guopin


def test_guopin_mapping_and_central_soe_detection() -> None:
    payload = json.loads((Path(__file__).parent / "fixtures" / "guopin_jobs.json").read_text(encoding="utf-8"))
    items = parse_guopin(payload, "生物", ["北京"], 25)
    assert len(items) == 1
    item = items[0]
    assert item.title == "生物信息算法工程师"
    assert item.extra["company"] == "中央示例集团"
    assert item.extra["education"] == "硕士"
    assert item.extra["city"] == "北京-海淀区"
    assert item.extra["recruitment_type"] == "校园招聘"
    assert item.extra["is_central_soe"] is True
    assert item.summary_zh == "负责单细胞数据分析与算法开发"
    assert item.url == "https://www.iguopin.com/job/detail?id=gp-001&source=campus"
