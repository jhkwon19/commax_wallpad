"""인증된 방문자 아카이브 API와 Lovelace 카드 소스를 제공한다."""

from __future__ import annotations

from http import HTTPStatus
from pathlib import Path

from aiohttp import web

from homeassistant.components.http import KEY_HASS, HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator_visitor import VisitorCoordinator, _FILENAME_RE

DATA_HTTP_REGISTERED = f"{DOMAIN}_visitor_http_registered"


def _coordinator(hass: HomeAssistant, entry_id: str) -> VisitorCoordinator | None:
    entry_data = hass.data.get(DOMAIN, {}).get(entry_id)
    return entry_data.get("visitor") if isinstance(entry_data, dict) else None


class CallDiagnosticsView(HomeAssistantView):
    """Bounded replay for a collector reconnect, using HA authentication."""

    url = "/api/commax_wallpad/diagnostics"
    name = "api:commax_wallpad:diagnostics"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app[KEY_HASS]
        journals = [data["diagnostics"].snapshot() for data in hass.data.get(DOMAIN, {}).values()
                    if isinstance(data, dict) and "diagnostics" in data]
        response = self.json({"schema": 1, "journals": journals})
        response.headers["Cache-Control"] = "private, no-store"
        return response


class VisitorArchiveListView(HomeAssistantView):
    """페이지 단위 방문자 목록. HomeAssistantView 기본 인증을 강제한다."""

    url = "/api/commax_wallpad/visitor/{entry_id}"
    name = "api:commax_wallpad:visitor:list"
    requires_auth = True

    async def get(self, request: web.Request, entry_id: str) -> web.Response:
        hass: HomeAssistant = request.app[KEY_HASS]
        coordinator = _coordinator(hass, entry_id)
        if coordinator is None:
            return self.json_message("Unknown config entry", HTTPStatus.NOT_FOUND)
        try:
            page = max(1, int(request.query.get("page", "1")))
            per_page = min(50, max(1, int(request.query.get("per_page", "30"))))
        except ValueError:
            return self.json_message("Invalid pagination", HTTPStatus.BAD_REQUEST)
        date_filter = request.query.get("date") or None
        location_filter = request.query.get("location") or None
        if request.query.get("version_only") == "1":
            version = await hass.async_add_executor_job(coordinator.archive_version)
            response = self.json({"version": version})
            response.headers["Cache-Control"] = "private, no-store"
            return response
        result = await hass.async_add_executor_job(
            coordinator.archive_page,
            page,
            per_page,
            date_filter,
            location_filter,
        )
        image_base = f"/api/commax_wallpad/visitor/{entry_id}/image/"
        for item in result["items"]:
            item["image_url"] = image_base + item["filename"]
        response = self.json(result)
        response.headers["Cache-Control"] = "private, no-store"
        return response


class VisitorArchiveImageView(HomeAssistantView):
    """검증된 JPEG 한 장을 인증된 사용자에게만 반환한다."""

    url = "/api/commax_wallpad/visitor/{entry_id}/image/{filename}"
    name = "api:commax_wallpad:visitor:image"
    requires_auth = True

    async def get(
        self, request: web.Request, entry_id: str, filename: str
    ) -> web.StreamResponse:
        hass: HomeAssistant = request.app[KEY_HASS]
        coordinator = _coordinator(hass, entry_id)
        if coordinator is None or _FILENAME_RE.fullmatch(filename) is None:
            raise web.HTTPNotFound()
        path = coordinator.archive_dir / filename
        if not await hass.async_add_executor_job(path.is_file):
            raise web.HTTPNotFound()
        response = web.FileResponse(path)
        response.content_type = "image/jpeg"
        response.headers["Cache-Control"] = "private, max-age=300"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response


class VisitorCardJavaScriptView(HomeAssistantView):
    """민감정보가 없는 카드 코드. 사진 API와 달리 리소스 로딩은 공개한다."""

    url = "/commax_wallpad/visitor-card.js"
    name = "api:commax_wallpad:visitor:card"
    requires_auth = False

    def __init__(self, source: str) -> None:
        self._source = source

    async def get(self, request: web.Request) -> web.Response:
        return web.Response(
            text=self._source,
            content_type="application/javascript",
            headers={"Cache-Control": "public, max-age=3600"},
        )


async def async_register_visitor_http(hass: HomeAssistant) -> None:
    """뷰를 HA 인스턴스당 한 번만 등록한다."""
    if hass.data.get(DATA_HTTP_REGISTERED):
        return
    source_path = Path(__file__).parent / "frontend" / "visitor-card.js"
    source = await hass.async_add_executor_job(source_path.read_text, "utf-8")
    hass.http.register_view(VisitorArchiveListView)
    hass.http.register_view(CallDiagnosticsView)
    hass.http.register_view(VisitorArchiveImageView)
    hass.http.register_view(VisitorCardJavaScriptView(source))
    hass.data[DATA_HTTP_REGISTERED] = True
