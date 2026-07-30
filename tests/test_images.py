import asyncio
import io
from pathlib import Path

import httpx
from PIL import Image, PngImagePlugin

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


def _png_bytes(color: str = "red", metadata: str | None = None) -> bytes:
    output = io.BytesIO()
    pnginfo = None
    if metadata is not None:
        pnginfo = PngImagePlugin.PngInfo()
        pnginfo.add_text("variant", metadata)
    Image.new("RGB", (20, 20), color).save(output, format="PNG", pnginfo=pnginfo)
    return output.getvalue()


def _jpeg_bytes(quality: int, changed: bool = False) -> bytes:
    image = Image.new("RGB", (256, 256))
    image.putdata(
        [
            (
                (x * 7 + y * 3) % 256,
                (x * 5 + y * 11) % 256,
                (x * 13 + y * 2) % 256,
            )
            for y in range(256)
            for x in range(256)
        ]
    )
    if changed:
        for y in range(80, 176):
            for x in range(80, 176):
                image.putpixel((x, y), (20, 240, 40))
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=quality)
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


def test_archive_deduplicates_same_pixels_with_different_file_bytes(
    tmp_path: Path, monkeypatch
):
    variants = {
        "/image-a": _png_bytes(metadata="first encoding"),
        "/image-b": _png_bytes(metadata="second encoding"),
    }
    assert variants["/image-a"] != variants["/image-b"]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=variants[request.url.path],
            headers={"content-type": "image/png"},
        )

    real_client = httpx.AsyncClient

    def client_factory(*_args, **_kwargs):
        return real_client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(images_module.httpx, "AsyncClient", client_factory)
    database = Database(tmp_path / "data" / "monitor.sqlite3")
    review_id = _review(database)
    archive = ImageArchive(tmp_path / "data" / "images", 0, 0)

    result = asyncio.run(
        archive.archive(
            database,
            review_id,
            ("https://example.test/image-a", "https://example.test/image-b"),
        )
    )
    rows = database.connection.execute(
        """SELECT sha256 FROM images
        WHERE review_id=? AND status='saved' AND is_current=1""",
        (review_id,),
    ).fetchall()

    assert result.current_count == 1
    assert len(rows) == 1
    database.close()


def test_archive_keeps_images_when_even_one_pixel_differs(tmp_path: Path, monkeypatch):
    first = Image.new("RGB", (20, 20), "red")
    second = first.copy()
    second.putpixel((10, 10), (254, 0, 0))
    variants = {}
    for path, image in (("/image-a", first), ("/image-b", second)):
        output = io.BytesIO()
        image.save(output, format="PNG")
        variants[path] = output.getvalue()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=variants[request.url.path],
            headers={"content-type": "image/png"},
        )

    real_client = httpx.AsyncClient

    def client_factory(*_args, **_kwargs):
        return real_client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(images_module.httpx, "AsyncClient", client_factory)
    database = Database(tmp_path / "data" / "monitor.sqlite3")
    review_id = _review(database)
    archive = ImageArchive(tmp_path / "data" / "images", 0, 0)

    result = asyncio.run(
        archive.archive(
            database,
            review_id,
            ("https://example.test/image-a", "https://example.test/image-b"),
        )
    )
    rows = database.connection.execute(
        """SELECT sha256,pixel_sha256 FROM images
        WHERE review_id=? AND status='saved' AND is_current=1""",
        (review_id,),
    ).fetchall()

    assert result.current_count == 2
    assert len(rows) == 2
    assert len({row["pixel_sha256"] for row in rows}) == 2
    database.close()


def test_archive_deduplicates_google_style_jpeg_recompression(
    tmp_path: Path, monkeypatch
):
    variants = {
        "/image-a": _jpeg_bytes(95),
        "/image-b": _jpeg_bytes(90),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=variants[request.url.path],
            headers={"content-type": "image/jpeg"},
        )

    real_client = httpx.AsyncClient

    def client_factory(*_args, **_kwargs):
        return real_client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(images_module.httpx, "AsyncClient", client_factory)
    database = Database(tmp_path / "data" / "monitor.sqlite3")
    review_id = _review(database)
    archive = ImageArchive(tmp_path / "data" / "images", 0, 0)

    result = asyncio.run(
        archive.archive(
            database,
            review_id,
            ("https://example.test/image-a", "https://example.test/image-b"),
        )
    )

    assert result.current_count == 1
    database.close()


def test_archive_keeps_visually_similar_but_different_jpegs(tmp_path: Path, monkeypatch):
    variants = {
        "/image-a": _jpeg_bytes(95),
        "/image-b": _jpeg_bytes(90, changed=True),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=variants[request.url.path],
            headers={"content-type": "image/jpeg"},
        )

    real_client = httpx.AsyncClient

    def client_factory(*_args, **_kwargs):
        return real_client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(images_module.httpx, "AsyncClient", client_factory)
    database = Database(tmp_path / "data" / "monitor.sqlite3")
    review_id = _review(database)
    archive = ImageArchive(tmp_path / "data" / "images", 0, 0)

    result = asyncio.run(
        archive.archive(
            database,
            review_id,
            ("https://example.test/image-a", "https://example.test/image-b"),
        )
    )

    assert result.current_count == 2
    database.close()
