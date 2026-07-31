from datetime import UTC, datetime

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
