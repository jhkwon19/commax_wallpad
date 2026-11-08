"""Commax Wallpad (MITM) 통합의 센서 플랫폼."""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, EV_STATUS_MAP, EV_STATUS_UNKNOWN
from .coordinator import ElevatorCoordinator
from .coordinator_visitor import VisitorCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    # 엘리베이터 센서와 방문자 사진 센서는 각각 기존 coordinator를 공유한다.
    data = hass.data[DOMAIN][entry.entry_id]
    elevator_coordinator: ElevatorCoordinator = data["elevator"]
    visitor_coordinator: VisitorCoordinator = data["visitor"]
    async_add_entities(
        [
            ElevatorFloorSensor(elevator_coordinator, entry),
            ElevatorDirectionSensor(elevator_coordinator, entry),
            ElevatorStatusSensor(elevator_coordinator, entry),
            LastVisitorSensor(visitor_coordinator, entry),
            VisitorArchiveSensor(visitor_coordinator, entry),
        ]
    )


class _CommaxEntityBase(CoordinatorEntity, SensorEntity):
    """기본 엔티티 — 서브클래스가 어느 기기(엘리베이터/월패드)에 속하는지 지정한다."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry


class _ElevatorEntityBase(_CommaxEntityBase):
    """'엘리베이터' 기기 아래로 묶이는 엔티티."""

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        # 호출 버튼도 같은 identifier를 사용하므로 HA 기기 화면에 함께 표시된다.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}-elevator")},
            name=f"Commax 엘리베이터 ({entry.data[CONF_HOST]})",
            manufacturer="Commax",
            model="CDP-1020MB elevator link (MITM)",
        )

    @property
    def extra_state_attributes(self) -> dict:
        last_success = self.coordinator.last_successful_update
        return {
            "update_mode": "manual",
            "last_refresh_error": self.coordinator.last_refresh_error,
            "last_successful_update": last_success.isoformat() if last_success else None,
        }


class _VisitorEntityBase(_CommaxEntityBase):
    """방문자 기록 전용 기기 아래로 묶이는 엔티티."""

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}-visitor")},
            name=f"Commax 방문자 기록 ({entry.data[CONF_HOST]})",
            manufacturer="Commax",
            model="CDP-1020MB visitor archive",
        )


class ElevatorFloorSensor(_ElevatorEntityBase):
    """현재 엘리베이터 차량 층수."""

    _attr_translation_key = "floor"
    _attr_icon = "mdi:elevator"

    def __init__(self, coordinator: ElevatorCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_floor"

    @property
    def native_value(self) -> str | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("floor_label")

    @property
    def extra_state_attributes(self) -> dict:
        if self.coordinator.data is None:
            return {}
        # 화면 표시값은 "B2"지만 자동화에서는 floor_numeric=-2를 사용할 수 있다.
        return {
            **super().extra_state_attributes,
            "floor_numeric": self.coordinator.data.get("floor"),
            "is_basement": self.coordinator.data.get("is_basement"),
            "raw_floor": self.coordinator.data.get("raw_floor"),
        }


class ElevatorDirectionSensor(_ElevatorEntityBase):
    """현재 엘리베이터 운행 방향(정지/상승/하강/알수없음).

    carDirection 0/1/2는 실제 운행으로 확인됨(2026-08-10): 0=정지,
    1=상승(상승 구간 내내), 2=하강(하강 구간 내내, 도착 후 0으로
    돌아오기 전 짧은 도어오픈 dwell 포함). wallpad-project 저장소의
    docs/04-discovered-endpoints.md(carDirection) 참고.
    """

    _attr_translation_key = "direction"
    _attr_icon = "mdi:swap-vertical"
    _attr_device_class = "enum"
    _attr_options = ["정지", "상승", "하강", "알수없음"]

    def __init__(self, coordinator: ElevatorCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_direction"

    @property
    def native_value(self) -> str | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("direction")

    @property
    def extra_state_attributes(self) -> dict:
        if self.coordinator.data is None:
            return {}
        return {
            **super().extra_state_attributes,
            "direction_code": self.coordinator.data.get("direction_code"),
        }


class ElevatorStatusSensor(_ElevatorEntityBase):
    """엘리베이터 운행 상태(정상/점검중/고장/도착완료 등).

    evStatus 0~9 값의 의미는 com.commax.elevator 앱(Util.getMessage)을
    리버싱해 확인했다(2026-08-26). 다만 실제 세대 호출 2회에서 도착 순간을
    포함해 evStatus가 계속 1(정상)로 고정이었다 — 이 설치에서는 9(도착완료)
    같은 값이 실제로 오지 않는 것으로 보이며, 다른 값들도 검증되지 않았다.
    이 센서는 "값이 온다면 뭘 뜻하는지"를 보여주는 참고용이고, 평소엔 계속
    "정상"만 보이는 게 정상이다. wallpad-project 저장소의
    docs/04-discovered-endpoints.md(evStatus 값 매핑) 참고.
    """

    _attr_translation_key = "status"
    _attr_icon = "mdi:elevator-passenger"
    _attr_device_class = "enum"
    _attr_options = [*EV_STATUS_MAP.values(), EV_STATUS_UNKNOWN]

    def __init__(self, coordinator: ElevatorCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_status"

    @property
    def native_value(self) -> str | None:
        if self.coordinator.data is None:
            return None
        code = self.coordinator.data.get("ev_status")
        return EV_STATUS_MAP.get(code, EV_STATUS_UNKNOWN) if code is not None else None

    @property
    def extra_state_attributes(self) -> dict:
        if self.coordinator.data is None:
            return {}
        return {
            **super().extra_state_attributes,
            "ev_status_code": self.coordinator.data.get("ev_status"),
        }


class LastVisitorSensor(_VisitorEntityBase):
    """월패드에 마지막으로 저장된 방문자 사진의 촬영 시각."""

    _attr_translation_key = "last_visitor"
    _attr_icon = "mdi:doorbell-video"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: VisitorCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_last_visitor"

    @property
    def native_value(self):
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("captured_datetime")

    @property
    def extra_state_attributes(self) -> dict:
        if self.coordinator.data is None:
            return {}
        return {
            "uid": self.coordinator.data.get("uid"),
            "flags": self.coordinator.data.get("flags"),
            "location": self.coordinator.data.get("location"),
            "marker": self.coordinator.data.get("marker"),
            "filename": self.coordinator.data.get("filename"),
            "last_checked_at": self.coordinator.data.get("last_checked_at"),
        }


class VisitorArchiveSensor(_VisitorEntityBase):
    """HA 로컬에 안전하게 보관된 방문자 사진 수."""

    _attr_translation_key = "visitor_archive"
    _attr_icon = "mdi:image-multiple"
    _attr_native_unit_of_measurement = "장"

    def __init__(self, coordinator: VisitorCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_visitor_archive"

    @property
    def native_value(self) -> int | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("archive_count")

    @property
    def extra_state_attributes(self) -> dict:
        if self.coordinator.data is None:
            return {}
        return {
            "pending": self.coordinator.data.get("archive_pending"),
            "api_url": self.coordinator.data.get("archive_api"),
            "card_resource": "/commax_wallpad/visitor-card.js",
            "archive_version": self.coordinator.archive_version(),
            "last_sync": self.coordinator.data.get("archive_last_sync"),
            "error": self.coordinator.data.get("archive_error"),
        }
