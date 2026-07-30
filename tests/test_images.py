import asyncio
import io
from pathlib import Path

import httpx
from PIL import Image

from maps_monitor.database import Database
import maps_monitor.images as images_module
from maps_monitor.images import ImageArchive


def test_builds_webp_thumbnail_without_changing_original(tmp_path: Path):
    database = Database(tmp_path / "data" / "monitor.sqlite3")
    now = "2026-07-30T00:00:00+00:00"
    target_id = database.connection.execute(
        """INSERT INTO targets(name,url,created_at,updated_at)
        VALUES('甲','https://www.google.com/maps/contrib/1/reviews',?,?)""",
        (now, now),
    ).lastrowid
    review_id = database.connection.execute(
        """INSERT INTO reviews
        (target_id,review_key,place_name,body,relative_time,content_hash,first_seen_at,last_seen_at)
        VALUES(?,?,?,?,?,?,?,?)""",
        (target_id, "r1", "店家", "文字", "1 天前", "hash", now, now),
    ).lastrowid
    image_root = tmp_path / "data" / "images"
    source = image_root / "aa" / "original.png"
    source.parent.mkdir(parents=True)
    Image.new("RGB", (1200, 600), "red").save(source)
    image_id = database.connection.execute(
        """INSERT INTO images
        (review_id,source_url,sha256,local_path,status,first_seen_at)
        VALUES(?,?,?,?, 'saved', ?)""",
        (review_id, "https://example.test/image", "a" * 64, str(source), now),
    ).lastrowid
    database.connection.commit()

    archive = ImageArchive(image_root, 0, 0)
    built, failed = archive.build_missing_thumbnails(database)
    row = database.connection.execute(
        "SELECT thumbnail_path FROM images WHERE id=?", (image_id,)
    ).fetchone()

    assert (built, failed) == (1, 0)
    thumbnail = Path(row["thumbnail_path"])
    assert thumbnail.suffix == ".webp"
    assert source.exists()
    with Image.open(thumbnail) as rendered:
        assert max(rendered.size) == 480
    database.close()


def _png_bytes(color: str = "red") -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (20, 20), color).save(output, format="PNG")
    return output.getvalue()


def _review(database: Database, key: str = "r1") -> int:
    now = "2026-07-30T00:00:00+00:00"
    target = database.connection.execute(
        """INSERT OR IGNORE INTO targets(name,url,created_at,updated_at)
        VALUES('甲','https://www.google.com/maps/contrib/1/reviews',?,?)""",
        (now, now),
    )
    target_id = target.lastrowid or database.connection.execute(
        "SELECT id FROM targets WHERE url='https://www.google.com/maps/contrib/1/reviews'"
    ).fetchone()[0]
    review_id = database.connection.execute(
        """INSERT INTO reviews
        (target_id,review_key,place_name,body,relative_time,content_hash,first_seen_at,last_seen_at)
        VALUES(?,?,?,?,?,?,?,?)""",
        (target_id, key, "店家", "文字", "1 天前", "hash", now, now),
    ).lastrowid
    database.connection.commit()
    return int(review_id)


def test_archive_deduplicates_content_and_confirms_removal_after_two_runs(
    tmp_path: Path, monkeypatch
):
    content = _png_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=content, headers={"content-type": "image/png"})

    real_client = httpx.AsyncClient

    def client_factory(*_args, **_kwargs):
        return real_client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(images_module.httpx, "AsyncClient", client_factory)
    database = Database(tmp_path / "data" / "monitor.sqlite3")
    review_id = _review(database)
    archive = ImageArchive(tmp_path / "data" / "images", 0, 0)

    first = asyncio.run(
        archive.archive(
            database,
            review_id,
            ("https://example.test/image-a", "https://example.test/image-b"),
        )
    )
    rows = database.connection.execute(
        "SELECT id,sha256,is_current,missing_count FROM images WHERE review_id=?",
        (review_id,),
    ).fetchall()

    assert first.complete is True
    assert first.current_count == 1
    assert first.added_count == 1
    assert len(rows) == 1

    first_missing = asyncio.run(archive.archive(database, review_id, ()))
    second_missing = asyncio.run(archive.archive(database, review_id, ()))
    assert first_missing.current_count == 1
    assert first_missing.removed_count == 0
    assert second_missing.current_count == 0
    assert second_missing.removed_count == 1
    database.close()


def test_incomplete_image_download_preserves_previous_current_set(tmp_path: Path, monkeypatch):
    content = _png_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("failed"):
            return httpx.Response(503)
        return httpx.Response(200, content=content, headers={"content-type": "image/png"})

    real_client = httpx.AsyncClient

    def client_factory(*_args, **_kwargs):
        return real_client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(images_module.httpx, "AsyncClient", client_factory)
    database = Database(tmp_path / "data" / "monitor.sqlite3")
    review_id = _review(database)
    archive = ImageArchive(tmp_path / "data" / "images", 0, 0)
    asyncio.run(archive.archive(database, review_id, ("https://example.test/current",)))

    result = asyncio.run(
        archive.archive(
            database,
            review_id,
            ("https://example.test/current", "https://example.test/failed"),
        )
    )

    assert result.complete is False
    assert result.current_count == 1
    assert result.added_count == 0
    assert result.removed_count == 0
    database.close()
