from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class Item:
    source: str
    rank: int
    title: str
    title_zh: str
    url: str
    hot_value: str | None = None
    summary_zh: str | None = None
    thumbnail: str | None = None
    published_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    days_on_board: int = 1
    is_new: bool = True
    rank_delta: int | str = "new"
    cluster_id: str | None = None
    cluster_size: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
