from __future__ import annotations

import asyncio
import email.utils
import json
import logging
import random
import sqlite3
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from .config import Settings
from .crawler import ReadOnlyCrawler, _looks_truncated_text
from .database import Database
from .date_service import advance_dense_target, due_dense_target_ids, record_and_assess
from .dates import parse_relative
from .images import ImageArchive
from .models import CrawlResult, ScrapedReview
from .util import iso_now, parse_iso, stable_hash, utc_now


CONTENT_EVENTS = {"new", "modified", "deleted", "restored", "date_changed"}


def _is_full_text_upgrade(existing: sqlite3.Row, scraped: ScrapedReview) -> bool:
    return bool(
        _looks_truncated_text(existing["body"])
        and scraped.text.strip()
        and not _looks_truncated_text(scraped.text)
        and existing["place_name"] == scraped.place_name
        and existing["place_url"] == scraped.place_url
        and existing["rating"] == scraped.rating
    )


def _event_payload(target: sqlite3.Row, review: ScrapedReview | sqlite3.Row, event_type: str) -> dict:
    if isinstance(review, ScrapedReview):
        data = review.event_content() | {
            "review_key": review.review_key,
            "review_url": review.review_url,
            "relative_time": review.relative_time,
        }
    else:
        data = {
            "review_key": review["review_key"],
            "review_url": review["review_url"],
            "place_name": review["place_name"],
            "place_url": review["place_url"],
            "rating": review["rating"],
            "text": review["body"],
            "relative_time": review["relative_time"],
            "photo_count": review["photo_count"],
            "publish_date": review["publish_date"],
            "edit_date": review["edit_date"],
        }
    return data | {"event_type": event_type, "target_name": target["name"], "target_url": target["url"]}


def _delivery_state(target: sqlite3.Row, now: datetime, event_type: str) -> str:
    if event_type not in CONTENT_EVENTS:
        return "pending"
    until = target["observation_until"]
    return "suppressed" if until and now < parse_iso(until) else "pending"


def _insert_event(
    con: sqlite3.Connection,
    event_type: str,
    target_id: int | None,
    review_id: int | None,
    payload: dict,
    state: str,
) -> int:
    cursor = con.execute(
        """INSERT INTO events
        (event_key,event_type,target_id,review_id,payload_json,delivery_state,created_at)
        VALUES(?,?,?,?,?,?,?)""",
        (
            stable_hash([event_type, target_id, review_id, iso_now(), random.random()]),
            event_type,
            target_id,
            review_id,
            json.dumps(payload, ensure_ascii=False),
            state,
            iso_now(),
        ),
    )
    return int(cursor.lastrowid)


def _update_event_payload(
    con: sqlite3.Connection, event_id: int, additions: dict
) -> None:
    row = con.execute("SELECT payload_json FROM events WHERE id=?", (event_id,)).fetchone()
    if not row:
        return
    payload = json.loads(row["payload_json"])
    payload.update(additions)
    con.execute(
        "UPDATE events SET payload_json=? WHERE id=?",
        (json.dumps(payload, ensure_ascii=False), event_id),
    )


