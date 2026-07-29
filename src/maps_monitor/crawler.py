from __future__ import annotations

import re
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from playwright.async_api import Browser, Page, async_playwright

from .models import CrawlResult, ScrapedReview
from .util import stable_hash


EXTRACT_SCRIPT = r"""
() => {
  const selectors = ['[data-review-id]', 'div.jftiEf', 'div.jJc9Ad'];
  const raw = Array.from(document.querySelectorAll(selectors.join(',')));
  const cards = raw.filter((node, index) => !raw.some((other, j) => j !== index && other.contains(node)));
  const textOf = (card, selector) => {
    const node = card.querySelector(selector);
    return node ? (node.textContent || '').trim() : '';
  };
  const urlFromStyle = (value) => {
    const match = (value || '').match(/url\(["']?(.*?)["']?\)/);
    return match ? match[1] : null;
  };
  return cards.map((card) => {
    const placeLink = Array.from(card.querySelectorAll('a[href]')).find((a) =>
      /\/maps\/(place|preview)|[?&]cid=|!1s/.test(a.href)
    );
    const ratingNode = Array.from(card.querySelectorAll('[role="img"][aria-label]')).find((node) =>
      /星|star/i.test(node.getAttribute('aria-label') || '')
    );
    const exactNode = card.querySelector('time[datetime], [data-review-timestamp], [data-publish-time]');
    const imageUrls = [];
    for (const img of card.querySelectorAll('img')) {
      const src = img.currentSrc || img.src || '';
      if (/googleusercontent|ggpht|gstatic/.test(src) && (img.naturalWidth >= 120 || img.naturalHeight >= 120)) imageUrls.push(src);
    }
    for (const node of card.querySelectorAll('[style*="background-image"]')) {
      const src = urlFromStyle(node.style.backgroundImage);
      if (src && /googleusercontent|ggpht|gstatic/.test(src)) imageUrls.push(src);
    }
    return {
      google_review_id: card.getAttribute('data-review-id') || card.dataset.reviewId || null,
      place_name: textOf(card, '.d4r55, .fontHeadlineSmall, .WNxzHc') || (placeLink ? placeLink.textContent.trim() : ''),
      place_url: placeLink ? placeLink.href : null,
      rating_label: ratingNode ? ratingNode.getAttribute('aria-label') : '',
      text: textOf(card, '.wiI7pd, .MyEned, [data-review-text]'),
      relative_time: textOf(card, '.rsqaWe, .xRkPPb, .DU9Pgb'),
      exact_timestamp: exactNode ?
        (exactNode.getAttribute('datetime') || exactNode.getAttribute('data-review-timestamp') || exactNode.getAttribute('data-publish-time')) : null,
      image_urls: [...new Set(imageUrls)],
    };
  }).filter((item) => item.google_review_id || item.place_url || item.text || item.rating_label);
}
"""


SCROLL_SCRIPT = r"""
() => {
  const first = document.querySelector('[data-review-id], div.jftiEf, div.jJc9Ad');
  let node = first;
  while (node && node !== document.body) {
    if (node.scrollHeight > node.clientHeight + 20) break;
    node = node.parentElement;
  }
  if (!node || node === document.body) {
    node = Array.from(document.querySelectorAll('main, [role="main"], div')).find((candidate) =>
      candidate.scrollHeight > candidate.clientHeight + 100 && getComputedStyle(candidate).overflowY !== 'visible'
    );
  }
  if (!node) return { found: false, height: 0, top: 0 };
  node.scrollTop = node.scrollHeight;
  return { found: true, height: node.scrollHeight, top: node.scrollTop };
}
"""


def _localized_url(url: str, locale: str) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["hl"] = locale
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _place_id(url: str | None) -> str | None:
    if not url:
        return None
    for pattern in (r"!1s([^!]+)", r"[?&]cid=([^&]+)"):
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def _rating(label: str) -> float | None:
    match = re.search(r"(\d+(?:[.,]\d+)?)", label)
    return float(match.group(1).replace(",", ".")) if match else None


def _highest_resolution(url: str) -> str:
    return re.sub(r"=w\d+(?:-h\d+)?(?:-[a-zA-Z0-9-]+)?$", "=s0", url)


def _raw_item_key(item: dict) -> str:
    """Stable enough to accumulate cards even when Google virtualizes the list DOM."""
    place_url = item.get("place_url")
    return str(
        _place_id(place_url)
        or place_url
        or item.get("google_review_id")
        or stable_hash([
            item.get("place_name"), item.get("rating_label"), item.get("text"),
            item.get("relative_time"), item.get("image_urls"),
        ])
    )


