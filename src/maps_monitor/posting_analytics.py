from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo


DISPLAY_TIMEZONE = ZoneInfo("Asia/Taipei")
CONFIRMED_DATE_CONFIDENCE = {"confirmed_date", "confirmed_time"}
# Only intervals whose total width is at most one hour may influence
# time-of-day analytics. The midpoint therefore has at most +/- 30 minutes.
MAX_ESTIMATED_TIME_HALF_WIDTH_SECONDS = 30 * 60
WEEKDAY_LABELS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")
PERIODS = (
    ("凌晨", 0, 6),
    ("上午", 6, 12),
    ("下午", 12, 18),
    ("晚上", 18, 24),
)
TWO_HOUR_SLOT_SIZE = 2
TWO_HOUR_SLOT_COUNT = 24 // TWO_HOUR_SLOT_SIZE


def _value(row: Mapping[str, object], key: str) -> object | None:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def _text(row: Mapping[str, object], key: str) -> str | None:
    value = _value(row, key)
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def _parse_datetime(value: object | None) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        candidate = value.strip()
        if len(candidate) <= 10:
            return None
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _parse_date(value: object | None) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except (ValueError, TypeError):
        return None


def _interval(
    row: Mapping[str, object], timezone: ZoneInfo
) -> tuple[datetime, datetime, datetime] | None:
    lower = _parse_datetime(_value(row, "publish_earliest"))
    upper = _parse_datetime(_value(row, "publish_latest"))
    if lower is None or upper is None or lower > upper:
        return None
    midpoint = _parse_datetime(_value(row, "publish_estimate"))
    if midpoint is None or not lower <= midpoint <= upper:
        midpoint = lower + (upper - lower) / 2
    return (
        lower.astimezone(timezone),
        midpoint.astimezone(timezone),
        upper.astimezone(timezone),
    )


def _dates_for_row(
    row: Mapping[str, object], timezone: ZoneInfo
) -> tuple[date | None, date | None, bool]:
    """Return trend date, weekday-safe date, and whether the trend date is confirmed."""
    confidence = _text(row, "confidence") or ""
    published_date = _parse_date(_value(row, "publish_date"))
    estimate = _parse_datetime(_value(row, "publish_estimate"))
    interval = _interval(row, timezone)
    interval_date = None
    if interval is not None and interval[0].date() == interval[2].date():
        interval_date = interval[0].date()

    confirmed = confidence in CONFIRMED_DATE_CONFIDENCE
    weekday_date = interval_date
    if confirmed:
        if confidence == "confirmed_time" and estimate is not None:
            weekday_date = estimate.astimezone(timezone).date()
        elif published_date is not None:
            weekday_date = published_date
        elif estimate is not None:
            weekday_date = estimate.astimezone(timezone).date()

    trend_date = weekday_date or published_date
    return trend_date, weekday_date, confirmed


def _time_for_row(
    row: Mapping[str, object], timezone: ZoneInfo
) -> tuple[datetime | None, str | None, str | None]:
    confidence = _text(row, "confidence") or ""
    estimate = _parse_datetime(_value(row, "publish_estimate"))
    interval = _interval(row, timezone)

    if confidence == "confirmed_time":
        if estimate is not None:
            value = estimate.astimezone(timezone)
            return value, "confirmed", _period_label(value)
        if interval is not None and interval[0] == interval[2]:
            return interval[1], "confirmed", _period_label(interval[1])
        return None, None, None

    if interval is None or _text(row, "precision") not in {"minute", "hour"}:
        return None, None, None
    lower, midpoint, upper = interval
    half_width = max(
        abs((midpoint - lower).total_seconds()),
        abs((upper - midpoint).total_seconds()),
    )
    if half_width <= MAX_ESTIMATED_TIME_HALF_WIDTH_SECONDS:
        lower_period = _period_label(lower)
        upper_period = _period_label(upper)
        period = lower_period if lower_period == upper_period else None
        return midpoint, "estimated", period
    return None, None, None


def _period_label(value: datetime) -> str:
    return next(
        label for label, start, end in PERIODS if start <= value.hour < end
    )