class MonitorEngine:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self.archive = ImageArchive(
            settings.image_dir, settings.disk_min_free_gb, settings.disk_min_free_percent
        )

    async def check_clock(self) -> None:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            try:
                response = await client.get("https://www.google.com/generate_204")
                header = response.headers.get("date")
                if not header:
                    return
                server_time = email.utils.parsedate_to_datetime(header).astimezone(UTC)
                skew = abs((utc_now() - server_time).total_seconds())
                if skew > 120:
                    raise RuntimeError(f"主機時間與網路時間相差 {skew:.0f} 秒")
            except httpx.HTTPError:
                return

    async def run(self, dense_only: bool = False) -> tuple[int, int]:
        await self.check_clock()
        targets = self.db.sync_targets(self.settings.targets, self.settings.observation_hours)
        if dense_only:
            due_ids = due_dense_target_ids(self.db.connection, utc_now())
            self.db.connection.commit()
            targets = [target for target in targets if int(target["id"]) in due_ids]
            if not targets:
                return 0, 0
        run_id = self.db.start_run()
        successes = 0
        failures = 0
        try:
            async with ReadOnlyCrawler(
                self.settings.locale,
                self.settings.timezone,
                self.settings.max_profile_minutes,
                self.settings.debug_dir,
            ) as crawler:
                for index, target in enumerate(targets):
                    if index:
                        await asyncio.sleep(random.randint(*self.settings.profile_delay_seconds))
                    try:
                        result = await crawler.crawl(target["name"], target["url"])
                        logging.info(
                            "對象 %s：本次抓取 %d 則評論（已捲動到底）",
                            target["name"], len(result.reviews),
                        )
                        await self.process_success(target, result, count_missing=not dense_only)
                        if dense_only:
                            advance_dense_target(self.db.connection, target["id"], utc_now())
                            self.db.connection.commit()
                        successes += 1
                    except Exception as exc:
                        self.process_failure(target, exc)
                        failures += 1
            if not dense_only:
                self._daily_health_summary(len(targets), successes, failures)
            self._prune_debug()
            status = "success" if failures == 0 else "partial"
            self.db.finish_run(run_id, status, successes, failures)
            return successes, failures
        except Exception as exc:
            self.db.create_event("system_failure", {"error": str(exc)}, event_key=None)
            self.db.finish_run(run_id, "failed", successes, failures, str(exc))
            raise

    async def process_success(
        self, target: sqlite3.Row, result: CrawlResult, count_missing: bool = True
    ) -> None:
        if not result.reached_end:
            raise RuntimeError("抓取結果不完整")
        now = utc_now()
        now_iso = now.isoformat()
        baseline = bool(target["baseline_complete"])
        seen_keys: set[str] = set()
        review_images: list[dict] = []
        with self.db.transaction() as con:
            for scraped in result.reviews:
                seen_keys.add(scraped.review_key)
                new_hash = stable_hash(scraped.event_content())
                existing = con.execute(
                    "SELECT * FROM reviews WHERE target_id=? AND review_key=?",
                    (target["id"], scraped.review_key),
                ).fetchone()
                if not existing:
                    previous_success = target["last_success_at"]
                    first_seen_window = None
                    if baseline and previous_success:
                        previous_time = parse_iso(previous_success)
                        first_seen_window = (previous_time, now)
                    cursor = con.execute(
                        """INSERT INTO reviews
                        (target_id,review_key,google_review_id,review_url,place_id,place_name,place_url,rating,body,
                         relative_time,photo_count,content_hash,first_seen_at,last_seen_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            target["id"], scraped.review_key, scraped.google_review_id,
                            scraped.review_url, scraped.place_id, scraped.place_name,
                            scraped.place_url, scraped.rating, scraped.text,
                            scraped.relative_time, 0, new_hash, now_iso, now_iso,
                        ),
                    )
                    review_id = int(cursor.lastrowid)
                    event_id = None
                    event_type = "new" if baseline else None
                    previous_last_seen = None
                    assessment = record_and_assess(
                        con, target, review_id, scraped, new_hash, now, self.settings.timezone,
                        None, first_seen_window, _delivery_state(target, now, "date_changed"),
                    )
                    if baseline:
                        event_id = _insert_event(
                            con, "new", target["id"], review_id,
                            _event_payload(target, scraped, "new")
                            | {"publish_date": assessment.estimate_date(self.settings.timezone)},
                            _delivery_state(target, now, "new"),
                        )
                else:
                    review_id = int(existing["id"])
                    previous_last_seen = existing["last_seen_at"]
                    event_type = None
                    event_id = None
                    edit_date = existing["edit_date"]
                    if existing["status"] == "deleted":
                        event_type = "restored"
                    elif (
                        existing["content_hash"] != new_hash
                        and not _is_full_text_upgrade(existing, scraped)
                    ):
                        event_type = "modified"
                        old_relative = parse_relative(existing["relative_time"])
                        new_relative = parse_relative(scraped.relative_time)
                        if new_relative and (
                            new_relative.is_edit
                            or (old_relative and new_relative.unit == old_relative.unit and new_relative.count < old_relative.count)
                        ):
                            scraped.explicitly_edited = True
                        midpoint = parse_iso(existing["last_seen_at"]) + (now - parse_iso(existing["last_seen_at"])) / 2
                        edit_date = midpoint.astimezone(ZoneInfo(self.settings.timezone)).date().isoformat()
                    assessment = record_and_assess(
                        con, target, review_id, scraped, new_hash, now, self.settings.timezone,
                        existing, None, _delivery_state(target, now, "date_changed"),
                    )
                    edit_date_update = None if assessment.time_subject == "last_edit" else edit_date
                    con.execute(
                        """UPDATE reviews SET google_review_id=?,review_url=COALESCE(?,review_url),
                        place_id=?,place_name=?,place_url=?,rating=?,body=?,
                        relative_time=?,edit_date=COALESCE(?,edit_date),photo_count=?,content_hash=?,status='active',
                        missing_count=0,last_seen_at=?,modified_at=CASE WHEN ?='modified' THEN ? ELSE modified_at END,
                        deleted_at=NULL WHERE id=?""",
                        (
                            scraped.google_review_id, scraped.review_url, scraped.place_id,
                            scraped.place_name, scraped.place_url,
                            scraped.rating, scraped.text, scraped.relative_time, edit_date_update,
                            existing["photo_count"], new_hash, now_iso, event_type, now_iso, review_id,
                        ),
                    )
                    if event_type and baseline:
                        event_id = _insert_event(
                            con, event_type, target["id"], review_id,
                            _event_payload(target, scraped, event_type)
                            | {
                                "publish_date": assessment.estimate_date(self.settings.timezone),
                                "edit_date": edit_date,
                            },
                            _delivery_state(target, now, event_type),
                        )
                review_images.append(
                    {
                        "review_id": review_id,
                        "urls": scraped.image_urls,
                        "scraped": scraped,
                        "existing": existing is not None,
                        "event_id": event_id,
                        "publish_date": assessment.estimate_date(self.settings.timezone),
                        "previous_last_seen": previous_last_seen,
                    }
                )

            active_rows = con.execute(
                "SELECT * FROM reviews WHERE target_id=? AND status='active'", (target["id"],)
            ).fetchall() if baseline and count_missing else []
            for row in active_rows:
                if row["review_key"] in seen_keys:
                    continue
                missing = int(row["missing_count"]) + 1
                if missing >= self.settings.delete_after_missing_runs:
                    con.execute(
                        "UPDATE reviews SET missing_count=?,status='deleted',deleted_at=? WHERE id=?",
                        (missing, now_iso, row["id"]),
                    )
                    _insert_event(
                        con, "deleted", target["id"], row["id"],
                        _event_payload(target, row, "deleted"),
                        _delivery_state(target, now, "deleted"),
                    )
                else:
                    con.execute("UPDATE reviews SET missing_count=? WHERE id=?", (missing, row["id"]))

            recovered = int(target["failure_alerted"]) == 1
            con.execute(
                """UPDATE targets SET baseline_complete=1,consecutive_failures=0,failure_alerted=0,
                last_success_at=?,updated_at=? WHERE id=?""",
                (now_iso, now_iso, target["id"]),
            )
            if recovered:
                _insert_event(
                    con, "target_recovered", target["id"], None,
                    {"target_name": target["name"], "target_url": target["url"]}, "pending",
                )
            summary_key = f"date-observation-summary:{target['id']}"
            observation_finished = target["observation_until"] and now >= parse_iso(target["observation_until"])
            summary_sent = con.execute("SELECT 1 FROM meta WHERE key=?", (summary_key,)).fetchone()
            if baseline and observation_finished and not summary_sent:
                counts = {
                    row["confidence"]: row["total"]
                    for row in con.execute(
                        "SELECT confidence,COUNT(*) AS total FROM reviews WHERE target_id=? GROUP BY confidence",
                        (target["id"],),
                    )
                }
                _insert_event(
                    con, "date_observation_summary", target["id"], None,
                    {
                        "target_name": target["name"], "target_url": target["url"],
                        "confirmed_count": counts.get("confirmed_time", 0) + counts.get("confirmed_date", 0),
                        "estimated_count": counts.get("high_estimate", 0) + counts.get("estimate", 0),
                        "unrecoverable_count": counts.get("unrecoverable", 0),
                    },
                    "pending",
                )
                con.execute("INSERT INTO meta(key,value) VALUES(?,?)", (summary_key, now_iso))

        saved_count = 0
        for job in review_images:
            image_result = await self.archive.archive(
                self.db, job["review_id"], job["urls"]
            )
            saved_count += len(image_result.saved_paths)
            self.db.connection.execute(
                "UPDATE reviews SET photo_count=? WHERE id=?",
                (image_result.current_count, job["review_id"]),
            )
            image_changed = bool(
                image_result.complete
                and (image_result.added_count or image_result.removed_count)
            )
            additions = {
                "photo_count": image_result.current_count,
                "image_added_count": image_result.added_count,
                "image_removed_count": image_result.removed_count,
            }
            if job["event_id"]:
                _update_event_payload(self.db.connection, job["event_id"], additions)
            elif baseline and job["existing"] and image_changed:
                previous_seen = parse_iso(job["previous_last_seen"])
                image_edit_date = (
                    previous_seen + (now - previous_seen) / 2
                ).astimezone(ZoneInfo(self.settings.timezone)).date().isoformat()
                self.db.connection.execute(
                    """UPDATE reviews SET modified_at=?,edit_date=?
                    WHERE id=?""",
                    (now_iso, image_edit_date, job["review_id"]),
                )
                _insert_event(
                    self.db.connection,
                    "modified",
                    target["id"],
                    job["review_id"],
                    _event_payload(target, job["scraped"], "modified")
                    | {
                        "publish_date": job["publish_date"],
                        "edit_date": image_edit_date,
                    }
                    | additions,
                    _delivery_state(target, now, "modified"),
                )
            self.db.connection.commit()
        self._update_disk_state()
        if not baseline:
            self.db.create_event(
                "baseline_summary",
                {
                    "target_name": target["name"], "target_url": target["url"],
                    "review_count": len(result.reviews), "saved_image_count": saved_count,
                    "observation_hours": self.settings.observation_hours,
                },
                target_id=target["id"],
                event_key=f"baseline:{target['id']}",
            )

    def process_failure(self, target: sqlite3.Row, exc: Exception) -> None:
        failures = int(target["consecutive_failures"]) + 1
        alerted = int(target["failure_alerted"])
        with self.db.transaction() as con:
            if failures >= 3 and not alerted:
                _insert_event(
                    con, "target_failure", target["id"], None,
                    {
                        "target_name": target["name"], "target_url": target["url"],
                        "consecutive_failures": failures, "error": str(exc),
                    },
                    "pending",
                )
                alerted = 1
            con.execute(
                "UPDATE targets SET consecutive_failures=?,failure_alerted=?,updated_at=? WHERE id=?",
                (failures, alerted, iso_now(), target["id"]),
            )

    def _update_disk_state(self) -> None:
        current = "ok" if self.archive.has_capacity() else "low"
        previous = self.db.get_meta("disk_state")
        if current != previous:
            if current == "low":
                self.db.create_event("disk_low", {"message": "磁碟空間不足，已暫停下載新圖片"})
            elif previous == "low":
                self.db.create_event("disk_recovered", {"message": "磁碟空間已恢復，將自動補抓圖片"})
            self.db.set_meta("disk_state", current)

    def _daily_health_summary(self, total: int, successes: int, failures: int) -> None:
        previous = self.db.get_meta("last_health_summary")
        now = utc_now()
        if previous and now - parse_iso(previous) < timedelta(hours=24):
            return
        self.db.create_event(
            "health_summary",
            {"target_count": total, "successes": successes, "failures": failures, "at": now.isoformat()},
            event_key=f"health:{now.date().isoformat()}",
        )
        self.db.set_meta("last_health_summary", now.isoformat())

    def _prune_debug(self) -> None:
        cutoff = utc_now().timestamp() - 30 * 86400
        for path in self.settings.debug_dir.iterdir():
            if path.is_dir() and path.stat().st_mtime < cutoff:
                for child in path.iterdir():
                    if child.is_file():
                        child.unlink(missing_ok=True)
                try:
                    path.rmdir()
                except OSError:
                    pass
