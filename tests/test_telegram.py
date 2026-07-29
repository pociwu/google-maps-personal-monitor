import asyncio

import httpx

from maps_monitor.database import Database
from maps_monitor.telegram import TelegramSender


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
