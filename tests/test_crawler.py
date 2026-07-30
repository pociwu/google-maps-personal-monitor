from maps_monitor.crawler import _safe_google_url


def test_only_https_google_links_are_exposed():
    review = "https://www.google.com/maps/reviews/data=review"
    short = "https://maps.app.goo.gl/example"

    assert _safe_google_url(review) == review
    assert _safe_google_url(short) == short
    assert _safe_google_url("http://www.google.com/maps/reviews/data=x") is None
    assert _safe_google_url("https://example.test/?reviewId=x") is None
