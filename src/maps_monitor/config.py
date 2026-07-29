from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True, slots=True)
class TargetConfig:
    name: str
    url: str
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class Settings:
    root: Path
    data_dir: Path
    database: Path
    image_dir: Path
    debug_dir: Path
    backup_dir: Path
    timezone: str
    locale: str
    observation_hours: int
    profile_delay_seconds: tuple[int, int]
    telegram_delay_seconds: tuple[int, int]
    delete_after_missing_runs: int
    max_profile_minutes: int
    disk_min_free_gb: int
    disk_min_free_percent: int
    targets: tuple[TargetConfig, ...]
    telegram_token: str | None
    telegram_chat_id: str | None


def _pair(value: Any, name: str) -> tuple[int, int]:
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(f"{name} 必須是含兩個整數的陣列")
    low, high = int(value[0]), int(value[1])
    if low < 0 or high < low:
        raise ValueError(f"{name} 範圍無效")
    return low, high


def load_settings(config_path: str | Path = "config/targets.yaml") -> Settings:
    path = Path(config_path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"找不到設定檔：{path}；請複製 config/targets.example.yaml")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw_targets = raw.get("targets", [])
    if len(raw_targets) > 10:
        raise ValueError("最多只能設定 10 位監控對象")
    targets: list[TargetConfig] = []
    seen_urls: set[str] = set()
    seen_names: set[str] = set()
    for item in raw_targets:
        target = TargetConfig(
            name=str(item["name"]).strip(),
            url=str(item["url"]).strip(),
            enabled=bool(item.get("enabled", True)),
        )
        if not target.url.startswith("https://www.google.com/maps/contrib/"):
            raise ValueError(f"{target.name} 不是允許的公開貢獻者網址")
        if target.url in seen_urls:
            raise ValueError(f"重複的監控網址：{target.url}")
        if target.name in seen_names:
            raise ValueError(f"重複的監控名稱：{target.name}")
        seen_urls.add(target.url)
        seen_names.add(target.name)
        targets.append(target)

    root = path.parent.parent
    data_dir = Path(os.getenv("MAPS_MONITOR_DATA_DIR", root / "data")).resolve()
    backup_dir = Path(os.getenv("MAPS_MONITOR_BACKUP_DIR", root / "backups")).resolve()
    debug_dir = Path(os.getenv("MAPS_MONITOR_DEBUG_DIR", root / "debug")).resolve()
    for directory in (data_dir, backup_dir, debug_dir, data_dir / "images"):
        directory.mkdir(parents=True, exist_ok=True)
    return Settings(
        root=root,
        data_dir=data_dir,
        database=data_dir / "monitor.sqlite3",
        image_dir=data_dir / "images",
        debug_dir=debug_dir,
        backup_dir=backup_dir,
        timezone=str(raw.get("timezone", "Asia/Taipei")),
        locale=str(raw.get("locale", "zh-TW")),
        observation_hours=int(raw.get("observation_hours", 24)),
        profile_delay_seconds=_pair(raw.get("profile_delay_seconds", [20, 60]), "profile_delay_seconds"),
        telegram_delay_seconds=_pair(raw.get("telegram_delay_seconds", [6, 10]), "telegram_delay_seconds"),
        delete_after_missing_runs=int(raw.get("delete_after_missing_runs", 3)),
        max_profile_minutes=int(raw.get("max_profile_minutes", 45)),
        disk_min_free_gb=int(raw.get("disk_min_free_gb", 10)),
        disk_min_free_percent=int(raw.get("disk_min_free_percent", 15)),
        targets=tuple(targets),
        telegram_token=os.getenv("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
    )
