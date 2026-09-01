from app.models import Item
from app.pipeline.cluster import cluster_items, normalize_title


def make(source: str, title: str) -> Item:
    return Item(source=source, rank=1, title=title, title_zh=title, url=f"https://example.com/{source}")


def test_cluster_cross_source_titles() -> None:
    items = [
        make("weibo", "#OpenAI发布GPT-6模型#"),
        make("youtube", "OpenAI 发布 GPT-6 模型"),
        make("github", "unrelated/repository"),
    ]
    count = cluster_items(items)
    assert count == 2
    assert items[0].cluster_id == items[1].cluster_id
    assert items[0].cluster_size == 2
    assert items[2].cluster_size == 1


def test_normalize_title_removes_topics_and_emoji() -> None:
    assert normalize_title("🔥 #今日 热点#！") == "今日 热点"
