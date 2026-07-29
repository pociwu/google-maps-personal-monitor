from __future__ import annotations

import csv
import json
import os
import shutil
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from .config import Settings
from .database import Database


def backup(db: Database, settings: Settings) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    destination = settings.backup_dir / stamp
    destination.mkdir(parents=True, exist_ok=False)
    target_db = sqlite3.connect(destination / "monitor.sqlite3")
    try:
        db.connection.backup(target_db)
    finally:
        target_db.close()
    shutil.copy2(settings.root / "config" / "targets.yaml", destination / "targets.yaml")
    image_snapshot = destination / "images"
    manifest: list[dict] = []
    for row in db.connection.execute(
        "SELECT sha256,local_path,byte_size FROM images WHERE status='saved' ORDER BY id"
    ):
        source = Path(row["local_path"])
        if not source.exists():
            continue
        relative = source.relative_to(settings.image_dir)
        linked = image_snapshot / relative
        linked.parent.mkdir(parents=True, exist_ok=True)
        if not linked.exists():
            os.link(source, linked)
        manifest.append({"sha256": row["sha256"], "path": str(relative), "bytes": row["byte_size"]})
    (destination / "images-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    cutoff = datetime.now() - timedelta(days=30)
    for folder in settings.backup_dir.iterdir():
        if folder.is_dir() and folder.name != stamp:
            try:
                created = datetime.strptime(folder.name, "%Y%m%d-%H%M%S")
            except ValueError:
                continue
            if created < cutoff:
                shutil.rmtree(folder)
    return destination


def export_reviews(db: Database, output: Path, format_name: str, target_name: str | None = None) -> int:
    query = """SELECT t.name AS target_name,t.url AS target_url,r.*,
               (SELECT GROUP_CONCAT(i.local_path, ' | ')
                FROM images i WHERE i.review_id=r.id AND i.status='saved') AS image_paths
               FROM reviews r JOIN targets t ON t.id=r.target_id"""
    params: tuple = ()
    if target_name:
        query += " WHERE t.name=?"
        params = (target_name,)
    query += " ORDER BY t.name,r.publish_date,r.id"
    rows = [dict(row) for row in db.connection.execute(query, params)]
    output.parent.mkdir(parents=True, exist_ok=True)
    if format_name == "json":
        output.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    elif format_name == "csv":
        with output.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()) if rows else ["target_name"])
            writer.writeheader()
            writer.writerows(rows)
    else:
        raise ValueError("匯出格式只能是 csv 或 json")
    return len(rows)
