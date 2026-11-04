from dataclasses import dataclass


@dataclass(frozen=True)
class Article:
    """スクレイピングで取得した元記事（モード共通）"""

    title: str
    content: str
    url: str
