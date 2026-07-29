from __future__ import annotations

import json
import random
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .dates import (
    DateAssessment,
    DateEvidence,
    add_units,
    assess_date,
    parse_relative,
    transition_assessment,
)
from .models import ScrapedReview
from .util import iso_now, parse_iso, stable_hash


CONFIRMED = {"confirmed_time", "confirmed_date"}


def _models(con: sqlite3.Connection) -> dict[str, tuple[str, str]]:
    result: dict[str, tuple[str, str]] = {}
    for row in con.execute("SELECT * FROM date_models"):
        version = row["version"] if row["status"] == "active" else "uncalibrated"
        result[row["unit"]] = (row["model_name"], version)
    return result


def _evidence(con: sqlite3.Connection, review_id: int) -> list[DateEvidence]:
    rows = con.execute(
        """SELECT observed_at,relative_time,exact_timestamp,crawl_complete
        FROM observations WHERE review_id=? ORDER BY observed_at,id""",
        (review_id,),
    ).fetchall()
    return [
        DateEvidence(
            observed_at=parse_iso(row["observed_at"]),
            relative_time=row["relative_time"],
            exact_timestamp=parse_iso(row["exact_timestamp"]) if row["exact_timestamp"] else None,
            crawl_complete=bool(row["crawl_complete"]),
        )
        for row in rows
    ]


def _first_seen_window(review: sqlite3.Row | None) -> tuple[datetime, datetime] | None:
    if not review or review["basis"] != "first_seen_interval":
        return None
    if not review["publish_earliest"] or not review["publish_latest"]:
        return None
    return parse_iso(review["publish_earliest"]), parse_iso(review["publish_latest"])


def _date_event_needed(old: sqlite3.Row | None, assessment: DateAssessment, timezone: str) -> bool:
    if not old:
        return False
    if old["basis"] in {"legacy", "legacy_midday"}:
        return False
    old_date = old["edit_date"] if assessment.time_subject == "last_edit" else old["publish_date"]
    new_date = assessment.estimate_date(timezone)
    confidence_upgrade = old["confidence"] not in CONFIRMED and assessment.confidence in CONFIRMED
    return bool(
        old_date != new_date
        or confidence_upgrade
        or old["time_subject"] != assessment.time_subject
        or (old["date_model_version"] or "") != (assessment.model_version or "")
    )


def _insert_date_event(
    con: sqlite3.Connection,
    target: sqlite3.Row,
    review_id: int,
    place_name: str,
    assessment: DateAssessment,
    timezone: str,
    delivery_state: str,
) -> None:
    estimate = assessment.estimate.astimezone(ZoneInfo(timezone)).isoformat() if assessment.estimate else None
    payload = {
        "target_name": target["name"],
        "target_url": target["url"],
        "place_name": place_name,
        "publish_estimate": estimate,
        "publish_date": assessment.estimate_date(timezone),
        "publish_earliest": assessment.earliest.isoformat() if assessment.earliest else None,
        "publish_latest": assessment.latest.isoformat() if assessment.latest else None,
        "precision": assessment.precision,
        "confidence": assessment.confidence,
        "basis": assessment.basis,
        "time_subject": assessment.time_subject,
        "model_version": assessment.model_version,
    }
    if assessment.time_subject == "last_edit":
        payload["edit_date"] = payload.pop("publish_date")
    signature = stable_hash([
        review_id, payload.get("publish_date") or payload.get("edit_date"), assessment.confidence,
        assessment.time_subject, assessment.model_version,
    ])
    con.execute(
        """INSERT OR IGNORE INTO events
        (event_key,event_type,target_id,review_id,payload_json,delivery_state,created_at)
        VALUES(?, 'date_changed', ?, ?, ?, ?, ?)""",
        (f"date:{signature}", target["id"], review_id, json.dumps(payload, ensure_ascii=False), delivery_state, iso_now()),
    )


