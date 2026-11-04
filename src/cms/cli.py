"""コマンドライン。cron からも手でも同じ入口を使う。"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from cms import article_queue, config, generate_retry, pipeline
from cms.ai import GeminiAccountConfig, GeminiGenerator
from cms.items_loader import load_items
from cms.publisher import StaticSitePublisher
from cms.repository import SQLiteArticleRepository
from cms.rss_scraper import RssScraper


def real_paths(root: str) -> tuple[str, str]:
    return str(Path(root, "data", "cms.db")), str(Path(root, "output"))


def demo_paths(root: str) -> tuple[str, str]:
    return str(Path(root, "data", "demo.db")), str(Path(root, "data", "demo-output"))


def build_publisher(name: str, mode: dict, db_path: str, output_root: str) -> StaticSitePublisher:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    return StaticSitePublisher(name, mode["site_title"], mode["base_url"], output_root, db_path)


def build_generator() -> GeminiGenerator:
    primary = GeminiAccountConfig(os.environ.get("GEMINI_PRIMARY_API_KEY", ""))
    key2 = os.environ.get("GEMINI_SECONDARY_API_KEY", "")
    return GeminiGenerator(
        primary, os.environ.get("GEMINI_MODEL") or "gemini-2.5-flash",
        secondary=GeminiAccountConfig(key2, label="secondary") if key2 else None,
    )


def _status_path(root: str) -> Path:
    return Path(root, "data", "status.json")


def load_status(root: str) -> dict:
    try:
        return json.loads(_status_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_status(root: str, name: str, state: str) -> None:
    all_status = load_status(root)
    entry = all_status.setdefault(name, {})
    entry["status"] = state
    if state == "running":
        entry["last_run_start"] = datetime.now().isoformat(timespec="seconds")
    path = _status_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(all_status, ensure_ascii=False, indent=2), encoding="utf-8")


def generate(name: str, mode: dict, root: str, db_path: str, generator, count: int = None) -> tuple[int, int]:
    prompt = config.load_prompt(mode, root)
    if mode["type"] == "rss":
        articles = RssScraper.from_config(mode).scrape_topics(limit=20)
        return pipeline.generate_from_articles(
            generator, articles, prompt, SQLiteArticleRepository(db_path),
            db_path, name, mode, count)
    return pipeline.generate_from_items(
        generator, load_items(mode, root), prompt, db_path, name, mode, count)


def cmd_generate(args) -> int:
    root = config.PROJECT_ROOT
    db_path, output_root = real_paths(root)
    mode = config.load_modes(root)[args.mode]
    build_publisher(args.mode, mode, db_path, output_root)

    count = None
    if args.retry:
        run, reason = generate_retry.should_retry(
            load_status(root).get(args.mode), generate_retry.queued_drafts(db_path, args.mode))
        print(f"再試行の判定: {reason}")
        if not run:
            return 0
        slots = generate_retry.remaining_slots(mode["publish_schedule"], datetime.now())
        if slots == 0:
            print("今日の公開枠が残っていないので作りません")
            return 0
        count = min(slots, mode["batch_count"]) if slots else None

    _write_status(root, args.mode, "running")
    try:
        made, failed = generate(args.mode, mode, root, db_path, build_generator(), count)
    except Exception:
        _write_status(root, args.mode, "error")
        raise
    # 1本も作れず失敗があった回だけをエラーとする。部分成功で再試行すると
    # 公開枠より多く作ってしまう。
    _write_status(root, args.mode, "error" if failed and not made else "success")
    print(f"{args.mode}: {made}本を生成、{failed}本が失敗")
    return 1 if failed and not made else 0


def cmd_publish(args) -> int:
    root = config.PROJECT_ROOT
    db_path, output_root = real_paths(root)
    mode = config.load_modes(root)[args.mode]
    path = pipeline.publish_next(build_publisher(args.mode, mode, db_path, output_root), mode)
    print(path or "公開する記事がありませんでした")
    return 0


def cmd_publish_due(args) -> int:
    root = config.PROJECT_ROOT
    db_path, output_root = real_paths(root)
    modes = config.load_modes(root)
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        items = article_queue.due(conn)
    for item in items:
        mode = modes.get(item["mode"])
        if mode is not None:
            pipeline.publish_or_trash(
                item, build_publisher(item["mode"], mode, db_path, output_root), mode)
    print(f"予約公開: {len(items)}件")
    return 0


def cmd_crontab(args) -> int:
    print("\n".join(crontab_lines(config.load_modes())))
    return 0


def crontab_lines(modes: dict) -> list[str]:
    lines = ["*/5 * * * * cms publish-due"]
    for name, mode in modes.items():
        lines.append(f"{mode['schedule']} cms generate {name}")
        for cron in generate_retry.retry_crons(mode["schedule"], mode["generate_retry_until"]):
            lines.append(f"{cron} cms generate {name} --retry")
        for cron in mode["publish_schedule"].splitlines():
            lines.append(f"{cron.strip()} cms publish {name}")
    return lines


def _serve(root: str, db_path: str, output_root: str, demo: bool, port: int) -> int:
    from cms.admin.app import create_app
    # 認証を持たないので、外から届くアドレスでは待ち受けない。
    create_app(root, db_path, output_root, demo=demo).run(host="127.0.0.1", port=port)
    return 0


def cmd_serve(args) -> int:
    root = config.PROJECT_ROOT
    return _serve(root, *real_paths(root), False, args.port)


def cmd_demo(args) -> int:
    from cms import demo
    root = config.PROJECT_ROOT
    db_path, output_root = demo_paths(root)
    demo.seed(root, db_path, output_root)
    print(f"デモデータを作りました。http://127.0.0.1:{args.port}/ を開いてください")
    return _serve(root, db_path, output_root, True, args.port)


def main(argv: list[str] = None) -> int:
    load_dotenv(Path(config.PROJECT_ROOT, ".env"))
    parser = argparse.ArgumentParser(prog="cms")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("generate", help="記事を生成してキューへ積む")
    p.add_argument("mode")
    p.add_argument("--retry", action="store_true", help="今日の生成が失敗していたときだけ作る")
    p.set_defaults(func=cmd_generate)

    p = sub.add_parser("publish", help="キューから1本公開する")
    p.add_argument("mode")
    p.set_defaults(func=cmd_publish)

    sub.add_parser("publish-due", help="時刻が来た予約を公開する").set_defaults(func=cmd_publish_due)
    sub.add_parser("crontab", help="crontab の行を出力する").set_defaults(func=cmd_crontab)

    for name, func, text in (("serve", cmd_serve, "管理UIを起動する"),
                             ("demo", cmd_demo, "架空データで管理UIを起動する")):
        p = sub.add_parser(name, help=text)
        p.add_argument("--port", type=int, default=5000)
        p.set_defaults(func=func)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
