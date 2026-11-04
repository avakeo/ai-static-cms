import requests
import re
import time
from xml.etree.ElementTree import fromstring
from typing import List, Optional
from bs4 import BeautifulSoup
from cms.models import Article
from cms.schema import Field


ATOM_NS = "http://www.w3.org/2005/Atom"


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


class RssScraper:
    """
    RSSフィードから記事一覧を取得し、必要に応じて記事ページから本文を取得する。

    content_selector: CSSクラス名を指定すると各記事URLから本文を取得する。
                      Noneの場合はRSSのdescriptionをそのまま使う。
    """

    #: このSourceが使う設定項目の自己申告（`src/schema.py` 参照）。
    FIELDS = (
        Field(
            "rss_url", "str", required=True,
            label="RSS URL",
            help="記事一覧を取得するRSS/AtomフィードのURL",
        ),
        Field(
            "content_selector", "str",
            label="本文セレクタ（任意）",
            help="指定すると各記事URLから、このCSSクラスを含む要素の本文を取得する。"
                 "空欄ならRSSのdescription/summaryをそのまま使う",
        ),
        Field(
            "content_limit", "int", default=0,
            label="本文の文字数上限（任意）",
            help="0なら無制限",
        ),
    )

    @classmethod
    def from_config(cls, cfg: dict) -> "RssScraper":
        """検証済み設定から組み立てる。cfg のキーは FIELDS の宣言と一致する。"""
        return cls(
            cfg["rss_url"],
            content_selector=cfg["content_selector"] or None,
            content_limit=cfg["content_limit"],
        )

    def __init__(self, rss_url: str, content_selector: Optional[str] = None, content_limit: int = 0):
        self.rss_url = rss_url
        self.content_selector = content_selector
        self.content_limit = content_limit

    def scrape_topics(self, limit: int = 0) -> List[Article]:
        try:
            resp = requests.get(self.rss_url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
        except requests.exceptions.RequestException as e:
            print(f"エラー: {self.rss_url} にアクセスできませんでした - {e}")
            return []

        try:
            root = fromstring(resp.content)
        except Exception as e:
            print(f"エラー: RSSのパースに失敗しました - {e}")
            return []

        items = root.findall(".//item")
        if items:
            return self._process(items, mode="rss2", limit=limit)

        items = root.findall(f".//{{{ATOM_NS}}}entry")
        if items:
            return self._process(items, mode="atom", limit=limit)

        print("エラー: RSS/Atomのアイテムが見つかりませんでした")
        return []

    def _process(self, items, mode: str, limit: int = 0) -> List[Article]:
        articles = []
        for item in items:
            if limit and len(articles) >= limit:
                break
            if mode == "rss2":
                title = (item.findtext("title") or "").strip()
                url = (item.findtext("link") or "").strip()
                description = _strip_html(item.findtext("description") or "")
            else:
                title = (item.findtext(f"{{{ATOM_NS}}}title") or "").strip()
                link_el = item.find(f"{{{ATOM_NS}}}link")
                url = (link_el.get("href") if link_el is not None else "") or ""
                summary = _strip_html(item.findtext(f"{{{ATOM_NS}}}summary") or "")
                content_el = item.find(f"{{{ATOM_NS}}}content")
                content_raw = _strip_html(content_el.text or "" if content_el is not None else "")
                description = content_raw or summary

            if not title or not url:
                continue

            # content_selectorが設定されているか、RSSにdescriptionがない場合は記事ページから取得
            if self.content_selector or not description:
                fetched = self._fetch_content(url)
                content = fetched or description or title
            else:
                content = description

            if self.content_limit > 0:
                content = content[:self.content_limit]

            articles.append(Article(title=title, content=content, url=url))
            time.sleep(0.5)

        return articles

    def _fetch_content(self, url: str) -> str:
        try:
            resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            soup = BeautifulSoup(resp.content, "html.parser")

            if self.content_selector:
                el = soup.find(class_=re.compile(self.content_selector))
                if el:
                    return el.get_text(separator="\n").strip()

            # セレクタ未指定またはマッチしなかった場合: <article> or <main> からテキスト抽出
            for tag in ("article", "main"):
                el = soup.find(tag)
                if el:
                    return el.get_text(separator="\n").strip()[:1000]

            return ""
        except requests.exceptions.RequestException as e:
            print(f"本文取得失敗 ({url}): {e}")
            return ""
