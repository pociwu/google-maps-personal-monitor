from maps_monitor.crawler import (
    EXPAND_REVIEW_TEXT_SCRIPT,
    _looks_truncated_text,
    _safe_google_url,
)


def test_only_https_google_links_are_exposed():
    review = "https://www.google.com/maps/reviews/data=review"
    short = "https://maps.app.goo.gl/example"

    assert _safe_google_url(review) == review
    assert _safe_google_url(short) == short
    assert _safe_google_url("http://www.google.com/maps/reviews/data=x") is None
    assert _safe_google_url("https://example.test/?reviewId=x") is None


def test_detects_google_truncated_review_text():
    assert _looks_truncated_text("好好吃…更多")
    assert _looks_truncated_text("Good food... More")
    assert not _looks_truncated_text("想知道更多")
    assert not _looks_truncated_text("全文內容")


def test_expand_script_has_exact_allowlist_and_interaction_denylist():
    assert "/^(更多|顯示更多|閱讀更多|more|show more|read more)$/i" in (
        EXPAND_REVIEW_TEXT_SCRIPT
    )
    for forbidden in ("按讚", "有幫助", "回覆", "分享", "like", "helpful", "reply", "share"):
        assert forbidden in EXPAND_REVIEW_TEXT_SCRIPT
