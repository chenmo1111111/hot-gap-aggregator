from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from app.collectors.base import BaseCollector, SourceUnavailable
from app.collectors.gongkao_types import article_province, article_type, timeline_type
from app.models import Item


class GongkaoCollector(BaseCollector):
    source = "gongkao"
    article_endpoint = "https://hera-webapp.fenbi.com/api/website/article/hot/list/v3"
    timeline_endpoint = "https://market-api.fenbi.com/toolkit/api/v1/timeline/getTimeLineDetails"
    common_params = {"app": "web", "av": 100, "hav": 100, "kav": 100, "client_context_id": ""}

    @staticmethod
    def parse_articles(payload: dict) -> list[Item]:
        items: list[Item] = []
        for index, row in enumerate(payload.get("data", {}).get("articles", []), 1):
            article_id = row.get("id")
            title = str(row.get("title") or "").strip()
            if not article_id or not title:
                continue
            info = row.get("announcementArticleInfoRet") or {}
            raw_tags = [tag for tag in row.get("tagsList") or [] if isinstance(tag, dict)]
            tags = [str(tag.get("name")) for tag in raw_tags if tag.get("name")]
            items.append(Item(
                source="gongkao", rank=index, title=title, title_zh=title,
                url=f"https://hera-webapp.fenbi.com/api/website/article/detail?deviceType=3&id={article_id}&app=web&av=100&hav=100&kav=100&client_context_id=",
                hot_value=str(info.get("timeStatus")) if info.get("timeStatus") is not None else None,
                summary_zh=str(row.get("preface") or "").strip() or None,
                published_at=_timestamp(row.get("updateTime")),
                extra={
                    "id": article_id, "sub": "announcement", "tags": tags,
                    "province": article_province(raw_tags), "exam_type": article_type(raw_tags),
                    "startSignUpTime": info.get("enrollStartTime"), "endSignUpTime": info.get("enrollEndTime"),
                    "startWriteTime": info.get("writtenExamTime"),
                },
            ))
        return items

    @staticmethod
    def parse_timeline(payload: dict) -> list[Item]:
        rows = payload.get("datas") or payload.get("data", {}).get("datas") or []
        items: list[Item] = []
        for index, row in enumerate(rows, 1):
            event_id = row.get("id")
            title = str(row.get("topic") or "").strip()
            if not event_id or not title:
                continue
            start_signup = _date(row.get("startSignUpTime"))
            end_signup = _date(row.get("endSignUpTime"))
            write_time = _date(row.get("startWriteTime"))
            summary = f"（{start_signup or '待定'} 至 {end_signup or '待定'} 报名，{write_time or '待定'} 笔试）"
            type_code = row.get("examType") if row.get("examType") is not None else row.get("type")
            items.append(Item(
                source="gongkao", rank=index, title=title, title_zh=title,
                url=f"https://www.fenbi.com/page/kaoshidetail/{event_id}", summary_zh=summary,
                extra={
                    "id": event_id, "sub": "timeline", "startSignUpTime": row.get("startSignUpTime"),
                    "endSignUpTime": row.get("endSignUpTime"), "startWriteTime": row.get("startWriteTime"),
                    "province": row.get("province") or "全国", "type": row.get("type"),
                    "examType": row.get("examType"), "exam_type": timeline_type(type_code),
                },
            ))
        return items

    async def fetch(self) -> list[Item]:
        article_params = {**self.common_params, "offset": 0, "num": 50}
        timeline_params = {**self.common_params, "districtId": 0, "type": -1, "offset": 0, "size": 50}
        results = await asyncio.gather(
            self.request(self.article_endpoint, params=article_params),
            self.request(self.timeline_endpoint, params=timeline_params),
            return_exceptions=True,
        )
        items: list[Item] = []
        errors: list[str] = []
        if isinstance(results[0], Exception):
            errors.append(str(results[0]))
        else:
            items.extend(self.parse_articles(results[0].json()))
        if isinstance(results[1], Exception):
            errors.append(str(results[1]))
        else:
            items.extend(self.parse_timeline(results[1].json()))
        if not items:
            raise SourceUnavailable("; ".join(errors) or "Fenbi returned no items", status="degraded")
        for index, item in enumerate(items, 1):
            item.rank = index
        return items


def _timestamp(value: object) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) or str(value).isdigit():
        stamp = int(value)
        if stamp > 10_000_000_000:
            stamp //= 1000
        return datetime.fromtimestamp(stamp, UTC).isoformat()
    return str(value)


def _date(value: object) -> str | None:
    timestamp = _timestamp(value)
    return timestamp[:10] if timestamp else None
