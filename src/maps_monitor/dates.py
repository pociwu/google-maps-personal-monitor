from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable
from zoneinfo import ZoneInfo


FIXED_SECONDS = {
    "minute": 60,
    "hour": 3600,
    "day": 86400,
    "week": 604800,
}

ZH_NUMBERS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "兩": 2, "两": 2, "三": 3,
    "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}

UNIT_ALIASES = {
    "分鐘": "minute", "分钟": "minute", "minute": "minute", "minutes": "minute",
    "小時": "hour", "小时": "hour", "hour": "hour", "hours": "hour",
    "天": "day", "日": "day", "day": "day", "days": "day",
    "週": "week", "周": "week", "星期": "week", "week": "week", "weeks": "week",
    "個月": "month", "个月": "month", "月": "month", "month": "month", "months": "month",
    "年": "year", "year": "year", "years": "year",
}

_RELATIVE = re.compile(
    r"(?P<n>\d+|[零〇一二兩两三四五六七八九十]+|a|an)\s*"
    r"(?P<unit>分鐘|分钟|小時|小时|天|日|週|周|星期|個月|个月|月|年|minutes?|hours?|days?|weeks?|months?|years?)"
    r"\s*(?:前|ago)",
    re.IGNORECASE,
)

_DECORATIVE_GLYPHS = re.compile(r"[\ue000-\uf8ff★☆⭐]+")


def normalize_relative_label(label: str) -> str:
    """Remove Google icon-font glyphs without discarding edit markers."""
    return " ".join(_DECORATIVE_GLYPHS.sub("", label or "").strip().split())


@dataclass(frozen=True, slots=True)
class ParsedRelative:
    count: int
    unit: str
    is_edit: bool
    raw: str


@dataclass(frozen=True, slots=True)
class DateWindow:
    earliest: datetime
    latest: datetime
    is_edit: bool = False
    parsed: ParsedRelative | None = None

    @property
    def midpoint(self) -> datetime:
        return self.earliest + (self.latest - self.earliest) / 2


@dataclass(frozen=True, slots=True)
class DateEvidence:
    observed_at: datetime
    relative_time: str
    exact_timestamp: datetime | None = None
    crawl_complete: bool = True


@dataclass(frozen=True, slots=True)
class DateAssessment:
    estimate: datetime | None
    earliest: datetime | None
    latest: datetime | None
    precision: str
    confidence: str
    basis: str
    time_subject: str
    model_version: str | None = None

    def estimate_date(self, timezone: str) -> str | None:
        return self.estimate.astimezone(ZoneInfo(timezone)).date().isoformat() if self.estimate else None


def _parse_number(value: str) -> int | None:
    lowered = value.lower()
    if lowered in {"a", "an"}:
        return 1
    if value.isdigit():
        return int(value)
    if value in ZH_NUMBERS:
        return ZH_NUMBERS[value]
    if value.startswith("十"):
        return 10 + ZH_NUMBERS.get(value[1:], 0)
    if "十" in value:
        tens, ones = value.split("十", 1)
        return ZH_NUMBERS.get(tens, 1) * 10 + ZH_NUMBERS.get(ones, 0)
    return None


def parse_relative(label: str) -> ParsedRelative | None:
    text = normalize_relative_label(label)
    lowered = text.lower()
    is_edit = any(token in lowered for token in (
        "已編輯", "已编辑", "編輯", "编辑", "已更新", "更新", "edited", "updated"
    ))
    if any(token in lowered for token in ("剛剛", "刚刚", "just now")):
        return ParsedRelative(0, "minute", is_edit, text)
    if any(token in lowered for token in ("昨天", "yesterday")):
        return ParsedRelative(1, "day", is_edit, text)
    match = _RELATIVE.search(text)
    if not match:
        return None
    count = _parse_number(match.group("n"))
    if count is None:
        return None
    return ParsedRelative(count, UNIT_ALIASES[match.group("unit").lower()], is_edit, text)


def subtract_calendar_months(value: datetime, months: int) -> datetime:
    total = value.year * 12 + value.month - 1 - months
    year, month_index = divmod(total, 12)
    month = month_index + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def subtract_calendar_years(value: datetime, years: int) -> datetime:
    year = value.year - years
    day = min(value.day, calendar.monthrange(year, value.month)[1])
    return value.replace(year=year, day=day)


def subtract_units(value: datetime, count: int, unit: str, model: str = "calendar") -> datetime:
    if unit in FIXED_SECONDS:
        return value - timedelta(seconds=FIXED_SECONDS[unit] * count)
    if unit == "month":
        if model.startswith("fixed:"):
            return value - timedelta(days=float(model.split(":", 1)[1]) * count)
        return subtract_calendar_months(value, count)
    if unit == "year":
        if model.startswith("fixed:"):
            return value - timedelta(days=float(model.split(":", 1)[1]) * count)
        return subtract_calendar_years(value, count)
    raise ValueError(f"未知時間單位：{unit}")


def add_units(value: datetime, count: int, unit: str, model: str = "calendar") -> datetime:
    if unit in FIXED_SECONDS:
        return value + timedelta(seconds=FIXED_SECONDS[unit] * count)
    if unit == "month":
        return subtract_calendar_months(value, -count) if not model.startswith("fixed:") else value + timedelta(days=float(model.split(":", 1)[1]) * count)
    if unit == "year":
        return subtract_calendar_years(value, -count) if not model.startswith("fixed:") else value + timedelta(days=float(model.split(":", 1)[1]) * count)
    raise ValueError(f"未知時間單位：{unit}")


