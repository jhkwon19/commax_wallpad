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
from datetime import datetime, timedelta

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
    FAST_POLL_MAX_DURATION_SECONDS,
    FAST_POLL_SETTLE_SECONDS,
    FAST_SCAN_INTERVAL_SECONDS,
    SCAN_INTERVAL_SECONDS,
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
    드러나게 한다.
    """
    return f"{_QUERY_HELPER} {int(elevator_index)} 2>&1"


class ElevatorCoordinator(DataUpdateCoordinator[dict]):
    """SSH로 MITM 라우터를 통해 엘리베이터 층수/방향을 폴링한다."""

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
            update_interval=timedelta(seconds=SCAN_INTERVAL_SECONDS),
        )
        self._client = client
        # 인덱스는 int로 고정해 원격 셸 명령에 임의 문자열이 들어갈 수 없게 한다.
        self._remote_command = _build_remote_command(elevator_index)
        self._fast_until: datetime | None = None
        self._idle_since: datetime | None = None
        self._consecutive_failures = 0

    async def async_start_fast_poll(self) -> None:
        """FAST_SCAN_INTERVAL_SECONDS로 전환한다 — 예: 호출 버튼을 누른 직후.

        차량이 FAST_POLL_SETTLE_SECONDS만큼 정지(idle) 상태로 머물거나,
        FAST_POLL_MAX_DURATION_SECONDS가 지나면(안전장치) 자동으로
        원래 속도로 복귀한다 — _maybe_revert_to_idle_rate() 참고.
        """
        now = dt_util.utcnow()
        self._fast_until = now + timedelta(seconds=FAST_POLL_MAX_DURATION_SECONDS)
        self._idle_since = None
        self.update_interval = timedelta(seconds=FAST_SCAN_INTERVAL_SECONDS)

        # DataUpdateCoordinator는 *예약된* refresh가 끝나야만 그 시점의
        # update_interval로 다음 타이머를 다시 잡는다. 지금처럼 수동으로
        # refresh를 요청하면 기존에 걸려 있던 타이머는 그대로 남기 때문에,
        # update_interval만 바꿔서는 그 낡은 타이머가 스스로 울릴 때까지
        # 반영되지 않는다 — 최악의 경우 예전 간격만큼 늦어져서 운행이 이미
        # 끝난 뒤일 수도 있다. 그래서 기존 타이머를 취소해서, 아래 refresh가
        # 새로 정한 빠른 속도로 바로 다시 예약되게 한다.
        unsub_refresh = getattr(self, "_unsub_refresh", None)
        if unsub_refresh:
            unsub_refresh()
            self._unsub_refresh = None

        await self.async_request_refresh()

    def _maybe_revert_to_idle_rate(self, direction: str) -> None:
        if self._fast_until is None:
            return  # already at idle rate

        now = dt_util.utcnow()
        if now >= self._fast_until:
            _LOGGER.debug("Fast-poll safety cap reached, reverting to idle rate")
            self._fast_until = None
            self._idle_since = None
            self.update_interval = timedelta(seconds=SCAN_INTERVAL_SECONDS)
            return

        # 운행이 다시 감지되면 정지 누적 시간을 초기화한다. 잠깐 0이 들어온 것만으로
        # 느린 폴링으로 복귀하지 않도록 연속 정지 시간을 요구한다.
        if direction != DIRECTION_IDLE:
            self._idle_since = None
            return

        if self._idle_since is None:
            self._idle_since = now
        elif (now - self._idle_since).total_seconds() >= FAST_POLL_SETTLE_SECONDS:
            _LOGGER.debug("Elevator settled at idle, reverting to idle rate")
            self._fast_until = None
            self._idle_since = None
            self.update_interval = timedelta(seconds=SCAN_INTERVAL_SECONDS)

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
                    return self._parse_result(body, floor_match)
                failure_details.append(
                    f"attempt {attempt}: no carFloor found (helper output: "
                    f"{body[:300]!r})"
                )

            if attempt < ELEVATOR_MAX_ATTEMPTS:
                await asyncio.sleep(ELEVATOR_RETRY_DELAY_SECONDS)

        self._consecutive_failures += 1
        detail = "; ".join(failure_details)

        if (
            self.data is not None
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

        # 파싱된 최신 방향을 빠른 폴링 종료 판단에도 사용한다.
        self._maybe_revert_to_idle_rate(direction)

        return {
            "floor": signed_floor,
            "floor_label": floor_label,
            "raw_floor": raw_floor,
            "is_basement": is_basement,
            "direction_code": direction_code,
            "direction": direction,
            "ev_status": int(status_match.group(1)) if status_match else None,
        }
