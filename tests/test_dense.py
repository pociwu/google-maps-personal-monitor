from datetime import UTC, datetime, timedelta

from maps_monitor.config import TargetConfig
from maps_monitor.database import Database
from maps_monitor.date_service import _schedule_dense, due_dense_target_ids
from maps_monitor.dates import DateAssessment, DateEvidence, ParsedRelative, assess_date


def test_dense_window_is_scheduled_and_becomes_due(tmp_path, monkeypatch):
    db = Database(tmp_path / "data" / "monitor.sqlite3")
    target = db.sync_targets(
        (TargetConfig("甲", "https://www.google.com/maps/contrib/123/reviews"),), 0
    )[0]
    now = datetime(2026, 7, 17, 4, tzinfo=UTC)
    assessment = DateAssessment(
        estimate=now - timedelta(days=2),
        earliest=now - timedelta(days=2, hours=2),
        latest=now - timedelta(days=2) + timedelta(hours=2),
        precision="date",
        confidence="high_estimate",
        basis="relative_window",
        time_subject="display_time",
    )
    _schedule_dense(
        db.connection, target["id"], 99, ParsedRelative(1, "day", False, "1 天前"),
        assessment, now, {},
    )
    row = db.connection.execute("SELECT * FROM dense_targets").fetchone()
    assert row is not None
    db.connection.execute(
        "UPDATE dense_targets SET next_check_at=?", ((now - timedelta(minutes=1)).isoformat(),)
    )
    assert due_dense_target_ids(db.connection, now) == {target["id"]}
    db.close()


def test_dense_observation_stops_after_interval_reaches_one_hour(tmp_path):
    db = Database(tmp_path / "data" / "monitor.sqlite3")
    target = db.sync_targets(
        (TargetConfig("甲", "https://www.google.com/maps/contrib/123/reviews"),), 0
    )[0]
    now = datetime(2026, 7, 17, 4, tzinfo=UTC)
    assessment = DateAssessment(
        estimate=now - timedelta(days=2),
        earliest=now - timedelta(days=2, minutes=30),
        latest=now - timedelta(days=2) + timedelta(minutes=30),
        precision="hour",
        confidence="confirmed_date",
        basis="day_transition",
        time_subject="publish_time",
    )

    _schedule_dense(
        db.connection,
        target["id"],
        99,
        ParsedRelative(2, "day", False, "2 天前"),
        assessment,
        now,
        {},
    )

    assert db.connection.execute("SELECT COUNT(*) FROM dense_targets").fetchone()[0] == 0
    db.close()


def test_wide_transition_window_does_not_trigger_dense_mode(tmp_path):
    db = Database(tmp_path / "data" / "monitor.sqlite3")
    target = db.sync_targets(
        (TargetConfig("甲", "https://www.google.com/maps/contrib/123/reviews"),), 0
    )[0]
    now = datetime(2026, 7, 17, 4, tzinfo=UTC)
    assessment = DateAssessment(
        estimate=now - timedelta(days=10),
        earliest=now - timedelta(days=14),
        latest=now - timedelta(days=7),
        precision="date",
        confidence="estimate",
        basis="relative_window",
        time_subject="display_time",
    )
    _schedule_dense(
        db.connection, target["id"], 99, ParsedRelative(1, "week", False, "1 週前"),
        assessment, now, {},
    )
    assert db.connection.execute("SELECT COUNT(*) FROM dense_targets").fetchone()[0] == 0
    db.close()


def test_wide_transition_continues_dense_observation_for_next_boundary(tmp_path):
    db = Database(tmp_path / "data" / "monitor.sqlite3")
    target = db.sync_targets(
        (TargetConfig("甲", "https://www.google.com/maps/contrib/123/reviews"),), 0
    )[0]
    now = datetime(2026, 7, 23, 4, tzinfo=UTC)
    assessment = DateAssessment(
        estimate=now - timedelta(weeks=3) + timedelta(hours=2),
        earliest=now - timedelta(weeks=3) + timedelta(hours=1),
        latest=now - timedelta(weeks=3) + timedelta(hours=3),
        precision="date",
        confidence="confirmed_date",
        basis="week_transition",
        time_subject="publish_time",
    )

    _schedule_dense(
        db.connection,
        target["id"],
        99,
        ParsedRelative(2, "week", False, "2 週前"),
        assessment,
        now,
        {},
    )

    row = db.connection.execute("SELECT * FROM dense_targets").fetchone()
    assert row is not None
    assert row["reason"] == "review:99:week"
    db.close()


def test_first_boundary_observation_stays_dense_until_second_confirmation(tmp_path):
    db = Database(tmp_path / "data" / "monitor.sqlite3")
    target = db.sync_targets(
        (TargetConfig("甲", "https://www.google.com/maps/contrib/123/reviews"),), 0
    )[0]
    old_seen = datetime(2026, 7, 11, 11, 30, tzinfo=UTC)
    first_new_seen = datetime(2026, 7, 11, 12, 10, tzinfo=UTC)
    evidence = [
        DateEvidence(old_seen, "1 天前"),
        DateEvidence(first_new_seen, "2 天前"),
    ]
    assessment = assess_date(evidence, "Asia/Taipei")
    assert assessment.basis != "day_transition"

    _schedule_dense(
        db.connection,
        target["id"],
        99,
        ParsedRelative(2, "day", False, "2 天前"),
        assessment,
        first_new_seen,
        {},
        evidence=evidence,
    )
    assert db.connection.execute("SELECT COUNT(*) FROM dense_targets").fetchone()[0] == 1

    confirm_seen = datetime(2026, 7, 11, 12, 35, tzinfo=UTC)
    evidence.append(DateEvidence(confirm_seen, "2 天前"))
    confirmed = assess_date(evidence, "Asia/Taipei")
    assert confirmed.basis == "day_transition"
    _schedule_dense(
        db.connection,
        target["id"],
        99,
        ParsedRelative(2, "day", False, "2 天前"),
        confirmed,
        confirm_seen,
        {},
        evidence=evidence,
    )
    assert db.connection.execute("SELECT COUNT(*) FROM dense_targets").fetchone()[0] == 0
    db.close()