def _weekday_two_hour_slot(
    row: Mapping[str, object], timezone: ZoneInfo
) -> tuple[int, int, str] | None:
    """Return a safe Taiwan weekday, two-hour slot, and time kind for one review."""
    confidence = _text(row, "confidence") or ""
    estimate = _parse_datetime(_value(row, "publish_estimate"))
    interval = _interval(row, timezone)

    if confidence == "confirmed_time":
        if estimate is not None:
            value = estimate.astimezone(timezone)
        elif interval is not None and interval[0] == interval[2]:
            value = interval[1]
        else:
            return None
        return value.weekday(), value.hour // TWO_HOUR_SLOT_SIZE, "confirmed"

    if interval is None or _text(row, "precision") not in {"minute", "hour"}:
        return None
    lower, midpoint, upper = interval
    half_width = max(
        abs((midpoint - lower).total_seconds()),
        abs((upper - midpoint).total_seconds()),
    )
    if half_width > MAX_ESTIMATED_TIME_HALF_WIDTH_SECONDS:
        return None
    if lower.date() != upper.date():
        return None
    lower_slot = lower.hour // TWO_HOUR_SLOT_SIZE
    upper_slot = upper.hour // TWO_HOUR_SLOT_SIZE
    if lower_slot != upper_slot:
        return None
    return lower.weekday(), lower_slot, "estimated"


def _make_bars(labels: Iterable[Any], counts: Counter[Any], total: int) -> list[dict[str, Any]]:
    label_list = list(labels)
    maximum = max((counts[label] for label in label_list), default=0)
    bars: list[dict[str, Any]] = []
    for label in label_list:
        count = counts[label]
        bars.append(
            {
                "label": str(label),
                "count": count,
                "percent": round(count * 100 / total, 1) if total else 0.0,
                "max_percent": round(count * 100 / maximum, 1) if maximum else 0.0,
            }
        )
    return bars


def _display_number(value: float) -> str:
    return str(int(value)) if value.is_integer() else f"{value:.1f}"


