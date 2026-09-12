from __future__ import annotations

from app.pipeline.purchased_classify import (
    classify_purchased_row,
    partition_purchased_rows,
)


def test_named_public_entities_route_to_gongkao() -> None:
    names = (
        "寿光市农业农村局",
        "中国农科院蔬菜花卉研究所",
        "荥阳市消防救援大队",
        "呼和浩特海关所属事业单位",
        "中科院地理科学与资源研究所",
        "中科院上海硅酸盐研究所",
        "中国社科院考古研究所",
        "龙邦出入境边防检查站",
        "扶绥县公安局",
        "来宾市公安局",
        "大连市公安局",
    )
    for name in names:
        kind, label = classify_purchased_row(
            {"company_name": name, "company_type": "其他", "position": "招聘岗位"}
        )
        assert kind == "公考", name
        assert label in {"机关事业单位", "科研院所"}


def test_central_soe_institute_stays_qiuzhao_with_meaningful_label() -> None:
    assert classify_purchased_row({
        "company_name": "中船七一四所", "company_type": "其他", "position": "研发岗",
    }) == ("秋招", "央企下属单位")


def test_partition_routes_public_row_and_relabels_unknown() -> None:
    qiuzhao, gongkao, labels = partition_purchased_rows(
        [
            {"company_name": "南京大学", "company_type": "其他", "position": "行政岗", "source_record_id": "1", "announcement_url": "https://n.example/1"},
            {"company_name": "某机构", "company_type": "其他", "position": "产品岗", "source_record_id": "2"},
        ],
        source="wanqing_feishu",
    )
    assert len(gongkao) == 1
    assert gongkao[0]["extra"]["unit"] == "南京大学"
    assert gongkao[0]["extra"]["record_kind"] == "公考"
    assert qiuzhao[0]["company_type"] == "机构性质待核"
    assert labels == {"机关事业单位": 1, "机构性质待核": 1}
