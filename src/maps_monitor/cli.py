from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import json
import logging
import os
import sys
from pathlib import Path

from .config import Settings, load_settings
from .database import Database
from .engine import MonitorEngine
from .images import ImageArchive
from .operations import backup, export_reviews, refresh_dashboard_snapshot
from .telegram import TelegramSender


def _sender(settings: Settings, db: Database) -> TelegramSender:
    if not settings.telegram_token or not settings.telegram_chat_id:
        raise RuntimeError("缺少 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID")
    return TelegramSender(
        db, settings.telegram_token, settings.telegram_chat_id, settings.telegram_delay_seconds
    )


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # httpx INFO messages contain the complete request URL. Telegram embeds
    # the bot token in that URL, so these request logs must remain disabled.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


@contextmanager
def _process_lock(settings: Settings):
    lock_path = settings.data_dir.parent / "monitor.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+")
    try:
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError("另一輪監控正在執行") from exc
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError("另一輪監控正在執行") from exc
        yield
    finally:
        handle.close()


async def _run_and_send(settings: Settings, db: Database, dense_only: bool = False) -> int:
    engine = MonitorEngine(settings, db)
    caught: Exception | None = None
    try:
        successes, failures = await engine.run(dense_only=dense_only)
        logging.info("巡查完成：成功 %d，失敗 %d", successes, failures)
    except Exception as exc:
        caught = exc
        logging.exception("整輪監控失敗")
    sender = _sender(settings, db)
    sent, send_failures = await sender.send_pending()
    logging.info("Telegram：已送 %d，失敗 %d", sent, send_failures)
    if caught:
        raise caught
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Google Maps 個人貢獻者評論監控")
    parser.add_argument("--config", default="config/targets.yaml", help="YAML 設定檔")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="抓取與比對，但不傳送通知")
    sub.add_parser("send", help="傳送尚未處理的通知")
    sub.add_parser("run-and-send", help="抓取、比對並傳送通知")
    sub.add_parser("dense-run", help="只執行到期的密集日期巡查")
    sub.add_parser("dense-run-and-send", help="密集日期巡查並傳送通知")
    sub.add_parser("backup", help="建立 SQLite 與圖片硬連結快照")
    sub.add_parser("build-thumbnails", help="補建既有永久圖片的 WebP 縮圖")
    sub.add_parser("refresh-dashboard", help="重新產生唯讀儀表板資料庫快照")
    test = sub.add_parser("test-telegram", help="傳送 Telegram 測試訊息")
    test.add_argument("--message", default="Google Maps 評論監控測試成功")
    failure = sub.add_parser("notify-system-failure", help="由 systemd 失敗服務呼叫")
    failure.add_argument("--message", default="systemd 偵測到監控服務失敗")
    export = sub.add_parser("export", help="匯出評論")
    export.add_argument("--format", choices=("csv", "json"), required=True)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--target")
    sub.add_parser("status", help="輸出目前狀態 JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    args = build_parser().parse_args(argv)
    settings = load_settings(args.config)
    db = Database(settings.database)
    snapshot = settings.data_dir.parent / "web" / "monitor.sqlite3"
    try:
        if args.command == "run":
            with _process_lock(settings):
                asyncio.run(MonitorEngine(settings, db).run())
        elif args.command == "send":
            asyncio.run(_sender(settings, db).send_pending())
        elif args.command == "run-and-send":
            with _process_lock(settings):
                return asyncio.run(_run_and_send(settings, db))
        elif args.command == "dense-run":
            with _process_lock(settings):
                asyncio.run(MonitorEngine(settings, db).run(dense_only=True))
        elif args.command == "dense-run-and-send":
            with _process_lock(settings):
                return asyncio.run(_run_and_send(settings, db, dense_only=True))
        elif args.command == "backup":
            print(backup(db, settings))
        elif args.command == "build-thumbnails":
            archive = ImageArchive(
                settings.image_dir, settings.disk_min_free_gb, settings.disk_min_free_percent
            )
            built, failed = archive.build_missing_thumbnails(db)
            print(f"縮圖補建完成：新增 {built}，失敗 {failed}")
            return 1 if failed else 0
        elif args.command == "refresh-dashboard":
            pass
        elif args.command == "test-telegram":
            db.create_event("test", {"message": args.message})
            asyncio.run(_sender(settings, db).send_pending())
        elif args.command == "notify-system-failure":
            db.create_event("system_failure", {"error": args.message})
            asyncio.run(_sender(settings, db).send_pending())
        elif args.command == "export":
            count = export_reviews(db, args.output, args.format, args.target)
            print(f"已匯出 {count} 則評論至 {args.output}")
        elif args.command == "status":
            row = db.connection.execute(
                """SELECT COUNT(*) AS targets,
                (SELECT COUNT(*) FROM reviews) AS reviews,
                (SELECT COUNT(*) FROM images WHERE status='saved') AS images,
                (SELECT COUNT(*) FROM events WHERE delivery_state='pending') AS pending_events,
                (SELECT COUNT(*) FROM dense_targets WHERE status='scheduled') AS dense_targets,
                (SELECT COUNT(*) FROM reviews WHERE confidence IN ('confirmed_time','confirmed_date')) AS confirmed_dates,
                (SELECT COUNT(*) FROM date_models WHERE status='active') AS calibrated_models
                FROM targets WHERE enabled=1"""
            ).fetchone()
            print(json.dumps(dict(row), ensure_ascii=False, indent=2))
        return 0
    finally:
        active_error = sys.exc_info()[0] is not None
        try:
            refresh_dashboard_snapshot(db, snapshot)
        except Exception:
            if active_error:
                logging.exception("儀表板快照更新失敗")
            else:
                raise
        finally:
            db.close()


if __name__ == "__main__":
    sys.exit(main())
