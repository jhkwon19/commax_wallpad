"""가장 최근 월패드 방문자 스냅샷 image 엔티티."""

from __future__ import annotations

from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator_visitor import VisitorCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: VisitorCoordinator = hass.data[DOMAIN][entry.entry_id]["visitor"]
    async_add_entities([LatestVisitorImage(coordinator, entry)])


class LatestVisitorImage(CoordinatorEntity, ImageEntity):
    """새 사진이 생겼을 때만 coordinator가 교체하는 최신 JPEG."""

    _attr_has_entity_name = True
    _attr_translation_key = "latest_visitor"
    _attr_content_type = "image/jpeg"

    def __init__(self, coordinator: VisitorCoordinator, entry: ConfigEntry) -> None:
        CoordinatorEntity.__init__(self, coordinator)
        ImageEntity.__init__(self, coordinator.hass)
        self._attr_unique_id = f"{entry.entry_id}_latest_visitor"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}-visitor")},
            name=f"Commax 방문자 기록 ({entry.data[CONF_HOST]})",
            manufacturer="Commax",
            model="CDP-1020MB visitor archive",
        )

    @property
    def image_last_updated(self):
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("image_last_updated")

    async def async_image(self) -> bytes | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("image")
