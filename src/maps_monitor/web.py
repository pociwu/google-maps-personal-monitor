from __future__ import annotations

import math
import os
import re
import sqlite3
from difflib import SequenceMatcher
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Iterator
from urllib.parse import parse_qs, urlencode
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.exceptions import HTTPException as StarletteHTTPException

from .dates import normalize_relative_label
from .target_admin import (
    TargetAdminError,
    add_target,
    remove_target,
    validate_contributor_url,
)


try:
    APP_VERSION = version("google-maps-contributor-monitor")
except PackageNotFoundError:
    APP_VERSION = "dev"

PACKAGE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = Path(
    os.getenv("MAPS_MONITOR_DATABASE", "/app/state/web/monitor.sqlite3")
).resolve()
IMAGE_ROOT = Path(
    os.getenv("MAPS_MONITOR_IMAGE_DIR", "/app/state/data/images")
).resolve()
TARGETS_CONFIG_PATH = Path(
    os.getenv("MAPS_MONITOR_TARGETS_CONFIG", "/app/config/targets.yaml")
).resolve()
PAGE_SIZE = 20
CONFIRMED_CONFIDENCE = {"confirmed_time", "confirmed_date"}
IMAGE_TOKEN = re.compile(r"^[0-9a-f]{64}$")
DISPLAY_TIMEZONE = ZoneInfo("Asia/Taipei")
PARSED_UNIT_LABELS = {
    "minute": "分鐘",
    "hour": "小時",
    "day": "天",
    "week": "週",
    "month": "個月",
    "year": "年",
}
MANAGEMENT_MESSAGES = {
    "added": ("success", "網址驗證成功；已加入監控清單，下一輪巡查後顯示卡片。"),
    "removed": ("success", "已移出監控清單；下一輪巡查後卡片會消失，歷史資料仍保留。"),
    "invalid": ("danger", "網址格式錯誤，請輸入 Google Maps 公開貢獻者評論網址。"),
    "unavailable": ("danger", "Google Maps 網址目前無法讀取或已導向其他頁面。"),
    "duplicate": ("warning", "這個貢獻者網址已在監控清單中。"),
    "limit": ("warning", "監控對象已達 10 人上限。"),
    "not_found": ("warning", "監控清單中找不到這個網址，可能已被移除。"),
    "config_error": ("danger", "監控設定檔暫時無法更新。"),
}


def _positive_hours(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} 必須是正整數") from exc
    if value <= 0:
        raise RuntimeError(f"{name} 必須是正整數")
    return value


STALE_WARNING_HOURS = _positive_hours("DASHBOARD_STALE_WARNING_HOURS", 12)
STALE_CRITICAL_HOURS = _positive_hours("DASHBOARD_STALE_CRITICAL_HOURS", 24)
if STALE_CRITICAL_HOURS <= STALE_WARNING_HOURS:
    raise RuntimeError("DASHBOARD_STALE_CRITICAL_HOURS 必須大於警告門檻")


templates = Environment(
    loader=FileSystemLoader(PACKAGE_DIR / "templates"),
    autoescape=select_autoescape(("html", "xml")),
    enable_async=False,
)


@contextmanager
def _read_connection() -> Iterator[sqlite3.Connection]:
    if not DATABASE_PATH.is_file():
        raise sqlite3.OperationalError("database unavailable")
    uri = f"file:{DATABASE_PATH.as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        yield connection
    finally:
        connection.close()


def _safe_int(value: str | None, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _local_datetime(value: str | None, *, seconds: bool = False) -> str:
    if not value:
        return "尚未巡查"
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        pattern = "%Y-%m-%d %H:%M:%S" if seconds else "%Y-%m-%d %H:%M"
        return parsed.astimezone(DISPLAY_TIMEZONE).strftime(pattern)
    except ValueError:
        return "時間未知"


def _freshness(value: str | None) -> str:
    if not value:
        return "critical"
    try:
        seen = datetime.fromisoformat(value)
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=UTC)
        age_hours = (datetime.now(UTC) - seen.astimezone(UTC)).total_seconds() / 3600
    except ValueError:
        return "critical"
    if age_hours > STALE_CRITICAL_HOURS:
        return "critical"
    if age_hours > STALE_WARNING_HOURS:
        return "warning"
    return "fresh"