def _normalize_timestamp(value: str | None) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    try:
        number = float(text)
        if number > 10_000_000_000:
            number /= 1000
        return datetime.fromtimestamp(number, tz=UTC).isoformat()
    except (ValueError, OverflowError, OSError):
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.isoformat()
    except ValueError:
        return None


class ReadOnlyCrawler:
    """Anonymous crawler that never sends pointer, touch, keyboard, or form input."""

    def __init__(self, locale: str, timezone: str, timeout_minutes: int, debug_dir: Path):
        self.locale = locale
        self.timezone = timezone
        self.timeout_seconds = timeout_minutes * 60
        self.debug_dir = debug_dir
        self._manager = None
        self._browser: Browser | None = None

    async def __aenter__(self) -> "ReadOnlyCrawler":
        self._manager = await async_playwright().start()
        self._browser = await self._manager.chromium.launch(
            headless=True,
            args=["--disable-background-networking", "--disable-sync", "--no-first-run"],
        )
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        if self._browser:
            await self._browser.close()
        if self._manager:
            await self._manager.stop()

    async def crawl(self, name: str, url: str) -> CrawlResult:
        if not self._browser:
            raise RuntimeError("crawler 尚未啟動")
        started = time.monotonic()
        context = await self._browser.new_context(
            locale=self.locale,
            timezone_id=self.timezone,
            service_workers="block",
            storage_state={"cookies": [], "origins": []},
        )
        page = await context.new_page()
        page.set_default_timeout(30_000)
        try:
            await page.goto(_localized_url(url, self.locale), wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_timeout(3000)
            stable_rounds = 0
            previous = (-1, -1)
            reached_end = False
            collected: dict[str, dict] = {}
            while time.monotonic() - started < self.timeout_seconds:
                items = await page.evaluate(EXTRACT_SCRIPT)
                for item in items:
                    collected[_raw_item_key(item)] = item
                position = await page.evaluate(SCROLL_SCRIPT)
                current = (len(collected), int(position.get("height", 0)))
                stable_rounds = stable_rounds + 1 if current == previous else 0
                previous = current
                # Google commonly loads reviews in batches.  Twelve seconds without
                # a new card or a taller scroll area is a safer end-of-list signal.
                if stable_rounds >= 8 and len(collected) > 0:
                    reached_end = True
                    break
                if not position.get("found") and len(collected) > 0:
                    reached_end = True
                    break
                await page.wait_for_timeout(1500)
            final_items = await page.evaluate(EXTRACT_SCRIPT)
            for item in final_items:
                collected[_raw_item_key(item)] = item
            raw_items = list(collected.values())
            if not reached_end:
                raise TimeoutError(f"{name} 未在限制時間內完整捲動到底")
            reviews: list[ScrapedReview] = []
            seen: set[str] = set()
            contributor_id = urlsplit(url).path.rstrip("/").split("/")[-2]
            for item in raw_items:
                place_url = item.get("place_url")
                place_id = _place_id(place_url)
                review_id = item.get("google_review_id")
                fallback = place_id or place_url or stable_hash(item)
                key = review_id or stable_hash([contributor_id, fallback])
                if key in seen:
                    continue
                seen.add(key)
                reviews.append(
                    ScrapedReview(
                        review_key=key,
                        google_review_id=review_id,
                        place_id=place_id,
                        place_name=item.get("place_name") or "未知店家",
                        place_url=place_url,
                        rating=_rating(item.get("rating_label", "")),
                        text=item.get("text", ""),
                        relative_time=item.get("relative_time", ""),
                        exact_timestamp=_normalize_timestamp(item.get("exact_timestamp")),
                        explicitly_edited=bool(re.search(r"已編輯|已编辑|已更新|edited|updated", item.get("relative_time", ""), re.I)),
                        image_urls=[_highest_resolution(value) for value in item.get("image_urls", [])],
                    )
                )
            if not reviews:
                raise RuntimeError(f"{name} 找不到任何評論卡片，可能是頁面改版或內容未載入")
            return CrawlResult(reviews, True, time.monotonic() - started)
        except Exception:
            await self._save_debug(page, name)
            raise
        finally:
            await context.close()

    async def _save_debug(self, page: Page, name: str) -> None:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        safe_name = re.sub(r"[^\w.-]+", "_", name, flags=re.UNICODE)
        folder = self.debug_dir / f"{timestamp}-{safe_name}"
        folder.mkdir(parents=True, exist_ok=True)
        try:
            await page.screenshot(path=folder / "page.png", full_page=False)
        except Exception:
            pass
        try:
            (folder / "page.html").write_text(await page.content(), encoding="utf-8")
            (folder / "url.txt").write_text(page.url, encoding="utf-8")
        except Exception:
            pass
