"""記事生成の共通パイプライン。

    取得（RSS / お題リスト）→ プロンプト整形 → AI 生成 → 検証 → キューへ保存
    キューから取り出し → HTML 変換 → 検証 → 静的ファイルへ公開 → ログ

生成と公開を分けているのは、AI の障害が公開を止めないようにするため。
在庫があれば、生成が落ちている間も公開枠は埋まる。
"""

from __future__ import annotations

import logging
import os
import random
import re
import sqlite3

from cms import article_queue, publisher as publisher_module, run_log
from cms.helper import get_similarity, remove_hiragana
from cms.threads_maker import (DEFAULT_POSTER_NAME, POST_CONTENT_RE, thread2html,
                               thread_max_posts)

logger = logging.getLogger(__name__)

# 公開に必要な最低レス数。**実測で決めた値**（2026-09-15）。公開済み 14,617 記事の
# レス数は 中央90 / 5%点80 / 1%点60 で、20を切るのは2件だけ（12レスと3レス。
# どちらも中身は正常）。よって3未満＝異常とみなす。上げすぎると短いだけの
# 正常な記事を落とすので、実測最小に合わせる。
MIN_THREAD_POSTS = 3

_TAG_RE = re.compile(r"<[^>]+>")


def filled_posts(html: str) -> list:
    """中身のあるレスだけを返す。画像だけのレスなどを本文と数えない。"""
    return [c for c in POST_CONTENT_RE.findall(html) if _TAG_RE.sub("", c).strip()]


def find_similar(title: str, recent_articles: list, threshold: float = 0.5) -> bool:
    """最近の記事に類似タイトルがあれば True を返す。

    ひらがなを除いて比べる。助詞や語尾の違いで別の話題と判定されないように。
    """
    stripped = remove_hiragana(title)
    return any(get_similarity(stripped, remove_hiragana(a.title)) > threshold
               for a in recent_articles)


def check_title(title: str, text: str) -> None:
    """生成結果のスレタイが記事として使えるかを確かめ、だめなら ValueError。"""
    # スレタイ行が無い・応答がほぼ空のまま「成功」扱いで公開されていた
    # （2026-08-07 に5件発見）。通常の生成失敗として扱い、ログに残す。
    if not title.strip():
        raise ValueError(
            "生成結果からタイトルを取得できませんでした"
            f"（応答が空、またはスレタイ行が欠落しています。冒頭: {text[:80]!r}）"
        )

    # 区切り文字の分割に失敗すると、記事全文がタイトルになる。個別の揺れは
    # threads_maker が吸収するが、未知の形式が来ても壊れた記事を公開しないよう、
    # 症状そのもの（タイトルに改行がある／異常に長い）をここで弾く。
    if "\n" in title or len(title) > 120:
        raise ValueError(
            "スレタイの抽出に失敗しました（区切り文字の解析に失敗し、本文が"
            f"タイトルに混入しています。title長={len(title)}, 冒頭: {title[:60]!r}）"
        )


def render_checked(text: str, mode: dict) -> tuple[str, str]:
    """生成テキストを HTML にし、記事として成立しているか確かめる。

    保存時と公開時の両方から呼ぶ。保存時に確かめていなかったころは、空の応答が
    在庫に入り、何日も後の公開枠で失敗していた（2026-10-05）。
    """
    title, html = thread2html(text, poster_name=mode.get("poster_name") or DEFAULT_POSTER_NAME,
                              max_posts=thread_max_posts(mode))
    check_title(title, text)

    # 上のチェックはタイトルしか見ていない。応答がスレタイ1行だけだと素通りして、
    # 読者には空白のページが見える（2026-09-15 の点検で4件発見）。
    filled = filled_posts(html)
    if len(filled) < MIN_THREAD_POSTS:
        raise ValueError(
            f"本文のレスが{len(filled)}件しかありません（最低{MIN_THREAD_POSTS}件必要）。"
            f"title={title[:40]!r}"
        )
    return title, html


# --- 生成（キューへ積む） -----------------------------------------------------

def store_draft(generator, db_path: str, mode_name: str, mode: dict, prompt: str,
                topic: str = "") -> int:
    """1本生成して検証し、キューへ積む。積んだ行の id を返す。"""
    text = generator.generate(prompt=prompt)
    render_checked(text, mode)
    with sqlite3.connect(db_path) as conn:
        return article_queue.add(conn, mode_name, text, topic=topic,
                                 used_account=generator.last_used_account,
                                 usage=generator.last_usage)


def _record_failure(db_path: str, mode_name: str, topic: str, exc: Exception) -> None:
    logger.warning("生成に失敗しました（%s）: %s", topic, exc)
    run_log.record(db_path, mode_name, "error", title=topic, error=str(exc))


