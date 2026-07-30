import ast
from pathlib import Path

import maps_monitor.crawler as crawler


FORBIDDEN = {
    "click",
    "dblclick",
    "tap",
    "press",
    "fill",
    "type",
    "check",
    "uncheck",
    "select_option",
    "dispatch_event",
}


def test_crawler_contains_no_interactive_browser_calls():
    path = Path(crawler.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert calls.isdisjoint(FORBIDDEN)


def test_page_scripts_do_not_activate_elements():
    combined = crawler.EXTRACT_SCRIPT + crawler.SCROLL_SCRIPT
    assert ".click(" not in combined
    assert "dispatchEvent" not in combined
    assert "submit(" not in combined


def test_only_review_text_expander_script_may_activate_an_element():
    script = crawler.EXPAND_REVIEW_TEXT_SCRIPT
    assert script.count(".click(") == 1
    assert "/^(更多|顯示更多|閱讀更多|more|show more|read more)$/i" in script
    for forbidden in ("按讚", "有幫助", "回覆", "回應", "分享", "like", "helpful", "reply", "share"):
        assert forbidden in script
    assert "dispatchEvent" not in script
    assert "submit(" not in script