def parse_relative_time(label: str, observed_at: datetime, model: str = "calendar") -> DateWindow | None:
    parsed = parse_relative(label)
    if not parsed:
        return None
    if parsed.count == 0 and parsed.unit == "minute":
        return DateWindow(observed_at - timedelta(minutes=1), observed_at, parsed.is_edit, parsed)
    if label.strip().lower() in {"昨天", "yesterday"}:
        return DateWindow(observed_at - timedelta(days=2), observed_at - timedelta(days=1), parsed.is_edit, parsed)
    latest = subtract_units(observed_at, parsed.count, parsed.unit, model)
    earliest = subtract_units(observed_at, parsed.count + 1, parsed.unit, model)
    return DateWindow(earliest, latest, parsed.is_edit, parsed)


def _window_assessment(
    windows: list[DateWindow], timezone: str,
    basis: str = "relative_window", subject: str = "display_time",
) -> DateAssessment:
    earliest = max(window.earliest for window in windows)
    latest = min(window.latest for window in windows)
    if earliest > latest:
        earliest = min(window.earliest for window in windows)
        latest = max(window.latest for window in windows)
    estimate = earliest + (latest - earliest) / 2
    seconds = (latest - earliest).total_seconds()
    precision = "minute" if seconds <= 60 else "hour" if seconds <= 3600 else "date"
    local_dates = {earliest.astimezone(ZoneInfo(timezone)).date(), latest.astimezone(ZoneInfo(timezone)).date()}
    confidence = "high_estimate" if len(local_dates) == 1 else "estimate"
    return DateAssessment(estimate, earliest, latest, precision, confidence, basis, subject)


def _transition_assessment(
    evidence: list[DateEvidence], timezone: str, models: dict[str, tuple[str, str]]
) -> DateAssessment | None:
    parsed = [(item, parse_relative(item.relative_time)) for item in evidence if item.crawl_complete]
    parsed = [(item, value) for item, value in parsed if value and not value.is_edit]
    for index in range(len(parsed) - 2, 0, -1):
        old_item, old = parsed[index - 1]
        new_item, new = parsed[index]
        confirm_item, confirm = parsed[index + 1]
        if not (old and new and confirm):
            continue
        if old.unit != new.unit or new.unit != confirm.unit:
            continue
        if new.count != old.count + 1 or confirm.count != new.count:
            continue
        model_name, model_version = models.get(new.unit, ("calendar", "uncalibrated"))
        earliest = subtract_units(old_item.observed_at, new.count, new.unit, model_name)
        latest = subtract_units(new_item.observed_at, new.count, new.unit, model_name)
        estimate = earliest + (latest - earliest) / 2
        local_earliest = earliest.astimezone(ZoneInfo(timezone)).date()
        local_latest = latest.astimezone(ZoneInfo(timezone)).date()
        calibrated = new.unit not in {"month", "year"} or model_version != "uncalibrated"
        confirmed = calibrated and local_earliest == local_latest
        confidence = "confirmed_date" if confirmed else "high_estimate"
        return DateAssessment(
            estimate, earliest, latest, "date", confidence,
            f"{new.unit}_transition", "publish_time", model_version,
        )
    return None


def assess_date(
    evidence: Iterable[DateEvidence],
    timezone: str = "Asia/Taipei",
    models: dict[str, tuple[str, str]] | None = None,
    first_seen_window: tuple[datetime, datetime] | None = None,
) -> DateAssessment:
    items = sorted((item for item in evidence if item.crawl_complete), key=lambda item: item.observed_at)
    exact = [item.exact_timestamp for item in items if item.exact_timestamp]
    if len(exact) >= 2 and exact[-1] == exact[-2]:
        latest_parsed = parse_relative(items[-1].relative_time) if items else None
        subject = "last_edit" if latest_parsed and latest_parsed.is_edit else "publish_time"
        return DateAssessment(exact[-1], exact[-1], exact[-1], "second", "confirmed_time", "public_timestamp", subject)
    if first_seen_window:
        earliest, latest = first_seen_window
        estimate = earliest + (latest - earliest) / 2
        same_date = earliest.astimezone(ZoneInfo(timezone)).date() == latest.astimezone(ZoneInfo(timezone)).date()
        return DateAssessment(
            estimate, earliest, latest, "hour" if latest - earliest <= timedelta(hours=1) else "date",
            "confirmed_date" if same_date else "high_estimate", "first_seen_interval", "publish_time",
        )
    transition = _transition_assessment(items, timezone, models or {})
    if transition:
        return transition
    all_windows = [
        window for item in items
        if (window := parse_relative_time(item.relative_time, item.observed_at))
    ]
    publish_windows = [window for window in all_windows if not window.is_edit]
    if publish_windows:
        return _window_assessment(publish_windows, timezone)
    edited_windows = [window for window in all_windows if window.is_edit]
    if edited_windows:
        result = _window_assessment(edited_windows, timezone, "edit_relative_window", "last_edit")
        return DateAssessment(
            result.estimate, result.earliest, result.latest, result.precision,
            "unrecoverable", result.basis, result.time_subject,
        )
    return DateAssessment(None, None, None, "unknown", "unrecoverable", "no_publish_evidence", "display_time")


def transition_assessment(
    evidence: Iterable[DateEvidence],
    timezone: str = "Asia/Taipei",
    models: dict[str, tuple[str, str]] | None = None,
) -> DateAssessment | None:
    return _transition_assessment(
        sorted((item for item in evidence if item.crawl_complete), key=lambda item: item.observed_at),
        timezone,
        models or {},
    )


def infer_single_date(observations: list[tuple[str, datetime]], timezone: str = "Asia/Taipei") -> str | None:
    assessment = assess_date(
        [DateEvidence(observed_at=seen, relative_time=label) for label, seen in observations], timezone
    )
    return None if assessment.time_subject == "last_edit" else assessment.estimate_date(timezone)
