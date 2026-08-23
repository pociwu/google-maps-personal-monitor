from maps_monitor.crawler import contributor_from_raw


def test_contributor_profile_parses_ratio_points_and_level():
    profile = contributor_from_raw(
        {
            "avatar_url": "https://lh3.googleusercontent.com/avatar=s256",
            "header_text": "Amber PENG\n在地嚮導 第 6 級\n3,454/5,000 分",
        }
    )

    assert profile.avatar_url == "https://lh3.googleusercontent.com/avatar=s256"
    assert profile.local_guide_level == 6
    assert profile.local_guide_points == 3454
    assert profile.next_level_points == 5000


def test_contributor_profile_derives_points_from_remaining_score():
    profile = contributor_from_raw(
        {"header_text": "吳佳欣\n在地嚮導 第 3 級\n再累積 91分即可升到第 4 級"}
    )

    assert profile.local_guide_level == 3
    assert profile.local_guide_points == 159
    assert profile.next_level_points == 250


def test_contributor_profile_tolerates_missing_guide_data():
    profile = contributor_from_raw({"avatar_url": "https://example.test/avatar"})

    assert profile.avatar_url == "https://example.test/avatar"
    assert profile.local_guide_level is None
    assert profile.local_guide_points is None
