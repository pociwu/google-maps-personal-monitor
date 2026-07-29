from datetime import UTC, datetime

from maps_monitor.dates import (
    DateEvidence,
    assess_date,
    infer_single_date,
    parse_relative,
    parse_relative_time,
    subtract_calendar_months,
    subtract_calendar_years,
)


def test_chinese_relative_day_window():
    observed = datetime(2026, 7, 15, 12, tzinfo=UTC)
    window = parse_relative_time("3 天前", observed)
    assert window is not None
    assert window.earliest == datetime(2026, 7, 11, 12, tzinfo=UTC)
    assert window.latest == datetime(2026, 7, 12, 12, tzinfo=UTC)


def test_inference_intersects_multiple_observations():
    observations = [
        ("3 天前", datetime(2026, 7, 15, 12, tzinfo=UTC)),
        ("4 天前", datetime(2026, 7, 16, 12, tzinfo=UTC)),
    ]
    assert infer_single_date(observations, "Asia/Taipei") == "2026-07-12"


def test_edited_label_not_used_as_publish_date():
    observed = datetime(2026, 7, 15, 12, tzinfo=UTC)
    assert infer_single_date([("已編輯：2 天前", observed)]) is None
    assessment = assess_date([DateEvidence(observed, "已編輯：2 天前")])
    assert assessment.time_subject == "last_edit"
    assert assessment.confidence == "unrecoverable"
    assert assessment.estimate is not None


def test_multilingual_relative_parser():
    assert parse_relative("一週前").count == 1
    assert parse_relative("两个月前").unit == "month"
    assert parse_relative("a year ago").count == 1
    assert parse_relative("已更新 3 小時前").is_edit is True
    decorated = parse_relative("\ue838\ue838\ue838\ue838\ue8381 個月前")
    assert decorated is not None
    assert (decorated.count, decorated.unit) == (1, "month")


def test_adjacent_transition_needs_second_confirmation():
    evidence = [
        DateEvidence(datetime(2026, 7, 11, 11, 30, tzinfo=UTC), "1 天前"),
        DateEvidence(datetime(2026, 7, 11, 12, 10, tzinfo=UTC), "2 天前"),
        DateEvidence(datetime(2026, 7, 11, 12, 40, tzinfo=UTC), "2 天前"),
    ]
    result = assess_date(evidence, "Asia/Taipei")
    assert result.confidence == "confirmed_date"
    assert result.basis == "day_transition"
    assert result.estimate_date("Asia/Taipei") == "2026-07-09"


def test_transition_crossing_taipei_midnight_is_not_confirmed():
    evidence = [
        DateEvidence(datetime(2026, 7, 23, 15, 30, tzinfo=UTC), "1 週前"),
        DateEvidence(datetime(2026, 7, 23, 16, 30, tzinfo=UTC), "2 週前"),
        DateEvidence(datetime(2026, 7, 23, 17, 0, tzinfo=UTC), "2 週前"),
    ]
    assert assess_date(evidence, "Asia/Taipei").confidence == "high_estimate"


def test_skipped_transition_is_not_confirmation():
    evidence = [
        DateEvidence(datetime(2026, 7, 1, tzinfo=UTC), "1 週前"),
        DateEvidence(datetime(2026, 7, 15, tzinfo=UTC), "3 週前"),
        DateEvidence(datetime(2026, 7, 16, tzinfo=UTC), "3 週前"),
    ]
    assert not assess_date(evidence).basis.endswith("_transition")


def test_exact_timestamp_requires_two_equal_observations():
    exact = datetime(2026, 7, 9, 3, 2, 1, tzinfo=UTC)
    evidence = [
        DateEvidence(datetime(2026, 7, 10, tzinfo=UTC), "1 天前", exact),
        DateEvidence(datetime(2026, 7, 10, 1, tzinfo=UTC), "1 天前", exact),
    ]
    result = assess_date(evidence)
    assert result.confidence == "confirmed_time"
    assert result.estimate == exact


def test_month_and_leap_year_clamping():
    value = datetime(2028, 3, 31, 12, tzinfo=UTC)
    assert subtract_calendar_months(value, 1) == datetime(2028, 2, 29, 12, tzinfo=UTC)
    leap = datetime(2028, 2, 29, 12, tzinfo=UTC)
    assert subtract_calendar_years(leap, 1) == datetime(2027, 2, 28, 12, tzinfo=UTC)


def test_month_transition_is_not_confirmed_before_calibration():
    evidence = [
        DateEvidence(datetime(2026, 3, 9, 1, tzinfo=UTC), "1 個月前"),
        DateEvidence(datetime(2026, 3, 9, 2, tzinfo=UTC), "2 個月前"),
        DateEvidence(datetime(2026, 3, 9, 3, tzinfo=UTC), "2 個月前"),
    ]
    result = assess_date(evidence)
    assert result.basis == "month_transition"
    assert result.confidence == "high_estimate"
