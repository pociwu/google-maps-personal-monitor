from datetime import UTC, datetime, timedelta

from maps_monitor.config import TargetConfig
from maps_monitor.database import Database
from maps_monitor.date_service import _schedule_dense, due_dense_target_ids
from maps_monitor.dates import DateAssessment, ParsedRelative


def test_dense_window_is_scheduled_and_becomes_due(tmp_path, monkeypatch):
    db = Database(tmp_path / "data" / "monitor.sqlite3")
    target = db.sync_targets(
        (TargetConfig("甲", "https://www.google.com/maps/contrib/123/reviews"),), 0
    )[0]
    now = datetime(2026, 7, 17, 4, tzinfo=UTC)
    assessment = DateAssessment(
        estimate=now - timedelta(days=2),
        earliest=now - timedelta(days=2, minutes=20),
        latest=now - timedelta(days=2) + timedelta(minutes=20),
        precision="hour",
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
