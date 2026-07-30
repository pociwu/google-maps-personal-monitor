import logging
import sqlite3

from maps_monitor import cli
from maps_monitor.database import Database
from maps_monitor.operations import refresh_dashboard_snapshot


def test_dashboard_snapshot_is_clean_read_only_database(tmp_path):
    source = tmp_path / "state" / "data" / "monitor.sqlite3"
    destination = tmp_path / "state" / "web" / "monitor.sqlite3"
    database = Database(source)
    now = "2026-07-30T00:00:00+00:00"
    database.connection.execute(
        """INSERT INTO targets
        (name,url,enabled,created_at,updated_at)
        VALUES('測試對象','https://www.google.com/maps/contrib/1/reviews',1,?,?)""",
        (now, now),
    )
    database.connection.commit()

    assert database.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    refresh_dashboard_snapshot(database, destination)

    snapshot = sqlite3.connect(f"file:{destination}?mode=ro", uri=True)
    try:
        assert snapshot.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert snapshot.execute("SELECT COUNT(*) FROM targets").fetchone()[0] == 1
    finally:
        snapshot.close()
        database.close()

    assert not destination.with_name(destination.name + "-wal").exists()
    assert not destination.with_name(destination.name + "-shm").exists()


def test_http_client_info_logs_are_disabled():
    cli.configure_logging()

    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING
