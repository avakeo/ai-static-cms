import re
from difflib import SequenceMatcher


def extract_hiragana(text: str) -> str:
    """文字列からひらがなだけを抽出して結合する"""
    if not text:
        return ""
    # [ぁ-ん] の範囲ですべてのひらがなを抽出
    hiragana = re.findall(r"[ぁ-ん]", text)
    return "".join(hiragana)


def remove_hiragana(text: str) -> str:
    """文字列からひらがなをすべて取り除く"""
    if not text:
        return ""
    return re.sub(r"[ぁ-ん]", "", text)


def get_similarity(a: str, b: str) -> float:
    """2つの文字列の類似度を 0.0 から 1.0 で返す"""
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()
