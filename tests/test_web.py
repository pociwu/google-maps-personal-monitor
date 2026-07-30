import importlib
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from maps_monitor.database import Database


def _seed(tmp_path: Path) -> tuple[Path, Path, str]:
    data = tmp_path / "state" / "data"
    image_root = data / "images"
    database_path = data / "monitor.sqlite3"
    database = Database(database_path)
    now = "2026-07-30T00:00:00+00:00"
    target_id = database.connection.execute(
        """INSERT INTO targets
        (name,url,enabled,last_success_at,created_at,updated_at)
        VALUES('測試貢獻者','https://www.google.com/maps/contrib/1/reviews',1,?,?,?)""",
        (now, now, now),
    ).lastrowid
    active_id = database.connection.execute(
        """INSERT INTO reviews
        (target_id,review_key,review_url,place_name,rating,body,relative_time,publish_date,
         confidence,content_hash,status,first_seen_at,last_seen_at,modified_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,'active',?,?,?)""",
        (
            target_id, "r1", "https://www.google.com/maps/reviews/data=review-1",
            "有效店家", 5, "完整評論內容", "2 天前", "2026-07-28",
            "confirmed_date", "hash1", now, now, now,
        ),
    ).lastrowid
    database.connection.execute(
        """INSERT INTO reviews
        (target_id,review_key,place_name,rating,body,relative_time,publish_date,
         confidence,content_hash,status,first_seen_at,last_seen_at)
        VALUES(?,?,?,?,?,?,?,?,?,'active',?,?)""",
        (
            target_id, "r2", "僅評分店家", 4, "", "1 年前", "2025-07-30",
            "estimate", "hash2", now, now,
        ),
    )
    database.connection.execute(
        """INSERT INTO reviews
        (target_id,review_key,place_name,rating,body,relative_time,publish_date,
         confidence,content_hash,status,first_seen_at,last_seen_at,deleted_at)
        VALUES(?,?,?,?,?,?,?,?,?,'deleted',?,?,?)""",
        (
            target_id, "r3", "已刪除店家", 3, "歷史內容", "2 年前", "2024-07-30",
            "estimate", "hash3", now, now, now,
        ),
    )
    digest = "b" * 64
    original = image_root / digest[:2] / f"{digest}.jpg"
    thumbnail = image_root / "thumbnails" / digest[:2] / f"{digest}.webp"
    original.parent.mkdir(parents=True)
    thumbnail.parent.mkdir(parents=True)
    Image.new("RGB", (20, 20), "blue").save(original)
    Image.new("RGB", (10, 10), "blue").save(thumbnail)
    database.connection.execute(
        """INSERT INTO images
        (review_id,source_url,sha256,local_path,thumbnail_path,status,first_seen_at,last_seen_at)
        VALUES(?,?,?,?,?,'saved',?,?)""",
        (
            active_id, "https://example.test/image", digest, str(original),
            str(thumbnail), now, now,
        ),
    )
    historical_digest = "d" * 64
    historical_original = image_root / historical_digest[:2] / f"{historical_digest}.jpg"
    historical_thumbnail = (
        image_root / "thumbnails" / historical_digest[:2] / f"{historical_digest}.webp"
    )
    historical_original.parent.mkdir(parents=True)
    historical_thumbnail.parent.mkdir(parents=True)
    Image.new("RGB", (20, 20), "green").save(historical_original)
    Image.new("RGB", (10, 10), "green").save(historical_thumbnail)
    database.connection.execute(
        """INSERT INTO images
        (review_id,source_url,sha256,local_path,thumbnail_path,status,first_seen_at,
         last_seen_at,is_current)
        VALUES(?,?,?,?,?,'saved',?,?,0)""",
        (
            active_id, "https://example.test/historical", historical_digest,
            str(historical_original), str(historical_thumbnail), now, now,
        ),
    )
    database.connection.execute(
        """INSERT INTO observations
        (review_id,observed_at,relative_time,content_hash,parsed_count,parsed_unit,
         is_edit,exact_timestamp,crawl_complete)
        VALUES(?,?,?,?,?,?,?,?,1)""",
        (
            active_id, "2026-07-30T01:23:45+00:00", "2 天前", "hash1",
            2, "day", 0, "2026-07-28T03:04:05+00:00",
        ),
    )
    database.connection.commit()
    database.close()
    return database_path, image_root, digest


