"""設定フィールドの宣言と検証。

各 Source が「自分が使うパラメータ」を `FIELDS` として自身のコード内で宣言し、
この宣言を以下の両方に使い回す:

- 管理UIのフォームを動的に生成する
- TOML を直接手編集したときの検証をする

実装と宣言が同じ場所にあるのでズレようがない。別ファイルに宣言を集約すると
手で同期させ続ける必要が生まれる、というのが不採用の理由。

入力の形が2通りあることに注意する。

- HTMLフォーム: 全部が文字列。チェックボックスは未チェックだとキー自体が来ない
- TOML: 既に型がついている（整数・真偽値・配列）

`coerce` はどちらも受け取れるようにしてある。
"""

from __future__ import annotations

from dataclasses import dataclass, field as _dc_field
from typing import Any, Literal, Sequence

FieldType = Literal["str", "int", "bool", "list"]

_TRUE = {"true", "1", "yes", "on"}
_FALSE = {"false", "0", "no", "off", ""}


class ValidationError(ValueError):
    """宣言に反する設定値。メッセージはそのまま画面に出せる日本語にする。"""


@dataclass(frozen=True)
class Field:
    """設定項目1つの宣言。

    name     TOML / フォームでのキー名
    type     str / int / bool / list
    required 未指定を許さないか（bool は既定値があるので実質 False 運用）
    default  未指定のときの値
    label    管理UIでの表示名（未指定なら name をそのまま出す）
    help     管理UIでの補足説明
    choices  値を限定する場合の候補。None なら制限なし
    """

    name: str
    type: FieldType
    required: bool = False
    default: Any = None
    label: str = ""
    help: str = ""
    choices: tuple[str, ...] | None = None

    def display_label(self) -> str:
        return self.label or self.name

    def coerce(self, value: Any) -> Any:
        """フォーム文字列 / TOML の値を、宣言された型に変換する。"""
        if value is None:
            return self.fallback()

        if self.type == "bool":
            return self._coerce_bool(value)
        if self.type == "int":
            return self._coerce_int(value)
        if self.type == "list":
            return self._coerce_list(value)
        return self._coerce_str(value)

    def fallback(self) -> Any:
        """未指定のときの値。default が無ければ型ごとの空値を返す。"""
        if self.default is not None:
            return self.default
        return {"str": "", "int": 0, "bool": False, "list": []}[self.type]

    # --- 型ごとの変換 ---------------------------------------------------

    def _coerce_bool(self, value: Any) -> bool:
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
        raise ValidationError(
            f"{self.display_label()}: 真偽値として解釈できません（{value!r}）"
        )

    def _coerce_int(self, value: Any) -> int:
        if isinstance(value, bool):  # bool は int のサブクラスなので先に弾く
            raise ValidationError(f"{self.display_label()}: 数値を指定してください")
        if isinstance(value, int):
            return value
        text = str(value).strip()
        if text == "":
            return self.fallback()
        try:
            return int(text)
        except ValueError:
            raise ValidationError(
                f"{self.display_label()}: 数値として解釈できません（{value!r}）"
            ) from None

    def _coerce_list(self, value: Any) -> list[str]:
        if isinstance(value, (list, tuple)):
            items = [str(v).strip() for v in value]
        else:
            # フォームの textarea は改行区切りで来る。# 始まりはコメント
            items = [line.strip() for line in str(value).splitlines()]
        return [x for x in items if x and not x.startswith("#")]

    def _coerce_str(self, value: Any) -> str:
        text = str(value).strip()
        if self.choices is not None and text and text not in self.choices:
            raise ValidationError(
                f"{self.display_label()}: '{text}' は選べません"
                f"（{' / '.join(self.choices)}）"
            )
        return text


def validate(
    fields: Sequence[Field],
    data: dict,
    *,
    reject_unknown: bool = True,
) -> dict:
    """宣言に従って data を検証・型変換した dict を返す。

    reject_unknown=True のとき、宣言に無いキーがあれば失敗させる。
    フォームのURL改ざんや、TOML の書き間違い（キー名のtypo）を検出するため。

    エラーは1件目で打ち切らず全部集めてから投げる。手編集したTOMLを直すとき、
    1個ずつ潰すより全部見えたほうが早いため。
    """
    errors: list[str] = []
    declared = {f.name: f for f in fields}

    if reject_unknown:
        for key in data:
            if key not in declared:
                near = _suggest(key, declared)
                hint = f"（もしかして: {near}）" if near else ""
                errors.append(f"知らない項目です: {key}{hint}")

    cleaned: dict[str, Any] = {}
    for name, f in declared.items():
        if name not in data or data[name] is None:
            if f.required:
                errors.append(f"{f.display_label()}: 必須項目です")
            cleaned[name] = f.fallback()
            continue
        try:
            value = f.coerce(data[name])
        except ValidationError as e:
            errors.append(str(e))
            continue
        if f.required and value in ("", [], None):
            errors.append(f"{f.display_label()}: 必須項目です")
        cleaned[name] = value

    if errors:
        raise ValidationError("\n".join(errors))
    return cleaned


def _suggest(key: str, declared: dict[str, Field]) -> str:
    """typo らしきキーに対して、近い宣言名を1つ返す。無ければ空文字。"""
    from difflib import get_close_matches

    hit = get_close_matches(key, list(declared), n=1, cutoff=0.7)
    return hit[0] if hit else ""
