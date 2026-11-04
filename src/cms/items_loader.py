"""お題リスト（CSV / JSON / TXT）を読み込む。

  .csv  — 1列目を使用、# で始まる行はコメント扱い
  .json — 文字列の配列 ["item1", "item2", ...]
  .txt  — 1行1アイテム、# で始まる行はコメント扱い
"""

import csv
import json
import os
from pathlib import Path

from cms.schema import Field

# list モードが使う設定項目の宣言（`schema.py` 参照）。
FIELDS = (
    Field("items_file", "str", required=True, label="お題ファイル",
          help="items/ 配下のファイル"),
)


def _safe_path(project_root: str, relative: str, allowed_dirs: tuple) -> Path:
    """project_root 配下の allowed_dirs 内に収まるパスのみ許可する。"""
    root = Path(project_root).resolve()
    resolved = (root / relative).resolve()
    for d in allowed_dirs:
        if resolved.is_relative_to(root / d):
            return resolved
    raise ValueError(f"アクセス拒否: 許可ディレクトリ外のパス {relative!r}")


def safe_project_path(project_root: str, relative: str) -> Path:
    """project_root 配下の items/ or prompts/ のみ許可する。"""
    return _safe_path(project_root, relative, ("items", "prompts"))


def load_items(mode: dict, project_root: str) -> list[str]:
    return load_from_file(str(safe_project_path(project_root, mode["items_file"])))


def load_from_file(path: str) -> list[str]:
    if not os.path.exists(path):
        raise FileNotFoundError(f"items_file が見つかりません: {path}")

    ext = os.path.splitext(path)[1].lower()

    if ext == ".json":
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            raise ValueError(f"{path}: JSON はリスト形式にしてください")
        return [str(x).strip() for x in data if str(x).strip()]

    if ext == ".csv":
        items = []
        with open(path, encoding="utf-8", newline="") as f:
            for row in csv.reader(f):
                if row and row[0].strip() and not row[0].strip().startswith("#"):
                    items.append(row[0].strip())
        return items

    with open(path, encoding="utf-8") as f:
        return [line.strip() for line in f
                if line.strip() and not line.strip().startswith("#")]
