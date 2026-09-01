from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path

import yaml

from app.collectors.base import BaseCollector, SourceUnavailable
from app.models import Item


class YouTubeCollector(BaseCollector):
    source = "youtube"
    endpoint = "https://www.googleapis.com/youtube/v3/videos"

    @staticmethod
    def parse(payload: dict, region: str) -> list[Item]:
        items: list[Item] = []
        for index, row in enumerate(payload.get("items", []), 1):
            video_id = row.get("id")
            snippet = row.get("snippet", {})
            title = str(snippet.get("title") or "").strip()
            if not video_id or not title:
                continue
            thumbnails = snippet.get("thumbnails", {})
            thumbnail = (thumbnails.get("high") or thumbnails.get("medium") or thumbnails.get("default") or {}).get("url")
            items.append(Item(
                source="youtube",
                rank=index,
                title=title,
                title_zh=title,
                url=f"https://youtube.com/watch?v={video_id}",
                hot_value=str(row.get("statistics", {}).get("viewCount")) if row.get("statistics", {}).get("viewCount") is not None else None,
                thumbnail=thumbnail,
                published_at=snippet.get("publishedAt"),
                extra={
                    "video_id": video_id,
                    "region": region,
                    "channel": snippet.get("channelTitle"),
                    "description": str(snippet.get("description") or "")[:300],
                },
            ))
        return items

    @staticmethod
    def parse_invidious(payload: list[dict], region: str = "US") -> list[Item]:
        items: list[Item] = []
        for index, row in enumerate(payload, 1):
            video_id = row.get("videoId")
            title = str(row.get("title") or "").strip()
            if not video_id or not title:
                continue
            thumbnails = row.get("videoThumbnails") or []
            thumbnail = thumbnails[-1].get("url") if thumbnails else None
            published = row.get("published")
            items.append(Item(
                source="youtube", rank=index, title=title, title_zh=title,
                url=f"https://youtube.com/watch?v={video_id}",
                hot_value=str(row.get("viewCount")) if row.get("viewCount") is not None else None,
                thumbnail=thumbnail,
                published_at=datetime.fromtimestamp(int(published), UTC).isoformat() if published else None,
                extra={
                    "video_id": video_id, "region": region, "channel": row.get("author"),
                    "description": str(row.get("description") or "")[:300], "via": "invidious",
                },
            ))
        return items[:30]

    async def _fetch_region(self, region: str, key: str) -> list[Item]:
        response = await self.request(self.endpoint, params={
            "part": "snippet,statistics", "chart": "mostPopular", "regionCode": region,
            "maxResults": 30, "key": key,
        })
        return self.parse(response.json(), region)

    def load_invidious_instances(self) -> list[str]:
        override = os.getenv("INVIDIOUS_BASE", "").strip().rstrip("/")
        if override:
            return [override]
        path = Path(os.getenv("INVIDIOUS_INSTANCES_CONFIG", "config/invidious_instances.yaml"))
        if not path.exists():
            return []
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        return [str(base).strip().rstrip("/") for base in data if str(base).strip()]

    async def _fetch_invidious_instance(self, base: str) -> list[Item]:
        merged: list[Item] = []
        seen: set[str] = set()
        errors: list[str] = []
        for region in ("US", "JP", "GB"):
            try:
                response = await self.request(f"{base}/api/v1/trending", params={"region": region})
                for item in self.parse_invidious(response.json(), region):
                    video_id = str(item.extra["video_id"])
                    if video_id not in seen:
                        seen.add(video_id)
                        merged.append(item)
            except Exception as exc:
                errors.append(f"{region}: {exc}")
        if not merged:
            raise SourceUnavailable(f"{base} failed: {'; '.join(errors)}", status="degraded")
        for index, item in enumerate(merged, 1):
            item.rank = index
            item.extra["invidious_base"] = base
        return merged[:90]

    async def fetch(self) -> list[Item]:
        key = os.getenv("YOUTUBE_API_KEY")
        if not key:
            errors: list[str] = []
            for base in self.load_invidious_instances():
                try:
                    return await self._fetch_invidious_instance(base)
                except Exception as exc:
                    errors.append(str(exc))
            raise SourceUnavailable("All Invidious instances failed: " + "; ".join(errors), status="degraded")
        results = await asyncio.gather(
            self._fetch_region("US", key), self._fetch_region("JP", key), return_exceptions=True,
        )
        merged: list[Item] = []
        seen: set[str] = set()
        errors: list[str] = []
        for result in results:
            if isinstance(result, Exception):
                errors.append(str(result))
                continue
            for item in result:
                video_id = str(item.extra["video_id"])
                if video_id not in seen:
                    seen.add(video_id)
                    merged.append(item)
        if not merged:
            raise SourceUnavailable("; ".join(errors) or "YouTube returned no items", status="degraded")
        for index, item in enumerate(merged, 1):
            item.rank = index
        return merged[:60]
