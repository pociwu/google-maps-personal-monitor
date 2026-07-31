from __future__ import annotations

import html
import os
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
import yaml


CONTRIBUTOR_PATH = re.compile(r"^/maps/contrib/(?P<id>[0-9]+)/reviews/?$")
TITLE_PATTERNS = (
    re.compile(
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)',
        re.IGNORECASE,
    ),
    re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL),
)


class TargetAdminError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def canonicalize_contributor_url(value: str) -> tuple[str, str]:
    try:
        parsed = urlsplit(value.strip())
    except ValueError as exc:
        raise TargetAdminError("invalid") from exc
    if parsed.scheme != "https" or parsed.hostname not in {"google.com", "www.google.com"}:
        raise TargetAdminError("invalid")
    match = CONTRIBUTOR_PATH.fullmatch(parsed.path)
    if not match:
        raise TargetAdminError("invalid")
    contributor_id = match.group("id")
    canonical = urlunsplit(
        ("https", "www.google.com", f"/maps/contrib/{contributor_id}/reviews", "", "")
    )
    return canonical, contributor_id


def _name_from_html(body: str, contributor_id: str) -> str:
    for pattern in TITLE_PATTERNS:
        match = pattern.search(body)
        if not match:
            continue
        name = html.unescape(re.sub(r"\s+", " ", match.group(1))).strip()
        name = re.sub(r"\s*[-–—]\s*Google Maps.*$", "", name, flags=re.IGNORECASE)
        name = re.sub(r"\s*的貢獻內容.*$", "", name)
        if name and name.lower() != "google maps":
            return name[:200]
    return f"貢獻者 {contributor_id[-6:]}"


async def validate_contributor_url(value: str) -> tuple[str, str]:
    canonical, contributor_id = canonicalize_contributor_url(value)
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=20.0,
            headers={"User-Agent": "Mozilla/5.0 maps-monitor-url-validator"},
        ) as client:
            response = await client.get(canonical)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise TargetAdminError("unavailable") from exc
    try:
        final_url, final_id = canonicalize_contributor_url(str(response.url))
    except TargetAdminError as exc:
        raise TargetAdminError("unavailable") from exc
    if final_url != canonical or final_id != contributor_id:
        raise TargetAdminError("unavailable")
    return canonical, _name_from_html(response.text, contributor_id)


def _document(path: Path) -> dict:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise TargetAdminError("config_error") from exc
    if not isinstance(document, dict) or not isinstance(document.get("targets", []), list):
        raise TargetAdminError("config_error")
    document.setdefault("targets", [])
    return document


def _write_document(path: Path, document: dict) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(
            yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise TargetAdminError("config_error") from exc


def add_target(path: Path, url: str, name: str) -> None:
    document = _document(path)
    targets = document["targets"]
    if any(str(item.get("url", "")).strip() == url for item in targets):
        raise TargetAdminError("duplicate")
    if len(targets) >= 10:
        raise TargetAdminError("limit")
    existing_names = {str(item.get("name", "")).strip() for item in targets}
    unique_name = name
    suffix = 2
    while unique_name in existing_names:
        unique_name = f"{name} ({suffix})"
        suffix += 1
    targets.append({"name": unique_name, "url": url, "enabled": True})
    _write_document(path, document)


def remove_target(path: Path, value: str) -> None:
    url, _contributor_id = canonicalize_contributor_url(value)
    document = _document(path)
    original = document["targets"]
    remaining = [
        item for item in original if str(item.get("url", "")).strip() != url
    ]
    if len(remaining) == len(original):
        raise TargetAdminError("not_found")
    document["targets"] = remaining
    _write_document(path, document)
