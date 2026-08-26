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
from .const import CONF_ELEVATOR_INDEX, DOMAIN, SERVICE_FAST_POLL
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


async def _async_handle_fast_poll(hass: HomeAssistant, call: ServiceCall) -> None:
    """설정된 모든 엘리베이터 coordinator를 빠른 폴링으로 전환한다.

    이 서비스는 의도적으로 수동이다. 내장 호출 버튼은 이걸 자동으로
    시작하지 않는데, 월패드가 호출 후 자체 상태조회를 수행하기 때문이다 —
    둘을 동시에 돌리면 월패드가 응답을 놓칠 수 있다.
    """
    # 여러 설정 항목이 생겨도 서비스 한 번으로 모든 엘리베이터를 전환한다.
    for data in hass.data.get(DOMAIN, {}).values():
        await data["elevator"].async_start_fast_poll()


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # 아래 모든 coordinator가 재사용하는 라우터 공유 SSH 연결 하나 —
    # 왜 각 poller에게 개별 연결을 안 주기로 했는지는 client.py 참고.
    client = CommaxRouterClient(
        host=entry.data[CONF_HOST],
        port=entry.data[CONF_PORT],
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
    )

    # 첫 refresh를 여기서 기다리므로 라우터 도우미가 준비되지 않았다면
    # 엔티티를 반쯤 생성하지 않고 설정 항목 자체를 실패 상태로 표시한다.
    elevator_coordinator = ElevatorCoordinator(
        hass, client, elevator_index=entry.data[CONF_ELEVATOR_INDEX]
    )
    await elevator_coordinator.async_config_entry_first_refresh()

    # 방문자 사진은 USB ADB라는 별도 경로를 사용한다. ADB 케이블이나 라우터
    # 도우미가 일시적으로 없어도 기존 엘리베이터 통합 로딩을 막지 않도록
    # 별도 SSH 연결을 쓰고, 첫 refresh 실패도 설정 항목 실패로 전파하지 않는다.
    visitor_client = CommaxRouterClient(
        host=entry.data[CONF_HOST],
        port=entry.data[CONF_PORT],
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
    )
    visitor_coordinator = VisitorCoordinator(hass, visitor_client, entry.entry_id)
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
    }

    # 사진은 /local 공개 경로가 아니라 인증이 강제되는 API로만 제공한다.
    await async_register_visitor_http(hass)

    # 지금 코드가 만들지 않는 기기(예: 0.9.0에서 제거된 EMS "월패드" 기기)의
    # 레지스트리 흔적을 정리한다. 플랫폼이 엔티티를 다시 등록하기 전에 해도
    # 안전하다 — 현재 유효한 기기는 identifier로 미리 화이트리스트돼 있다.
    _async_prune_stale_devices(hass, entry)

    if not hass.services.has_service(DOMAIN, SERVICE_FAST_POLL):
        # async 핸들러를 그대로 등록한다 — hass.async_create_task()를 부르는
        # sync 람다로 감싸면 안 된다. 그렇게 감싸면 HA가 이 콜백을 sync 서비스
        # 핸들러로 보고 executor 스레드 풀로 넘겨버리는데, 그 스레드 안에서
        # async_create_task()를 부르는 건 안전하지 않다(HA의 스레드 안전성
        # 검사기가 실제로 이걸 잡아낸다). async 함수를 있는 그대로 등록하면
        # 스레드를 거치지 않고 HA가 평소처럼 이벤트 루프에서 await해준다.
        async def _service_handler(call: ServiceCall) -> None:
            await _async_handle_fast_poll(hass, call)

        hass.services.async_register(DOMAIN, SERVICE_FAST_POLL, _service_handler)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await visitor_call_coordinator.async_start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        # 마지막 설정 항목이 내려갈 때만 공유 서비스를 제거한다.
        data = hass.data[DOMAIN].pop(entry.entry_id)
        await data["visitor_call"].async_stop()
        await data["client"].close()
        await data["visitor_client"].close()
        if not hass.data[DOMAIN]:
            hass.services.async_remove(DOMAIN, SERVICE_FAST_POLL)
    return unload_ok
