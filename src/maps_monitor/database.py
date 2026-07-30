from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from .config import TargetConfig
from .dates import parse_relative
from .util import iso_now, stable_hash, utc_now


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS targets (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1,
    baseline_complete INTEGER NOT NULL DEFAULT 0,
    observation_until TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    failure_alerted INTEGER NOT NULL DEFAULT 0,
    last_success_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    target_successes INTEGER NOT NULL DEFAULT 0,
    target_failures INTEGER NOT NULL DEFAULT 0,
    error TEXT
);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY,
    target_id INTEGER NOT NULL REFERENCES targets(id),
    review_key TEXT NOT NULL,
    google_review_id TEXT,
    review_url TEXT,
    place_id TEXT,
    place_name TEXT NOT NULL,
    place_url TEXT,
    rating REAL,
    body TEXT NOT NULL,
    relative_time TEXT NOT NULL,
    publish_date TEXT,
    date_source TEXT NOT NULL DEFAULT 'relative',
    legacy_publish_date TEXT,
    publish_estimate TEXT,
    publish_earliest TEXT,
    publish_latest TEXT,
    precision TEXT NOT NULL DEFAULT 'unknown',
    confidence TEXT NOT NULL DEFAULT 'estimate',
    basis TEXT NOT NULL DEFAULT 'legacy',
    time_subject TEXT NOT NULL DEFAULT 'display_time',
    date_model_version TEXT,
    edit_date TEXT,
    edit_estimate TEXT,
    edit_earliest TEXT,
    edit_latest TEXT,
    edit_confidence TEXT,
    edit_basis TEXT,
    photo_count INTEGER NOT NULL DEFAULT 0,
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    missing_count INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    modified_at TEXT,
    deleted_at TEXT,
    UNIQUE(target_id, review_key)
);
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY,
    review_id INTEGER NOT NULL REFERENCES reviews(id),
    observed_at TEXT NOT NULL,
    relative_time TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    parsed_count INTEGER,
    parsed_unit TEXT,
    is_edit INTEGER NOT NULL DEFAULT 0,
    exact_timestamp TEXT,
    crawl_complete INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_observations_review ON observations(review_id, observed_at);
