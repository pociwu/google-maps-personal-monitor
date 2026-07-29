import sqlite3

from maps_monitor.database import Database


def test_old_database_is_backed_up_and_migrated(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    path = data / "monitor.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """CREATE TABLE reviews (
        id INTEGER PRIMARY KEY,publish_date TEXT,date_source TEXT DEFAULT 'relative'
        );
        CREATE TABLE observations (
        id INTEGER PRIMARY KEY,review_id INTEGER,observed_at TEXT,relative_time TEXT,content_hash TEXT
        );
        CREATE TABLE meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);
        INSERT INTO reviews(id,publish_date) VALUES(1,'2026-07-09');"""
    )
    connection.close()

    db = Database(path)
    columns = {row[1] for row in db.connection.execute("PRAGMA table_info(reviews)")}
    assert "publish_estimate" in columns
    row = db.connection.execute("SELECT legacy_publish_date FROM reviews WHERE id=1").fetchone()
    assert row[0] == "2026-07-09"
    assert list((tmp_path / "backups").glob("pre-schema-v3-*.sqlite3"))
    db.close()
