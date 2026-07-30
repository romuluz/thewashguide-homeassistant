"""The Wash Guide: laundry state and events for your smart home.

Polls The Wash Guide cloud with the connect key generated in the app, and
exposes the household as native entities: pending tasks, the detergent
shield, the latest wash, the machine's own care, and each member's wash
personality. Fires bus events when a new wash lands, a task moves, or the
machine gets cleaned, for automations.

With a PRO control key it also acts: posting tasks to the household board,
asking the house for a maintenance wash, and recording the clean when the
cycle finishes.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_API_KEY,
    CONF_CONTROL_KEY,
    CONF_UPDATE_INTERVAL,
    CONTROL_URL,
    DEFAULT_UPDATE_INTERVAL_SECONDS,
    DOMAIN,
    EVENT_MACHINE_CLEANED,
    EVENT_TASK_COMPLETED,
    EVENT_TASK_CREATED,
    EVENT_WASH_LOGGED,
    FEED_URL,
    SERVICE_CANCEL_TASK,
    SERVICE_CREATE_TASK,
    SERVICE_LOG_MACHINE_CLEAN,
    SERVICE_REQUEST_MAINTENANCE,
)

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["sensor", "binary_sensor"]

CREATE_TASK_SCHEMA = vol.Schema(
    {
        vol.Optional("summary"): cv.string,
        vol.Optional("note"): cv.string,
        vol.Optional("item_type"): cv.string,
        vol.Optional("shade"): cv.string,
        vol.Optional("fabric"): cv.string,
        vol.Optional("load_size"): cv.string,
        vol.Optional("tokens"): vol.All(vol.Coerce(int), vol.Range(min=0, max=99)),
    }
)
CANCEL_TASK_SCHEMA = vol.Schema({vol.Required("task_id"): cv.string})
REQUEST_MAINTENANCE_SCHEMA = vol.Schema(
    {vol.Optional("assignee_name"): cv.string, vol.Optional("note"): cv.string}
)
LOG_CLEAN_SCHEMA = vol.Schema(
    {
        vol.Optional("kind", default="maintenance_wash"): vol.In(
            ["maintenance_wash", "filter_clean"]
        ),
        vol.Optional("by"): cv.string,
    }
)


async def fetch_feed(hass: HomeAssistant, api_key: str) -> dict:
    """One poll of the cloud feed; raises on auth failure."""
    session = async_get_clientsession(hass)
    async with session.get(
        FEED_URL,
        headers={"X-Api-Key": api_key},
        timeout=aiohttp.ClientTimeout(total=15),
    ) as resp:
        if resp.status == 401:
            raise ConfigEntryAuthFailed("connect key rejected")
        resp.raise_for_status()
        return await resp.json()


async def call_control(hass: HomeAssistant, control_key: str, payload: dict) -> dict:
    """One write-action against the control endpoint.

    The endpoint answers refusals in plain English (a junior may not create
    tasks; only a maintenance task may be completed from the smart home), so
    the body is carried straight into the error the user sees rather than
    being flattened into a status code.
    """
    session = async_get_clientsession(hass)
    async with session.post(
        CONTROL_URL,
        headers={"X-Api-Key": control_key, "Content-Type": "application/json"},
        json=payload,
        timeout=aiohttp.ClientTimeout(total=15),
    ) as resp:
        if resp.status < 300:
            return await resp.json()
        detail = (await resp.text()).strip()
        raise HomeAssistantError(f"The Wash Guide refused that: {detail}")


class WashGuideCoordinator(DataUpdateCoordinator[dict]):
    """Polls the feed and fires bus events on fresh activity."""

    def __init__(
        self,
        hass: HomeAssistant,
        api_key: str,
        control_key: str | None,
        requested_minutes: int | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_UPDATE_INTERVAL_SECONDS),
        )
        self._api_key = api_key
        self.control_key = control_key
        # The user's chosen cadence, if any. The feed's declared floor wins
        # whenever it is slower; see _pace_from_feed.
        self._requested_seconds = requested_minutes * 60 if requested_minutes else None
        self._last_wash_ts: str | None = None
        self._last_care_ts: str | None = None
        self._seen_completions: set[str] = set()
        self._seen_pending: set[str] = set()

    @property
    def machine(self) -> dict[str, Any]:
        """The drum the household shares, as the feed describes it."""
        return self.data.get("machine") or {}

    def pending_machine_task(self) -> dict[str, Any] | None:
        """The maintenance wash waiting on the board, if the house asked for one.

        A panel that logs a finished clean should close that task too, or the
        board goes on asking for a job the house can see has been done.
        """
        for task in self.data.get("tasks", []):
            if task.get("status") == "pending" and task.get("kind") == "machine":
                return task
        return None

    def _pace_from_feed(self, data: dict) -> None:
        """Match the poll cadence to the floor the feed declares.

        The feed says how often this key may poll (min_poll_seconds: a minute
        for a PRO household, fifteen for free). The user may choose slower in
        the options, never faster: the floor is the plan's, not ours. A
        household that upgrades or lapses changes cadence on its next poll
        with nothing to re-configure. A feed without the field (an older
        deploy) changes nothing.
        """
        floor = data.get("min_poll_seconds")
        if not isinstance(floor, int) or floor <= 0:
            return
        seconds = max(self._requested_seconds, floor) if self._requested_seconds else floor
        if self.update_interval != timedelta(seconds=seconds):
            _LOGGER.info("The Wash Guide will poll every %d seconds", seconds)
            self.update_interval = timedelta(seconds=seconds)

    async def _async_update_data(self) -> dict:
        try:
            data = await fetch_feed(self.hass, self._api_key)
        except ConfigEntryAuthFailed:
            raise
        except Exception as err:  # noqa: BLE001 - surfaced as UpdateFailed
            raise UpdateFailed(f"feed unavailable: {err}") from err

        self._pace_from_feed(data)

        # A new wash since the last poll becomes an automation trigger.
        wash = data.get("last_wash")
        if wash and wash.get("ts") and wash["ts"] != self._last_wash_ts:
            if self._last_wash_ts is not None:
                self.hass.bus.async_fire(EVENT_WASH_LOGGED, wash)
            self._last_wash_ts = wash["ts"]

        # An act of machine care likewise, whichever door it came through: the
        # app, another member's phone, or a wall panel with the control key.
        record = (data.get("machine") or {}).get("record") or []
        if record and record[0].get("ts") != self._last_care_ts:
            if self._last_care_ts is not None:
                self.hass.bus.async_fire(EVENT_MACHINE_CLEANED, record[0])
            self._last_care_ts = record[0].get("ts")

        # Newly completed tasks likewise (keyed by completion time + summary),
        # and newly created ones, so speakers and displays can announce an
        # assignment the moment it lands.
        first_poll = not self._seen_completions and not self._seen_pending
        for task in data.get("tasks", []):
            if task.get("status") == "completed" and task.get("completed_at"):
                marker = f"{task['completed_at']}|{task.get('summary')}"
                if marker not in self._seen_completions:
                    if not first_poll:
                        self.hass.bus.async_fire(EVENT_TASK_COMPLETED, task)
                    self._seen_completions.add(marker)
            if task.get("status") == "pending" and task.get("created_at"):
                marker = f"{task['created_at']}|{task.get('summary')}"
                if marker not in self._seen_pending:
                    if not first_poll:
                        self.hass.bus.async_fire(EVENT_TASK_CREATED, task)
                    self._seen_pending.add(marker)

        return data


def _register_services(hass: HomeAssistant) -> None:
    """Register the control services once, for whichever entry has a key."""

    def _coordinator_with_key() -> WashGuideCoordinator:
        for coordinator in hass.data.get(DOMAIN, {}).values():
            if coordinator.control_key:
                return coordinator
        raise HomeAssistantError(
            "No control key. Generate one in The Wash Guide app "
            "(Settings, Smart home, Generate control key; it needs PRO), then "
            "add it to the integration with Configure."
        )

    async def _act(payload: dict) -> dict:
        coordinator = _coordinator_with_key()
        result = await call_control(hass, coordinator.control_key, payload)
        # Act, then look: the entities should not wait a minute to catch up
        # with something this house just did on purpose.
        await coordinator.async_request_refresh()
        return result

    async def create_task(call: ServiceCall) -> None:
        await _act({"action": "create_open_task", **call.data})

    async def cancel_task(call: ServiceCall) -> None:
        await _act({"action": "cancel_task", "task_id": call.data["task_id"]})

    async def request_maintenance(call: ServiceCall) -> None:
        await _act({"action": "create_machine_task", **call.data})

    async def log_machine_clean(call: ServiceCall) -> None:
        coordinator = _coordinator_with_key()
        kind = call.data.get("kind", "maintenance_wash")
        payload: dict[str, Any] = {"action": "log_machine_clean", **call.data}
        # If the house has the maintenance wash on the board, a finished clean
        # completes that task rather than logging a second, parallel truth.
        if kind == "maintenance_wash":
            task = coordinator.pending_machine_task()
            if task:
                payload = {
                    "action": "complete_machine_task",
                    "task_id": task["id"],
                }
                if "by" in call.data:
                    payload["by"] = call.data["by"]
        await _act(payload)

    hass.services.async_register(
        DOMAIN, SERVICE_CREATE_TASK, create_task, schema=CREATE_TASK_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_CANCEL_TASK, cancel_task, schema=CANCEL_TASK_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_REQUEST_MAINTENANCE,
        request_maintenance,
        schema=REQUEST_MAINTENANCE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_LOG_MACHINE_CLEAN, log_machine_clean, schema=LOG_CLEAN_SCHEMA
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up The Wash Guide from a config entry."""
    # The control key may arrive later, through Configure, without re-pasting
    # the read key; options win when they are set.
    control_key = entry.options.get(
        CONF_CONTROL_KEY, entry.data.get(CONF_CONTROL_KEY)
    )
    requested_minutes = entry.options.get(
        CONF_UPDATE_INTERVAL, entry.data.get(CONF_UPDATE_INTERVAL)
    )
    coordinator = WashGuideCoordinator(
        hass,
        entry.data[CONF_API_KEY],
        control_key or None,
        requested_minutes or None,
    )
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # The services exist whether or not a key is present; without one they say
    # exactly what is missing, which is friendlier than not appearing at all.
    if not hass.services.has_service(DOMAIN, SERVICE_CREATE_TASK):
        _register_services(hass)

    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """A control key added or changed in Configure takes effect immediately."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        hass.data[DOMAIN].pop(entry.entry_id)
        if not hass.data[DOMAIN]:
            for service in (
                SERVICE_CREATE_TASK,
                SERVICE_CANCEL_TASK,
                SERVICE_REQUEST_MAINTENANCE,
                SERVICE_LOG_MACHINE_CLEAN,
            ):
                hass.services.async_remove(DOMAIN, service)
    return ok
