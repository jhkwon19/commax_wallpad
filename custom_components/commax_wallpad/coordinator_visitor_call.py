"""ADB logcat 스트림에서 현관 호출과 PAM 실시간 검침을 감지한다."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .client import CommaxRouterClient
from .coordinator_visitor import VisitorCoordinator

_LOGGER = logging.getLogger(__name__)

_MONITOR_HELPER = "/root/wallpad-visitor-call-monitor.sh"
_RECONNECT_DELAYS = (1, 2, 5, 10, 30)
_LOCATION_BY_PLACE = {"2": "세대현관", "5": "공동현관"}
_METER_SORTS = {"electricMeter", "gasMeter", "waterMeter"}


class VisitorCallCoordinator(DataUpdateCoordinator[dict]):
    """호출 시작·종료를 push 방식으로 HA 엔티티에 전달한다."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: CommaxRouterClient,
        visitor_coordinator: VisitorCoordinator,
    ) -> None:
        super().__init__(hass, _LOGGER, name="commax_wallpad_visitor_call")
        self._client = client
        self._visitor_coordinator = visitor_coordinator
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._event_sequence = 0
        self._reconnect_attempt = 0
        self._reconnect_count = 0
        self.data = {
            "active": False,
            "state": "idle",
            "location": None,
            "place": None,
            "started_at": None,
            "ended_at": None,
            "last_duration_seconds": None,
            "last_event": None,
            "last_event_id": None,
            "connected": False,
            "last_connected_at": None,
            "last_stream_event_at": None,
            "reconnect_count": 0,
            "meters": {},
            "error": None,
        }

    async def _async_update_data(self) -> dict:
        """초기 엔티티 생성용 스냅샷이며 네트워크 조회는 하지 않는다."""
        return dict(self.data)

    async def async_start(self) -> None:
        """설정 항목당 logcat 감시 작업 하나를 시작한다.

        `_async_monitor_loop()`는 `_stopping`이 될 때까지 절대 끝나지 않는
        의도적인 무한루프다. 일반 `async_create_task()`로 만들면 HA
        부트스트랩이 `async_setup_entry()` 중 생성된 태스크를 잠깐 기다리다가
        "Setup timed out for bootstrap ... moving forward" 경고를 남긴다
        (2026-08-26 실사용 로그로 확인). `async_create_background_task()`는
        이런 장기 실행 감시 태스크를 부트스트랩 대기 대상에서 제외하도록 HA가
        제공하는 전용 API라 이 경고 없이 정상적으로 백그라운드에서 계속
        돈다.
        """
        if self._task is None or self._task.done():
            self._stopping = False
            self._task = self.hass.async_create_background_task(
                self._async_monitor_loop(),
                name="commax_wallpad_visitor_call_monitor",
            )

    async def async_stop(self) -> None:
        """unload 시 SSH와 감시 작업을 정리한다."""
        self._stopping = True
        await self._client.close()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _async_monitor_loop(self) -> None:
        while not self._stopping:
            try:
                await self._client.stream_lines(
                    _MONITOR_HELPER, self._async_handle_line
                )
                raise RuntimeError("visitor call monitor ended unexpectedly")
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - 끊김 후 자체 재연결
                if self._stopping:
                    return
                self._reconnect_count += 1
                self._set_connection_state(False, str(err))
                delay = _RECONNECT_DELAYS[
                    min(self._reconnect_attempt, len(_RECONNECT_DELAYS) - 1)
                ]
                self._reconnect_attempt += 1
                log = _LOGGER.warning if self._reconnect_attempt == 1 else _LOGGER.debug
                log(
                    "Visitor call monitor disconnected; retrying in %ss: %s",
                    delay,
                    err,
                )
                await asyncio.sleep(delay)

    def _set_connection_state(self, connected: bool, error: str | None) -> None:
        updated = {
            **self.data,
            "connected": connected,
            "error": error,
            "reconnect_count": self._reconnect_count,
        }
        if connected:
            updated["last_connected_at"] = datetime.now(tz=UTC)
        self.async_set_updated_data(updated)

    async def _async_handle_line(self, line: str) -> None:
        parts = line.split("\t", 3)
        if len(parts) == 3 and parts[0] == "ready":
            self._reconnect_attempt = 0
            self._set_connection_state(True, None)
            return
        if len(parts) == 4 and parts[0] == "metering":
            await self._async_handle_metering(parts[2], parts[3])
            return
        if len(parts) != 3 or parts[0] not in {"ringing", "ended"}:
            if line:
                _LOGGER.debug("Ignoring visitor call monitor output: %s", line[:200])
            return
        event_type, place, raw_epoch = parts
        self._reconnect_attempt = 0
        try:
            event_time = datetime.fromtimestamp(int(raw_epoch), tz=UTC)
        except (ValueError, OSError):
            _LOGGER.debug("Ignoring visitor event with invalid epoch: %s", line[:200])
            return

        self._event_sequence += 1
        location = _LOCATION_BY_PLACE.get(place, "알 수 없음")
        updated = {
            **self.data,
            "connected": True,
            "error": None,
            "last_stream_event_at": event_time,
            "last_event": event_type,
            "last_event_id": self._event_sequence,
        }

        if event_type == "ringing":
            updated.update(
                {
                    "active": True,
                    "state": "ringing",
                    "location": location,
                    "place": place,
                    "started_at": event_time,
                    "ended_at": None,
                    "last_duration_seconds": None,
                }
            )
            self.async_set_updated_data(updated)
            # 평소 10초 폴링은 유지하고 실제 호출 뒤 잠깐만 1초로 좁힌다.
            await self._visitor_coordinator.async_start_call_fast_poll()
            return

        started_at = self.data.get("started_at")
        duration = None
        if isinstance(started_at, datetime):
            duration = max(0, int((event_time - started_at).total_seconds()))
        updated.update(
            {
                "active": False,
                "state": "idle",
                "location": location if place else self.data.get("location"),
                "place": place or self.data.get("place"),
                "ended_at": event_time,
                "last_duration_seconds": duration,
            }
        )
        self.async_set_updated_data(updated)

    async def _async_handle_metering(self, raw_epoch: str, payload: str) -> None:
        """검증된 smartMetering report 하나를 상태에 반영한다."""
        try:
            event_time = datetime.fromtimestamp(int(raw_epoch), tz=UTC)
            message = json.loads(payload)
        except (ValueError, OSError, json.JSONDecodeError):
            _LOGGER.debug("Ignoring invalid PAM metering output: %s", payload[:200])
            return

        if not isinstance(message, dict):
            return
        obj = message.get("object")
        if (
            message.get("command") != "report"
            or not isinstance(obj, dict)
            or obj.get("commaxDevice") != "smartMetering"
        ):
            return

        reports = obj.get("subDevice")
        if not isinstance(reports, list):
            return

        meters = dict(self.data.get("meters", {}))
        changed = False
        for report in reports:
            if not isinstance(report, dict) or report.get("sort") not in _METER_SORTS:
                continue
            try:
                raw_value = int(report["value"])
                precision = int(report["precision"])
                if precision < 0 or precision > 9:
                    raise ValueError("precision out of range")
            except (KeyError, TypeError, ValueError):
                _LOGGER.debug("Ignoring invalid PAM meter report: %s", report)
                continue
            meter_sort = report["sort"]
            meters[meter_sort] = {
                "value": raw_value / (10**precision),
                "raw_value": raw_value,
                "precision": precision,
                "scale": report.get("scale"),
                "last_reported_at": event_time,
            }
            changed = True

        if changed:
            self._reconnect_attempt = 0
            self.async_set_updated_data(
                {
                    **self.data,
                    "meters": meters,
                    "connected": True,
                    "last_stream_event_at": event_time,
                    "error": None,
                }
            )
