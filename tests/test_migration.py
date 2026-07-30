import sqlite3

from maps_monitor.database import Database
from maps_monitor.util import stable_hash


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
    assert list((tmp_path / "backups").glob("pre-schema-v4-*.sqlite3"))
    db.close()


def test_schema_v4_backs_up_and_removes_duplicate_image_references(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    path = data / "monitor.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE targets (
            id INTEGER PRIMARY KEY,name TEXT,url TEXT,enabled INTEGER DEFAULT 1,
            baseline_complete INTEGER DEFAULT 0,observation_until TEXT,
            consecutive_failures INTEGER DEFAULT 0,failure_alerted INTEGER DEFAULT 0,
            last_success_at TEXT,created_at TEXT,updated_at TEXT
        );
        CREATE TABLE reviews (
            id INTEGER PRIMARY KEY,target_id INTEGER,review_key TEXT,google_review_id TEXT,
            place_id TEXT,place_name TEXT,place_url TEXT,rating REAL,body TEXT,
            relative_time TEXT,publish_date TEXT,date_source TEXT DEFAULT 'relative',
            legacy_publish_date TEXT,publish_estimate TEXT,publish_earliest TEXT,
            publish_latest TEXT,precision TEXT DEFAULT 'unknown',confidence TEXT DEFAULT 'estimate',
            basis TEXT DEFAULT 'legacy',time_subject TEXT DEFAULT 'display_time',
            date_model_version TEXT,edit_date TEXT,edit_estimate TEXT,edit_earliest TEXT,
            edit_latest TEXT,edit_confidence TEXT,edit_basis TEXT,photo_count INTEGER DEFAULT 0,
            content_hash TEXT,status TEXT DEFAULT 'active',missing_count INTEGER DEFAULT 0,
            first_seen_at TEXT,last_seen_at TEXT,modified_at TEXT,deleted_at TEXT
        );
        CREATE TABLE images (
            id INTEGER PRIMARY KEY,review_id INTEGER,source_url TEXT,sha256 TEXT,
            local_path TEXT,thumbnail_path TEXT,byte_size INTEGER,status TEXT,error TEXT,
            first_seen_at TEXT,UNIQUE(review_id,source_url)
        );
        """
    )
    now = "2026-07-30T00:00:00+00:00"
    connection.execute(
        "INSERT INTO targets(id,name,url,created_at,updated_at) VALUES(1,'甲','u',?,?)",
        (now, now),
    )
    connection.execute(
        """INSERT INTO reviews
        (id,target_id,review_key,place_name,body,relative_time,content_hash,first_seen_at,last_seen_at)
        VALUES(1,1,'r','店家','內容','1 天前','h',?,?)""",
        (now, now),
    )
    for image_id, source_url in ((1, "https://example.test/a"), (2, "https://example.test/b")):
        connection.execute(
            """INSERT INTO images
            (id,review_id,source_url,sha256,local_path,status,first_seen_at)
            VALUES(?,?,?,?,?,'saved',?)""",
            (image_id, 1, source_url, "a" * 64, "/images/a.jpg", now),
        )
    connection.commit()
    connection.close()

    database = Database(path)
    rows = database.connection.execute(
        "SELECT id,is_current,missing_count,last_seen_at FROM images ORDER BY id"
    ).fetchall()
    migrated_hash = database.connection.execute(
        "SELECT content_hash FROM reviews WHERE id=1"
    ).fetchone()[0]
    schema_version = database.get_meta("schema_version")
    review_columns = {
        row[1] for row in database.connection.execute("PRAGMA table_info(reviews)").fetchall()
    }
    database.close()

    assert [row["id"] for row in rows] == [1]
    assert rows[0]["is_current"] == 1
    assert rows[0]["missing_count"] == 0
    assert rows[0]["last_seen_at"] == now
    assert "review_url" in review_columns
    assert schema_version == "4"
    assert migrated_hash == stable_hash(
        {"place_name": "店家", "place_url": None, "rating": None, "text": "內容"}
    )
    assert list((tmp_path / "backups").glob("pre-schema-v4-*.sqlite3"))
