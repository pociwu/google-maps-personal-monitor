from pathlib import Path
import asyncio
import json

from maps_monitor.config import Settings, TargetConfig
from maps_monitor.database import Database
from maps_monitor.engine import MonitorEngine
from maps_monitor.images import ImageSyncResult
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


def test_expanding_legacy_summary_to_full_text_is_silent(tmp_path):
    cfg = settings(tmp_path)
    db = Database(cfg.database)
    engine = MonitorEngine(cfg, db)
    target = db.sync_targets(cfg.targets, 0)[0]

    asyncio.run(
        engine.process_success(
            target,
            CrawlResult([review("好好吃，服務親切…更多")], True, 1),
        )
    )
    target = db.connection.execute(
        "SELECT * FROM targets WHERE id=?",
        (target["id"],),
    ).fetchone()
    asyncio.run(
        engine.process_success(
            target,
            CrawlResult(
                [review("好好吃，服務親切，而且環境乾淨，下次還會再來。")],
                True,
                1,
            ),
        )
    )

    row = db.connection.execute("SELECT body,modified_at FROM reviews").fetchone()
    modified_events = db.connection.execute(
        "SELECT COUNT(*) FROM events WHERE event_type='modified'"
    ).fetchone()[0]
    assert row["body"] == "好好吃，服務親切，而且環境乾淨，下次還會再來。"
    assert row["modified_at"] is None
    assert modified_events == 0
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


class _ImageResults:
    def __init__(self, *results: ImageSyncResult):
        self.results = iter(results)

    async def archive(self, _database, _review_id, _urls):
        return next(self.results)

    def has_capacity(self):
        return True


def test_only_unique_image_set_changes_create_modified_event(tmp_path):
    cfg = settings(tmp_path)
    db = Database(cfg.database)
    engine = MonitorEngine(cfg, db)
    engine.archive = _ImageResults(
        ImageSyncResult(("one",), 1, 1, 0, True),
        ImageSyncResult(("one",), 1, 0, 0, True),
        ImageSyncResult(("one", "two"), 2, 1, 0, True),
    )
    target = db.sync_targets(cfg.targets, 0)[0]
    initial = review()
    initial.image_urls = ["https://example.test/a"]
    asyncio.run(engine.process_success(target, CrawlResult([initial], True, 1)))

    target = db.connection.execute("SELECT * FROM targets WHERE id=?", (target["id"],)).fetchone()
    duplicate_urls = review()
    duplicate_urls.image_urls = ["https://example.test/a", "https://example.test/alias"]
    asyncio.run(engine.process_success(target, CrawlResult([duplicate_urls], True, 1)))
    assert db.connection.execute(
        "SELECT COUNT(*) FROM events WHERE event_type='modified'"
    ).fetchone()[0] == 0

    target = db.connection.execute("SELECT * FROM targets WHERE id=?", (target["id"],)).fetchone()
    new_unique_image = review()
    new_unique_image.image_urls = ["https://example.test/a", "https://example.test/new"]
    asyncio.run(engine.process_success(target, CrawlResult([new_unique_image], True, 1)))
    event = db.connection.execute(
        "SELECT payload_json FROM events WHERE event_type='modified'"
    ).fetchone()
    payload = json.loads(event["payload_json"])
    assert payload["image_added_count"] == 1
    assert payload["image_removed_count"] == 0
    assert payload["photo_count"] == 2
    db.close()
