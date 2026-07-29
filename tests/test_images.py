from pathlib import Path

from PIL import Image

from maps_monitor.database import Database
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
