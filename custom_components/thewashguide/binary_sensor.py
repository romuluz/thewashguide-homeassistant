"""The problem sensors: a wash the house has stopped doing, and a machine the
house has stopped looking after.

The overdue-wash sensor is the app's nudge ladder bridged into the physical
house; the automations write themselves (the laundry-room light that slowly
turns amber, the speaker that reports the socks are organising). The two
machine sensors are the drum's own state, and they are the ones that will
still be true in five years: a machine gets cleaned when something remembers
to ask.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import WashGuideCoordinator
from .const import DOMAIN, OVERDUE_AFTER_HOURS


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: WashGuideCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            OverdueWashBinarySensor(coordinator, entry),
            MaintenanceDueBinarySensor(coordinator, entry),
            FilterDueBinarySensor(coordinator, entry),
        ]
    )


class WashGuideProblem(
    CoordinatorEntity[WashGuideCoordinator], BinarySensorEntity
):
    """Base: one Wash Guide device, one problem-class sensor."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: WashGuideCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        household = coordinator.data.get("household") or {}
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=household.get("name") or "The Wash Guide",
            manufacturer="The Wash Guide",
            model=household.get("label") or "Household",
        )


class OverdueWashBinarySensor(WashGuideProblem):
    """On when a pending task is overdue: nudged by the server, or simply
    older than the first nudge rung."""

    _attr_name = "Wash overdue"
    _attr_icon = "mdi:basket-unfill"

    def __init__(self, coordinator: WashGuideCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_wash_overdue"

    def _overdue(self) -> list[dict[str, Any]]:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=OVERDUE_AFTER_HOURS)
        out: list[dict[str, Any]] = []
        for task in self.coordinator.data.get("tasks", []):
            if task.get("status") != "pending":
                continue
            nudged = (task.get("nudge_level") or 0) >= 1
            aged = False
            created = task.get("created_at")
            if created:
                try:
                    aged = datetime.fromisoformat(
                        str(created).replace("Z", "+00:00")
                    ) < cutoff
                except ValueError:
                    aged = False
            if nudged or aged:
                out.append(task)
        return out

    @property
    def is_on(self) -> bool:
        return len(self._overdue()) > 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        overdue = self._overdue()
        return {
            "count": len(overdue),
            "tasks": overdue,
        }


class MaintenanceDueBinarySensor(WashGuideProblem):
    """On when the drum has earned its empty hot cycle: thirty loads, or two
    months, whichever arrives first. The app decides, not this sensor, so the
    house and the phone never disagree about what due means."""

    _attr_name = "Maintenance wash due"
    _attr_icon = "mdi:washing-machine-alert"

    def __init__(self, coordinator: WashGuideCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_maintenance_due"

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.machine.get("clean_due"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        machine = self.coordinator.machine
        pending = self.coordinator.pending_machine_task()
        return {
            "loads_since_clean": machine.get("loads_since_clean"),
            "days_since_clean": machine.get("days_since_clean"),
            "last_clean": machine.get("last_clean"),
            # Already asked for: the panel should offer to run it, not to ask again.
            "on_the_board": pending is not None,
            "task_id": pending.get("id") if pending else None,
        }


class FilterDueBinarySensor(WashGuideProblem):
    """On when the pump filter is due (a quarterly job, time-based only: it
    fills with lint and coins rather than loads)."""

    _attr_name = "Filter due"
    _attr_icon = "mdi:air-filter"

    def __init__(self, coordinator: WashGuideCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_filter_due"

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.machine.get("filter_due"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        machine = self.coordinator.machine
        return {
            "last_filter_clean": machine.get("last_filter"),
            "days_since_filter": machine.get("days_since_filter"),
        }
