from pathlib import Path
import asyncio

from maps_monitor.config import Settings, TargetConfig
from maps_monitor.database import Database
from maps_monitor.engine import MonitorEngine
from maps_monitor.models import CrawlResult, ScrapedReview


def settings(tmp_path: Path) -> Settings:
    for name in ("data", "images", "debug", "backups"):
        (tmp_path / name).mkdir()
    return Settings(
        root=tmp_path,
        data_dir=tmp_path / "data",
        database=tmp_path / "data" / "monitor.sqlite3",
        image_dir=tmp_path / "images",
        debug_dir=tmp_path / "debug",
        backup_dir=tmp_path / "backups",
        timezone="Asia/Taipei",
        locale="zh-TW",
        observation_hours=0,
        profile_delay_seconds=(0, 0),
        telegram_delay_seconds=(0, 0),
        delete_after_missing_runs=3,
        max_profile_minutes=45,
        disk_min_free_gb=0,
        disk_min_free_percent=0,
        targets=(TargetConfig("甲", "https://www.google.com/maps/contrib/123/reviews"),),
        telegram_token=None,
        telegram_chat_id=None,
    )


def review(text: str = "原文") -> ScrapedReview:
    return ScrapedReview(
        review_key="r1",
        google_review_id="r1",
        place_id="p1",
        place_name="店家",
        place_url="https://www.google.com/maps/place/x?cid=1",
        rating=5,
        text=text,
        relative_time="2 天前",
        image_urls=[],
    )


def test_lifecycle_baseline_modify_delete_restore(tmp_path):
    cfg = settings(tmp_path)
    db = Database(cfg.database)
    engine = MonitorEngine(cfg, db)
    target = db.sync_targets(cfg.targets, 0)[0]

    asyncio.run(engine.process_success(target, CrawlResult([review()], True, 1)))
    assert db.connection.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 1
    assert db.connection.execute("SELECT COUNT(*) FROM events WHERE event_type='new'").fetchone()[0] == 0

    target = db.connection.execute("SELECT * FROM targets WHERE id=?", (target["id"],)).fetchone()
    asyncio.run(engine.process_success(target, CrawlResult([review("修改後")], True, 1)))
    assert db.connection.execute("SELECT COUNT(*) FROM events WHERE event_type='modified'").fetchone()[0] == 1
    target = db.connection.execute("SELECT * FROM targets WHERE id=?", (target["id"],)).fetchone()
    asyncio.run(engine.process_success(target, CrawlResult([review("修改後")], True, 1)))
    assert db.connection.execute("SELECT COUNT(*) FROM events WHERE event_type='modified'").fetchone()[0] == 1

    for _ in range(3):
        target = db.connection.execute("SELECT * FROM targets WHERE id=?", (target["id"],)).fetchone()
        asyncio.run(engine.process_success(target, CrawlResult([], True, 1)))
    row = db.connection.execute("SELECT * FROM reviews").fetchone()
    assert row["status"] == "deleted"
    assert db.connection.execute("SELECT COUNT(*) FROM events WHERE event_type='deleted'").fetchone()[0] == 1
    target = db.connection.execute("SELECT * FROM targets WHERE id=?", (target["id"],)).fetchone()
    asyncio.run(engine.process_success(target, CrawlResult([], True, 1)))
    assert db.connection.execute("SELECT COUNT(*) FROM events WHERE event_type='deleted'").fetchone()[0] == 1

    target = db.connection.execute("SELECT * FROM targets WHERE id=?", (target["id"],)).fetchone()
    asyncio.run(engine.process_success(target, CrawlResult([review("修改後")], True, 1)))
    assert db.connection.execute("SELECT status FROM reviews").fetchone()[0] == "active"
    assert db.connection.execute("SELECT COUNT(*) FROM events WHERE event_type='restored'").fetchone()[0] == 1
    db.close()


def test_dense_success_does_not_increment_missing_count(tmp_path):
    cfg = settings(tmp_path)
    db = Database(cfg.database)
    engine = MonitorEngine(cfg, db)
    target = db.sync_targets(cfg.targets, 0)[0]
    asyncio.run(engine.process_success(target, CrawlResult([review()], True, 1)))
    target = db.connection.execute("SELECT * FROM targets WHERE id=?", (target["id"],)).fetchone()
    for _ in range(4):
        asyncio.run(engine.process_success(target, CrawlResult([], True, 1), count_missing=False))
    row = db.connection.execute("SELECT status,missing_count FROM reviews").fetchone()
    assert row["status"] == "active"
    assert row["missing_count"] == 0
    db.close()


def test_preexisting_edited_review_keeps_publish_date_unknown(tmp_path):
    cfg = settings(tmp_path)
    db = Database(cfg.database)
    engine = MonitorEngine(cfg, db)
    target = db.sync_targets(cfg.targets, 0)[0]
    edited = review("編輯後內容")
    edited.relative_time = "已更新 2 天前"
    edited.explicitly_edited = True
    asyncio.run(engine.process_success(target, CrawlResult([edited], True, 1)))
    row = db.connection.execute("SELECT * FROM reviews").fetchone()
    assert row["publish_date"] is None
    assert row["edit_date"] is not None
    assert row["time_subject"] == "last_edit"
    db.close()
