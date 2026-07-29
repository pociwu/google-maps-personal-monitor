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