def _client(monkeypatch, database_path: Path, image_root: Path) -> TestClient:
    monkeypatch.setenv("MAPS_MONITOR_DATABASE", str(database_path))
    monkeypatch.setenv("MAPS_MONITOR_IMAGE_DIR", str(image_root))
    monkeypatch.setenv("DASHBOARD_STALE_WARNING_HOURS", "12")
    monkeypatch.setenv("DASHBOARD_STALE_CRITICAL_HOURS", "24")
    import maps_monitor.web

    module = importlib.reload(maps_monitor.web)
    return TestClient(module.app)


def test_dashboard_filters_and_noindex(tmp_path, monkeypatch):
    database_path, image_root, _digest = _seed(tmp_path)
    client = _client(monkeypatch, database_path, image_root)

    response = client.get("/")
    assert response.status_code == 200
    assert "有效店家" in response.text
    assert "僅評分店家" in response.text
    assert "已刪除店家" not in response.text
    assert "僅評分" in response.text
    assert "發表日期：2026-07-28" in response.text
    assert "發表日期：約 2025-07-30" in response.text
    assert "2 天前" in response.text
    assert "查看 Google 評論" in response.text
    assert "在 Google Maps 搜尋店家" in response.text
    assert 'target="_blank"' in response.text
    assert 'rel="noopener noreferrer"' in response.text
    assert "查看日期推算證據" in response.text
    assert "歷史圖片（1）" in response.text
    assert "2026-07-30 09:23:45" not in response.text
    assert response.headers["x-robots-tag"] == "noindex, nofollow"
    assert "google-analytics" not in response.text.lower()
    assert all(
        not (set(route.methods or set()) - {"GET", "HEAD"})
        for route in client.app.routes
        if hasattr(route, "methods")
    )
    assert client.get("/openapi.json").status_code == 404

    deleted = client.get("/?status=deleted")
    assert "已刪除店家" in deleted.text
    assert "有效店家" not in deleted.text

    evidence = client.get(f"/reviews/{1}/evidence")
    assert evidence.status_code == 200
    assert "觀察紀錄" in evidence.text
    assert "2026-07-30 09:23:45" in evidence.text
    assert "2026-07-28 11:04:05" in evidence.text
    assert "2 天" in evidence.text
    assert client.get("/reviews/999/evidence").status_code == 404


def test_media_is_database_addressed_and_path_safe(tmp_path, monkeypatch):
    database_path, image_root, digest = _seed(tmp_path)
    client = _client(monkeypatch, database_path, image_root)

    assert client.get(f"/media/{digest}/thumbnail").status_code == 200
    assert client.get(f"/media/{digest}/original").status_code == 200
    assert client.get("/media/not-a-hash/original").status_code == 404

    database = Database(database_path)
    outside = tmp_path / "outside.jpg"
    Image.new("RGB", (5, 5), "red").save(outside)
    bad_digest = "c" * 64
    review_id = database.connection.execute("SELECT id FROM reviews LIMIT 1").fetchone()[0]
    database.connection.execute(
        """INSERT INTO images
        (review_id,source_url,sha256,local_path,status,first_seen_at)
        VALUES(?,?,?,?, 'saved','2026-07-30T00:00:00+00:00')""",
        (review_id, "https://example.test/outside", bad_digest, str(outside)),
    )
    database.connection.commit()
    database.close()
    assert client.get(f"/media/{bad_digest}/original").status_code == 404


def test_health_and_safe_unavailable_page(tmp_path, monkeypatch):
    database_path, image_root, _digest = _seed(tmp_path)
    client = _client(monkeypatch, database_path, image_root)
    assert client.get("/healthz").json() == {"status": "ok"}

    missing_client = _client(monkeypatch, tmp_path / "missing.sqlite3", image_root)
    response = missing_client.get("/")
    assert response.status_code == 503
    assert "資料暫時無法使用" in response.text
    assert str(tmp_path) not in response.text

    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_text("not a sqlite database", encoding="utf-8")
    corrupt_client = _client(monkeypatch, corrupt, image_root)
    assert corrupt_client.get("/healthz").status_code == 503
    assert corrupt_client.get("/").headers["cache-control"] == "no-store"
