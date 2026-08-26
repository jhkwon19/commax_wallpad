"""Commax Wallpad (MITM) 통합의 센서 플랫폼."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_HOST,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfPower,
    UnitOfVolume,
    UnitOfVolumeFlowRate,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, EV_STATUS_MAP, EV_STATUS_UNKNOWN
from .coordinator import ElevatorCoordinator
from .coordinator_visitor import VisitorCoordinator
from .coordinator_visitor_call import VisitorCallCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    # 하나의 엘리베이터 coordinator는 층/방향 센서가 공유하고, 장기 logcat
    # 스트림 하나가 방문자 호출과 실시간 검침을 함께 전달한다.
    data = hass.data[DOMAIN][entry.entry_id]
    elevator_coordinator: ElevatorCoordinator = data["elevator"]
    visitor_coordinator: VisitorCoordinator = data["visitor"]
    event_coordinator: VisitorCallCoordinator = data["visitor_call"]
    async_add_entities(
        [
            ElevatorFloorSensor(elevator_coordinator, entry),
            ElevatorDirectionSensor(elevator_coordinator, entry),
            ElevatorStatusSensor(elevator_coordinator, entry),
            RealtimeElectricPowerSensor(event_coordinator, entry),
            RealtimeGasFlowSensor(event_coordinator, entry),
            RealtimeWaterFlowSensor(event_coordinator, entry),
            EstimatedGasConsumptionSensor(event_coordinator, entry),
            EstimatedWaterConsumptionSensor(event_coordinator, entry),
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


class _RealtimeMeterEntityBase(_CommaxEntityBase):
    """PAM 순간 검침 엔티티를 별도 기기로 묶는 공통 기반."""

    _meter_sort: str
    _unique_suffix: str
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(
        self, coordinator: VisitorCallCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_{self._unique_suffix}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}-realtime-metering")},
            name=f"Commax 실시간 검침 ({entry.data[CONF_HOST]})",
            manufacturer="Commax",
            model="CDP-1020MB Smart Metering",
        )

    @property
    def _meter_data(self) -> dict | None:
        data = self.coordinator.data or {}
        meter = data.get("meters", {}).get(self._meter_sort)
        return meter if isinstance(meter, dict) else None

    @property
    def native_value(self) -> float | None:
        meter = self._meter_data
        return meter.get("value") if meter is not None else None

    @property
    def available(self) -> bool:
        data = self.coordinator.data or {}
        return bool(data.get("connected"))

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        meter = self._meter_data or {}
        return {
            "raw_value": meter.get("raw_value"),
            "precision": meter.get("precision"),
            "last_reported_at": meter.get("last_reported_at"),
            "stream_connected": bool(data.get("connected")),
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
        return {"direction_code": self.coordinator.data.get("direction_code")}


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
        return EV_STATUS_MAP.get(code, EV_STATUS_UNKNOWN)

    @property
    def extra_state_attributes(self) -> dict:
        if self.coordinator.data is None:
            return {}
        return {"ev_status_code": self.coordinator.data.get("ev_status")}


class RealtimeElectricPowerSensor(_RealtimeMeterEntityBase):
    """PAM이 보고한 현재 소비 전력."""

    _meter_sort = "electricMeter"
    _unique_suffix = "realtime_electric_power"
    _attr_translation_key = "realtime_electric_power"
    _attr_icon = "mdi:flash"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT


class RealtimeGasFlowSensor(_RealtimeMeterEntityBase):
    """PAM이 보고한 현재 가스 유량."""

    _meter_sort = "gasMeter"
    _unique_suffix = "realtime_gas_flow"
    _attr_translation_key = "realtime_gas_flow"
    _attr_icon = "mdi:fire"
    _attr_device_class = SensorDeviceClass.VOLUME_FLOW_RATE
    _attr_native_unit_of_measurement = UnitOfVolumeFlowRate.CUBIC_METERS_PER_HOUR


class RealtimeWaterFlowSensor(_RealtimeMeterEntityBase):
    """PAM이 보고한 현재 수도 유량."""

    _meter_sort = "waterMeter"
    _unique_suffix = "realtime_water_flow"
    _attr_translation_key = "realtime_water_flow"
    _attr_icon = "mdi:water"
    _attr_device_class = SensorDeviceClass.VOLUME_FLOW_RATE
    _attr_native_unit_of_measurement = UnitOfVolumeFlowRate.CUBIC_METERS_PER_HOUR


class _EstimatedConsumptionSensor(CoordinatorEntity, RestoreSensor):
    """유량 보고를 왼쪽 직사각형 방식으로 적산한 복원 가능한 추정치."""

    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfVolume.CUBIC_METERS
    _attr_suggested_display_precision = 4
    _meter_sort: str
    _unique_suffix: str

    def __init__(
        self, coordinator: VisitorCallCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{self._unique_suffix}"
        self._attr_native_value = 0.0
        self._last_seen_report_at: datetime | None = None
        self._integration_report_at: datetime | None = None
        self._integration_flow: float | None = None
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}-realtime-metering")},
            name=f"Commax 실시간 검침 ({entry.data[CONF_HOST]})",
            manufacturer="Commax",
            model="CDP-1020MB Smart Metering",
        )

    async def async_added_to_hass(self) -> None:
        """누적값만 복원하고 중단 시간은 사용량으로 적산하지 않는다."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state and last_state.state not in {STATE_UNKNOWN, STATE_UNAVAILABLE}:
            try:
                restored = float(last_state.state)
            except (TypeError, ValueError):
                restored = 0.0
            if restored >= 0:
                self._attr_native_value = restored

    @property
    def available(self) -> bool:
        return bool(self.coordinator.data and self.coordinator.data.get("connected"))

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "estimated": True,
            "source_meter": self._meter_sort,
            "source_flow": self._integration_flow,
            "last_integrated_at": self._integration_report_at,
            "stream_connected": bool(
                self.coordinator.data and self.coordinator.data.get("connected")
            ),
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        data = self.coordinator.data or {}
        if not data.get("connected"):
            # 다음 연결의 첫 보고는 새 기준점으로만 사용한다. 연결이 끊긴 동안
            # 마지막 유량이 계속됐다고 가정하면 허위 사용량이 생길 수 있다.
            self._integration_report_at = None
            self._integration_flow = None
            super()._handle_coordinator_update()
            return

        meter = data.get("meters", {}).get(self._meter_sort)
        if not isinstance(meter, dict):
            super()._handle_coordinator_update()
            return
        report_at = meter.get("last_reported_at")
        flow = meter.get("value")
        if (
            not isinstance(report_at, datetime)
            or not isinstance(flow, int | float)
            or (
                self._last_seen_report_at is not None
                and report_at <= self._last_seen_report_at
            )
        ):
            super()._handle_coordinator_update()
            return

        if (
            self._integration_report_at is not None
            and self._integration_flow is not None
            and report_at > self._integration_report_at
        ):
            elapsed_hours = (
                report_at - self._integration_report_at
            ).total_seconds() / 3600
            self._attr_native_value = round(
                float(self._attr_native_value or 0)
                + self._integration_flow * elapsed_hours,
                6,
            )

        self._last_seen_report_at = report_at
        self._integration_report_at = report_at
        self._integration_flow = float(flow)
        super()._handle_coordinator_update()


class EstimatedGasConsumptionSensor(_EstimatedConsumptionSensor):
    """에너지 대시보드용 추정 누적 가스 사용량."""

    _meter_sort = "gasMeter"
    _unique_suffix = "estimated_gas_consumption"
    _attr_translation_key = "estimated_gas_consumption"
    _attr_icon = "mdi:meter-gas"
    _attr_device_class = SensorDeviceClass.GAS


class EstimatedWaterConsumptionSensor(_EstimatedConsumptionSensor):
    """에너지 대시보드용 추정 누적 수도 사용량."""

    _meter_sort = "waterMeter"
    _unique_suffix = "estimated_water_consumption"
    _attr_translation_key = "estimated_water_consumption"
    _attr_icon = "mdi:water-circle"
    _attr_device_class = SensorDeviceClass.WATER


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