def record_and_assess(
    con: sqlite3.Connection,
    target: sqlite3.Row,
    review_id: int,
    scraped: ScrapedReview,
    content_hash: str,
    observed_at: datetime,
    timezone: str,
    old_review: sqlite3.Row | None,
    first_seen_window: tuple[datetime, datetime] | None,
    delivery_state: str,
) -> DateAssessment:
    parsed = parse_relative(scraped.relative_time)
    con.execute(
        """INSERT INTO observations
        (review_id,observed_at,relative_time,content_hash,parsed_count,parsed_unit,is_edit,
         exact_timestamp,crawl_complete)
        VALUES(?,?,?,?,?,?,?,?,1)""",
        (
            review_id, observed_at.isoformat(), scraped.relative_time, content_hash,
            parsed.count if parsed else None, parsed.unit if parsed else None,
            int(scraped.explicitly_edited or bool(parsed and parsed.is_edit)), scraped.exact_timestamp,
        ),
    )
    evidence = _evidence(con, review_id)
    preserved_window = first_seen_window or _first_seen_window(old_review)
    assessment = assess_date(evidence, timezone, _models(con), preserved_window)
    if _date_event_needed(old_review, assessment, timezone):
        _insert_date_event(
            con, target, review_id, scraped.place_name, assessment, timezone, delivery_state
        )
    estimate_date = assessment.estimate_date(timezone)
    if assessment.time_subject == "last_edit":
        con.execute(
            """UPDATE reviews SET edit_date=?,edit_estimate=?,edit_earliest=?,edit_latest=?,
            edit_confidence=?,edit_basis=?,precision=?,confidence=?,basis=?,time_subject=? WHERE id=?""",
            (
                estimate_date, assessment.estimate.isoformat() if assessment.estimate else None,
                assessment.earliest.isoformat() if assessment.earliest else None,
                assessment.latest.isoformat() if assessment.latest else None,
                assessment.confidence, assessment.basis, assessment.precision, assessment.confidence,
                assessment.basis, assessment.time_subject, review_id,
            ),
        )
    else:
        con.execute(
            """UPDATE reviews SET publish_date=?,publish_estimate=?,publish_earliest=?,publish_latest=?,
            precision=?,confidence=?,basis=?,time_subject=?,date_model_version=?,date_source=? WHERE id=?""",
            (
                estimate_date,
                assessment.estimate.isoformat() if assessment.estimate else None,
                assessment.earliest.isoformat() if assessment.earliest else None,
                assessment.latest.isoformat() if assessment.latest else None,
                assessment.precision, assessment.confidence, assessment.basis, assessment.time_subject,
                assessment.model_version, assessment.basis, review_id,
            ),
        )
    _update_calibration(con, review_id, old_review, evidence, timezone)
    _schedule_dense(con, target["id"], review_id, parsed, assessment, observed_at, _models(con))
    return assessment


def _update_calibration(
    con: sqlite3.Connection,
    review_id: int,
    old_review: sqlite3.Row | None,
    evidence: list[DateEvidence],
    timezone: str,
) -> None:
    if not old_review or old_review["basis"] != "first_seen_interval" or not old_review["publish_date"]:
        return
    known_date = old_review["publish_date"]
    for unit, candidates in {
        "month": ("calendar", "fixed:30.4375"),
        "year": ("calendar", "fixed:365.25"),
    }.items():
        current_model = con.execute("SELECT * FROM date_models WHERE unit=?", (unit,)).fetchone()
        for candidate in candidates:
            transition = transition_assessment(
                evidence, timezone, {unit: (candidate, "candidate")}
            )
            if not transition or transition.basis != f"{unit}_transition":
                continue
            transition_at = transition.latest.isoformat() if transition.latest else iso_now()
            agrees = int(transition.estimate_date(timezone) == known_date)
            con.execute(
                """INSERT OR IGNORE INTO date_model_samples
                (review_id,unit,transition_at,candidate_model,agrees,created_at)
                VALUES(?,?,?,?,?,?)""",
                (review_id, unit, transition_at, candidate, agrees, iso_now()),
            )
        counts = con.execute(
            """SELECT candidate_model,COUNT(DISTINCT review_id) AS samples
            FROM date_model_samples WHERE unit=?
            GROUP BY candidate_model
            HAVING SUM(CASE WHEN agrees=0 THEN 1 ELSE 0 END)=0
            ORDER BY samples DESC""",
            (unit,),
        ).fetchall()
        if current_model and current_model["status"] == "active":
            contradiction = con.execute(
                """SELECT COUNT(DISTINCT review_id) FROM date_model_samples
                WHERE unit=? AND candidate_model=? AND agrees=0""",
                (unit, current_model["model_name"]),
            ).fetchone()[0]
            if contradiction:
                con.execute(
                    "UPDATE date_models SET status='invalid',updated_at=? WHERE unit=?",
                    (iso_now(), unit),
                )
                con.execute(
                    """UPDATE reviews SET confidence='high_estimate'
                    WHERE date_model_version=? AND confidence='confirmed_date'""",
                    (current_model["version"],),
                )
                target = con.execute(
                    """SELECT t.id,t.name,t.url FROM reviews r JOIN targets t ON t.id=r.target_id
                    WHERE r.id=?""",
                    (review_id,),
                ).fetchone()
                con.execute(
                    """INSERT OR IGNORE INTO events
                    (event_key,event_type,target_id,review_id,payload_json,delivery_state,created_at)
                    VALUES(?, 'date_model_invalid', ?, ?, ?, 'pending', ?)""",
                    (
                        f"model-invalid:{unit}:{current_model['version']}", target["id"], review_id,
                        json.dumps({
                            "target_name": target["name"], "target_url": target["url"],
                            "model_unit": unit, "model_version": current_model["version"],
                        }, ensure_ascii=False), iso_now(),
                    ),
                )
                continue
        if counts and counts[0]["samples"] >= 3 and (len(counts) == 1 or counts[0]["samples"] > counts[1]["samples"]):
            model = counts[0]["candidate_model"]
            generation = 1
            if current_model and current_model["version"].startswith(f"{unit}-v"):
                try:
                    previous_generation = int(current_model["version"].split("-v", 1)[1].split("-", 1)[0])
                    generation = previous_generation if current_model["status"] == "active" else previous_generation + 1
                except (ValueError, IndexError):
                    generation = 1
            version = f"{unit}-v{generation}-{model.replace(':', '-')}"
            was_active = current_model and current_model["status"] == "active" and current_model["version"] == version
            con.execute(
                """UPDATE date_models SET model_name=?,version=?,status='active',
                consistent_samples=?,updated_at=? WHERE unit=?""",
                (model, version, counts[0]["samples"], iso_now(), unit),
            )
            if not was_active:
                target = con.execute(
                    """SELECT t.id,t.name,t.url FROM reviews r JOIN targets t ON t.id=r.target_id
                    WHERE r.id=?""",
                    (review_id,),
                ).fetchone()
                con.execute(
                    """INSERT OR IGNORE INTO events
                    (event_key,event_type,target_id,review_id,payload_json,delivery_state,created_at)
                    VALUES(?, 'date_model_calibrated', ?, ?, ?, 'pending', ?)""",
                    (
                        f"model-calibrated:{unit}:{version}", target["id"], review_id,
                        json.dumps({
                            "target_name": target["name"], "target_url": target["url"],
                            "model_unit": unit, "model_version": version,
                        }, ensure_ascii=False), iso_now(),
                    ),
                )


