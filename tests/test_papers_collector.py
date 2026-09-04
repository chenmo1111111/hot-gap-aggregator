import json
from datetime import date
from pathlib import Path

import pytest

from app.collectors.base import SourceUnavailable
from app.collectors.papers import PapersCollector
from app.models import Item
from app.pipeline.processor import process_items
from app.pipeline.translator import Translator
from app.store.database import Database

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def fixture_json(name: str) -> dict:
    return json.loads(fixture_text(name))


def test_arxiv_fixture_maps_atom_fields_and_dedupe_key() -> None:
    items = PapersCollector.parse_arxiv(fixture_text("papers_arxiv.xml"))
    assert len(items) == 2
    assert items[0].title.startswith("Agent-assisted")
    assert items[0].published_at == "2026-09-02T10:00:00Z"
    assert items[0].extra["field"] == "q-bio.GN"
    assert items[0].extra["doi"] == "10.1000/arxiv.demo"
    assert items[0].extra["mode"] == "all"
    assert items[0].extra["dedupe_key"] == "doi:10.1000/arxiv.demo"
    assert items[1].extra["dedupe_key"] == "arxiv:2609.00002"


def test_biorxiv_fixture_filters_categories() -> None:
    items = PapersCollector.parse_preprints(
        fixture_json("papers_biorxiv.json"), "biorxiv", {"bioinformatics", "developmental biology"},
    )
    assert [item.extra["field"] for item in items] == ["bioinformatics", "developmental biology"]
    assert items[0].url == "https://doi.org/10.1101/2026.09.02.111111"
    assert all(item.extra["subsource"] == "biorxiv" for item in items)
    assert all(item.extra["mode"] == "filter" for item in items)


def test_medrxiv_uses_same_json_mapping_without_biorxiv_filter() -> None:
    items = PapersCollector.parse_preprints(fixture_json("papers_biorxiv.json"), "medrxiv")
    assert len(items) == 3
    assert all(item.extra["subsource"] == "medrxiv" for item in items)


def test_pubmed_fixture_maps_articles_abstract_and_dates() -> None:
    assert PapersCollector.parse_pubmed_ids(fixture_json("papers_pubmed_search.json")) == ["11111111", "22222222"]
    items = PapersCollector.parse_pubmed(fixture_text("papers_pubmed.xml"))
    assert items[0].title == "Leiden refinement for single-cell clustering"
    assert "Cell clustering" in items[0].extra["description"]
    assert items[0].extra["journal"] == "Nature Methods"
    assert items[0].published_at == "2026-09-02"
    assert items[0].extra["dedupe_key"] == "doi:10.1038/demo-001"
    assert items[1].published_at == "2026-08-01"
    assert items[1].extra["dedupe_key"] == "pubmed:22222222"


def test_crossref_fixture_keeps_journal_articles_and_cleans_jats() -> None:
    items = PapersCollector.parse_crossref(fixture_json("papers_crossref.json"), "生物信息学", "1672-5565")
    assert len(items) == 2
    assert items[0].extra["tier"] == "中文核心"
    assert items[0].extra["description"] == "A benchmark for rare cell detection."
    assert items[0].published_at == "2026-09-01"
    assert items[1].extra["description"] == ""

    english = PapersCollector.parse_crossref(
        fixture_json("papers_crossref.json"), "BMC Bioinformatics", "1471-2105",
        tier="英文顶刊", mode="all",
    )
    assert english[0].extra["tier"] == "英文顶刊"
    assert english[0].extra["mode"] == "all"


