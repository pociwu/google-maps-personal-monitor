from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class ScrapedReview:
    review_key: str
    google_review_id: str | None
    place_id: str | None
    place_name: str
    place_url: str | None
    rating: float | None
    text: str
    relative_time: str
    review_url: str | None = None
    exact_timestamp: str | None = None
    explicitly_edited: bool = False
    image_urls: list[str] = field(default_factory=list)

    def event_content(self) -> dict[str, Any]:
        return {
            "place_name": self.place_name,
            "place_url": self.place_url,
            "rating": self.rating,
            "text": self.text,
        }

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class CrawlResult:
    reviews: list[ScrapedReview]
    reached_end: bool
    elapsed_seconds: float
