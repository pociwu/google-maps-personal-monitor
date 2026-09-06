from datetime import date, timedelta

from maps_monitor.posting_analytics import build_posting_analytics


def _confirmed(timestamp: str, *, status: str = "active") -> dict[str, object]:
    return {
        "publish_date": timestamp[:10],
        "publish_estimate": timestamp,
        "publish_earliest": timestamp,
        "publish_latest": timestamp,
        "confidence": "confirmed_time",
        "status": status,
    }


def test_builds_distributions_and_detects_conservative_patterns():
    rows = [
        _confirmed("2026-08-03T01:00:00+00:00"),
        _confirmed("2026-08-10T01:00:00+00:00"),
        _confirmed("2026-08-17T01:00:00+00:00"),
        _confirmed("2026-08-25T01:00:00+00:00"),
        _confirmed("2026-09-02T01:00:00+00:00", status="deleted"),
    ]

    result = build_posting_analytics(rows)

    assert result["total_reviews"] == 5
    assert result["dated_count"] == 5
    assert result["weekday_eligible_count"] == 5
    assert result["confirmed_time_count"] == 5
    assert result["estimated_time_count"] == 0
    assert result["excluded_time_count"] == 0
    assert result["status_counts"] == {"active": 4, "deleted": 1}
    assert result["weekday_bars"][0] == {
        "label": "星期一",
        "count": 3,
        "percent": 60.0,
        "max_percent": 100.0,
        "weekday": 0,
    }
    assert result["hour_bars"][9]["count"] == 5
    assert result["period_bars"][1]["label"] == "上午"
    assert result["period_bars"][1]["count"] == 5
    assert [(bar["label"], bar["count"]) for bar in result["month_bars"]] == [
        ("2026-08", 4),
        ("2026-09", 1),
    ]
    assert result["cadence"]["regular"] is True
    assert result["cadence"]["median_days"] == 7.5
    assert result["cadence"]["mad_days"] == 0.5
    assert "星期一" in result["findings"][0]
    assert "上午" in result["findings"][1]


def test_only_narrow_estimates_are_used_for_hour_chart():
    rows = [
        {
            "publish_date": "2026-08-03",
            "publish_estimate": "2026-08-03T03:00:00+00:00",
            "publish_earliest": "2026-08-03T02:30:00+00:00",
            "publish_latest": "2026-08-03T03:30:00+00:00",
            "precision": "hour",
            "confidence": "high_estimate",
            "status": "active",
        },
        {
            "publish_date": "2026-08-04",
            "publish_estimate": "2026-08-04T04:00:00+00:00",
            "publish_earliest": "2026-08-04T00:00:00+00:00",
            "publish_latest": "2026-08-04T08:00:00+00:00",
            "precision": "date",
            "confidence": "high_estimate",
            "status": "active",
        },
        {
            "publish_date": "2026-08-05",
            "confidence": "estimate",
            "status": "active",
        },
        {
            "publish_date": "2026-08-06",
            "publish_estimate": "2026-08-06T15:00:00+00:00",
            "publish_earliest": "2026-08-06T14:30:00+00:00",
            "publish_latest": "2026-08-06T15:30:00+00:00",
            "precision": "hour",
            "confidence": "estimate",
            "status": "active",
        },
    ]

    result = build_posting_analytics(rows)

    assert result["dated_count"] == 4
    assert result["confirmed_date_count"] == 0
    assert result["estimated_date_count"] == 4
    # The wide interval still stays on one Taiwan calendar date, so it is
    # usable for weekday statistics even though it is excluded from time charts.
    assert result["weekday_eligible_count"] == 3
    assert result["time_eligible_count"] == 2
    assert result["period_eligible_count"] == 2
    assert result["excluded_period_count"] == 0
    assert result["estimated_time_count"] == 2
    assert result["excluded_time_count"] == 2
    assert result["hour_bars"][11]["count"] == 1
    assert result["hour_bars"][23]["count"] == 1
    august = result["month_bars"][0]
    assert august["estimated_count"] == 4
    assert "不足以判斷星期偏好" in result["findings"][0]
    assert "不足以判斷偏好時段" in result["findings"][1]


def test_estimated_period_requires_the_whole_interval_to_stay_in_one_period():
    rows = [
        {
            "publish_date": "2026-08-03",
            "publish_estimate": "2026-08-03T05:00:00+00:00",
            "publish_earliest": "2026-08-03T04:30:00+00:00",
            "publish_latest": "2026-08-03T05:30:00+00:00",
            "precision": "hour",
            "confidence": "high_estimate",
            "status": "active",
        }
    ]

    result = build_posting_analytics(rows)

    assert result["time_eligible_count"] == 1
    assert result["period_eligible_count"] == 1
    afternoon = next(item for item in result["period_bars"] if item["label"] == "下午")
    assert afternoon["count"] == 1
    assert afternoon["estimated_count"] == 1


