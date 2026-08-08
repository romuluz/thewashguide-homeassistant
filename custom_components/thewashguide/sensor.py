"""Sensors for The Wash Guide: the household at a glance."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import WashGuideCoordinator
from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create the household sensors, plus one personality sensor per member."""
    coordinator: WashGuideCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = [
        PendingTasksSensor(coordinator, entry),
        DetergentShieldSensor(coordinator, entry),
        LastWashSensor(coordinator, entry),
        NextWashSensor(coordinator, entry),
        MachineLoadsSensor(coordinator, entry),
        LastMachineCleanSensor(coordinator, entry),
    ]
    for member in coordinator.data.get("members", []):
        entities.append(MemberProfileSensor(coordinator, entry, member["name"]))
    async_add_entities(entities)


class WashGuideEntity(CoordinatorEntity[WashGuideCoordinator], SensorEntity):
    """Base: one Wash Guide device grouping every sensor."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: WashGuideCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        # The device is the integration, so it carries the app's name; the
        # household's own name stays on the config entry title.
        household = (coordinator.data.get("household") or {})
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="The Wash Guide",
            manufacturer="The Wash Guide",
            model=household.get("label") or "Household",
        )


def _pretty(slug: str) -> str:
    """Catalogue slugs become readable names: ariel_allin1_pods -> Ariel Allin1 Pods."""
    return slug.replace("_", " ").replace("-", " ").title()


class PendingTasksSensor(WashGuideEntity):
    """How many washes are waiting on the board, with the list as attributes
    so template sensors can slice per person."""

    _attr_icon = "mdi:clipboard-list-outline"
    _attr_name = "Pending tasks"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: WashGuideCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_pending_tasks"

    @property
    def native_value(self) -> int | None:
        household = self.coordinator.data.get("household") or {}
        return household.get("pending_tasks")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        pending = [
            t for t in self.coordinator.data.get("tasks", [])
            if t.get("status") == "pending"
        ]
        return {"tasks": pending}


class DetergentShieldSensor(WashGuideEntity):
    """The weakest item in the shared cupboard as a percentage, with the full
    cupboard as attributes for shopping-list automations."""

    _attr_icon = "mdi:shield-check-outline"
    _attr_name = "Detergent shield"
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: WashGuideCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_detergent_shield"

    @property
    def native_value(self) -> int | None:
        household = self.coordinator.data.get("household") or {}
        return household.get("shield_pct")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        cupboard = [
            {
                "name": _pretty(item.get("id", "")),
                "remaining": item.get("remaining"),
                "pack_size": item.get("pack_size"),
                "pct": item.get("pct"),
            }
            for item in self.coordinator.data.get("cupboard", [])
        ]
        return {
            "cupboard": cupboard,
            "lowest": cupboard[0]["name"] if cupboard else None,
        }


class NextWashSensor(WashGuideEntity):
    """The next standing wash on the household's schedule: the feed's most
    automatable fact, because automations get to act BEFORE it."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:calendar-clock"
    _attr_name = "Next scheduled wash"

    def __init__(self, coordinator: WashGuideCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_next_wash"

    def _next(self) -> dict[str, Any] | None:
        schedules = self.coordinator.data.get("schedules") or []
        return schedules[0] if schedules else None

    @property
    def native_value(self) -> datetime | None:
        nxt = self._next()
        if not nxt or not nxt.get("next_run_at"):
            return None
        return datetime.fromisoformat(str(nxt["next_run_at"]).replace("Z", "+00:00"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        nxt = self._next() or {}
        return {
            "summary": nxt.get("summary"),
            "for": nxt.get("for"),
            "interval_weeks": nxt.get("interval_weeks"),
            "tokens": nxt.get("tokens"),
            "schedules": self.coordinator.data.get("schedules") or [],
        }


class LastWashSensor(WashGuideEntity):
    """When the household last ran a wash, with the recipe as attributes."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:washing-machine"
    _attr_name = "Last wash"

    def __init__(self, coordinator: WashGuideCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_last_wash"

    @property
    def native_value(self) -> datetime | None:
        wash = self.coordinator.data.get("last_wash")
        if not wash or not wash.get("ts"):
            return None
        return datetime.fromisoformat(wash["ts"].replace("Z", "+00:00"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self.coordinator.data.get("last_wash") or {}


class MachineLoadsSensor(WashGuideEntity):
    """Loads run since the last maintenance wash: the number the app leads with,
    and the one an automation can act on before the drum starts to smell."""

    _attr_icon = "mdi:washing-machine"
    _attr_name = "Loads since machine clean"
    _attr_native_unit_of_measurement = "loads"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: WashGuideCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_machine_loads"

    @property
    def native_value(self) -> int | None:
        return self.coordinator.machine.get("loads_since_clean")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        machine = self.coordinator.machine
        return {
            "clean_due": machine.get("clean_due"),
            "days_since_clean": machine.get("days_since_clean"),
            "interval_loads": machine.get("wash_interval_loads"),
        }


class LastMachineCleanSensor(WashGuideEntity):
    """When the machine was last cleaned, with the household's care record (who
    did what, and when) as attributes."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:calendar-check-outline"
    _attr_name = "Last machine clean"

    def __init__(self, coordinator: WashGuideCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_machine_last_clean"

    @property
    def native_value(self) -> datetime | None:
        last = self.coordinator.machine.get("last_clean")
        if not last:
            return None
        return datetime.fromisoformat(str(last).replace("Z", "+00:00"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        machine = self.coordinator.machine
        return {
            "by": machine.get("last_clean_by"),
            "last_filter_clean": machine.get("last_filter"),
            "days_since_filter": machine.get("days_since_filter"),
            "record": machine.get("record") or [],
        }


class MemberProfileSensor(WashGuideEntity):
    """A member's wash personality, with tier and monthly count attached."""

    _attr_icon = "mdi:account-star-outline"

    def __init__(
        self, coordinator: WashGuideCoordinator, entry: ConfigEntry, member_name: str
    ) -> None:
        super().__init__(coordinator, entry)
        self._member_name = member_name
        self._attr_name = f"{member_name} personality"
        slug = member_name.lower().replace(" ", "_")
        self._attr_unique_id = f"{entry.entry_id}_profile_{slug}"

    def _member(self) -> dict[str, Any] | None:
        for member in self.coordinator.data.get("members", []):
            if member.get("name") == self._member_name:
                return member
        return None

    @property
    def native_value(self) -> str | None:
        member = self._member()
        return member.get("profile") if member else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        member = self._member() or {}
        household = self.coordinator.data.get("household") or {}
        return {
            "tier": member.get("tier"),
            "washes_30d": member.get("washes_30d"),
            "rewards_30d": member.get("rewards_30d"),
            "reward_name": household.get("reward_name"),
        }
