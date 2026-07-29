from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path
from typing import Iterable

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError

from .database import Database
from .util import iso_now


MAX_IMAGE_BYTES = 25 * 1024 * 1024
THUMBNAIL_MAX_EDGE = 480
THUMBNAIL_QUALITY = 82


class ImageArchive:
    def __init__(self, root: Path, min_free_gb: int, min_free_percent: int):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.min_free_bytes = min_free_gb * 1024**3
        self.min_free_percent = min_free_percent

    def has_capacity(self) -> bool:
        usage = shutil.disk_usage(self.root)
        free_percent = usage.free * 100 / usage.total
        return usage.free >= self.min_free_bytes and free_percent >= self.min_free_percent

    def _thumbnail_path(self, digest: str) -> Path:
        return self.root / "thumbnails" / digest[:2] / f"{digest}.webp"

    def create_thumbnail(self, source: Path, digest: str) -> Path:
        destination = self._thumbnail_path(digest)
        if destination.exists():
            return destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(".webp.part")
        try:
            with Image.open(source) as image:
                image.seek(0)
                image = ImageOps.exif_transpose(image)
                if image.mode not in ("RGB", "RGBA"):
                    image = image.convert("RGBA" if "transparency" in image.info else "RGB")
                image.thumbnail((THUMBNAIL_MAX_EDGE, THUMBNAIL_MAX_EDGE), Image.Resampling.LANCZOS)
                image.save(temporary, format="WEBP", quality=THUMBNAIL_QUALITY, method=6)
            os.replace(temporary, destination)
            return destination
        except (OSError, UnidentifiedImageError):
            temporary.unlink(missing_ok=True)
            raise

    def build_missing_thumbnails(self, db: Database) -> tuple[int, int]:
        built = 0
        failed = 0
        rows = db.connection.execute(
            """SELECT id,sha256,local_path,thumbnail_path FROM images
            WHERE status='saved' AND sha256 IS NOT NULL AND local_path IS NOT NULL"""
        ).fetchall()
        for row in rows:
            existing = Path(row["thumbnail_path"]) if row["thumbnail_path"] else None
            if existing and existing.exists():
                continue
            try:
                thumbnail = self.create_thumbnail(Path(row["local_path"]), row["sha256"])
                db.connection.execute(
                    "UPDATE images SET thumbnail_path=?,error=NULL WHERE id=?",
                    (str(thumbnail), row["id"]),
                )
                built += 1
            except Exception as exc:
                db.connection.execute(
                    "UPDATE images SET error=? WHERE id=?",
                    (f"縮圖建立失敗：{str(exc)[:1900]}", row["id"]),
                )
                failed += 1
        db.connection.commit()
        return built, failed

    async def archive(self, db: Database, review_id: int, urls: Iterable[str]) -> list[str]:
        saved: list[str] = []
        if not self.has_capacity():
            return saved
        async with httpx.AsyncClient(follow_redirects=True, timeout=60.0) as client:
            for source_url in dict.fromkeys(urls):
                row = db.connection.execute(
                    "SELECT * FROM images WHERE review_id=? AND source_url=?",
                    (review_id, source_url),
                ).fetchone()
                if row and row["status"] == "saved" and row["local_path"]:
                    if not row["thumbnail_path"] or not Path(row["thumbnail_path"]).exists():
                        try:
                            thumbnail = self.create_thumbnail(Path(row["local_path"]), row["sha256"])
                            db.connection.execute(
                                "UPDATE images SET thumbnail_path=?,error=NULL WHERE id=?",
                                (str(thumbnail), row["id"]),
                            )
                            db.connection.commit()
                        except Exception:
                            pass
                    saved.append(str(row["local_path"]))
                    continue
                if not row:
                    cursor = db.connection.execute(
                        "INSERT INTO images(review_id,source_url,status,first_seen_at) VALUES(?,?,'pending',?)",
                        (review_id, source_url, iso_now()),
                    )
                    image_id = int(cursor.lastrowid)
                    db.connection.commit()
                else:
                    image_id = int(row["id"])
                try:
                    response = await client.get(source_url)
                    response.raise_for_status()
                    content = response.content
                    if len(content) > MAX_IMAGE_BYTES:
                        raise ValueError("單張圖片超過 25 MB")
                    digest = hashlib.sha256(content).hexdigest()
                    content_type = response.headers.get("content-type", "").lower()
                    extension = {
                        "image/jpeg": ".jpg",
                        "image/png": ".png",
                        "image/webp": ".webp",
                        "image/gif": ".gif",
                        "image/avif": ".avif",
                    }.get(content_type.split(";", 1)[0], ".img")
                    destination = self.root / digest[:2] / f"{digest}{extension}"
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if not destination.exists():
                        temporary = destination.with_suffix(destination.suffix + ".part")
                        temporary.write_bytes(content)
                        os.replace(temporary, destination)
                    thumbnail = self.create_thumbnail(destination, digest)
                    db.connection.execute(
                        """UPDATE images SET sha256=?,local_path=?,thumbnail_path=?,byte_size=?,
                        status='saved',error=NULL
                        WHERE id=?""",
                        (digest, str(destination), str(thumbnail), len(content), image_id),
                    )
                    db.connection.commit()
                    saved.append(str(destination))
                except Exception as exc:
                    db.connection.execute(
                        "UPDATE images SET status='failed',error=? WHERE id=?",
                        (str(exc)[:2000], image_id),
                    )
                    db.connection.commit()
                    detail = db.connection.execute(
                        """SELECT t.id AS target_id,t.name AS target_name,t.url AS target_url,
                        r.place_name,r.place_url FROM reviews r
                        JOIN targets t ON t.id=r.target_id WHERE r.id=?""",
                        (review_id,),
                    ).fetchone()
                    db.create_event(
                        "image_failure",
                        {
                            "target_name": detail["target_name"],
                            "target_url": detail["target_url"],
                            "place_name": detail["place_name"],
                            "place_url": detail["place_url"],
                            "source_url": source_url,
                            "error": str(exc),
                        },
                        target_id=detail["target_id"],
                        review_id=review_id,
                        event_key=f"image-failure:{image_id}:{hashlib.sha256(str(exc).encode()).hexdigest()}",
                    )
        return saved
