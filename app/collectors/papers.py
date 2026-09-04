from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from app.collectors.base import BaseCollector, SourceUnavailable
from app.models import Item

UTC = timezone.utc

LOGGER = logging.getLogger(__name__)
ATOM = "http://www.w3.org/2005/Atom"
ARXIV = "http://arxiv.org/schemas/atom"


def _text(node: ET.Element | None) -> str:
    return " ".join("".join(node.itertext()).split()) if node is not None else ""


def _normalise_doi(value: str | None) -> str:
    doi = (value or "").strip().lower()
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi)
    return doi


def _published_timestamp(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.timestamp()
    except ValueError:
        return 0.0


class PapersCollector(BaseCollector):
    source = "papers"
    timeout = 20.0
    arxiv_endpoint = "http://export.arxiv.org/api/query"
    biorxiv_endpoint = "https://api.biorxiv.org/details"
    pubmed_search_endpoint = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
    pubmed_fetch_endpoint = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
    crossref_endpoint = "https://api.crossref.org/works"

    def __init__(self, config_path: str | Path | None = None, *, today: date | None = None) -> None:
        self.config_path = Path(config_path or os.getenv("PAPERS_CONFIG", "config/papers.yaml"))
        self.today = today

    def load_config(self) -> dict[str, Any]:
        if not self.config_path.exists():
            raise SourceUnavailable(f"Papers config missing: {self.config_path}", status="degraded")
        config = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(config, dict):
            raise SourceUnavailable("Papers config must be a mapping", status="degraded")
        return config

    @staticmethod
    def parse_arxiv(xml_text: str) -> list[Item]:
        root = ET.fromstring(xml_text)
        items: list[Item] = []
        for entry in root.findall(f"{{{ATOM}}}entry"):
            title = _text(entry.find(f"{{{ATOM}}}title"))
            url = _text(entry.find(f"{{{ATOM}}}id"))
            if not title or not url:
                continue
            abstract = _text(entry.find(f"{{{ATOM}}}summary"))
            published_at = _text(entry.find(f"{{{ATOM}}}published")) or None
            category = entry.find(f"{{{ATOM}}}category")
            field = category.get("term") if category is not None else None
            doi = _normalise_doi(_text(entry.find(f"{{{ARXIV}}}doi"))) or None
            arxiv_id = re.sub(r"v\d+$", "", url.rstrip("/").rsplit("/", 1)[-1], flags=re.I)
            items.append(Item(
                source="papers", rank=0, title=title, title_zh=title, url=url,
                published_at=published_at,
                extra={
                    "subsource": "arxiv", "field": field, "doi": doi,
                    "tier": "预印本",
                    "mode": "all" if str(field or "").casefold().startswith("q-bio.") else "filter",
                    "description": abstract, "identifier": arxiv_id,
                    "dedupe_key": f"doi:{doi}" if doi else f"arxiv:{arxiv_id.lower()}",
                },
            ))
        return items

    @staticmethod
    def parse_preprints(payload: dict[str, Any], server: str, allowed_categories: set[str] | None = None) -> list[Item]:
        items: list[Item] = []
        allowed = {category.casefold() for category in allowed_categories or set()}
        for row in payload.get("collection", []):
            if not isinstance(row, dict):
                continue
            title = " ".join(str(row.get("title") or "").split())
            doi = _normalise_doi(str(row.get("doi") or ""))
            category = " ".join(str(row.get("category") or "").split())
            if not title or not doi:
                continue
            if server == "biorxiv" and allowed and category.casefold() not in allowed:
                continue
            abstract = " ".join(str(row.get("abstract") or "").split())
            items.append(Item(
                source="papers", rank=0, title=title, title_zh=title,
                url=f"https://doi.org/{doi}", published_at=str(row.get("date") or "") or None,
                extra={
                    "subsource": server, "field": category or None, "doi": doi,
                    "tier": "预印本",
                    # The category whitelist already removes unrelated bioRxiv
                    # papers. Topic and keyword matches control ranking, not survival.
                    "mode": "all" if server == "biorxiv" and allowed else "filter",
                    "description": abstract, "dedupe_key": f"doi:{doi}",
                },
            ))
        return items

    @staticmethod
    def parse_pubmed_ids(payload: dict[str, Any]) -> list[str]:
        result = payload.get("esearchresult", {})
        return [str(pmid) for pmid in result.get("idlist", []) if str(pmid).strip()]

    @staticmethod
    def parse_pubmed(xml_text: str) -> list[Item]:
        root = ET.fromstring(xml_text)
        items: list[Item] = []
        for record in root.findall(".//PubmedArticle"):
            citation = record.find("./MedlineCitation")
            article = citation.find("./Article") if citation is not None else None
            pmid = _text(citation.find("./PMID")) if citation is not None else ""
            title = _text(article.find("./ArticleTitle")) if article is not None else ""
            if not pmid or not title:
                continue
            abstract_nodes = article.findall("./Abstract/AbstractText") if article is not None else []
            abstract = " ".join(filter(None, (_text(node) for node in abstract_nodes)))
            journal = _text(article.find("./Journal/Title")) if article is not None else ""
            pub_date = article.find("./Journal/JournalIssue/PubDate") if article is not None else None
            published_at = PapersCollector._parse_pubmed_date(pub_date)
            doi_node = record.find("./PubmedData/ArticleIdList/ArticleId[@IdType='doi']")
            doi = _normalise_doi(_text(doi_node)) or None
            items.append(Item(
                source="papers", rank=0, title=title, title_zh=title,
                url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/", published_at=published_at,
                extra={
                    "subsource": "pubmed", "journal": journal or None, "doi": doi,
                    "tier": "英文顶刊",
                    "mode": "filter",
                    "description": abstract, "identifier": pmid,
                    "dedupe_key": f"doi:{doi}" if doi else f"pubmed:{pmid}",
                },
            ))
        return items

    @staticmethod
    def _plain_jats(value: str | None) -> str:
        if not value:
            return ""
        return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", value)).split())

    @staticmethod
    def _crossref_date(row: dict[str, Any]) -> str | None:
        for field in ("published", "published-online", "published-print", "issued"):
            parts = row.get(field, {}).get("date-parts", []) if isinstance(row.get(field), dict) else []
            if not parts or not isinstance(parts[0], list) or not parts[0]:
                continue
            values = [int(value) for value in parts[0][:3]]
            try:
                return date(values[0], values[1] if len(values) > 1 else 1, values[2] if len(values) > 2 else 1).isoformat()
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def parse_crossref(
        payload: dict[str, Any], configured_name: str, issn: str,
        *, tier: str = "中文核心", mode: str = "filter",
    ) -> list[Item]:
        items: list[Item] = []
        message = payload.get("message", {}) if isinstance(payload, dict) else {}
        for row in message.get("items", []) if isinstance(message, dict) else []:
            if not isinstance(row, dict) or row.get("type") != "journal-article":
                continue
            titles = row.get("title") or []
            title = " ".join(str(titles[0] if isinstance(titles, list) and titles else titles).split())
            doi = _normalise_doi(str(row.get("DOI") or ""))
            if not title or not doi:
                continue
            containers = row.get("container-title") or []
            journal = " ".join(str(containers[0] if isinstance(containers, list) and containers else configured_name).split())
            abstract = PapersCollector._plain_jats(str(row.get("abstract") or ""))
            items.append(Item(
                source="papers", rank=0, title=title, title_zh=title,
                url=f"https://doi.org/{doi}", published_at=PapersCollector._crossref_date(row),
                extra={
                    "subsource": "crossref", "tier": tier, "journal": journal or configured_name,
                    "mode": mode if mode == "all" else "filter", "issn": issn, "doi": doi,
                    "description": abstract, "dedupe_key": f"doi:{doi}",
                },
            ))
        return items

    @staticmethod
    def _parse_pubmed_date(node: ET.Element | None) -> str | None:
        if node is None:
            return None
        medline = _text(node.find("./MedlineDate"))
        year_text = _text(node.find("./Year"))
        month_text = _text(node.find("./Month"))
        day_text = _text(node.find("./Day"))
        if medline and not year_text:
            year_match = re.search(r"\b(19|20)\d{2}\b", medline)
            year_text = year_match.group(0) if year_match else ""
            month_match = re.search(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b", medline, re.I)
            month_text = month_match.group(0) if month_match else ""
        if not year_text:
            return None
        months = {name: index for index, name in enumerate(
            ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1
        )}
        try:
            month = int(month_text) if month_text.isdigit() else months.get(month_text[:3].casefold(), 1)
            day_value = int(day_text) if day_text.isdigit() else 1
            return date(int(year_text), month, day_value).isoformat()
        except ValueError:
            return year_text

    async def _fetch_arxiv(self, config: dict[str, Any]) -> list[Item]:
        items: list[Item] = []
        errors: list[str] = []
        for category in config.get("arxiv_categories", []):
            try:
                response = await self.request(self.arxiv_endpoint, params={
                    "search_query": f"cat:{category}", "sortBy": "submittedDate",
                    "sortOrder": "descending", "max_results": 30,
                }, headers={"Accept": "application/atom+xml"})
                items.extend(self.parse_arxiv(response.text))
            except Exception as exc:  # categories are isolated inside the arXiv subsource
                errors.append(f"{category}: {exc}")
        if not items and errors:
            raise SourceUnavailable("arXiv failed: " + "; ".join(errors), status="degraded")
        if errors:
            LOGGER.warning("papers arXiv partial failure: %s", "; ".join(errors))
        return items

    async def _fetch_preprints(self, server: str, config: dict[str, Any]) -> list[Item]:
        end = self.today or datetime.now(UTC).date()
        start = end - timedelta(days=max(0, int(config.get("lookback_days", 3))))
        response = await self.request(f"{self.biorxiv_endpoint}/{server}/{start.isoformat()}/{end.isoformat()}/0")
        categories = set(map(str, config.get("biorxiv_categories", []))) if server == "biorxiv" else None
        return self.parse_preprints(response.json(), server, categories)

    async def _fetch_pubmed(self, config: dict[str, Any]) -> list[Item]:
        journals: list[dict[str, str]] = []
        for row in config.get("pubmed_journals", []):
            if isinstance(row, dict):
                name = str(row.get("name") or "").strip()
                mode = "all" if str(row.get("mode") or "filter").casefold() == "all" else "filter"
            else:
                name, mode = str(row).strip(), "filter"
            if name:
                journals.append({"name": name, "mode": mode})
        if not journals:
            return []
        end = self.today or datetime.now(UTC).date()
        start = end - timedelta(days=max(0, int(config.get("lookback_days", 3))))
        journal_filter = " OR ".join(f'"{journal["name"]}"[Journal]' for journal in journals)
        api_key = str(os.getenv("NCBI_API_KEY") or config.get("NCBI_API_KEY") or "").strip()
        common = {"api_key": api_key} if api_key else {}
        search = await self.request(self.pubmed_search_endpoint, params={
            "db": "pubmed", "retmode": "json", "retmax": 40, "sort": "date",
            "term": f'({journal_filter}) AND ("{start.isoformat()}"[Date - Publication] : "3000"[Date - Publication])',
            **common,
        })
        ids = self.parse_pubmed_ids(search.json())
        if not ids:
            return []
        fetched = await self.request(self.pubmed_fetch_endpoint, params={
            "db": "pubmed", "retmode": "xml", "id": ",".join(ids), **common,
        }, headers={"Accept": "application/xml"})
        items = self.parse_pubmed(fetched.text)
        configured_modes = {
            re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", journal["name"].casefold()): journal["mode"]
            for journal in journals
        }
        for item in items:
            journal_key = re.sub(
                r"[^a-z0-9\u4e00-\u9fff]+", "", str(item.extra.get("journal") or "").casefold()
            )
            item.extra["mode"] = configured_modes.get(journal_key, "filter")
        return items

    async def _fetch_crossref(self, config: dict[str, Any]) -> list[Item]:
        journals = [row for row in config.get("journals_by_issn", []) if isinstance(row, dict)]
        if not journals:
            return []
        end = self.today or datetime.now(UTC).date()
        default_lookback = max(1, int(config.get("crossref_lookback_days", 45)))
        tier_lookbacks = config.get("crossref_lookback_days_by_tier", {})
        if not isinstance(tier_lookbacks, dict):
            tier_lookbacks = {}
        mailto = str(os.getenv("CROSSREF_MAILTO") or config.get("crossref_mailto") or "").strip()
        items: list[Item] = []
        errors: list[str] = []
        for journal in journals:
            name, issn = str(journal.get("name") or "").strip(), str(journal.get("issn") or "").strip()
            if not name or not issn:
                continue
            tier = str(journal.get("tier") or "中文核心").strip() or "中文核心"
            mode = "all" if str(journal.get("mode") or "filter").casefold() == "all" else "filter"
            lookback_days = max(1, int(journal.get("lookback_days") or tier_lookbacks.get(tier) or default_lookback))
            start = end - timedelta(days=lookback_days)
            params = {
                "filter": f"issn:{issn},from-pub-date:{start.isoformat()},until-pub-date:{end.isoformat()}",
                "sort": "published", "order": "desc", "rows": 40,
                "select": "title,DOI,published,abstract,container-title,type",
            }
            if mailto:
                params["mailto"] = mailto
            try:
                response = await self.request(self.crossref_endpoint, params=params, headers={"Accept": "application/json"})
                items.extend(self.parse_crossref(response.json(), name, issn, tier=tier, mode=mode))
            except Exception as exc:  # each journal is isolated from the rest of Crossref
                errors.append(f"{name}({issn}): {exc}")
        if not items and errors:
            raise SourceUnavailable("Crossref failed: " + "; ".join(errors), status="degraded")
        if errors:
            LOGGER.warning("papers Crossref partial failure: %s", "; ".join(errors))
        return items

    @staticmethod
    def _annotate_matches(item: Item, config: dict[str, Any]) -> None:
        haystack = f"{item.title} {item.extra.get('description') or ''}".casefold()
        topic_hits: list[str] = []
        priority_rank = 999
        for index, topic in enumerate(config.get("priority_topics", [])):
            if not isinstance(topic, dict):
                continue
            patterns = [str(pattern).strip() for pattern in topic.get("match", []) if str(pattern).strip()]
            if any(pattern.casefold() in haystack for pattern in patterns):
                topic_hits.append(str(topic.get("name") or f"topic-{index + 1}"))
                priority_rank = min(priority_rank, index)
        keyword_hits = [
            str(keyword) for keyword in config.get("keywords_boost", [])
            if str(keyword).strip() and str(keyword).casefold() in haystack
        ]
        item.extra["topic_hit"] = topic_hits
        item.extra["priority_rank"] = priority_rank
        item.extra["keyword_hit"] = keyword_hits

    @staticmethod
    def _sort_key(item: Item) -> tuple[int, int, float]:
        priority = int(item.extra.get("priority_rank", 999))
        keyword_rank = 0 if item.extra.get("keyword_hit") else 1
        return priority, keyword_rank, -_published_timestamp(item.published_at)

    @staticmethod
    def _keep_relevant(item: Item) -> bool:
        mode = "all" if str(item.extra.get("mode") or "filter").casefold() == "all" else "filter"
        item.extra["mode"] = mode
        return mode == "all" or bool(item.extra.get("topic_hit") or item.extra.get("keyword_hit"))

    @staticmethod
    def _safe_error(error: BaseException, config: dict[str, Any]) -> str:
        message = str(error)
        secrets = (os.getenv("NCBI_API_KEY"), config.get("NCBI_API_KEY"))
        for secret in secrets:
            if secret:
                message = message.replace(str(secret), "[redacted]")
        return message

    async def fetch(self) -> list[Item]:
        config = self.load_config()
        jobs: list[tuple[str, Any]] = []
        if config.get("arxiv_categories"):
            jobs.append(("arxiv", self._fetch_arxiv(config)))
        if config.get("biorxiv", False):
            jobs.append(("biorxiv", self._fetch_preprints("biorxiv", config)))
        if config.get("medrxiv", False):
            jobs.append(("medrxiv", self._fetch_preprints("medrxiv", config)))
        if config.get("pubmed_journals"):
            jobs.append(("pubmed", self._fetch_pubmed(config)))
        if config.get("journals_by_issn"):
            jobs.append(("crossref", self._fetch_crossref(config)))
        if not jobs:
            raise SourceUnavailable("Papers config has no enabled subsources", status="degraded")

        results = await asyncio.gather(*(job for _, job in jobs), return_exceptions=True)
        per_limit = max(1, int(config.get("per_subsource_limit", 15)))
        merged: list[Item] = []
        errors: list[str] = []
        successful_subsources = 0
        for (subsource, _), result in zip(jobs, results, strict=True):
            if isinstance(result, BaseException):
                errors.append(f"{subsource}: {self._safe_error(result, config)}")
                continue
            successful_subsources += 1
            relevant: list[Item] = []
            for item in result:
                self._annotate_matches(item, config)
                if self._keep_relevant(item):
                    relevant.append(item)
            if subsource == "crossref":
                # Crossref carries both English and Chinese journal tiers. A
                # single shared limit lets high-volume English journals evict
                # every Chinese-core result before the final tier selection.
                by_tier: dict[str, list[Item]] = {}
                for item in relevant:
                    by_tier.setdefault(str(item.extra.get("tier") or "英文顶刊"), []).append(item)
                for tier_items in by_tier.values():
                    merged.extend(sorted(tier_items, key=self._sort_key)[:per_limit])
            else:
                merged.extend(sorted(relevant, key=self._sort_key)[:per_limit])

        if successful_subsources == 0:
            raise SourceUnavailable("All papers subsources failed: " + "; ".join(errors), status="degraded")
        if errors:
            LOGGER.warning("papers partial failure: %s", "; ".join(errors))

        deduplicated: list[Item] = []
        seen: set[str] = set()
        for item in sorted(merged, key=self._sort_key):
            key = str(item.extra.get("dedupe_key") or item.url).casefold()
            if key in seen:
                continue
            seen.add(key)
            deduplicated.append(item)
        total_limit = max(1, int(config.get("total_limit", 45)))
        per_tier_limit = max(1, int(config.get("per_tier_limit", per_limit)))
        tier_order = ("英文顶刊", "中文核心", "预印本")
        selected: list[Item] = []
        selected_keys: set[str] = set()
        for tier in tier_order:
            tier_items = [item for item in deduplicated if str(item.extra.get("tier") or "英文顶刊") == tier]
            for item in tier_items[:per_tier_limit]:
                key = str(item.extra.get("dedupe_key") or item.url).casefold()
                if key not in selected_keys:
                    selected.append(item)
                    selected_keys.add(key)
        # Unknown tiers and overflow can fill unused capacity without taking
        # away the reserved space for the three visible frontend sections.
        for item in deduplicated:
            if len(selected) >= total_limit:
                break
            key = str(item.extra.get("dedupe_key") or item.url).casefold()
            if key not in selected_keys:
                selected.append(item)
                selected_keys.add(key)
        output = selected[:total_limit]
        for rank, item in enumerate(output, 1):
            item.rank = rank
        return output