def _schedule_dense(
    con: sqlite3.Connection,
    target_id: int,
    review_id: int,
    parsed,
    assessment: DateAssessment,
    now: datetime,
    models: dict[str, tuple[str, str]],
) -> None:
    if not parsed or parsed.is_edit or parsed.unit not in {"day", "week", "month", "year"}:
        return
    if not assessment.earliest or not assessment.latest:
        return
    if assessment.basis.endswith("_transition"):
        con.execute(
            "DELETE FROM dense_targets WHERE target_id=? AND reason=?",
            (target_id, f"review:{review_id}:{parsed.unit}"),
        )
        return
    model = models.get(parsed.unit, ("calendar", "uncalibrated"))[0]
    earliest_transition = add_units(assessment.earliest, parsed.count + 1, parsed.unit, model)
    latest_transition = add_units(assessment.latest, parsed.count + 1, parsed.unit, model)
    window_start = earliest_transition - timedelta(hours=6)
    window_end = latest_transition + timedelta(hours=6)
    if window_end < now or window_start > now + timedelta(hours=6):
        return
    if window_end - window_start > timedelta(hours=48):
        return
    capped_end = min(window_end, window_start + timedelta(hours=48))
    next_check = max(now, window_start) + timedelta(minutes=random.randint(30, 60))
    con.execute(
        """INSERT INTO dense_targets
        (target_id,reason,window_start,window_end,next_check_at,started_at,status,updated_at)
        VALUES(?,?,?,?,?,?, 'scheduled', ?)
        ON CONFLICT(target_id) DO UPDATE SET reason=excluded.reason,
        window_start=excluded.window_start,window_end=excluded.window_end,
        next_check_at=excluded.next_check_at,status='scheduled',updated_at=excluded.updated_at""",
        (
            target_id, f"review:{review_id}:{parsed.unit}", window_start.isoformat(),
            capped_end.isoformat(), next_check.isoformat(), now.isoformat(), now.isoformat(),
        ),
    )


def due_dense_target_ids(con: sqlite3.Connection, now: datetime) -> set[int]:
    con.execute(
        "UPDATE dense_targets SET status='expired',updated_at=? WHERE window_end<? AND status='scheduled'",
        (now.isoformat(), now.isoformat()),
    )
    return {
        int(row["target_id"])
        for row in con.execute(
            """SELECT target_id FROM dense_targets
            WHERE status='scheduled' AND next_check_at<=? AND window_end>=?""",
            (now.isoformat(), now.isoformat()),
        )
    }


def advance_dense_target(con: sqlite3.Connection, target_id: int, now: datetime) -> None:
    con.execute(
        """UPDATE dense_targets SET next_check_at=?,status=CASE WHEN window_end<? THEN 'expired' ELSE status END,
        updated_at=? WHERE target_id=?""",
        (
            (now + timedelta(minutes=random.randint(30, 60))).isoformat(),
            now.isoformat(), now.isoformat(), target_id,
        ),
    )
