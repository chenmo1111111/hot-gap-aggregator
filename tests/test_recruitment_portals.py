import json
from pathlib import Path

import pytest

from app.recruitment_portals import export_recruitment_portals, load_recruitment_portals


ROOT = Path(__file__).resolve().parents[1]


def test_portal_config_exports_three_groups_and_valid_urls(tmp_path: Path) -> None:
    config = tmp_path / "portals.yaml"
    config.write_text("""
groups:
  - name: 官方平台
    items:
      - {name: 人社部, url: "https://www.mohrss.gov.cn/", category: 官方平台}
  - name: 省人社厅
    items:
      - {name: 山东, url: "https://hrss.shandong.gov.cn/", category: 省人社厅, province: 山东}
  - name: 央企
    items:
      - {name: 国家电网, url: "https://www.sgcc.com.cn/", category: 央企}
""", encoding="utf-8")
    target = tmp_path / "portals.json"
    payload = export_recruitment_portals(config, target)
    assert payload["item_count"] == 3
    assert [group["name"] for group in payload["groups"]] == ["官方平台", "省人社厅", "央企"]
    assert json.loads(target.read_text(encoding="utf-8"))["groups"][1]["items"][0]["province"] == "山东"


def test_portal_config_rejects_company_name_in_url_field(tmp_path: Path) -> None:
    config = tmp_path / "portals.yaml"
    config.write_text("""
groups:
  - {name: 官方平台, items: []}
  - {name: 省人社厅, items: []}
  - name: 央企
    items:
      - {name: 无网址企业, url: 无网址企业, category: 央企}
""", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid recruitment portal"):
        load_recruitment_portals(config)


def test_checked_portal_inventory_has_expected_valid_counts() -> None:
    groups = load_recruitment_portals(ROOT / "config" / "recruitment_portals.yaml")
    assert {group["name"]: len(group["items"]) for group in groups} == {
        "官方平台": 12,
        "省人社厅": 31,
        "央企": 97,
    }
