import asyncio

import httpx

from maps_monitor.database import Database
from maps_monitor.telegram import TelegramSender, format_event


class AmbiguousClient:
    calls = 0

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None

    async def post(self, *args, **kwargs):
        type(self).calls += 1
        raise httpx.ReadTimeout("結果不明")


def test_ambiguous_telegram_result_is_never_retried(tmp_path, monkeypatch):
    db = Database(tmp_path / "monitor.sqlite3")
    db.create_event("test", {"message": "x"})
    monkeypatch.setattr("maps_monitor.telegram.httpx.AsyncClient", AmbiguousClient)
    sender = TelegramSender(db, "token", "chat", (0, 0))
    asyncio.run(sender.send_pending())
    asyncio.run(sender.send_pending())
    row = db.connection.execute("SELECT delivery_state,attempts FROM events").fetchone()
    assert row["delivery_state"] == "attempted"
    assert row["attempts"] == 1
    assert AmbiguousClient.calls == 1
    db.close()


def test_low_confidence_date_event_is_suppressed(tmp_path):
    db = Database(tmp_path / "monitor.sqlite3")
    db.create_event(
        "date_changed",
        {"publish_date": "2026-07-30", "confidence": "high_estimate"},
    )
    sender = TelegramSender(db, "unused-token", "unused-chat", (0, 0))

    assert asyncio.run(sender.send_pending()) == (0, 0)
    row = db.connection.execute(
        "SELECT delivery_state,attempts,last_error FROM events"
    ).fetchone()
    assert row["delivery_state"] == "suppressed"
    assert row["attempts"] == 0
    assert "尚未達高可信" in row["last_error"]
    db.close()


def test_modified_message_contains_before_and_after_summary():
    message = format_event(
        "modified",
        {
            "target_name": "甲",
            "place_name": "店家",
            "rating": 4,
            "previous_rating": 5,
            "previous_text": "原本內容",
            "text": "修改後內容",
        },
    )
    assert "星等變更：5 → 4" in message
    assert "修改前：原本內容" in message
    assert "修改後：修改後內容" in message
