"""モード定義（`modes/{name}.toml`）の読み込みと検証。

1サイト＝1モード。ファイルを1つ足すだけで別テーマのサイトを増やせる。
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from cms import items_loader
from cms.article_queue import PUBLISH_ORDERS
from cms.rss_scraper import RssScraper
from cms.schema import Field, validate

PROJECT_ROOT = str(Path(os.environ.get("CMS_ROOT") or Path(__file__).resolve().parents[2]))

COMMON_FIELDS = (
    Field("site_title", "str", required=True, label="サイト名"),
    Field("base_url", "str", required=True, label="公開URL"),
    Field("type", "str", required=True, label="データ取得の方式", choices=("rss", "list")),
    Field("prompt_file", "str", required=True, label="プロンプト"),
    Field("poster_name", "str", default="名無しさん", label="投稿者名"),
    Field("batch_count", "int", default=3, label="1回に作る本数"),
    Field("thread_max_posts", "int", default=90, label="レス数の上限"),
    Field("schedule", "str", required=True, label="生成の cron"),
    Field("publish_schedule", "str", required=True, label="公開の cron"),
    Field("generate_retry_until", "str", label="再試行の締め切り"),
    Field("publish_order", "str", default="fifo", label="公開順", choices=PUBLISH_ORDERS),
)

# データ取得の方式ごとの設定項目。各実装が持つ宣言をそのまま使う。
SOURCE_FIELDS = {
    "rss": RssScraper.FIELDS,
    "list": items_loader.FIELDS,
}


def validate_mode(data: dict) -> dict:
    fields = COMMON_FIELDS + SOURCE_FIELDS.get(data.get("type"), ())
    return validate(fields, data)


def load_modes(root: str = PROJECT_ROOT) -> dict[str, dict]:
    modes = {}
    for path in sorted(Path(root, "modes").glob("*.toml")):
        with open(path, "rb") as f:
            modes[path.stem] = validate_mode(tomllib.load(f))
    return modes


def load_prompt(mode: dict, root: str = PROJECT_ROOT) -> str:
    path = items_loader.safe_project_path(root, mode["prompt_file"])
    return path.read_text(encoding="utf-8")
