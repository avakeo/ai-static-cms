"""AI の応答（区切り文字で分けたテキスト）を、スレッド形式の HTML へ変換する。

入力の約束:

    スレタイ
    ^
    1つ目のレス
    ^
    2つ目のレス

AI はこの約束を毎回は守らない。ここにある正規表現の緩さは、すべて実際に
受け取った崩れた出力に合わせたもの。
"""

import json
import re
from html import escape

# レス本文の `<p>` を拾う正規表現。**ここ1箇所に置く。**
# マークアップの形を知っているのは組み立てる側なので、読む側が各自で
# 正規表現を書かずにここを使う。
POST_CONTENT_RE = re.compile(r"<p class='post-content[^']*'>(.*?)</p>", re.DOTALL)

DEFAULT_POSTER_NAME = "名無しさん"

# 1スレッドに残すレスの上限の既定値。モデルが指示より多く返したときに切る。
DEFAULT_MAX_POSTS = 90

# 上限なしを表す値。
NO_LIMIT = 0


def thread_max_posts(mode: dict) -> int:
    """モード設定から上限を読む。**読み取りを1箇所にする。**

    公開・プレビューの経路が同じ値を使う必要がある。別々に書くと、片方だけ
    直したときに「プレビューでは出ていたレスが公開すると消える」になる。
    """
    return int((mode or {}).get("thread_max_posts") or DEFAULT_MAX_POSTS)


def post_number_step(title: str, index: int) -> int:
    """`index` 番目（1始まり）のレスの次に空ける番号の幅（1〜4）。

    掲示板らしく番号を飛ばすが、公開のたびに乱数を引くとプレビューと公開で
    番号がずれる。スレタイとレスの順番だけで決まるようにした。
    FNV-1a（32bit）なのは、JS でも同じ結果を数行で書けるため。
    """
    h = 0x811C9DC5
    for b in f"{title}\n{index}".encode("utf-8"):
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF
    return 1 + h % 4


def format_thread2json(thread: str, poster_name: str = DEFAULT_POSTER_NAME,
                       max_posts: int = DEFAULT_MAX_POSTS) -> str:
    # 区切りは `\n^` だが、モデルは次のように揺れる。いずれも実際に受け取った形。
    #
    # - `\n1:^` のように番号が付く（2026-08-02）。分割が一度もマッチせず、
    #   記事全文がタイトルになる壊れた記事ができていた
    # - タイトルの前にも `^` を書く（2026-08-07）
    # - `1: ^` のように間に空白が入る（2026-08-10）
    thread = re.sub(r"^[ \t]*\d*[ \t]*:?[ \t]*\^[ \t]*\n", "", thread, count=1)
    split_list = re.split(r"(\n[ \t]*\d*[ \t]*:?[ \t]*\^)", thread)

    thread_title = split_list[0].strip()
    thread_title = thread_title.replace("記事タイトル", "").strip()
    thread_res = split_list[1:]

    # Pass 1: AI が数えた連番 → 表示番号 の対応を確定する
    id_map: dict[int, int] = {}
    post_number = 1
    sequential = 1
    raw_posts: list[tuple[int, str]] = []

    # `NO_LIMIT`(0) と None は「切らない」。**`or DEFAULT` にしない** ——
    # 0 を偽として既定値に落とすと、上限なしを表現できなくなる。
    limit = DEFAULT_MAX_POSTS if max_posts is None else int(max_posts)
    for i in range(1, len(thread_res), 2):
        if limit > 0 and sequential > limit:
            break
        id_map[sequential] = post_number
        raw_posts.append((post_number, thread_res[i].strip()))
        post_number += post_number_step(thread_title, sequential)
        sequential += 1

    # Pass 2: 本文の >>N を表示番号へ付け替える。`>> 6` のように間に空白が
    # 入る書き方も拾い、出力は密着形に正規化する（2026-09-07）。
    anker_pat = re.compile(r">>[ \t　]*(\d+)")

    def _replace(m: re.Match) -> str:
        n = int(m.group(1))
        return f">>{id_map.get(n, n)}"

    posts = [
        {"id": display_id, "user": poster_name, "content": anker_pat.sub(_replace, content)}
        for display_id, content in raw_posts
    ]
    return json.dumps({"title": thread_title, "thread_res": posts}, ensure_ascii=False, indent=2)


_ANKER_HTML_PAT = re.compile(r"&gt;&gt;[ \t　]*(\d+)")

# 番号を伴わない `>>` だけのレス。飛び先が復元できないので、素の `>>` として
# 残すより消したほうが読みやすい。行頭に単独で現れるものだけを対象にする。
_LONE_ANKER_PAT = re.compile(r"(?:(?<=^)|(?<=<br>))&gt;&gt;(?![ \t　]*\d)[ \t　]*(?:<br>)?")


def _linkify_anchors(escaped_content: str, valid_ids: set[int] | None = None,
                     strip_lone: bool = True) -> str:
    """エスケープ済みテキスト内の &gt;&gt;N をページ内リンクに変換する。

    `valid_ids` に無い番号はリンクにせず、素のテキストのまま残す。
    AI がレス数を超える番号を書くことがあり、26,419本中73本が存在しない
    レスへのリンクになっていた（2026-08-22）。飛び先が無いリンクは、
    リンクでないより読者を混乱させる。
    """
    def _to_link(m: re.Match) -> str:
        n = m.group(1)
        if valid_ids is not None and int(n) not in valid_ids:
            return m.group(0)
        return f'<a href="#res-{n}" class="anker">&gt;&gt;{n}</a>'

    linked = _ANKER_HTML_PAT.sub(_to_link, escaped_content)
    return _LONE_ANKER_PAT.sub("", linked) if strip_lone else linked


def convert_json2html(json_data: str) -> tuple[str, str]:
    thread_data = json.loads(json_data)
    posts = thread_data["thread_res"]
    valid_ids = {int(p["id"]) for p in posts}

    html_output = ""
    for post in posts:
        content = _linkify_anchors(escape(post["content"]).replace("\n", "<br>"), valid_ids)
        user = escape(str(post["user"]))
        post_id = int(post["id"])
        html_output += f"""<div class='post-container' id='res-{post_id}'>
    <p class='id-line'>{post_id}：<span class='user-name'>{user}</span></p>
    <p class='post-content'>{content}</p>
</div>
"""
    return thread_data["title"], html_output


def thread2html(thread: str, poster_name: str = DEFAULT_POSTER_NAME,
                max_posts: int = DEFAULT_MAX_POSTS) -> tuple[str, str]:
    return convert_json2html(format_thread2json(thread, poster_name, max_posts))
