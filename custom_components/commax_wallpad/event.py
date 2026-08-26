"""새 방문자 사진이 저장됐을 때 발생하는 event 엔티티."""

from __future__ import annotations

from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator_visitor import VisitorCoordinator
from .coordinator_visitor_call import VisitorCallCoordinator

EVENT_VISITOR_DETECTED = "visitor_detected"
EVENT_VISITOR_RINGING = "ringing"
EVENT_VISITOR_ENDED = "ended"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: VisitorCoordinator = data["visitor"]
    call_coordinator: VisitorCallCoordinator = data["visitor_call"]
    async_add_entities(
        [
            VisitorEvent(coordinator, entry),
            VisitorCallEvent(call_coordinator, entry),
        ]
    )


class VisitorEvent(CoordinatorEntity, EventEntity):
    """초기 아카이브는 건너뛰고 이후 새로 보관된 사진마다 이벤트를 만든다."""

    _attr_has_entity_name = True
    _attr_translation_key = "visitor"
    _attr_event_types = [EVENT_VISITOR_DETECTED]

    def __init__(self, coordinator: VisitorCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_visitor_event"
        self._seen_new_files: set[str] = set()
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}-visitor")},
            name=f"Commax 방문자 기록 ({entry.data[CONF_HOST]})",
            manufacturer="Commax",
            model="CDP-1020MB visitor archive",
        )

    def _handle_coordinator_update(self) -> None:
        data = self.coordinator.data
        if data is not None:
            for visitor in data.get("new_visitors", []):
                filename = visitor.get("filename")
                if filename and filename not in self._seen_new_files:
                    self._seen_new_files.add(filename)
                    self._trigger_event(
                        EVENT_VISITOR_DETECTED,
                        {
                            "captured_at": visitor.get("captured_at"),
                            "uid": visitor.get("uid"),
                            "flags": visitor.get("flags"),
                            "location": visitor.get("location"),
                            "filename": filename,
                            "archive_api": data.get("archive_api"),
                        },
                    )
        super()._handle_coordinator_update()


class VisitorCallEvent(CoordinatorEntity, EventEntity):
    """사진보다 먼저 벨 시작을, 종료 로그에서는 통화 종료를 발생시킨다."""

    _attr_has_entity_name = True
    _attr_translation_key = "visitor_call"
    _attr_event_types = [EVENT_VISITOR_RINGING, EVENT_VISITOR_ENDED]

    def __init__(
        self, coordinator: VisitorCallCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_visitor_call_event"
        self._last_event_id: int | None = None
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}-visitor")},
            name=f"Commax 방문자 기록 ({entry.data[CONF_HOST]})",
            manufacturer="Commax",
            model="CDP-1020MB visitor archive",
        )

    def _handle_coordinator_update(self) -> None:
        data = self.coordinator.data or {}
        event_id = data.get("last_event_id")
        event_type = data.get("last_event")
        if event_id is not None and event_id != self._last_event_id and event_type in {
            EVENT_VISITOR_RINGING,
            EVENT_VISITOR_ENDED,
        }:
            self._last_event_id = event_id
            event_data = {
                "location": data.get("location"),
                "place": data.get("place"),
                "started_at": data.get("started_at"),
            }
            if event_type == EVENT_VISITOR_ENDED:
                event_data.update(
                    {
                        "ended_at": data.get("ended_at"),
                        "duration_seconds": data.get("last_duration_seconds"),
                    }
                )
            self._trigger_event(event_type, event_data)
        super()._handle_coordinator_update()
