import asyncio
import inspect
from pathlib import Path

import httpx
import pytest
import yaml

import maps_monitor.target_admin as target_admin
from maps_monitor.target_admin import (
    TargetAdminError,
    add_target,
    canonicalize_contributor_url,
    remove_target,
    validate_contributor_url,
)


def _config(path: Path, count: int = 0) -> Path:
    target = path / "targets.yaml"
    target.write_text(
        yaml.safe_dump(
            {
                "timezone": "Asia/Taipei",
                "targets": [
                    {
                        "name": f"人物 {index}",
                        "url": f"https://www.google.com/maps/contrib/{index}/reviews",
                        "enabled": True,
                    }
                    for index in range(count)
                ],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return target


def test_canonicalizes_only_public_google_contributor_review_urls():
    assert canonicalize_contributor_url(
        "https://google.com/maps/contrib/123/reviews/?hl=zh-TW"
    ) == ("https://www.google.com/maps/contrib/123/reviews", "123")
    for value in (
        "http://www.google.com/maps/contrib/123/reviews",
        "https://evil.example/maps/contrib/123/reviews",
        "https://www.google.com/maps/place/123",
        "https://www.google.com/maps/contrib/not-numeric/reviews",
    ):
        with pytest.raises(TargetAdminError, match="invalid"):
            canonicalize_contributor_url(value)


def test_add_remove_duplicate_and_limit(tmp_path):
    config = _config(tmp_path, 1)
    add_target(
        config,
        "https://www.google.com/maps/contrib/2/reviews",
        "新人物",
    )
    document = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert [item["name"] for item in document["targets"]] == ["人物 0", "新人物"]

    with pytest.raises(TargetAdminError, match="duplicate"):
        add_target(
            config,
            "https://www.google.com/maps/contrib/2/reviews",
            "重複",
        )

    remove_target(config, "https://www.google.com/maps/contrib/2/reviews")
    document = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert len(document["targets"]) == 1

    full = _config(tmp_path, 10)
    with pytest.raises(TargetAdminError, match="limit"):
        add_target(
            full,
            "https://www.google.com/maps/contrib/11/reviews",
            "超過上限",
        )


def test_live_validation_requires_same_contributor_page_and_extracts_name(monkeypatch):
    real_client = httpx.AsyncClient

    async def handler(request):
        return httpx.Response(
            200,
            request=request,
            text="<html><title>王小明 - Google Maps</title></html>",
        )

    def client_factory(**kwargs):
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(target_admin.httpx, "AsyncClient", client_factory)
    result = asyncio.run(
        validate_contributor_url(
            "https://www.google.com/maps/contrib/123/reviews?hl=zh-TW"
        )
    )
    assert result == (
        "https://www.google.com/maps/contrib/123/reviews",
        "王小明",
    )


def test_live_validation_skips_localized_google_product_title(monkeypatch):
    real_client = httpx.AsyncClient

    async def handler(request):
        return httpx.Response(
            200,
            request=request,
            text=(
                '<html><head><meta property="og:title" content="Google マップ">'
                "<title>Amber PENG - Google マップ</title></head></html>"
            ),
        )

    def client_factory(**kwargs):
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(target_admin.httpx, "AsyncClient", client_factory)
    result = asyncio.run(
        validate_contributor_url(
            "https://www.google.com/maps/contrib/113447067819558938464/reviews"
        )
    )
    assert result == (
        "https://www.google.com/maps/contrib/113447067819558938464/reviews",
        "Amber PENG",
    )


def test_live_validation_uses_rendered_profile_name_for_generic_html(monkeypatch):
    real_client = httpx.AsyncClient

    async def handler(request):
        return httpx.Response(
            200,
            request=request,
            text="<html><title>Google 地圖</title></html>",
        )

    def client_factory(**kwargs):
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    async def rendered_name(url, contributor_id):
        assert url == "https://www.google.com/maps/contrib/113447067819558938464/reviews"
        assert contributor_id == "113447067819558938464"
        return "Amber PENG"

    monkeypatch.setattr(target_admin.httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(
        target_admin,
        "_name_from_rendered_page",
        rendered_name,
        raising=False,
    )
    result = asyncio.run(
        validate_contributor_url(
            "https://www.google.com/maps/contrib/113447067819558938464/reviews"
        )
    )
    assert result == (
        "https://www.google.com/maps/contrib/113447067819558938464/reviews",
        "Amber PENG",
    )


def test_rendered_name_validation_never_activates_page_controls():
    implementation = inspect.getsource(target_admin._name_from_rendered_page)
    assert ".click(" not in implementation
    assert ".click(" not in target_admin.PROFILE_NAME_SCRIPT
