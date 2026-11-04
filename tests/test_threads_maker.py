"""AI の応答 → HTML の変換。崩れた出力は、実際に受け取った形を固定している。"""
import pytest

from cms.threads_maker import (NO_LIMIT, POST_CONTENT_RE, _linkify_anchors, post_number_step,
                               thread2html, thread_max_posts)

SAMPLE = "スレタイはこれ\n^\n最初のレスや\n^\n2番目のレスやで"


def test_returns_title_and_posts():
    title, html = thread2html(SAMPLE)
    assert title == "スレタイはこれ"
    assert len(POST_CONTENT_RE.findall(html)) == 2


def test_poster_name_is_escaped():
    _, html = thread2html(SAMPLE, poster_name="<b>x</b>")
    assert "<b>x</b>" not in html


def test_post_body_is_escaped():
    _, html = thread2html("t\n^\n<script>alert(1)</script>")
    assert "<script>" not in html


@pytest.mark.parametrize("text", [
    "タイトル\n1:^\nレス1\n2:^\nレス2",       # 番号付きの区切り
    "^\nタイトル\n^\nレス1\n^\nレス2",        # タイトルの前にも区切り
    "タイトル\n1: ^\nレス1\n2: ^\nレス2",     # 空白入りの区切り
])
def test_delimiter_variants(text):
    title, html = thread2html(text)
    assert title == "タイトル"
    assert len(POST_CONTENT_RE.findall(html)) == 2


def test_max_posts_cuts_extra():
    _, html = thread2html("t" + "\n^\nレス" * 5, max_posts=2)
    assert len(POST_CONTENT_RE.findall(html)) == 2


def test_no_limit_keeps_all():
    _, html = thread2html("t" + "\n^\nレス" * 100, max_posts=NO_LIMIT)
    assert len(POST_CONTENT_RE.findall(html)) == 100


def test_anchor_is_mapped_to_display_number():
    text = "t\n^\n1つ目\n^\n2つ目\n^\n>>2\nそれな"
    _, html = thread2html(text)
    second_id = html.split("id='res-")[2].split("'")[0]
    assert f'href="#res-{second_id}"' in html


def test_anchor_with_space_is_normalized():
    _, html = thread2html("t\n^\n1つ目\n^\n>> 1\nそれな")
    assert 'href="#res-1"' in html


def test_anchor_to_missing_post_is_not_linked():
    _, html = thread2html("t\n^\n1つ目\n^\n>>99\nそれな")
    assert "#res-99" not in html
    assert "&gt;&gt;99" in html


def test_lone_anchor_is_removed():
    assert _linkify_anchors("&gt;&gt;<br>本文", {1}) == "本文"


def test_number_step_is_stable():
    assert post_number_step("タイトル", 3) == post_number_step("タイトル", 3)
    assert all(1 <= post_number_step("タイトル", i) <= 4 for i in range(1, 50))


def test_thread_max_posts_default():
    assert thread_max_posts({}) == 90
    assert thread_max_posts({"thread_max_posts": 60}) == 60