def _percentile(values: list[int], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower_index = int(position)
    upper_index = min(lower_index + 1, len(ordered) - 1)
    weight = position - lower_index
    return ordered[lower_index] * (1 - weight) + ordered[upper_index] * weight


def _cadence(dates: list[date]) -> dict[str, Any]:
    sorted_dates = sorted(dates)
    gaps = [
        (later - earlier).days
        for earlier, later in zip(sorted_dates, sorted_dates[1:], strict=False)
    ]
    result: dict[str, Any] = {
        "sample_count": len(sorted_dates),
        "gap_count": len(gaps),
        "median_days": None,
        "mad_days": None,
        "iqr_days": None,
        "relative_mad": None,
        "within_tolerance_share": None,
        "within_tolerance_percent": None,
        "split_median_difference": None,
        "split_median_difference_percent": None,
        "regular": False,
        "stable": False,
        "span_days": (sorted_dates[-1] - sorted_dates[0]).days if sorted_dates else 0,
        "text": "可確定日期的樣本不足 5 筆，尚無法判斷發文間隔是否固定。",
    }
    if len(sorted_dates) < 5 or len(gaps) < 4:
        return result

    median_days = float(median(gaps))
    mad_days = float(median(abs(gap - median_days) for gap in gaps))
    result["median_days"] = median_days
    result["mad_days"] = mad_days
    if median_days <= 0:
        result["text"] = "多筆樣本集中在同一天，發文間隔中位數為 0 天，無法判定固定週期。"
        return result

    relative_mad = mad_days / median_days
    iqr_days = _percentile(gaps, 0.75) - _percentile(gaps, 0.25)
    tolerance_days = max(1.0, median_days * 0.35)
    within_tolerance_share = sum(
        abs(gap - median_days) <= tolerance_days for gap in gaps
    ) / len(gaps)
    split_index = len(gaps) // 2
    first_half_median = float(median(gaps[:split_index]))
    second_half_median = float(median(gaps[split_index:]))
    split_median_difference = abs(first_half_median - second_half_median) / median_days
    regular = (
        relative_mad <= 0.35
        and iqr_days / median_days <= 0.75
        and within_tolerance_share >= 0.75
        and split_median_difference <= 0.35
    )
    stable = regular and len(sorted_dates) >= 28 and result["span_days"] >= 84
    result["iqr_days"] = round(iqr_days, 3)
    result["relative_mad"] = round(relative_mad, 3)
    result["within_tolerance_share"] = round(within_tolerance_share, 3)
    result["within_tolerance_percent"] = round(within_tolerance_share * 100, 1)
    result["split_median_difference"] = round(split_median_difference, 3)
    result["split_median_difference_percent"] = round(
        split_median_difference * 100, 1
    )
    result["regular"] = regular
    result["stable"] = stable
    median_text = _display_number(median_days)
    mad_text = _display_number(mad_days)
    if stable:
        result["text"] = (
            f"目前樣本可能呈現較穩定的發文間隔：中位數約 {median_text} 天，"
            f"中位絕對偏差 {mad_text} 天；仍需後續資料持續驗證。"
        )
    elif regular:
        result["text"] = (
            f"目前發文間隔的分散程度較低：中位數約 {median_text} 天，"
            f"中位絕對偏差 {mad_text} 天；樣本或觀察期尚不足以確認穩定規律。"
        )
    else:
        result["text"] = (
            f"目前樣本的發文間隔不固定：中位數約 {median_text} 天，"
            f"中位絕對偏差 {mad_text} 天。"
        )
    return result


def build_posting_analytics(
    rows: Iterable[Mapping[str, object]], timezone: ZoneInfo = DISPLAY_TIMEZONE
) -> dict[str, Any]:
    """Build conservative, presentation-ready posting-time analytics."""
    review_rows = list(rows)
    weekday_counts: Counter[int] = Counter()
    hour_counts: Counter[int] = Counter()
    period_counts: Counter[str] = Counter()
    month_counts: Counter[str] = Counter()
    month_confirmed_counts: Counter[str] = Counter()
    month_estimated_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    cadence_dates: list[date] = []
    confirmed_time_count = 0
    estimated_time_count = 0
    period_eligible_count = 0
    dated_count = 0
    confirmed_date_count = 0
    excluded_edit_count = 0
    unrecoverable_count = 0
    period_confirmed_counts: Counter[str] = Counter()
    period_estimated_counts: Counter[str] = Counter()
    weekday_two_hour_counts: Counter[tuple[int, int]] = Counter()
    weekday_two_hour_confirmed_counts: Counter[tuple[int, int]] = Counter()
    weekday_two_hour_estimated_counts: Counter[tuple[int, int]] = Counter()
    weekday_two_hour_confirmed_count = 0
    weekday_two_hour_estimated_count = 0
    weekday_pattern_keys: set[tuple[object, date]] = set()
    time_pattern_keys: set[tuple[object, date, str]] = set()

    for index, row in enumerate(review_rows):
        status_counts[_text(row, "status") or "unknown"] += 1
        confidence = _text(row, "confidence") or ""
        if _text(row, "time_subject") == "last_edit":
            excluded_edit_count += 1
            continue
        if confidence == "unrecoverable":
            unrecoverable_count += 1
            continue
        contributor_key: object = (
            _value(row, "target_id")
            or _value(row, "contributor_id")
            or _text(row, "target_name")
            or _text(row, "contributor_name")
            or ("row", index)
        )
        trend_date, weekday_date, date_confirmed = _dates_for_row(row, timezone)
        if trend_date is not None:
            dated_count += 1
            month_key = trend_date.strftime("%Y-%m")
            month_counts[month_key] += 1
            if date_confirmed:
                confirmed_date_count += 1
                month_confirmed_counts[month_key] += 1
            else:
                month_estimated_counts[month_key] += 1
        if weekday_date is not None:
            weekday_counts[weekday_date.weekday()] += 1
            weekday_pattern_keys.add((contributor_key, weekday_date))

        weekday_two_hour_slot = _weekday_two_hour_slot(row, timezone)
        if weekday_two_hour_slot is not None:
            weekday, slot, slot_kind = weekday_two_hour_slot
            cell_key = (slot, weekday)
            weekday_two_hour_counts[cell_key] += 1
            if slot_kind == "confirmed":
                weekday_two_hour_confirmed_counts[cell_key] += 1
                weekday_two_hour_confirmed_count += 1
            else:
                weekday_two_hour_estimated_counts[cell_key] += 1
                weekday_two_hour_estimated_count += 1

        time_value, time_kind, period_label = _time_for_row(row, timezone)
        if time_value is None:
            continue
        hour_counts[time_value.hour] += 1
        if period_label is not None:
            period_eligible_count += 1
            period_counts[period_label] += 1
            time_pattern_keys.add((contributor_key, time_value.date(), period_label))
            if time_kind == "confirmed":
                period_confirmed_counts[period_label] += 1
            else:
                period_estimated_counts[period_label] += 1
        if time_kind == "confirmed":
            confirmed_time_count += 1
        else:
            estimated_time_count += 1

    weekday_eligible_count = sum(weekday_counts.values())
    time_eligible_count = confirmed_time_count + estimated_time_count
    total_reviews = len(review_rows)
    cadence_dates.extend(item[1] for item in weekday_pattern_keys)
    cadence = _cadence(cadence_dates)

    weekday_bars = _make_bars(range(7), weekday_counts, weekday_eligible_count)
    for index, bar in enumerate(weekday_bars):
        bar["weekday"] = index
        bar["label"] = WEEKDAY_LABELS[index]

    hour_bars = _make_bars(range(24), hour_counts, time_eligible_count)
    for hour, bar in enumerate(hour_bars):
        bar["hour"] = hour
        bar["label"] = f"{hour:02d}:00"

    weekday_two_hour_eligible_count = (
        weekday_two_hour_confirmed_count + weekday_two_hour_estimated_count
    )
    weekday_two_hour_max_count = max(weekday_two_hour_counts.values(), default=0)
    weekday_two_hour_rows: list[dict[str, Any]] = []
    for slot in range(TWO_HOUR_SLOT_COUNT):
        start_hour = slot * TWO_HOUR_SLOT_SIZE
        end_hour = start_hour + TWO_HOUR_SLOT_SIZE
        cells: list[dict[str, Any]] = []
        for weekday, weekday_label in enumerate(WEEKDAY_LABELS):
            cell_key = (slot, weekday)
            count = weekday_two_hour_counts[cell_key]
            cells.append(
                {
                    "weekday": weekday,
                    "weekday_label": weekday_label,
                    "slot": slot,
                    "count": count,
                    "confirmed_count": weekday_two_hour_confirmed_counts[cell_key],
                    "estimated_count": weekday_two_hour_estimated_counts[cell_key],
                    "max_percent": (
                        round(count * 100 / weekday_two_hour_max_count, 1)
                        if weekday_two_hour_max_count else 0.0
                    ),
                    "strength": (
                        0
                        if count == 0 or weekday_two_hour_max_count == 0
                        else max(
                            1,
                            min(
                                4,
                                (count * 4 + weekday_two_hour_max_count - 1)
                                // weekday_two_hour_max_count,
                            ),
                        )
                    ),
                }
            )
        weekday_two_hour_rows.append(
            {
                "slot": slot,
                "start_hour": start_hour,
                "end_hour": end_hour,
                "label": f"{start_hour:02d}:00–{end_hour - 1:02d}:59",
                "count": sum(cell["count"] for cell in cells),
                "cells": cells,
            }
        )

    weekday_two_hour_heatmap = {
        "weekdays": [
            {
                "weekday": weekday,
                "label": label.removeprefix("星期"),
                "full_label": label,
            }
            for weekday, label in enumerate(WEEKDAY_LABELS)
        ],
        "rows": weekday_two_hour_rows,
        "eligible_count": weekday_two_hour_eligible_count,
        "confirmed_count": weekday_two_hour_confirmed_count,
        "estimated_count": weekday_two_hour_estimated_count,
        "excluded_count": total_reviews - weekday_two_hour_eligible_count,
        "max_count": weekday_two_hour_max_count,
    }

    period_labels = [item[0] for item in PERIODS]
    period_bars = _make_bars(period_labels, period_counts, period_eligible_count)
    for bar, (_label, start, end) in zip(period_bars, PERIODS, strict=True):
        bar["range"] = f"{start:02d}:00–{end - 1:02d}:59"
        bar["confirmed_count"] = period_confirmed_counts[bar["label"]]
        bar["estimated_count"] = period_estimated_counts[bar["label"]]

    month_labels = sorted(month_counts)
    month_bars = _make_bars(month_labels, month_counts, dated_count)
    for bar in month_bars:
        key = bar["label"]
        bar["confirmed_count"] = month_confirmed_counts[key]
        bar["estimated_count"] = month_estimated_counts[key]

    findings: list[str] = []
    weekday_pattern_counts = Counter(item[1].weekday() for item in weekday_pattern_keys)
    weekday_pattern_count = len(weekday_pattern_keys)
    weekday_pattern_dates = [item[1] for item in weekday_pattern_keys]
    weekday_span_days = (
        (max(weekday_pattern_dates) - min(weekday_pattern_dates)).days
        if weekday_pattern_dates else 0
    )
    if weekday_pattern_count < 5:
        findings.append(
            f"去除同一貢獻者同日重複後，日期樣本只有 {weekday_pattern_count} 筆，"
            "尚不足以判斷星期偏好。"
        )
    else:
        top_weekday, top_weekday_count = max(
            weekday_pattern_counts.items(), key=lambda item: item[1]
        )
        top_weekday_share = top_weekday_count / weekday_pattern_count
        if top_weekday_count >= 3 and top_weekday_share >= 0.4:
            if weekday_pattern_count >= 28 and weekday_span_days >= 84:
                findings.append(
                    f"目前長期樣本較常在"
                    f"{WEEKDAY_LABELS[top_weekday]}發表"
                    f"（{top_weekday_count} 筆，{top_weekday_share:.0%}）；"
                    "這仍是描述性統計，不能單獨證明固定規律。"
                )
            else:
                findings.append(
                    f"目前樣本初步偏向在{WEEKDAY_LABELS[top_weekday]}發表"
                    f"（{top_weekday_count} 筆，{top_weekday_share:.0%}）；"
                    "樣本或觀察期尚不足，不能確認穩定規律。"
                )
        else:
            findings.append("星期分布未達偏好門檻，目前看不出固定在某一天發表。")

    time_pattern_counts = Counter(item[2] for item in time_pattern_keys)
    time_pattern_count = len(time_pattern_keys)
    time_pattern_dates = [item[1] for item in time_pattern_keys]
    time_span_days = (
        (max(time_pattern_dates) - min(time_pattern_dates)).days if time_pattern_dates else 0
    )
    if time_pattern_count < 5:
        findings.append(
            f"去除同一貢獻者同日同時段重複後，時段樣本只有 {time_pattern_count} 筆，"
            "尚不足以判斷偏好時段。"
        )
    else:
        top_period, top_period_count = max(
            time_pattern_counts.items(), key=lambda item: item[1]
        )
        top_period_share = top_period_count / time_pattern_count
        if top_period_count >= 3 and top_period_share >= 0.5:
            if time_pattern_count >= 20 and time_span_days >= 56:
                findings.append(
                    f"目前長期樣本較常在{top_period}發表"
                    f"（{top_period_count} 筆，{top_period_share:.0%}）；"
                    "推算時間只納入總區間不超過 1 小時者；"
                    "這仍是描述性統計，不能單獨證明固定規律。"
                )
            else:
                findings.append(
                    f"目前樣本初步偏向在{top_period}發表"
                    f"（{top_period_count} 筆，{top_period_share:.0%}）；"
                    "樣本或觀察期尚不足，不能確認穩定規律。"
                )
        else:
            findings.append("時段分布未達偏好門檻，目前看不出固定發文時段。")
    findings.append(cadence["text"])

    return {
        "total_reviews": total_reviews,
        "dated_count": dated_count,
        "confirmed_date_count": confirmed_date_count,
        "estimated_date_count": dated_count - confirmed_date_count,
        "weekday_eligible_count": weekday_eligible_count,
        "time_eligible_count": time_eligible_count,
        "confirmed_time_count": confirmed_time_count,
        "estimated_time_count": estimated_time_count,
        "excluded_time_count": total_reviews - time_eligible_count,
        "period_eligible_count": period_eligible_count,
        "excluded_period_count": time_eligible_count - period_eligible_count,
        "excluded_edit_count": excluded_edit_count,
        "unrecoverable_count": unrecoverable_count,
        "weekday_pattern_count": weekday_pattern_count,
        "weekday_span_days": weekday_span_days,
        "time_pattern_count": time_pattern_count,
        "time_span_days": time_span_days,
        "status_counts": dict(status_counts),
        "sample_summary": {
            "total": total_reviews,
            "dated": dated_count,
            "weekday_eligible": weekday_eligible_count,
            "time_eligible": time_eligible_count,
            "confirmed_time": confirmed_time_count,
            "estimated_time": estimated_time_count,
            "excluded_time": total_reviews - time_eligible_count,
        },
        "weekday_bars": weekday_bars,
        "hour_bars": hour_bars,
        "weekday_two_hour_heatmap": weekday_two_hour_heatmap,
        "period_bars": period_bars,
        "month_bars": month_bars,
        "cadence": cadence,
        "findings": findings,
    }