@pytest.mark.asyncio
async def test_crossref_queries_each_issn_and_survives_one_failure(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "papers.yaml"
    config_path.write_text(
        "journals_by_issn:\n  - {name: 生物信息学, issn: '1672-5565', tier: 英文顶刊, mode: all}\n"
        "  - {name: 遗传, issn: '0253-9772'}\ncrossref_lookback_days: 45\n",
        encoding="utf-8",
    )
    collector = PapersCollector(config_path, today=date(2026, 9, 3))
    calls: list[dict] = []

    class Response:
        def json(self):
            return fixture_json("papers_crossref.json")

    async def request(_url, **kwargs):
        calls.append(kwargs["params"])
        if "0253-9772" in kwargs["params"]["filter"]:
            raise RuntimeError("one journal unavailable")
        return Response()

    monkeypatch.setenv("CROSSREF_MAILTO", "reader@example.test")
    monkeypatch.setattr(collector, "request", request)
    items = await collector._fetch_crossref(collector.load_config())
    assert len(items) == 2
    assert len(calls) == 2
    assert calls[0]["mailto"] == "reader@example.test"
    assert "from-pub-date:2026-07-20" in calls[0]["filter"]
    assert items[0].extra["tier"] == "英文顶刊"
    assert items[0].extra["mode"] == "all"


@pytest.mark.asyncio
async def test_pubmed_makes_search_then_fetch_and_passes_optional_key(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "papers.yaml"
    config_path.write_text(
        "pubmed_journals:\n  - {name: Nature, mode: all}\n"
        "  - {name: Nature Methods, mode: filter}\nlookback_days: 4\n",
        encoding="utf-8",
    )
    collector = PapersCollector(config_path, today=date(2026, 9, 2))
    calls: list[tuple[str, dict]] = []

    class Response:
        def __init__(self, *, text: str = "", payload: dict | None = None) -> None:
            self.text = text
            self.payload = payload

        def json(self) -> dict:
            assert self.payload is not None
            return self.payload

    async def fake_request(url: str, **kwargs):
        calls.append((url, kwargs["params"]))
        if "esearch" in url:
            return Response(payload=fixture_json("papers_pubmed_search.json"))
        return Response(text=fixture_text("papers_pubmed.xml"))

    monkeypatch.setenv("NCBI_API_KEY", "fixture-key")
    monkeypatch.setattr(collector, "request", fake_request)
    items = await collector._fetch_pubmed(collector.load_config())
    assert len(calls) == 2
    assert calls[0][1]["api_key"] == "fixture-key"
    assert '"Nature"[Journal] OR "Nature Methods"[Journal]' in calls[0][1]["term"]
    assert calls[1][1]["id"] == "11111111,22222222"
    assert len(items) == 2
    assert items[0].extra["mode"] == "filter"
    assert items[1].extra["mode"] == "all"


def test_keyword_matching_is_case_insensitive_and_mode_controls_filtering() -> None:
    config = {
        "priority_topics": [{"name": "单细胞聚类", "match": ["ScRNA-seq", "单细胞"]}],
        "keywords_boost": ["GNN", "图神经网络"],
    }
    matched = Item(
        source="papers", rank=0, title="GNN for 单细胞 analysis", title_zh="", url="https://example.test/1",
        extra={"description": "SCRNA-SEQ atlas", "mode": "filter"},
    )
    unmatched = Item(
        source="papers", rank=0, title="Unrelated chemistry", title_zh="", url="https://example.test/2",
        extra={"description": "No configured terms", "mode": "filter"},
    )
    broad_qbio = Item(
        source="papers", rank=0, title="General q-bio paper", title_zh="", url="https://example.test/3",
        extra={"description": "", "mode": "all"},
    )
    for item in (matched, unmatched, broad_qbio):
        PapersCollector._annotate_matches(item, config)

    assert matched.extra["topic_hit"] == ["单细胞聚类"]
    assert matched.extra["keyword_hit"] == ["GNN"]
    assert PapersCollector._keep_relevant(matched) is True
    assert PapersCollector._keep_relevant(unmatched) is False
    assert PapersCollector._keep_relevant(broad_qbio) is True


@pytest.mark.asyncio
async def test_fetch_keeps_working_when_one_subsource_fails_and_sorts_priorities(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "papers.yaml"
    config_path.write_text("""
priority_topics:
  - name: 单细胞聚类
    match: [single-cell clustering]
  - name: 稀有细胞
    match: [rare cell]
keywords_boost: [foundation model]
arxiv_categories: [cs.AI]
biorxiv: true
medrxiv: false
pubmed_journals: [Nature]
per_subsource_limit: 20
total_limit: 2
""", encoding="utf-8")
    collector = PapersCollector(config_path)
    rare = Item(source="papers", rank=0, title="Rare cell discovery", title_zh="", url="https://doi.org/rare", published_at="2026-09-02", extra={"subsource": "biorxiv", "description": "", "dedupe_key": "doi:rare"})
    priority = Item(source="papers", rank=0, title="Single-cell clustering", title_zh="", url="https://pubmed/1", published_at="2026-09-01", extra={"subsource": "pubmed", "description": "", "dedupe_key": "pubmed:1"})
    boosted = Item(source="papers", rank=0, title="Foundation model", title_zh="", url="https://pubmed/2", published_at="2026-09-03", extra={"subsource": "pubmed", "description": "", "dedupe_key": "pubmed:2"})

    async def failed_arxiv(_config):
        raise RuntimeError("arXiv unavailable")

    async def preprints(_server, _config):
        return [rare]

    async def pubmed(_config):
        return [boosted, priority]

    monkeypatch.setattr(collector, "_fetch_arxiv", failed_arxiv)
    monkeypatch.setattr(collector, "_fetch_preprints", preprints)
    monkeypatch.setattr(collector, "_fetch_pubmed", pubmed)
    items = await collector.fetch()
    assert [item.title for item in items] == ["Single-cell clustering", "Rare cell discovery"]
    assert [item.rank for item in items] == [1, 2]
    assert items[0].extra["topic_hit"] == ["单细胞聚类"]
    assert items[1].extra["priority_rank"] == 1


@pytest.mark.asyncio
async def test_fetch_degrades_only_when_every_enabled_subsource_fails(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "papers.yaml"
    config_path.write_text("arxiv_categories: [cs.AI]\nbiorxiv: true\nmedrxiv: false\n", encoding="utf-8")
    collector = PapersCollector(config_path)

    async def fail(*_args):
        raise RuntimeError("request with fixture-secret failed")

    monkeypatch.setenv("NCBI_API_KEY", "fixture-secret")
    monkeypatch.setattr(collector, "_fetch_arxiv", fail)
    monkeypatch.setattr(collector, "_fetch_preprints", fail)
    with pytest.raises(SourceUnavailable, match=r"All papers subsources failed") as captured:
        await collector.fetch()
    assert getattr(captured.value, "status", None) == "degraded"
    assert "fixture-secret" not in str(captured.value)
    assert "[redacted]" in str(captured.value)


class PaperTranslator(Translator):
    provider = "paper-test"

    async def batch_translate(self, texts: list[str]) -> list[str]:
        return [f"中文：{text}" for text in texts]


@pytest.mark.asyncio
async def test_processor_translates_paper_title_and_truncates_abstract(tmp_path) -> None:
    database = Database(tmp_path / "papers.db")
    translator = PaperTranslator(database)
    item = Item(
        source="papers", rank=1, title="A paper title", title_zh="", url="https://example.test/paper",
        extra={"description": "A" * 500},
    )
    await process_items([item], translator)
    assert item.title_zh == "中文：A paper title"
    assert item.summary_zh is not None and item.summary_zh.startswith("中文：")
    assert len(item.summary_zh) == 400
    database.close()
