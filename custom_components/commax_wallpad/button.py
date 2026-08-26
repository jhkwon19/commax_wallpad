"""Commax 엘리베이터 호출 버튼 플랫폼.

버튼은 라우터의 검증된 단발 도우미만 실행한다. SOAP 생성, 월패드 IP/MAC 사용,
순간적인 flood 차단과 원복은 모두 라우터 스크립트가 책임진다.
"""

from __future__ import annotations

import asyncio
import re
from time import monotonic

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .client import CommaxRouterClient
from .const import DOMAIN, EV_STATUS_MAP, EV_STATUS_NORMAL, EV_STATUS_UNKNOWN
from .coordinator import ElevatorCoordinator

# 이 세대는 17층이므로 캡처에서 확인한 아래 방향(callDown=1)을 사용한다.
_CALL_HELPER = "/root/wallpad-elevator-call.sh down"
# HTTP 200만으로 성공을 판단하지 않는다. gSOAP의 명시적 성공값도 확인한다.
_SUCCESS_RE = re.compile(r"<ns:setEvStatusResponse><out>0</out>")
# UI 연타나 자동화 중복 실행으로 같은 호출을 짧은 시간에 반복하지 않게 한다.
_MIN_CALL_INTERVAL_SECONDS = 5


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    # __init__.py가 만든 공유 SSH client와 층/방향 coordinator를 재사용한다.
    data = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [ElevatorCallButton(data["client"], data["elevator"], entry)]
    )


class ElevatorCallButton(ButtonEntity):
    """이 세대 층으로 엘리베이터를 하강 호출한다."""

    _attr_has_entity_name = True
    _attr_translation_key = "call_elevator"
    _attr_icon = "mdi:elevator-down"

    def __init__(
        self,
        client: CommaxRouterClient,
        coordinator: ElevatorCoordinator,
        entry: ConfigEntry,
    ) -> None:
        self._client = client
        self._coordinator = coordinator
        self._last_call_at = float("-inf")
        # 동시에 들어온 두 번의 press도 반드시 순서대로 처리한다.
        self._press_lock = asyncio.Lock()
        self._attr_unique_id = f"{entry.entry_id}_call_elevator"
        # 센서와 동일한 identifier를 사용해 하나의 엘리베이터 기기에 묶는다.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}-elevator")},
            name=f"Commax 엘리베이터 ({entry.data[CONF_HOST]})",
            manufacturer="Commax",
            model="CDP-1020MB elevator link (MITM)",
        )

    async def async_press(self) -> None:
        async with self._press_lock:
            # 앱도 evStatus가 정상(1)이 아니면 호출 버튼을 비활성화한다 —
            # 점검중/고장/화재운행 중에 호출을 보내면 무의미하거나 위험할 수
            # 있으므로 같은 가드를 여기서도 둔다. coordinator.data는
            # async_config_entry_first_refresh() 이후에만 이 버튼이 생성되므로
            # None인 경우는 없지만, 방어적으로 확인한다.
            status = (self._coordinator.data or {}).get("ev_status")
            if status is not None and status != EV_STATUS_NORMAL:
                label = EV_STATUS_MAP.get(status, EV_STATUS_UNKNOWN)
                raise HomeAssistantError(
                    f"Elevator call blocked: status is '{label}', not normal"
                )

            now = monotonic()
            if now - self._last_call_at < _MIN_CALL_INTERVAL_SECONDS:
                raise HomeAssistantError(
                    "Elevator call ignored: wait at least 5 seconds between calls"
                )

            # CommaxRouterClient도 명령을 직렬화하므로 상태조회와 SSH 출력이
            # 섞이지 않는다. 라우터에서는 별도의 파일 잠금이 한 번 더 보호한다.
            try:
                body = await self._client.exec(_CALL_HELPER, timeout=10)
            except Exception as err:  # noqa: BLE001 - surface router failures to HA
                raise HomeAssistantError(f"Elevator call failed: {err}") from err

            if not _SUCCESS_RE.search(body):
                raise HomeAssistantError(
                    "Elevator server did not confirm the call; check the router helper"
                )

            self._last_call_at = monotonic()
            # 호출 직후 월패드도 자체 상태조회를 시작한다. 여기서 HA의 1초
            # 폴링까지 자동 실행하면 두 조회가 겹칠 수 있으므로 호출만 끝낸다.