def generate_from_articles(generator, articles: list, prompt_template: str, repo,
                           db_path: str, mode_name: str, mode: dict,
                           count: int = None) -> tuple[int, int]:
    """取得した記事のうち未掲載のものを生成してキューへ積む。`(成功, 失敗)` を返す。

    1本の失敗で残りを止めない。AI の応答の揺れは記事ごとに起きる。
    """
    limit = mode["batch_count"] if count is None else count
    recent = repo.get_recent_articles(limit=50, mode=mode_name)
    made = failed = 0
    for article in articles:
        if made >= limit:
            break
        if find_similar(article.title, recent):
            continue
        prompt = prompt_template.format(TITLE=article.title, CONTENT=article.content)
        try:
            store_draft(generator, db_path, mode_name, mode, prompt, topic=article.title)
        except Exception as e:  # noqa: BLE001 — 例外の種類を問わず次の記事へ進む
            _record_failure(db_path, mode_name, article.title, e)
            failed += 1
            continue
        repo.save_article(article, mode=mode_name)
        recent.insert(0, article)
        made += 1
    return made, failed


def used_topics(db_path: str, mode_name: str) -> set[str]:
    """既出のお題（キューの中と公開済み）。

    ゴミ箱の下書きは数えない。捨てた下書きはたいてい出来が悪いもので、
    お題ごと封じる理由が無い。
    """
    with sqlite3.connect(db_path) as conn:
        article_queue.ensure_table(conn)
        publisher_module.ensure_table(conn)
        rows = conn.execute(
            "SELECT topic FROM draft_threads WHERE mode = ?"
            " UNION SELECT topic FROM posted_threads WHERE mode = ?",
            (mode_name, mode_name)).fetchall()
    return {r[0] for r in rows if r[0]}


def generate_from_items(generator, items: list[str], prompt_template: str, db_path: str,
                        mode_name: str, mode: dict, count: int = None) -> tuple[int, int]:
    """お題リストの未使用分から生成してキューへ積む。`(成功, 失敗)` を返す。"""
    limit = mode["batch_count"] if count is None else count
    remaining = [i for i in items if i not in used_topics(db_path, mode_name)]
    random.shuffle(remaining)
    made = failed = 0
    for item in remaining:
        if made >= limit:
            break
        try:
            store_draft(generator, db_path, mode_name, mode,
                        prompt_template.format(ITEM=item), topic=item)
            made += 1
        except Exception as e:  # noqa: BLE001 — 例外の種類を問わず次のお題へ進む
            _record_failure(db_path, mode_name, item, e)
            failed += 1
    return made, failed


# --- 公開 --------------------------------------------------------------------

def publish_item(item: dict, publisher, mode: dict) -> str:
    """キューの1件を公開し、成功したら行を消す。保存先パスを返す。

    行を消すのは公開に成功してから。先に消すと、書き込みの失敗で本文が失われる。
    """
    title, html = render_checked(item["raw_text"], mode)
    path = publisher.publish_thread(title, html, item["raw_text"], topic=item["topic"],
                                    filename=item["origin_filename"])
    with sqlite3.connect(publisher.db_path) as conn:
        article_queue.remove(conn, item["id"])
    run_log.record(publisher.db_path, publisher.mode, "success", title=title,
                   filename=os.path.basename(path), used_account=item["used_account"],
                   usage=item["usage"])
    return path


def publish_or_trash(item: dict, publisher, mode: dict) -> str | None:
    """公開する。できない記事はゴミ箱へ移し、ログに残して None を返す。

    キューに残すと fifo の先頭に居座り、以後の公開枠がすべて同じ失敗になる。
    """
    try:
        return publish_item(item, publisher, mode)
    except ValueError as e:
        with sqlite3.connect(publisher.db_path) as conn:
            article_queue.trash(conn, item["id"])
        run_log.record(publisher.db_path, publisher.mode, "error", title=item["title"],
                       error=str(e))
        return None


def publish_next(publisher, mode: dict) -> str | None:
    """公開枠が来たら1本出す。在庫切れはスキップとして記録する。"""
    with sqlite3.connect(publisher.db_path) as conn:
        item = article_queue.next_item(conn, publisher.mode, mode.get("publish_order", "fifo"))
    if item is None:
        run_log.record(publisher.db_path, publisher.mode, "skipped", error="下書きがありません")
        return None
    return publish_or_trash(item, publisher, mode)


def unpublish(publisher, filename: str) -> bool:
    """公開中の記事を取り下げ、本文をキューへ戻す。

    先にキューへ積む。逆だと途中で落ちたときに本文が失われる。
    """
    thread = publisher.get_thread(filename)
    if thread is None:
        return False
    with sqlite3.connect(publisher.db_path) as conn:
        article_queue.add(conn, publisher.mode, thread["raw_text"], topic=thread["topic"],
                          state="hidden", origin_filename=filename)
    return publisher.remove_thread(filename)
