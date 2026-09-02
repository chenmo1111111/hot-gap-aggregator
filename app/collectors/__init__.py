from .bilibili import BilibiliCollector
from .douyin import DouyinCollector
from .github import GitHubCollector
from .gongkao import GongkaoCollector
from .papers import PapersCollector
from .telegram import TelegramCollector
from .weibo import WeiboCollector
from .xiaohongshu import XiaohongshuCollector
from .youtube import YouTubeCollector

__all__ = [
    "BilibiliCollector", "DouyinCollector", "GitHubCollector", "GongkaoCollector", "PapersCollector",
    "TelegramCollector", "WeiboCollector", "XiaohongshuCollector", "YouTubeCollector",
]