def _uncertainty_seconds(
    estimate: str | None,
    earliest: str | None,
    latest: str | None,
) -> float | None:
    if not earliest or not latest:
        return None
    try:
        lower = datetime.fromisoformat(earliest)
        upper = datetime.fromisoformat(latest)
        if lower.tzinfo is None:
            lower = lower.replace(tzinfo=UTC)
        if upper.tzinfo is None:
            upper = upper.replace(tzinfo=UTC)
        midpoint = datetime.fromisoformat(estimate) if estimate else lower + (upper - lower) / 2
        if midpoint.tzinfo is None:
            midpoint = midpoint.replace(tzinfo=UTC)
        return max(
            abs((midpoint - lower).total_seconds()),
            abs((upper - midpoint).total_seconds()),
        )
    except ValueError:
        return None


def _uncertainty_text(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    if seconds < 3600:
        return f"± {max(1, math.ceil(seconds / 60))} 分"
    if seconds < 86400:
        return f"± {math.ceil(seconds / 3600)} 小時"
    return f"± {math.ceil(seconds / 86400)} 日"


def _display_date(
    value: str | None,
    confidence: str | None,
    label: str,
    uncertainty_text: str | None = None,
) -> str:
    if not value:
        return f"{label}：尚未確認"
    prefix = "" if confidence in CONFIRMED_CONFIDENCE else "約 "
    uncertainty = (
        f"（{uncertainty_text}）"
        if confidence not in CONFIRMED_CONFIDENCE and uncertainty_text
        else ""
    )
    return f"{label}：{prefix}{value}{uncertainty}"


def _like(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _query_url(params: dict[str, str | int | None], **updates: str | int | None) -> str:
    merged = params | updates
    clean = {
        key: str(value)
        for key, value in merged.items()
        if value not in (None, "", "active") and not (key == "page" and value == 1)
    }
    encoded = urlencode(clean)
    return f"/?{encoded}" if encoded else "/"


def _maps_search_url(place_name: str) -> str:
    return "https://www.google.com/maps/search/?" + urlencode(
        {"api": "1", "query": place_name}
    )


def _dashboard_data(request: Request) -> dict:
    query = request.query_params
    contributor = query.get("contributor", "").strip()[:200]
    search = query.get("q", "").strip()[:200]
    status = query.get("status", "active")
    if status not in {"active", "deleted", "all"}:
        status = "active"
    rating_value = _safe_int(query.get("rating"), 0)
    rating = rating_value if 1 <= rating_value <= 5 else 0
    page = max(1, _safe_int(query.get("page"), 1))

    params: dict[str, str | int | None] = {
        "contributor": contributor or None,
        "q": search or None,
        "status": status,
        "rating": rating or None,
        "page": page,
    }
    with _read_connection() as connection:
        contributor_rows = connection.execute(
            """SELECT t.name,t.url,t.last_success_at,
            COUNT(r.id) AS total_count,
            COALESCE(SUM(CASE WHEN r.status='active' THEN 1 ELSE 0 END),0) AS active_count,
            COALESCE(SUM(CASE WHEN r.modified_at IS NOT NULL THEN 1 ELSE 0 END),0) AS modified_count,
            COALESCE(SUM(CASE WHEN r.status='deleted' THEN 1 ELSE 0 END),0) AS deleted_count,
            COALESCE(SUM(CASE WHEN TRIM(r.body)='' THEN 1 ELSE 0 END),0) AS rating_only_count
            FROM targets t LEFT JOIN reviews r ON r.target_id=t.id
            WHERE t.enabled=1
            GROUP BY t.id,t.name,t.url,t.last_success_at ORDER BY t.name"""
        ).fetchall()
        contributor_names = {row["name"] for row in contributor_rows}
        if contributor and contributor not in contributor_names:
            contributor = ""
            params["contributor"] = None

        where = ["t.enabled=1"]
        values: list[object] = []
        if contributor:
            where.append("t.name=?")
            values.append(contributor)
        if status != "all":
            where.append("r.status=?")
            values.append(status)
        if rating:
            where.append("CAST(ROUND(r.rating) AS INTEGER)=?")
            values.append(rating)
        if search:
            where.append("(r.place_name LIKE ? ESCAPE '\\' OR r.body LIKE ? ESCAPE '\\')")
            pattern = _like(search)
            values.extend((pattern, pattern))
        where_sql = " AND ".join(where)
        total = connection.execute(
            f"""SELECT COUNT(*) FROM reviews r JOIN targets t ON t.id=r.target_id
            WHERE {where_sql}""",
            values,
        ).fetchone()[0]
        total_pages = max(1, math.ceil(total / PAGE_SIZE))
        page = min(page, total_pages)
        params["page"] = page
        reviews = [
            dict(row)
            for row in connection.execute(
                f"""SELECT r.id,t.name AS contributor_name,r.place_name,r.rating,r.body,
                r.publish_date,r.publish_estimate,r.publish_earliest,r.publish_latest,
                r.confidence,r.edit_date,r.edit_confidence,r.status,
                r.modified_at,r.last_seen_at,r.relative_time,r.review_url,r.place_url,
                (SELECT COUNT(*) FROM review_versions rv WHERE rv.review_id=r.id)
                    AS version_count
                FROM reviews r JOIN targets t ON t.id=r.target_id
                WHERE {where_sql}
                ORDER BY
                  CASE WHEN COALESCE(r.publish_date,r.edit_date) IS NULL THEN 1 ELSE 0 END,
                  COALESCE(r.publish_date,r.edit_date) DESC,r.id DESC
                LIMIT ? OFFSET ?""",
                (*values, PAGE_SIZE, (page - 1) * PAGE_SIZE),
            ).fetchall()
        ]
        review_ids = [row["id"] for row in reviews]
        current_images: dict[int, list[dict]] = {review_id: [] for review_id in review_ids}
        historical_images: dict[int, list[dict]] = {review_id: [] for review_id in review_ids}
        if review_ids:
            placeholders = ",".join("?" for _ in review_ids)
            image_rows = connection.execute(
                f"""SELECT review_id,sha256,thumbnail_path,local_path,is_current,last_seen_at
                FROM images
                WHERE status='saved' AND sha256 IS NOT NULL AND review_id IN ({placeholders})
                ORDER BY id""",
                review_ids,
            ).fetchall()
            for image in image_rows:
                item = {
                    "thumbnail_url": (
                        f"/media/{image['sha256']}/thumbnail"
                        if image["thumbnail_path"] else f"/media/{image['sha256']}/original"
                    ),
                    "original_url": f"/media/{image['sha256']}/original",
                    "last_seen_text": _local_datetime(image["last_seen_at"], seconds=True),
                }
                destination = (
                    current_images if image["is_current"] else historical_images
                )
                destination[image["review_id"]].append(item)

    contributors = []
    for row in contributor_rows:
        item = dict(row)
        item["last_success_text"] = _local_datetime(item["last_success_at"])
        item["freshness"] = _freshness(item["last_success_at"])
        item["href"] = _query_url(params, contributor=item["name"], page=1)
        contributors.append(item)
    for review in reviews:
        review["rating_only"] = not (review["body"] or "").strip()
        review["relative_time"] = normalize_relative_label(review["relative_time"])
        review["modified"] = bool(review["modified_at"])
        uncertainty_seconds = _uncertainty_seconds(
            review["publish_estimate"],
            review["publish_earliest"],
            review["publish_latest"],
        )
        publish_value = review["publish_date"]
        if (
            review["confidence"] not in CONFIRMED_CONFIDENCE
            and uncertainty_seconds is not None
            and uncertainty_seconds < 86400
            and review["publish_estimate"]
        ):
            publish_value = _local_datetime(review["publish_estimate"])
        review["publish_text"] = _display_date(
            publish_value,
            review["confidence"],
            "發表日期",
            _uncertainty_text(uncertainty_seconds),
        )
        review["edit_text"] = _display_date(
            review["edit_date"], review["edit_confidence"], "最後修改"
        )
        review["images"] = current_images.get(review["id"], [])
        review["historical_images"] = historical_images.get(review["id"], [])
        review["evidence_url"] = f"/reviews/{review['id']}/evidence"
        if review["review_url"]:
            review["link_url"] = review["review_url"]
            review["link_label"] = "查看 Google 評論"
        elif review["place_url"]:
            review["link_url"] = review["place_url"]
            review["link_label"] = "查看店家"
        else:
            review["link_url"] = _maps_search_url(review["place_name"])
            review["link_label"] = "在 Google Maps 搜尋店家"

    return {
        "app_version": APP_VERSION,
        "contributors": contributors,
        "selected_contributor": contributor,
        "reviews": reviews,
        "total": total,
        "page": page,
        "total_pages": total_pages,
        "status": status,
        "rating": rating,
        "search": search,
        "last_updated": _local_datetime(
            max(
                (row["last_success_at"] for row in contributor_rows if row["last_success_at"]),
                default=None,
            )
        ),
        "all_href": _query_url(params, contributor=None, page=1),
        "previous_href": _query_url(params, page=page - 1) if page > 1 else None,
        "next_href": _query_url(params, page=page + 1) if page < total_pages else None,
        "warning_hours": STALE_WARNING_HOURS,
        "critical_hours": STALE_CRITICAL_HOURS,
        "management_message": MANAGEMENT_MESSAGES.get(query.get("manage", "")),
    }


def _evidence_data(review_id: int) -> dict | None:
    with _read_connection() as connection:
        review_row = connection.execute(
            """SELECT r.id,r.publish_date,r.publish_estimate,r.publish_earliest,
            r.publish_latest,r.precision,r.confidence,r.basis,r.time_subject,
            r.date_model_version,r.edit_date,r.edit_estimate,r.edit_earliest,
            r.edit_latest,r.edit_confidence,r.edit_basis
            FROM reviews r JOIN targets t ON t.id=r.target_id
            WHERE r.id=? AND t.enabled=1""",
            (review_id,),
        ).fetchone()
        if not review_row:
            return None
        observations = [
            dict(row)
            for row in connection.execute(
                """SELECT observed_at,relative_time,parsed_count,parsed_unit,is_edit,
                exact_timestamp,crawl_complete
                FROM observations WHERE review_id=? ORDER BY observed_at,id""",
                (review_id,),
            ).fetchall()
        ]
    review = dict(review_row)
    review["publish_text"] = _display_date(
        review["publish_date"], review["confidence"], "發表日期"
    )
    review["publish_estimate_text"] = (
        _local_datetime(review["publish_estimate"], seconds=True)
        if review["publish_estimate"] else "—"
    )
    review["publish_earliest_text"] = (
        _local_datetime(review["publish_earliest"], seconds=True)
        if review["publish_earliest"] else "—"
    )
    review["publish_latest_text"] = (
        _local_datetime(review["publish_latest"], seconds=True)
        if review["publish_latest"] else "—"
    )
    review["high_confidence"] = review["confidence"] in CONFIRMED_CONFIDENCE
    review["confirmation_interval_text"] = (
        f"{review['publish_earliest_text']} ～ {review['publish_latest_text']}"
        if review["high_confidence"]
        and review["publish_earliest"]
        and review["publish_latest"]
        else None
    )
    review["edit_text"] = _display_date(
        review["edit_date"], review["edit_confidence"], "最後修改"
    )
    review["edit_estimate_text"] = (
        _local_datetime(review["edit_estimate"], seconds=True)
        if review["edit_estimate"] else "—"
    )
    review["edit_earliest_text"] = (
        _local_datetime(review["edit_earliest"], seconds=True)
        if review["edit_earliest"] else "—"
    )
    review["edit_latest_text"] = (
        _local_datetime(review["edit_latest"], seconds=True)
        if review["edit_latest"] else "—"
    )
    for observation in observations:
        observation["relative_time"] = normalize_relative_label(
            observation["relative_time"]
        )
        observation["observed_text"] = _local_datetime(
            observation["observed_at"], seconds=True
        )
        observation["exact_text"] = (
            _local_datetime(observation["exact_timestamp"], seconds=True)
            if observation["exact_timestamp"] else "—"
        )
        if observation["parsed_count"] is None or not observation["parsed_unit"]:
            observation["parsed_text"] = "無法解析"
        else:
            unit = PARSED_UNIT_LABELS.get(
                observation["parsed_unit"], observation["parsed_unit"]
            )
            observation["parsed_text"] = f"{observation['parsed_count']} {unit}"
    return {
        "app_version": APP_VERSION,
        "review": review,
        "observations": observations,
    }


def _diff_segments(old: str, new: str) -> tuple[list[dict], list[dict]]:
    old_segments: list[dict] = []
    new_segments: list[dict] = []
    for operation, old_start, old_end, new_start, new_end in SequenceMatcher(
        None, old, new, autojunk=False
    ).get_opcodes():
        old_text = old[old_start:old_end]
        new_text = new[new_start:new_end]
        if operation in {"equal", "delete", "replace"} and old_text:
            old_segments.append(
                {"text": old_text, "changed": operation != "equal"}
            )
        if operation in {"equal", "insert", "replace"} and new_text:
            new_segments.append(
                {"text": new_text, "changed": operation != "equal"}
            )
    return old_segments, new_segments


def _history_data(review_id: int) -> dict | None:
    with _read_connection() as connection:
        review_row = connection.execute(
            """SELECT r.id,r.place_name,r.review_url,r.place_url,t.name AS contributor_name
            FROM reviews r JOIN targets t ON t.id=r.target_id
            WHERE r.id=? AND t.enabled=1""",
            (review_id,),
        ).fetchone()
        if not review_row:
            return None
        versions = [
            dict(row)
            for row in connection.execute(
                """SELECT version_number,captured_at,place_name,place_url,rating,body,
                relative_time,source FROM review_versions
                WHERE review_id=? ORDER BY version_number""",
                (review_id,),
            ).fetchall()
        ]
    changes = []
    for previous, current in zip(versions[:-1], versions[1:], strict=True):
        old_segments, new_segments = _diff_segments(previous["body"], current["body"])
        changes.append(
            {
                "from_version": previous["version_number"],
                "to_version": current["version_number"],
                "captured_text": _local_datetime(current["captured_at"], seconds=True),
                "old": previous,
                "new": current,
                "old_segments": old_segments,
                "new_segments": new_segments,
                "text_changed": previous["body"] != current["body"],
                "rating_changed": previous["rating"] != current["rating"],
                "place_changed": previous["place_name"] != current["place_name"],
            }
        )
    review = dict(review_row)
    review["link_url"] = review["review_url"] or review["place_url"]
    return {
        "app_version": APP_VERSION,
        "review": review,
        "changes": list(reversed(changes)),
        "version_count": len(versions),
    }


def _render(name: str, context: dict, status_code: int = 200) -> HTMLResponse:
    template = templates.get_template(name)
    return HTMLResponse(template.render(**context), status_code=status_code)


async def _management_fields(request: Request) -> dict[str, str]:
    if request.headers.get("content-type", "").split(";", 1)[0] != (
        "application/x-www-form-urlencoded"
    ):
        raise TargetAdminError("invalid")
    body = await request.body()
    if len(body) > 4096:
        raise TargetAdminError("invalid")
    try:
        parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True)
    except UnicodeDecodeError as exc:
        raise TargetAdminError("invalid") from exc
    return {key: values[-1] for key, values in parsed.items() if values}


def _management_redirect(code: str) -> RedirectResponse:
    return RedirectResponse(url=f"/?{urlencode({'manage': code})}", status_code=303)


def create_app() -> FastAPI:
    application = FastAPI(
        title="Google Maps 個人評論監控",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    application.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")

    @application.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        )
        if response.headers.get("content-type", "").startswith("text/html"):
            response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "style-src 'self' https://cdn.jsdelivr.net; "
            "script-src 'self' https://cdn.jsdelivr.net; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
        )
        return response

    @application.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        try:
            return _render("dashboard.html", _dashboard_data(request))
        except sqlite3.Error:
            return _render("unavailable.html", {}, status_code=503)

    @application.post("/targets/add")
    async def target_add(request: Request):
        try:
            fields = await _management_fields(request)
            url = fields.get("target_url", "")
            if len(url) > 1000:
                raise TargetAdminError("invalid")
            canonical, name = await validate_contributor_url(url)
            add_target(TARGETS_CONFIG_PATH, canonical, name)
            return _management_redirect("added")
        except TargetAdminError as exc:
            return _management_redirect(exc.code)

    @application.post("/targets/remove")
    async def target_remove(request: Request):
        try:
            fields = await _management_fields(request)
            remove_target(TARGETS_CONFIG_PATH, fields.get("target_url", ""))
            return _management_redirect("removed")
        except TargetAdminError as exc:
            return _management_redirect(exc.code)

    @application.get("/media/{digest}/{kind}")
    def media(digest: str, kind: str):
        if not IMAGE_TOKEN.fullmatch(digest) or kind not in {"thumbnail", "original"}:
            raise HTTPException(status_code=404)
        try:
            with _read_connection() as connection:
                row = connection.execute(
                    """SELECT local_path,thumbnail_path FROM images
                    WHERE sha256=? AND status='saved' ORDER BY id LIMIT 1""",
                    (digest,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise HTTPException(status_code=503) from exc
        if not row:
            raise HTTPException(status_code=404)
        selected = row["thumbnail_path"] if kind == "thumbnail" else row["local_path"]
        if not selected:
            raise HTTPException(status_code=404)
        try:
            path = Path(selected).resolve(strict=True)
            if not path.is_relative_to(IMAGE_ROOT):
                raise HTTPException(status_code=404)
        except (OSError, RuntimeError) as exc:
            raise HTTPException(status_code=404) from exc
        return FileResponse(
            path,
            headers={"Cache-Control": "private, max-age=86400"},
            content_disposition_type="inline",
        )

    @application.get("/reviews/{review_id}/evidence", response_class=HTMLResponse)
    def review_evidence(review_id: int):
        try:
            data = _evidence_data(review_id)
        except sqlite3.Error:
            return _render("evidence_unavailable.html", {}, status_code=503)
        if not data:
            raise HTTPException(status_code=404)
        return _render("evidence_page.html", data)

    @application.get("/reviews/{review_id}/history", response_class=HTMLResponse)
    def review_history(review_id: int):
        try:
            data = _history_data(review_id)
        except sqlite3.Error:
            return _render("unavailable.html", {}, status_code=503)
        if not data:
            raise HTTPException(status_code=404)
        return _render("history_page.html", data)

    @application.get("/healthz")
    def health():
        try:
            with _read_connection() as connection:
                connection.execute("SELECT COUNT(*) FROM sqlite_schema").fetchone()
            return JSONResponse({"status": "ok"})
        except sqlite3.Error:
            return JSONResponse({"status": "unavailable"}, status_code=503)

    @application.get("/robots.txt", response_class=PlainTextResponse)
    def robots():
        return "User-agent: *\nDisallow: /\n"

    @application.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, _exc: RequestValidationError):
        return _render("unavailable.html", {}, status_code=400)

    @application.exception_handler(StarletteHTTPException)
    async def http_error(_request: Request, exc: StarletteHTTPException):
        if exc.status_code == 404:
            return _render("not_found.html", {}, status_code=404)
        if exc.status_code == 503:
            return _render("unavailable.html", {}, status_code=503)
        return _render("unavailable.html", {}, status_code=exc.status_code)

    return application


app = create_app()
