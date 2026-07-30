from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError

from .database import Database
from .image_fingerprint import (
    decoded_pixel_sha256,
    visual_fingerprint,
    visually_equivalent,
)
from .util import iso_now


MAX_IMAGE_BYTES = 25 * 1024 * 1024
THUMBNAIL_MAX_EDGE = 480
THUMBNAIL_QUALITY = 82


@dataclass(frozen=True, slots=True)
class ImageSyncResult:
    saved_paths: tuple[str, ...]
    current_count: int
    added_count: int
    removed_count: int
    complete: bool


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

    async def archive(self, db: Database, review_id: int, urls: Iterable[str]) -> ImageSyncResult:
        source_urls = list(dict.fromkeys(urls))
        prior_current = {
            str(row["sha256"])
            for row in db.connection.execute(
                """SELECT sha256 FROM images
                WHERE review_id=? AND status='saved' AND sha256 IS NOT NULL AND is_current=1""",
                (review_id,),
            ).fetchall()
        }
        if source_urls and not self.has_capacity():
            return ImageSyncResult((), len(prior_current), 0, 0, False)
        saved: dict[str, str] = {}
        seen: set[str] = set()
        complete = True
        async with httpx.AsyncClient(follow_redirects=True, timeout=60.0) as client:
            for source_url in source_urls:
                row = db.connection.execute(
                    "SELECT * FROM images WHERE review_id=? AND source_url=?",
                    (review_id, source_url),
                ).fetchone()
                if (
                    row
                    and row["status"] == "saved"
                    and row["sha256"]
                    and row["local_path"]
                    and Path(row["local_path"]).exists()
                ):
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
                    digest = str(row["sha256"])
                    seen.add(digest)
                    saved[digest] = str(row["local_path"])
                    db.connection.execute(
                        "UPDATE images SET last_seen_at=?,error=NULL WHERE id=?",
                        (iso_now(), row["id"]),
                    )
                    db.connection.commit()
                    continue
                if not row:
                    cursor = db.connection.execute(
                        """INSERT INTO images
                        (review_id,source_url,status,first_seen_at,is_current,missing_count)
                        VALUES(?,?,'pending',?,0,0)""",
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
                    pixel_digest = decoded_pixel_sha256(destination)
                    candidate_fingerprint = visual_fingerprint(destination)
                    thumbnail = self.create_thumbnail(destination, digest)
                    canonical = db.connection.execute(
                        """SELECT id,sha256,local_path,visual_hash FROM images
                        WHERE review_id=? AND status='saved' AND id<>?
                          AND (sha256=? OR pixel_sha256=?)
                        ORDER BY id LIMIT 1""",
                        (review_id, image_id, digest, pixel_digest),
                    ).fetchone()
                    if canonical is None:
                        candidates = db.connection.execute(
                            """SELECT id,sha256,local_path,visual_hash FROM images
                            WHERE review_id=? AND status='saved' AND id<>?
                              AND visual_hash IS NOT NULL AND local_path IS NOT NULL
                            ORDER BY id""",
                            (review_id, image_id),
                        ).fetchall()
                        for possible in candidates:
                            possible_path = Path(possible["local_path"])
                            if not possible_path.exists():
                                continue
                            possible_fingerprint = visual_fingerprint(possible_path)
                            if visually_equivalent(
                                possible_fingerprint,
                                candidate_fingerprint,
                            ):
                                canonical = possible
                                break
                    if canonical:
                        db.connection.execute(
                            "UPDATE images SET last_seen_at=?,error=NULL WHERE id=?",
                            (iso_now(), canonical["id"]),
                        )
                        db.connection.execute("DELETE FROM images WHERE id=?", (image_id,))
                        canonical_digest = str(canonical["sha256"])
                        saved_path = str(canonical["local_path"] or destination)
                    else:
                        db.connection.execute(
                            """UPDATE images SET sha256=?,pixel_sha256=?,visual_hash=?,
                            local_path=?,thumbnail_path=?,byte_size=?,status='saved',
                            error=NULL,last_seen_at=? WHERE id=?""",
                            (
                                digest,
                                pixel_digest,
                                candidate_fingerprint.difference_hash,
                                str(destination),
                                str(thumbnail),
                                len(content),
                                iso_now(),
                                image_id,
                            ),
                        )
                        canonical_digest = digest
                        saved_path = str(destination)
                    db.connection.commit()
                    seen.add(canonical_digest)
                    saved[canonical_digest] = saved_path
                except Exception as exc:
                    complete = False
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
        removed_count = 0
        if complete:
            current_rows = db.connection.execute(
                """SELECT id,sha256,missing_count FROM images
                WHERE review_id=? AND status='saved' AND sha256 IS NOT NULL AND is_current=1""",
                (review_id,),
            ).fetchall()
            for row in current_rows:
                digest = str(row["sha256"])
                if digest in seen:
                    db.connection.execute(
                        "UPDATE images SET missing_count=0,last_seen_at=? WHERE id=?",
                        (iso_now(), row["id"]),
                    )
                    continue
                missing_count = int(row["missing_count"]) + 1
                if missing_count >= 2:
                    db.connection.execute(
                        "UPDATE images SET is_current=0,missing_count=? WHERE id=?",
                        (missing_count, row["id"]),
                    )
                    removed_count += 1
                else:
                    db.connection.execute(
                        "UPDATE images SET missing_count=? WHERE id=?",
                        (missing_count, row["id"]),
                    )
            if seen:
                placeholders = ",".join("?" for _ in seen)
                db.connection.execute(
                    f"""UPDATE images SET is_current=1,missing_count=0,last_seen_at=?
                    WHERE review_id=? AND status='saved' AND sha256 IN ({placeholders})""",
                    (iso_now(), review_id, *sorted(seen)),
                )
            db.connection.commit()
        current_count = int(
            db.connection.execute(
                """SELECT COUNT(*) FROM images
                WHERE review_id=? AND status='saved' AND sha256 IS NOT NULL AND is_current=1""",
                (review_id,),
            ).fetchone()[0]
        )
        added_count = len(seen - prior_current) if complete else 0
        return ImageSyncResult(
            tuple(saved.values()),
            current_count,
            added_count,
            removed_count,
            complete,
        )
