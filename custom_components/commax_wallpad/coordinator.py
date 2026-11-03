"""Commax Wallpad (MITM) 통합 — 엘리베이터용 데이터 업데이트 coordinator.

OpenWrt 라우터의 순수 브리지 조회 도우미를 통해 아파트 로컬 엘리베이터 상태
서버를 조회한다. 이 도우미는 원래 월패드 L2 경로를 그대로 유지하면서,
수명이 짧은 getEvStatus 응답만 라우터 쪽으로 우회시킨다. 이 방식이 어떻게
리버스 엔지니어링되고 공동현관 영상/도어오픈 트래픽으로 검증됐는지는
wallpad-project 저장소의 docs/04-discovered-endpoints.md 참고.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .client import CommaxRouterClient
from .const import (
    DIRECTION_IDLE,
    DIRECTION_MAP,
    DIRECTION_UNKNOWN,
    ELEVATOR_FAILURES_BEFORE_UNAVAILABLE,
    ELEVATOR_MAX_ATTEMPTS,
    ELEVATOR_RETRY_DELAY_SECONDS,
)

_LOGGER = logging.getLogger(__name__)

# 서버 응답은 XML이지만 구조가 작고 고정적이다. 전체 XML namespace 파서 대신
# 필요한 필드만 추출해 오래된 gSOAP 응답의 namespace 차이에 덜 민감하게 한다.
_FLOOR_RE = re.compile(r"<carFloor>(-?\d+)</carFloor>")
_BASEMENT_RE = re.compile(r"<isBasement>(\d+)</isBasement>")
_DIRECTION_RE = re.compile(r"<carDirection>(\d+)</carDirection>")
_STATUS_RE = re.compile(r"<evStatus>(\d+)</evStatus>")
_QUERY_HELPER = "/root/wallpad-elevator-query.sh"


def _build_remote_command(elevator_index: int) -> str:
    """라우터의 검증된 단발 조회 도우미를 실행할 명령을 만든다.

    stderr를 stdout에 합쳐서, 도우미 초반 실패(조회 락 타임아웃, 조회
    브리지 미준비 등)가 원인 불명의 빈 응답으로만 보이지 않고 HA 로그에
    드러나게 한다 — coordinator_energy.py의 도우미 명령과 동일한 이유.
    """
    return f"{_QUERY_HELPER} {int(elevator_index)} 2>&1"


class ElevatorCoordinator(DataUpdateCoordinator[dict]):
    """사용자가 요청할 때만 엘리베이터 상태를 조회한다."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: CommaxRouterClient,
        elevator_index: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="commax_wallpad_elevator",
            update_interval=None,
        )
        self._client = client
        # 인덱스는 int로 고정해 원격 셸 명령에 임의 문자열이 들어갈 수 없게 한다.
        self._remote_command = _build_remote_command(elevator_index)
        self._consecutive_failures = 0
        self.last_successful_update: datetime | None = None
        self.last_refresh_error: str | None = None
        self._manual_refresh_lock = asyncio.Lock()
        # Loading entities must never trigger a getEvStatus request.
        self.async_set_updated_data({})

    async def async_refresh_once(self) -> None:
        """사용자 요청 한 번을 처리하고 자동 갱신은 예약하지 않는다."""
        if self._manual_refresh_lock.locked():
            raise UpdateFailed("Elevator refresh is already in progress")
        async with self._manual_refresh_lock:
            await self.async_refresh()
            if self.last_refresh_error or not self.last_update_success:
                raise UpdateFailed(self.last_refresh_error or str(self.last_exception))

    async def _async_update_data(self) -> dict:
        # HA는 아파트망에 직접 연결되지 않는다. 모든 조회는 SSH를 통해 라우터의
        # 단발 도우미를 실행하고 그 stdout(+stderr)만 받아 파싱한다.
        #
        # 조회 도우미는 lan1의 flood/learning을 순간적으로 끄고, 중복 IP
        # 소켓으로 실제 월패드 트래픽과 경합한다(wallpad-elevator-query.sh
        # 참고) — 그래서 carFloor가 도착하기 전에 응답이 잘리는 일이 가끔
        # 생긴다. 실패할 때마다 바로 UpdateFailed를 던지는 대신 한 번 짧게
        # 재시도하고, 그래도 안 되면 최근 값을 잠깐 유지해서 일회성 실패가
        # 로그 오류나 unavailable로 새지 않게 한다.
        self.last_refresh_error = None
        failure_details: list[str] = []
        for attempt in range(1, ELEVATOR_MAX_ATTEMPTS + 1):
            try:
                body = await self._client.exec(self._remote_command)
            except Exception as err:  # noqa: BLE001 - client already narrows to real failures
                failure_details.append(f"attempt {attempt}: SSH command failed: {err}")
            else:
                floor_match = _FLOOR_RE.search(body)
                if floor_match:
                    self._consecutive_failures = 0
                    self.last_successful_update = dt_util.utcnow()
                    return self._parse_result(body, floor_match)
                failure_details.append(
                    f"attempt {attempt}: no carFloor found (helper output: "
                    f"{body[:300]!r})"
                )

            if attempt < ELEVATOR_MAX_ATTEMPTS:
                await asyncio.sleep(ELEVATOR_RETRY_DELAY_SECONDS)

        self._consecutive_failures += 1
        detail = "; ".join(failure_details)
        self.last_refresh_error = detail

        if (
            self.last_successful_update is not None
            and self._consecutive_failures < ELEVATOR_FAILURES_BEFORE_UNAVAILABLE
        ):
            _LOGGER.warning(
                "Elevator poll failed (%s/%s); keeping last valid reading: %s",
                self._consecutive_failures,
                ELEVATOR_FAILURES_BEFORE_UNAVAILABLE,
                detail,
            )
            return self.data

        raise UpdateFailed(
            f"{detail} — is wallpad-l2-mitm.sh active and is "
            "wallpad-elevator-query.sh installed on the router?"
        )

    def _parse_result(self, body: str, floor_match: re.Match[str]) -> dict:
        direction_match = _DIRECTION_RE.search(body)
        direction_code = int(direction_match.group(1)) if direction_match else None
        basement_match = _BASEMENT_RE.search(body)
        status_match = _STATUS_RE.search(body)

        raw_floor = int(floor_match.group(1))
        is_basement = bool(int(basement_match.group(1))) if basement_match else False
        # carFloor는 지상/지하 구분 없이 항상 양수로 오고 isBasement 플래그로만
        # 지하 여부를 구분한다 — 예: 지하2층은 carFloor=2, isBasement=1로 옴.
        # 이를 자동화용 부호 있는 층수(B2 -> -2)와 표시용 "B2" 라벨로 변환한다.
        signed_floor = -raw_floor if is_basement else raw_floor
        floor_label = f"B{raw_floor}" if is_basement else str(raw_floor)
        direction = DIRECTION_MAP.get(direction_code, DIRECTION_UNKNOWN)

        return {
            "floor": signed_floor,
            "floor_label": floor_label,
            "raw_floor": raw_floor,
            "is_basement": is_basement,
            "direction_code": direction_code,
            "direction": direction,
            "ev_status": int(status_match.group(1)) if status_match else None,
        }