def test_cross_date_interval_is_not_used_for_weekday_and_zero_gap_is_safe():
    same_day_rows = [
        _confirmed(f"2026-08-03T0{hour}:00:00+00:00")
        for hour in range(1, 6)
    ]
    same_day_rows.append(
        {
            "publish_date": "2026-08-04",
            "publish_estimate": "2026-08-03T16:00:00+00:00",
            "publish_earliest": "2026-08-03T15:00:00+00:00",
            "publish_latest": "2026-08-03T17:00:00+00:00",
            "confidence": "estimate",
            "status": "active",
        }
    )

    result = build_posting_analytics(same_day_rows)

    assert result["dated_count"] == 6
    assert result["weekday_eligible_count"] == 5
    assert result["cadence"]["median_days"] == 0.0
    assert result["cadence"]["relative_mad"] is None
    assert result["cadence"]["regular"] is False
    assert "中位數為 0 天" in result["cadence"]["text"]


def test_bimodal_short_and_long_gaps_are_not_called_regular():
    current = date(2026, 1, 1)
    dates = [current]
    for index in range(27):
        current += timedelta(days=1 if index % 2 == 0 else 30)
        dates.append(current)
    rows = [
        {
            **_confirmed(f"{value.isoformat()}T12:00:00+00:00"),
            "target_id": 1,
        }
        for value in dates
    ]

    result = build_posting_analytics(rows)

    assert result["cadence"]["regular"] is False
    assert result["cadence"]["stable"] is False
    assert result["cadence"]["iqr_days"] > 20
    assert "不固定" in result["cadence"]["text"]


def test_cadence_change_between_early_and_late_samples_is_not_regular():
    current = date(2026, 1, 1)
    dates = [current]
    for index in range(27):
        current += timedelta(days=7 if index < 14 else 14)
        dates.append(current)
    rows = [
        {
            **_confirmed(f"{value.isoformat()}T12:00:00+00:00"),
            "target_id": 1,
        }
        for value in dates
    ]

    result = build_posting_analytics(rows)

    assert result["cadence"]["regular"] is False
    assert result["cadence"]["stable"] is False
    assert result["cadence"]["split_median_difference"] > 0.35


def test_invalid_or_missing_dates_return_empty_bars_without_crashing():
    result = build_posting_analytics(
        [
            {
                "publish_date": "not-a-date",
                "publish_estimate": "broken",
                "publish_earliest": "2026-08-04T03:00:00+00:00",
                "publish_latest": "2026-08-03T03:00:00+00:00",
                "confidence": "estimate",
            },
            {},
        ]
    )

    assert result["total_reviews"] == 2
    assert result["dated_count"] == 0
    assert result["weekday_eligible_count"] == 0
    assert result["time_eligible_count"] == 0
    assert result["month_bars"] == []
    assert all(bar["count"] == 0 for bar in result["weekday_bars"])
    assert all(bar["count"] == 0 for bar in result["hour_bars"])


def test_findings_deduplicate_same_contributor_day_and_exclude_edit_time():
    rows = [
        {
            **_confirmed(f"2026-08-03T0{hour}:00:00+00:00"),
            "target_id": 1,
        }
        for hour in range(1, 6)
    ]
    rows.extend(
        [
            {
                **_confirmed("2026-08-10T01:00:00+00:00"),
                "target_id": 1,
                "time_subject": "last_edit",
            },
            {
                "target_id": 1,
                "publish_date": "2026-08-11",
                "confidence": "unrecoverable",
                "status": "active",
            },
        ]
    )

    result = build_posting_analytics(rows)

    assert result["total_reviews"] == 7
    assert result["dated_count"] == 5
    assert result["weekday_eligible_count"] == 5
    assert result["weekday_pattern_count"] == 1
    assert result["time_eligible_count"] == 5
    assert result["time_pattern_count"] == 2
    assert result["excluded_edit_count"] == 1
    assert result["unrecoverable_count"] == 1
    assert "只有 1 筆" in result["findings"][0]
    assert "只有 2 筆" in result["findings"][1]
    morning = next(item for item in result["period_bars"] if item["label"] == "上午")
    assert morning["confirmed_count"] == 3
    assert morning["estimated_count"] == 0


def test_weekday_two_hour_heatmap_has_fixed_shape_and_uses_taiwan_time():
    rows = [
        _confirmed("2026-08-03T16:30:00+00:00"),  # Tuesday 00:30 in Taiwan
        _confirmed("2026-08-04T17:59:00+00:00"),  # Wednesday 01:59
        _confirmed("2026-08-04T18:00:00+00:00"),  # Wednesday 02:00
    ]

    result = build_posting_analytics(rows)
    heatmap = result["weekday_two_hour_heatmap"]

    assert [item["full_label"] for item in heatmap["weekdays"]] == [
        "星期一",
        "星期二",
        "星期三",
        "星期四",
        "星期五",
        "星期六",
        "星期日",
    ]
    assert len(heatmap["rows"]) == 12
    assert sum(len(row["cells"]) for row in heatmap["rows"]) == 84
    assert heatmap["rows"][0]["label"] == "00:00–01:59"
    assert heatmap["rows"][11]["label"] == "22:00–23:59"
    assert heatmap["rows"][0]["cells"][1]["count"] == 1
    assert heatmap["rows"][0]["cells"][2]["count"] == 1
    assert heatmap["rows"][1]["cells"][2]["count"] == 1
    assert heatmap["eligible_count"] == 3
    assert heatmap["confirmed_count"] == 3
    assert heatmap["estimated_count"] == 0
    assert heatmap["excluded_count"] == 0
    assert sum(
        cell["count"]
        for row in heatmap["rows"]
        for cell in row["cells"]
    ) == heatmap["eligible_count"]


