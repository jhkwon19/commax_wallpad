"""Commax 월패드 통합의 진입점.

설정 항목마다 엘리베이터 연결, 방문자 사진 연결, logcat 스트림
연결을 만들고 각 플랫폼이 해당 객체를 공유하도록 ``hass.data``에 보관한다.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
    Platform,
)
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr

from .client import CommaxRouterClient
from .call_diagnostics import DiagnosticJournal
from .const import CONF_ELEVATOR_INDEX, DOMAIN, SERVICE_FAST_POLL, SERVICE_REFRESH_ELEVATOR
from .coordinator import ElevatorCoordinator
from .coordinator_visitor import VisitorCoordinator
from .coordinator_visitor_call import VisitorCallCoordinator
from .visitor_http import async_register_visitor_http

_LOGGER = logging.getLogger(__name__)

# sensor.py와 button.py는 아래 coordinator/client를 새로 만들지 않고 공유한다.
PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BUTTON,
    Platform.IMAGE,
    Platform.EVENT,
    Platform.BINARY_SENSOR,
]

# 지금 코드가 만드는 기기들의 identifier suffix. 0.9.0 이전에는 부정확한 당월
# EMS 센서들이 "월패드" 기기(identifier가 그냥 entry_id이거나 "-wallpad"였음)
# 아래 있었는데, 그 엔티티들은 제거됐지만 빈 기기 껍데기는 레지스트리에 남아
# HA UI에서 수동으로 지워야 했다. 아래 목록에 없는 기기는 지금 코드 어디서도
# 다시 만들지 않으므로, 매 시작마다 안전하게 정리한다.
_CURRENT_DEVICE_SUFFIXES = ("elevator", "visitor", "realtime-metering")


def _async_prune_stale_devices(hass: HomeAssistant, entry: ConfigEntry) -> None:
    registry = dr.async_get(hass)
    current_identifiers = {
        (DOMAIN, f"{entry.entry_id}-{suffix}") for suffix in _CURRENT_DEVICE_SUFFIXES
    }
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        if not device.identifiers & current_identifiers:
            _LOGGER.info(
                "Removing stale device registry entry %s (%s)",
                device.id,
                device.name,
            )
            registry.async_remove_device(device.id)


async def _async_handle_refresh(hass: HomeAssistant, call: ServiceCall) -> None:
    """명시적인 요청마다 엘리베이터를 한 번 갱신한다."""
    for data in hass.data.get(DOMAIN, {}).values():
        await data["elevator"].async_refresh_once()


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # 아래 모든 coordinator가 재사용하는 라우터 공유 SSH 연결 하나 —
    # 왜 각 poller에게 개별 연결을 안 주기로 했는지는 client.py 참고.
    client = CommaxRouterClient(
        host=entry.data[CONF_HOST],
        port=entry.data[CONF_PORT],
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
    )

    # 시작/재로드 때 엘리베이터를 조회하지 않는다. 방문자 ADB 처리는 별도다.
    elevator_coordinator = ElevatorCoordinator(
        hass, client, elevator_index=entry.data[CONF_ELEVATOR_INDEX]
    )

    # 방문자 사진은 USB ADB라는 별도 경로를 사용한다. ADB 케이블이나 라우터
    # 도우미가 일시적으로 없어도 기존 엘리베이터 통합 로딩을 막지 않도록
    # 별도 SSH 연결을 쓰고, 첫 refresh 실패도 설정 항목 실패로 전파하지 않는다.
    visitor_client = CommaxRouterClient(
        host=entry.data[CONF_HOST],
        port=entry.data[CONF_PORT],
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
    )
    diagnostics = DiagnosticJournal(hass, entry.entry_id)
    await diagnostics.async_start()
    visitor_coordinator = VisitorCoordinator(hass, visitor_client, entry.entry_id, diagnostics)
    await visitor_coordinator.async_refresh()

    # logcat은 장기 실행 스트림이므로 사진 조회와 SSH 연결을 공유하지 않는다.
    # 도우미가 아직 설치되지 않았더라도 통합 로드는 성공하고 진단 속성에만
    # 오류를 남긴 채 지수형 지연으로 재연결한다.
    visitor_call_client = CommaxRouterClient(
        host=entry.data[CONF_HOST],
        port=entry.data[CONF_PORT],
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
    )
    visitor_call_coordinator = VisitorCallCoordinator(
        hass, visitor_call_client, visitor_coordinator
    )
    await visitor_call_coordinator.async_refresh()

    # 플랫폼 모듈은 이 저장소에서 동일한 SSH 연결과 coordinator를 꺼내 쓴다.
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "client": client,
        "elevator": elevator_coordinator,
        "visitor_client": visitor_client,
        "visitor": visitor_coordinator,
        "visitor_call_client": visitor_call_client,
        "visitor_call": visitor_call_coordinator,
        "diagnostics": diagnostics,
    }

    # 사진은 /local 공개 경로가 아니라 인증이 강제되는 API로만 제공한다.
    await async_register_visitor_http(hass)

    # 지금 코드가 만들지 않는 기기(예: 0.9.0에서 제거된 EMS "월패드" 기기)의
    # 레지스트리 흔적을 정리한다. 플랫폼이 엔티티를 다시 등록하기 전에 해도
    # 안전하다 — 현재 유효한 기기는 identifier로 미리 화이트리스트돼 있다.
    _async_prune_stale_devices(hass, entry)

    async def _service_handler(call: ServiceCall) -> None:
        await _async_handle_refresh(hass, call)

    # Compatibility alias: fast_poll now performs one refresh, never starts a timer.
    for service in (SERVICE_REFRESH_ELEVATOR, SERVICE_FAST_POLL):
        if not hass.services.has_service(DOMAIN, service):
            hass.services.async_register(DOMAIN, service, _service_handler)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await visitor_call_coordinator.async_start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        # 마지막 설정 항목이 내려갈 때만 공유 서비스를 제거한다.
        data = hass.data[DOMAIN].pop(entry.entry_id)
        await data["visitor_call"].async_stop()
        data["visitor"].stop_observation()
        await data["diagnostics"].async_close()
        await data["client"].close()
        await data["visitor_client"].close()
        if not hass.data[DOMAIN]:
            for service in (SERVICE_REFRESH_ELEVATOR, SERVICE_FAST_POLL):
                hass.services.async_remove(DOMAIN, service)
    return unload_ok
