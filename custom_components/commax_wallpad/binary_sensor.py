"""사진 생성보다 먼저 표시하는 실시간 현관 호출 센서."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator_visitor_call import VisitorCallCoordinator
from .intercom import HOUSEHOLD_PLACE, LOBBY_PLACE, intercom_device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: VisitorCallCoordinator = hass.data[DOMAIN][entry.entry_id][
        "visitor_call"
    ]
    async_add_entities(
        [
            VisitorCallActiveBinarySensor(coordinator, entry),
            IntercomCallBinarySensor(coordinator, entry, LOBBY_PLACE, "lobby_call"),
            IntercomCallBinarySensor(coordinator, entry, HOUSEHOLD_PLACE, "household_call"),
        ]
    )


class VisitorCallActiveBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """SIP Offering부터 Disconnected까지 켜지는 호출 중 센서."""

    _attr_has_entity_name = True
    _attr_translation_key = "visitor_call_active"
    _attr_icon = "mdi:doorbell"

    def __init__(
        self, coordinator: VisitorCallCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_visitor_call_active"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}-visitor")},
            name=f"Commax 방문자 기록 ({entry.data[CONF_HOST]})",
            manufacturer="Commax",
            model="CDP-1020MB visitor archive",
        )

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.data and self.coordinator.data.get("active"))

    @property
    def available(self) -> bool:
        return bool(
            self.coordinator.data and self.coordinator.data.get("connected")
        )

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        return {
            "call_state": data.get("state"),
            "location": data.get("location"),
            "place": data.get("place"),
            "started_at": data.get("started_at"),
            "ended_at": data.get("ended_at"),
            "last_duration_seconds": data.get("last_duration_seconds"),
            "monitor_connected": data.get("connected"),
            "monitor_error": data.get("error"),
        }



class IntercomCallBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Show the current call and retain the last call for this entrance."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:doorbell"

    def __init__(
        self, coordinator: VisitorCallCoordinator, entry: ConfigEntry, place: str, key: str
    ) -> None:
        super().__init__(coordinator)
        self._place = place
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = intercom_device_info(entry)

    @property
    def available(self) -> bool:
        return bool((self.coordinator.data or {}).get("connected"))

    @property
    def is_on(self) -> bool:
        data = self.coordinator.data or {}
        return bool(self.available and data.get("active") and data.get("place") == self._place)

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        latest = data.get("intercom_calls", {}).get(self._place, {})
        attributes = {"location": latest.get("location"), "started_at": latest.get("started_at")}
        if self._place == LOBBY_PLACE:
            attributes["floor"] = latest.get("floor")
        return attributes
