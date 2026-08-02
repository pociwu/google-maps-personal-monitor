from __future__ import annotations

import html
import os
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
import yaml
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright


CONTRIBUTOR_PATH = re.compile(r"^/maps/contrib/(?P<id>[0-9]+)/reviews/?$")
TITLE_PATTERNS = (
    re.compile(
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)',
        re.IGNORECASE,
    ),
    re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL),
)
GOOGLE_MAPS_PRODUCT = re.compile(
    r"^Google\s*(?:Maps|Map|地圖|地图|マップ|지도|Karten|Cartes|Mapas|Mappe|Kaarten|Haritalar)$",
    re.IGNORECASE,
)
GOOGLE_MAPS_SUFFIX = re.compile(
    r"\s*[-–—]\s*Google\s*(?:Maps|Map|地圖|地图|マップ|지도|Karten|Cartes|Mapas|Mappe|Kaarten|Haritalar).*$",
    re.IGNORECASE,
)
PROFILE_NAME_SCRIPT = r"""
() => {
  const main = document.querySelector('main[aria-label], [role="main"][aria-label]');
  if (!main) return null;
  const label = (main.getAttribute('aria-label') || '').trim();
  if (!label) return null;
  const candidates = [];
  for (const node of main.querySelectorAll('button')) {
    for (const raw of [node.getAttribute('aria-label'), node.textContent]) {
      const value = (raw || '').replace(/\s+/g, ' ').trim();
      if (value && value.length < label.length && label.startsWith(value)) {
        candidates.push(value);
      }
    }
  }
  candidates.sort((left, right) => right.length - left.length);
  return {label, candidates: [...new Set(candidates)]};
}
"""


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


def _clean_name(value: str) -> str | None:
    name = html.unescape(re.sub(r"\s+", " ", value)).strip()
    name = GOOGLE_MAPS_SUFFIX.sub("", name).strip()
    if not name or GOOGLE_MAPS_PRODUCT.fullmatch(name):
        return None
    return name[:200]


def _name_from_html(body: str) -> str | None:
    for pattern in TITLE_PATTERNS:
        match = pattern.search(body)
        if not match:
            continue
        name = _clean_name(match.group(1))
        if name:
            return name
    return None


async def _name_from_rendered_page(url: str, contributor_id: str) -> str | None:
    async with async_playwright() as manager:
        browser = await manager.chromium.launch(
            headless=True,
            args=[
                "--disable-background-networking",
                "--disable-dev-shm-usage",
                "--disable-sync",
                "--no-first-run",
            ],
        )
        context = await browser.new_context(
            locale="zh-TW",
            storage_state={"cookies": [], "origins": []},
        )
        page = await context.new_page()
        page.set_default_timeout(20_000)
        try:
            await page.goto(
                f"{url}?hl=zh-TW",
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            await page.wait_for_selector(
                'main[aria-label], [role="main"][aria-label]',
                state="attached",
            )
            final = urlsplit(page.url)
            expected_path = f"/maps/contrib/{contributor_id}/reviews"
            if (
                final.scheme != "https"
                or final.hostname not in {"google.com", "www.google.com"}
                or not final.path.startswith(expected_path)
            ):
                return None
            profile = await page.evaluate(PROFILE_NAME_SCRIPT)
            if not isinstance(profile, dict):
                return None
            for candidate in profile.get("candidates", []):
                name = _clean_name(str(candidate))
                if name:
                    return name
            label = str(profile.get("label", ""))
            label = re.sub(
                r"(?:的貢獻內容|的贡献内容|の投稿|님의 참여.*|'s contributions)$",
                "",
                label,
                flags=re.IGNORECASE,
            )
            return _clean_name(label)
        finally:
            await context.close()
            await browser.close()


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
    name = _name_from_html(response.text)
    if not name:
        try:
            name = await _name_from_rendered_page(canonical, contributor_id)
        except (PlaywrightError, PlaywrightTimeoutError):
            name = None
    return canonical, name or f"貢獻者 {contributor_id[-6:]}"


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


def target_urls_in_order(path: Path) -> list[str]:
    document = _document(path)
    ordered: list[str] = []
    try:
        for item in document["targets"]:
            if bool(item.get("enabled", True)):
                ordered.append(canonicalize_contributor_url(str(item.get("url", "")))[0])
    except (AttributeError, TargetAdminError) as exc:
        raise TargetAdminError("config_error") from exc
    return ordered


def reorder_targets(path: Path, ordered_urls: list[str]) -> None:
    if not isinstance(ordered_urls, list) or len(ordered_urls) > 10:
        raise TargetAdminError("invalid")
    try:
        canonical_order = [canonicalize_contributor_url(value)[0] for value in ordered_urls]
    except (AttributeError, TypeError, TargetAdminError) as exc:
        raise TargetAdminError("invalid") from exc
    if len(canonical_order) != len(set(canonical_order)):
        raise TargetAdminError("invalid")

    document = _document(path)
    enabled: dict[str, dict] = {}
    enabled_order: list[str] = []
    disabled: list[dict] = []
    try:
        for item in document["targets"]:
            canonical = canonicalize_contributor_url(str(item.get("url", "")))[0]
            if bool(item.get("enabled", True)):
                if canonical in enabled:
                    raise TargetAdminError("config_error")
                enabled[canonical] = item
                enabled_order.append(canonical)
            else:
                disabled.append(item)
    except (AttributeError, TargetAdminError) as exc:
        if isinstance(exc, TargetAdminError) and exc.code == "config_error":
            raise
        raise TargetAdminError("config_error") from exc
    if not set(canonical_order).issubset(enabled):
        raise TargetAdminError("invalid")
    remaining = [url for url in enabled_order if url not in set(canonical_order)]
    document["targets"] = [enabled[url] for url in canonical_order + remaining] + disabled
    _write_document(path, document)
