from datetime import UTC, datetime, timedelta

from maps_monitor.date_service import _date_event_needed
from maps_monitor.dates import DateAssessment


OLD_REVIEW = {
    "basis": "relative_window",
    "publish_date": "2026-07-01",
    "edit_date": None,
    "confidence": "estimate",
    "time_subject": "publish_time",
    "date_model_version": None,
}


def _assessment(confidence: str, day: int) -> DateAssessment:
    estimate = datetime(2026, 7, day, 12, tzinfo=UTC)
    return DateAssessment(
        estimate=estimate,
        earliest=estimate,
        latest=estimate,
        precision="date",
        confidence=confidence,
        basis="day_transition",
        time_subject="publish_time",
    )


def test_date_update_notification_requires_confirmed_confidence():
    assert not _date_event_needed(
        OLD_REVIEW, _assessment("estimate", 2), "Asia/Taipei"
    )
    assert not _date_event_needed(
        OLD_REVIEW, _assessment("high_estimate", 2), "Asia/Taipei"
    )
    assert _date_event_needed(
        OLD_REVIEW, _assessment("confirmed_date", 2), "Asia/Taipei"
    )
    assert _date_event_needed(
        OLD_REVIEW, _assessment("confirmed_time", 2), "Asia/Taipei"
    )


def test_date_update_notifies_when_confirmed_interval_first_reaches_one_hour():
    old = OLD_REVIEW | {
        "basis": "day_transition",
        "publish_date": "2026-07-02",
        "confidence": "confirmed_date",
        "publish_earliest": "2026-07-02T08:00:00+00:00",
        "publish_latest": "2026-07-02T12:00:00+00:00",
    }
    estimate = datetime(2026, 7, 2, 10, tzinfo=UTC)
    narrowed = DateAssessment(
        estimate=estimate,
        earliest=estimate - timedelta(minutes=30),
        latest=estimate + timedelta(minutes=30),
        precision="hour",
        confidence="confirmed_date",
        basis="day_transition",
        time_subject="publish_time",
    )

    assert _date_event_needed(old, narrowed, "Asia/Taipei")


def test_exact_timestamp_upgrade_notifies_even_after_date_was_confirmed():
    old = OLD_REVIEW | {
        "basis": "day_transition",
        "publish_date": "2026-07-02",
        "confidence": "confirmed_date",
        "publish_earliest": "2026-07-02T09:30:00+00:00",
        "publish_latest": "2026-07-02T10:30:00+00:00",
    }
    exact = datetime(2026, 7, 2, 10, tzinfo=UTC)
    assessment = DateAssessment(
        estimate=exact,
        earliest=exact,
        latest=exact,
        precision="second",
        confidence="confirmed_time",
        basis="public_timestamp",
        time_subject="publish_time",
    )

    assert _date_event_needed(old, assessment, "Asia/Taipei")
