from __future__ import annotations

import hashlib
import re
from collections import defaultdict

import jieba
from rapidfuzz import fuzz

from app.models import Item

jieba.setLogLevel(30)


def normalize_title(title: str) -> str:
    without_topics = title.replace("#", "")
    words_only = re.sub(r"[^\w\u4e00-\u9fff]+", " ", without_topics, flags=re.UNICODE)
    return " ".join(words_only.lower().split())


def token_string(title: str) -> str:
    tokens = {token.strip() for token in jieba.lcut(normalize_title(title)) if token.strip()}
    return " ".join(sorted(tokens))


def cluster_items(items: list[Item]) -> int:
    if not items:
        return 0
    parent = list(range(len(items)))
    tokenized = [token_string(item.title_zh or item.title) for item in items]

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for left in range(len(items)):
        if not tokenized[left]:
            continue
        for right in range(left + 1, len(items)):
            if items[left].source == items[right].source or not tokenized[right]:
                continue
            if fuzz.token_set_ratio(tokenized[left], tokenized[right]) > 80:
                union(left, right)

    groups: dict[int, list[int]] = defaultdict(list)
    for index in range(len(items)):
        groups[find(index)].append(index)

    for indexes in groups.values():
        signature = "|".join(sorted(f"{items[index].source}:{normalize_title(items[index].title_zh)}" for index in indexes))
        cluster_id = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:12]
        source_count = len({items[index].source for index in indexes})
        for index in indexes:
            items[index].cluster_id = cluster_id
            items[index].cluster_size = source_count
    return len(groups)