CREATE TABLE IF NOT EXISTS images (
    id INTEGER PRIMARY KEY,
    review_id INTEGER NOT NULL REFERENCES reviews(id),
    source_url TEXT NOT NULL,
    sha256 TEXT,
    local_path TEXT,
    thumbnail_path TEXT,
    byte_size INTEGER,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT,
    is_current INTEGER NOT NULL DEFAULT 1,
    missing_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE(review_id, source_url)
);
CREATE INDEX IF NOT EXISTS idx_images_sha ON images(sha256);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    event_key TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    target_id INTEGER REFERENCES targets(id),
    review_id INTEGER REFERENCES reviews(id),
    payload_json TEXT NOT NULL,
    delivery_state TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    attempted_at TEXT,
    sent_at TEXT,
    telegram_message_id INTEGER,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_delivery ON events(delivery_state, created_at);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS date_models (
    unit TEXT PRIMARY KEY,
    model_name TEXT NOT NULL DEFAULT 'calendar',
    version TEXT NOT NULL DEFAULT 'uncalibrated',
    status TEXT NOT NULL DEFAULT 'calibrating',
    consistent_samples INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS date_model_samples (
    id INTEGER PRIMARY KEY,
    review_id INTEGER NOT NULL REFERENCES reviews(id),
    unit TEXT NOT NULL,
    transition_at TEXT NOT NULL,
    candidate_model TEXT NOT NULL,
    agrees INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(review_id,unit,transition_at,candidate_model)
);
CREATE TABLE IF NOT EXISTS dense_targets (
    target_id INTEGER PRIMARY KEY REFERENCES targets(id),
    reason TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    next_check_at TEXT NOT NULL,
    started_at TEXT,
    status TEXT NOT NULL DEFAULT 'scheduled',
    updated_at TEXT NOT NULL
);
"""

REVIEW_MIGRATION_COLUMNS = {
    "review_url": "TEXT",
    "date_source": "TEXT NOT NULL DEFAULT 'relative'",
    "legacy_publish_date": "TEXT",
    "publish_estimate": "TEXT",
    "publish_earliest": "TEXT",
    "publish_latest": "TEXT",
    "precision": "TEXT NOT NULL DEFAULT 'unknown'",
    "confidence": "TEXT NOT NULL DEFAULT 'estimate'",
    "basis": "TEXT NOT NULL DEFAULT 'legacy'",
    "time_subject": "TEXT NOT NULL DEFAULT 'display_time'",
    "date_model_version": "TEXT",
    "edit_estimate": "TEXT",
    "edit_earliest": "TEXT",
    "edit_latest": "TEXT",
    "edit_confidence": "TEXT",
    "edit_basis": "TEXT",
}

OBSERVATION_MIGRATION_COLUMNS = {
    "parsed_count": "INTEGER",
    "parsed_unit": "TEXT",
    "is_edit": "INTEGER NOT NULL DEFAULT 0",
    "exact_timestamp": "TEXT",
    "crawl_complete": "INTEGER NOT NULL DEFAULT 1",
}

IMAGE_MIGRATION_COLUMNS = {
    "thumbnail_path": "TEXT",
    "last_seen_at": "TEXT",
    "is_current": "INTEGER NOT NULL DEFAULT 1",
    "missing_count": "INTEGER NOT NULL DEFAULT 0",
}


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        probe = self.path.parent / ".write-test"
        try:
            probe.write_text("ok", encoding="ascii")
            probe.unlink()
        except OSError as exc:
            raise PermissionError(
                f"資料目錄不可寫：{self.path.parent}；請修正宿主機 state 目錄權限"
            ) from exc
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        try:
            self.connection.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError as exc:
            self.connection.close()
            raise PermissionError(
                f"SQLite 無法寫入：{self.path}；請確認資料庫檔與所在目錄均可寫"
            ) from exc
        existing_tables = {
            row[0] for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        old_review_columns = {
            row[1] for row in self.connection.execute("PRAGMA table_info(reviews)").fetchall()
        } if "reviews" in existing_tables else set()
        old_image_columns = {
            row[1] for row in self.connection.execute("PRAGMA table_info(images)").fetchall()
        } if "images" in existing_tables else set()
        existing_indexes = {
            row[0]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        migration_needed = bool(existing_tables) and (
            (bool(old_review_columns) and "publish_estimate" not in old_review_columns)
            or (bool(old_review_columns) and "review_url" not in old_review_columns)
            or (bool(old_image_columns) and "thumbnail_path" not in old_image_columns)
            or (bool(old_image_columns) and "is_current" not in old_image_columns)
            or (bool(old_image_columns) and "uq_images_review_sha" not in existing_indexes)
        )
        migration_backup = self._backup_before_migration() if migration_needed else None
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self.connection.executescript(SCHEMA)
            self._add_missing_columns("reviews", REVIEW_MIGRATION_COLUMNS)
            self._add_missing_columns("observations", OBSERVATION_MIGRATION_COLUMNS)
            self._add_missing_columns("images", IMAGE_MIGRATION_COLUMNS)
            self.connection.execute(
                """UPDATE images SET
                last_seen_at=COALESCE(last_seen_at,first_seen_at),
                is_current=COALESCE(is_current,1),
                missing_count=COALESCE(missing_count,0)"""
            )
            scanned, removed = self._deduplicate_image_references()
            rehashed = (
                self._rebuild_review_content_hashes(old_review_columns)
                if migration_needed else 0
            )
            self.connection.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS uq_images_review_sha
                ON images(review_id,sha256)
                WHERE status='saved' AND sha256 IS NOT NULL"""
            )
            self.connection.execute(
                """UPDATE reviews SET
                legacy_publish_date=COALESCE(legacy_publish_date,publish_date),
                publish_estimate=COALESCE(publish_estimate,publish_date || 'T12:00:00+08:00'),
                basis=CASE WHEN basis='legacy' THEN 'legacy_midday' ELSE basis END"""
            )
            now = iso_now()
            for unit in ("month", "year"):
                self.connection.execute(
                    """INSERT OR IGNORE INTO date_models
                    (unit,model_name,version,status,consistent_samples,updated_at)
                    VALUES(?, 'calendar', 'uncalibrated', 'calibrating', 0, ?)""",
                    (unit, now),
                )
            self._backfill_observation_parsing()
            self.connection.execute(
                "INSERT INTO meta(key,value) VALUES('schema_version','4') "
                "ON CONFLICT(key) DO UPDATE SET value='4'"
            )
            self.connection.commit()
            if migration_needed:
                logging.info(
                    "圖片去重遷移完成：掃描 %d 筆已保存圖片，刪除 %d 筆重複引用",
                    scanned,
                    removed,
                )
                logging.info(
                    "評論內容雜湊遷移完成：重建 %d 則，圖片網址數量不再觸發修改",
                    rehashed,
                )
        except Exception:
            self.connection.rollback()
            self.connection.close()
            if migration_backup:
                shutil.copy2(migration_backup, self.path)
                self.path.with_name(self.path.name + "-wal").unlink(missing_ok=True)
                self.path.with_name(self.path.name + "-shm").unlink(missing_ok=True)
            raise

    def _deduplicate_image_references(self) -> tuple[int, int]:
        scanned = int(
            self.connection.execute(
                """SELECT COUNT(*) FROM images
                WHERE status='saved' AND sha256 IS NOT NULL"""
            ).fetchone()[0]
        )
        duplicate_ids = [
            int(row[0])
            for row in self.connection.execute(
                """SELECT DISTINCT duplicate.id
                FROM images AS duplicate
                JOIN images AS keeper
                  ON keeper.review_id=duplicate.review_id
                 AND keeper.sha256=duplicate.sha256
                 AND keeper.status='saved'
                 AND keeper.id < duplicate.id
                WHERE duplicate.status='saved' AND duplicate.sha256 IS NOT NULL"""
            ).fetchall()
        ]
        if duplicate_ids:
            placeholders = ",".join("?" for _ in duplicate_ids)
            self.connection.execute(
                f"DELETE FROM images WHERE id IN ({placeholders})",
                duplicate_ids,
            )
        return scanned, len(duplicate_ids)

    def _rebuild_review_content_hashes(self, old_columns: set[str]) -> int:
        required = {"id", "place_name", "place_url", "rating", "body", "content_hash"}
        if not required.issubset(old_columns):
            return 0
        rows = self.connection.execute(
            "SELECT id,place_name,place_url,rating,body FROM reviews"
        ).fetchall()
        for row in rows:
            content_hash = stable_hash(
                {
                    "place_name": row["place_name"],
                    "place_url": row["place_url"],
                    "rating": row["rating"],
                    "text": row["body"],
                }
            )
            self.connection.execute(
                "UPDATE reviews SET content_hash=? WHERE id=?",
                (content_hash, row["id"]),
            )
        return len(rows)

    def _backup_before_migration(self) -> Path:
        backup_dir = self.path.parent.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        destination = backup_dir / f"pre-schema-v4-{datetime.now().strftime('%Y%m%d-%H%M%S')}.sqlite3"
        target = sqlite3.connect(destination)
        try:
            self.connection.backup(target)
        finally:
            target.close()
        return destination

    def _add_missing_columns(self, table: str, definitions: dict[str, str]) -> None:
        existing = {
            row[1] for row in self.connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for name, definition in definitions.items():
            if name not in existing:
                self.connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def _backfill_observation_parsing(self) -> None:
        rows = self.connection.execute(
            """SELECT id,relative_time FROM observations
            WHERE parsed_count IS NULL OR parsed_unit IS NULL"""
        ).fetchall()
        for row in rows:
            parsed = parse_relative(row["relative_time"])
            if parsed:
                self.connection.execute(
                    """UPDATE observations
                    SET parsed_count=?,parsed_unit=?,is_edit=CASE WHEN ? THEN 1 ELSE is_edit END
                    WHERE id=?""",
                    (parsed.count, parsed.unit, int(parsed.is_edit), row["id"]),
                )

    def close(self) -> None:
        self.connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            yield self.connection
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def sync_targets(self, targets: tuple[TargetConfig, ...], observation_hours: int) -> list[sqlite3.Row]:
        now = utc_now()
        configured_urls = {target.url for target in targets}
        with self.transaction() as con:
            for target in targets:
                existing = con.execute("SELECT * FROM targets WHERE url = ?", (target.url,)).fetchone()
                if existing:
                    reenabled = not bool(existing["enabled"]) and target.enabled
                    con.execute(
                        """UPDATE targets SET name=?,enabled=?,
                        baseline_complete=CASE WHEN ? THEN 0 ELSE baseline_complete END,
                        observation_until=CASE WHEN ? THEN ? ELSE observation_until END,
                        updated_at=? WHERE url=?""",
                        (
                            target.name, int(target.enabled), int(reenabled), int(reenabled),
                            now.isoformat(), now.isoformat(), target.url,
                        ),
                    )
                    if bool(existing["enabled"]) and not target.enabled:
                        con.execute(
                            """INSERT INTO events
                            (event_key,event_type,target_id,payload_json,delivery_state,created_at)
                            VALUES(?,?,?,?,?,?)""",
                            (
                                str(uuid.uuid4()), "target_disabled", existing["id"],
                                json.dumps({"target_name": target.name, "target_url": target.url}, ensure_ascii=False),
                                "pending", now.isoformat(),
                            ),
                        )
                else:
                    con.execute(
                        """INSERT INTO targets
                        (name,url,enabled,observation_until,created_at,updated_at)
                        VALUES (?,?,?,?,?,?)""",
                        (
                            target.name,
                            target.url,
                            int(target.enabled),
                            (now + timedelta(hours=observation_hours)).isoformat(),
                            now.isoformat(),
                            now.isoformat(),
                        ),
                    )
            rows = con.execute("SELECT id,url FROM targets").fetchall()
            for row in rows:
                if row["url"] not in configured_urls:
                    existing = con.execute("SELECT * FROM targets WHERE id=?", (row["id"],)).fetchone()
                    if existing["enabled"]:
                        con.execute(
                            "UPDATE targets SET enabled=0,updated_at=? WHERE id=?",
                            (now.isoformat(), row["id"]),
                        )
                        con.execute(
                            """INSERT INTO events
                            (event_key,event_type,target_id,payload_json,delivery_state,created_at)
                            VALUES(?,?,?,?,?,?)""",
                            (
                                str(uuid.uuid4()), "target_disabled", row["id"],
                                json.dumps(
                                    {"target_name": existing["name"], "target_url": existing["url"]},
                                    ensure_ascii=False,
                                ),
                                "pending", now.isoformat(),
                            ),
                        )
        return self.connection.execute("SELECT * FROM targets WHERE enabled=1 ORDER BY id").fetchall()

    def start_run(self) -> int:
        cursor = self.connection.execute(
            "INSERT INTO runs(started_at,status) VALUES (?, 'running')", (iso_now(),)
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def finish_run(self, run_id: int, status: str, successes: int, failures: int, error: str | None = None) -> None:
        self.connection.execute(
            """UPDATE runs SET finished_at=?,status=?,target_successes=?,target_failures=?,error=?
            WHERE id=?""",
            (iso_now(), status, successes, failures, error, run_id),
        )
        self.connection.commit()

    def create_event(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        target_id: int | None = None,
        review_id: int | None = None,
        delivery_state: str = "pending",
        event_key: str | None = None,
    ) -> int | None:
        key = event_key or str(uuid.uuid4())
        cursor = self.connection.execute(
            """INSERT OR IGNORE INTO events
            (event_key,event_type,target_id,review_id,payload_json,delivery_state,created_at)
            VALUES (?,?,?,?,?,?,?)""",
            (key, event_type, target_id, review_id, json.dumps(payload, ensure_ascii=False), delivery_state, iso_now()),
        )
        self.connection.commit()
        return int(cursor.lastrowid) if cursor.rowcount else None

    def get_pending_events(self) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM events WHERE delivery_state='pending' ORDER BY id"
        ).fetchall()

    def mark_event_attempted(self, event_id: int) -> None:
        self.connection.execute(
            """UPDATE events SET delivery_state='attempted', attempts=attempts+1,
            attempted_at=? WHERE id=? AND delivery_state='pending'""",
            (iso_now(), event_id),
        )
        self.connection.commit()

    def mark_event_sent(self, event_id: int, message_id: int | None) -> None:
        self.connection.execute(
            "UPDATE events SET delivery_state='sent',sent_at=?,telegram_message_id=?,last_error=NULL WHERE id=?",
            (iso_now(), message_id, event_id),
        )
        self.connection.commit()

    def mark_event_explicit_failure(self, event_id: int, error: str, retry: bool) -> None:
        state = "pending" if retry else "failed"
        self.connection.execute(
            "UPDATE events SET delivery_state=?,last_error=? WHERE id=?",
            (state, error[:2000], event_id),
        )
        self.connection.commit()

    def get_meta(self, key: str) -> str | None:
        row = self.connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.connection.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.connection.commit()
