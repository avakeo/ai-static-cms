"""管理UI。記事キュー・実行ログ・スケジュールの3画面。

認証を持たないので、ローカルでの利用だけを想定する（`cli._serve` が
127.0.0.1 に固定している）。スケジュールは読み取り専用で、変更は
`modes/*.toml` を編集して行う。
"""

from __future__ import annotations

import sqlite3

from flask import Flask, abort, redirect, render_template, request, send_from_directory, url_for

from cms import article_queue, config, generate_retry, pipeline, run_log
from cms.publisher import StaticSitePublisher


def create_app(root: str, db_path: str, output_root: str, demo: bool = False) -> Flask:
    app = Flask(__name__)

    def modes() -> dict:
        return config.load_modes(root)

    def publisher_for(name: str) -> StaticSitePublisher:
        mode = modes()[name]
        return StaticSitePublisher(name, mode["site_title"], mode["base_url"], output_root, db_path)

    def connect() -> sqlite3.Connection:
        return sqlite3.connect(db_path)

    def back(endpoint: str = "queue_list", **kwargs):
        return redirect(url_for(endpoint, **{k: v for k, v in kwargs.items() if v}))

    @app.context_processor
    def inject():
        return {"demo": demo, "jst": article_queue.utc_to_jst,
                "flash": request.args.get("flash"), "error": request.args.get("error")}

    @app.route("/")
    def index():
        return redirect(url_for("queue_list"))

    # --- 記事キュー ---------------------------------------------------------

    @app.route("/queue")
    def queue_list():
        mode = request.args.get("mode", "").strip()
        state = request.args.get("state", "").strip()
        with connect() as conn:
            items = article_queue.list_items(conn, mode or None, state or None)
            summary = article_queue.counts(conn)
        return render_template("queue.html", items=items, summary=summary, mode_filter=mode,
                               state_filter=state, states=article_queue.STATES,
                               modes=list(modes()))

    @app.post("/queue/<int:item_id>/schedule")
    def queue_schedule(item_id: int):
        raw = request.form.get("publish_at", "").strip()
        try:
            publish_at = article_queue.jst_to_utc(raw)
        except ValueError:
            return back(error="日時の形式が正しくありません。")
        with connect() as conn:
            article_queue.schedule(conn, item_id, publish_at)
        return back(flash=f"#{item_id} の公開予定を{'設定' if raw else '解除'}しました。")

    @app.post("/queue/<int:item_id>/publish")
    def queue_publish(item_id: int):
        with connect() as conn:
            item = article_queue.get(conn, item_id)
        if item is None or item["mode"] not in modes():
            return back(error="対象が見つかりません。")
        try:
            pipeline.publish_item(item, publisher_for(item["mode"]), modes()[item["mode"]])
        except ValueError as e:
            run_log.record(db_path, item["mode"], "error", title=item["title"], error=str(e))
            return back(error=f"公開できませんでした: {e}")
        return back(flash=f"#{item_id} を公開しました。")

    @app.route("/queue/<int:item_id>/preview")
    def queue_preview(item_id: int):
        """公開後と同じ見た目で表示する。何も保存しない。"""
        with connect() as conn:
            item = article_queue.get(conn, item_id)
        if item is None or item["mode"] not in modes():
            abort(404)
        try:
            title, html = pipeline.render_checked(item["raw_text"], modes()[item["mode"]])
        except ValueError as e:
            return back(error=f"プレビューできません: {e}")
        prefix = url_for("site_file", mode=item["mode"], filename="")
        return publisher_for(item["mode"]).render_thread(title, html, prefix=prefix)

    @app.post("/queue/<int:item_id>/delete")
    def queue_delete(item_id: int):
        with connect() as conn:
            ok = article_queue.trash(conn, item_id)
        if not ok:
            return back(error="対象が見つかりません。")
        return back(flash=f"#{item_id} をゴミ箱へ移しました。")

    @app.route("/queue/trash")
    def queue_trash():
        with connect() as conn:
            items = article_queue.list_trashed(conn)
        return render_template("trash.html", items=items)

    @app.post("/queue/trash/<int:item_id>/restore")
    def queue_trash_restore(item_id: int):
        with connect() as conn:
            problem = article_queue.restore(conn, item_id)
        if problem:
            return back("queue_trash", error=problem)
        return back("queue_trash", flash=f"#{item_id} をキューへ戻しました。")

    @app.post("/queue/trash/<int:item_id>/purge")
    def queue_trash_purge(item_id: int):
        with connect() as conn:
            ok = article_queue.purge(conn, item_id)
        if not ok:
            return back("queue_trash", error="ゴミ箱に見つかりません。")
        return back("queue_trash", flash=f"#{item_id} を完全に削除しました。")

    # --- 実行ログ -----------------------------------------------------------

    @app.route("/logs")
    def post_logs():
        mode = request.args.get("mode", "").strip()
        status = request.args.get("status", "").strip()
        logs = run_log.list_logs(db_path, mode or None, status or None)
        names = list(modes())
        live = {(n, t["filename"]) for n in names for t in publisher_for(n).list_threads()}
        return render_template("logs.html", logs=logs, live=live, mode_filter=mode,
                               status_filter=status, statuses=run_log.STATUSES, modes=names)

    @app.post("/logs/unpublish")
    def unpublish():
        mode = request.form.get("mode", "").strip()
        filename = request.form.get("filename", "").strip()
        if mode not in modes() or not pipeline.unpublish(publisher_for(mode), filename):
            return back("post_logs", error="対象が見つかりません。")
        return back(flash="取り下げました。ここから再公開できます。", state="hidden")

    # --- スケジュール -------------------------------------------------------

    @app.route("/schedule")
    def schedule():
        rows = [{
            "name": name, "mode": mode,
            "retry": generate_retry.retry_crons(mode["schedule"], mode["generate_retry_until"]),
            "publish": mode["publish_schedule"].splitlines(),
        } for name, mode in modes().items()]
        return render_template("schedule.html", rows=rows)

    # --- 公開サイトの確認 ---------------------------------------------------

    @app.route("/site/<mode>/")
    @app.route("/site/<mode>/<path:filename>")
    def site_file(mode: str, filename: str = "index.html"):
        if mode not in modes():
            abort(404)
        return send_from_directory(f"{output_root}/{mode}", filename or "index.html")

    return app