def test_weekday_two_hour_heatmap_only_accepts_estimates_inside_one_cell():
    rows = [
        {
            "publish_date": "2026-08-03",
            "publish_estimate": "2026-08-03T01:00:00+00:00",
            "publish_earliest": "2026-08-03T00:30:00+00:00",
            "publish_latest": "2026-08-03T01:30:00+00:00",
            "precision": "hour",
            "confidence": "high_estimate",
            "status": "active",
        },
        {
            "publish_date": "2026-08-03",
            "publish_estimate": "2026-08-03T02:00:00+00:00",
            "publish_earliest": "2026-08-03T01:30:00+00:00",
            "publish_latest": "2026-08-03T02:30:00+00:00",
            "precision": "hour",
            "confidence": "high_estimate",
            "status": "active",
        },
        {
            "publish_date": "2026-08-03",
            "publish_estimate": "2026-08-03T16:00:00+00:00",
            "publish_earliest": "2026-08-03T15:30:00+00:00",
            "publish_latest": "2026-08-03T16:30:00+00:00",
            "precision": "hour",
            "confidence": "high_estimate",
            "status": "active",
        },
    ]

    heatmap = build_posting_analytics(rows)["weekday_two_hour_heatmap"]

    assert heatmap["eligible_count"] == 1
    assert heatmap["confirmed_count"] == 0
    assert heatmap["estimated_count"] == 1
    assert heatmap["excluded_count"] == 2
    monday_08_to_09 = heatmap["rows"][4]["cells"][0]
    assert monday_08_to_09["count"] == 1
    assert monday_08_to_09["estimated_count"] == 1
    assert monday_08_to_09["strength"] == 4
    assert heatmap["rows"][0]["cells"][0]["strength"] == 0


def test_weekday_two_hour_heatmap_excludes_non_publish_time_but_keeps_deleted():
    rows = [
        {**_confirmed("2026-08-03T04:00:00+00:00", status="deleted")},
        {
            **_confirmed("2026-08-03T05:00:00+00:00"),
            "time_subject": "last_edit",
        },
        {
            "publish_date": "2026-08-03",
            "confidence": "unrecoverable",
            "status": "active",
        },
        {
            "publish_date": "2026-08-03",
            "confidence": "estimate",
            "status": "active",
        },
    ]

    heatmap = build_posting_analytics(rows)["weekday_two_hour_heatmap"]

    assert heatmap["eligible_count"] == 1
    assert heatmap["confirmed_count"] == 1
    assert heatmap["excluded_count"] == 3
    assert heatmap["rows"][6]["cells"][0]["count"] == 1


def test_empty_weekday_two_hour_heatmap_still_contains_all_cells():
    heatmap = build_posting_analytics([])["weekday_two_hour_heatmap"]

    assert heatmap["eligible_count"] == 0
    assert heatmap["max_count"] == 0
    assert len(heatmap["rows"]) == 12
    assert all(
        cell["count"] == 0 and cell["strength"] == 0
        for row in heatmap["rows"]
        for cell in row["cells"]
    )


def test_time_chart_requires_trusted_precision_and_at_most_one_hour_total():
    rows = [
        {
            "publish_date": "2026-08-03",
            "publish_estimate": "2026-08-03T03:30:00+00:00",
            "publish_earliest": "2026-08-03T03:00:00+00:00",
            "publish_latest": "2026-08-03T04:00:00+00:00",
            "precision": "hour",
            "confidence": "high_estimate",
        },
        {
            "publish_date": "2026-08-04",
            "publish_estimate": "2026-08-04T03:30:00.500000+00:00",
            "publish_earliest": "2026-08-04T03:00:00+00:00",
            "publish_latest": "2026-08-04T04:00:01+00:00",
            "precision": "hour",
            "confidence": "high_estimate",
        },
        {
            "publish_date": "2026-08-05",
            "publish_estimate": "2026-08-05T03:15:00+00:00",
            "publish_earliest": "2026-08-05T03:00:00+00:00",
            "publish_latest": "2026-08-05T03:30:00+00:00",
            "precision": "date",
            "confidence": "high_estimate",
        },
    ]

    result = build_posting_analytics(rows)

    assert result["time_eligible_count"] == 1
    assert result["estimated_time_count"] == 1
    assert result["excluded_time_count"] == 2
