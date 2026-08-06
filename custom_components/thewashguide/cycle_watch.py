"""The measured machine (WG-16a): wiring the cycle detector into the house.

The watcher subscribes to one power sensor (a metering plug on the machine, or
the machine's own integration), feeds every reading to the pure detector in
cycle.py, and when a cycle finishes it does two things in a fixed order: fires
`thewashguide_cycle_finished` on the local bus (free, works offline), then
posts the small summary to the cloud so the household's record can learn from
it. Raw power readings never leave the house; the summary is start, end,
kilowatt-hours and two watt figures, nothing else.

A quiet plug stops sending state changes, so the end of a cycle cannot be
detected from samples alone: whenever the detector is timing a quiet spell,
the watcher arms a timer for the moment the spell would count as finished and
lets the detector's tick() make the call.

Uploads are best-effort with patience: a summary that cannot be posted right
now (no internet, the cloud mid-deploy) waits in a small queue and is retried
every fifteen minutes. The detector mints one cycle_id per cycle and the
endpoint upserts on it, so a retry can never double-count a wash.

Monitoring only, always: if the plug behind the sensor can switch, nothing in
this file or anywhere else in the integration will ever touch that switch.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

import aiohttp

from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
)

from .const import CYCLE_URL, EVENT_CYCLE_FINISHED
from .cycle import QUIET_SECONDS, CycleDetector, CycleSummary

_LOGGER = logging.getLogger(__name__)

# How long a failed upload waits before trying again, and how many summaries
# the queue will hold. Twenty is a week and a half of heavy laundry; an outage
# longer than that loses the oldest cycles, not the newest.
RETRY_SECONDS = 900
MAX_QUEUE = 20
# The timer fires a touch after the quiet spell completes, so the detector's
# own clock has unambiguously run out.
TIMER_MARGIN_SECONDS = 5


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


async def post_cycle(hass: HomeAssistant, api_key: str, payload: dict) -> None:
    """One cycle summary up to the cloud; raises so the caller can queue it."""
    session = async_get_clientsession(hass)
    async with session.post(
        CYCLE_URL,
        headers={"X-Api-Key": api_key, "Content-Type": "application/json"},
        json=payload,
        timeout=aiohttp.ClientTimeout(total=15),
    ) as resp:
        if resp.status < 300:
            return
        detail = (await resp.text()).strip()
        # A 4xx is the endpoint saying "not with that summary": a blip the
        # fences caught, a malformed field. Retrying cannot change its mind,
        # so the summary is dropped with its reason on record.
        if 400 <= resp.status < 500:
            _LOGGER.warning("cycle summary rejected (%s): %s", resp.status, detail)
            return
        raise aiohttp.ClientResponseError(
            resp.request_info, resp.history, status=resp.status, message=detail
        )


class CycleWatcher:
    """Watches one power entity and reports finished cycles."""

    def __init__(self, hass: HomeAssistant, api_key: str, entity_id: str) -> None:
        self._hass = hass
        self._api_key = api_key
        self._entity_id = entity_id
        self._detector = CycleDetector()
        self._unsub_state = None
        self._unsub_quiet = None
        self._unsub_retry = None
        self._queue: list[dict] = []

    def start(self) -> None:
        self._unsub_state = async_track_state_change_event(
            self._hass, [self._entity_id], self._on_state
        )
        _LOGGER.info("watching %s for washing-machine cycles", self._entity_id)

    @callback
    def stop(self) -> None:
        for unsub in (self._unsub_state, self._unsub_quiet, self._unsub_retry):
            if unsub:
                unsub()
        self._unsub_state = self._unsub_quiet = self._unsub_retry = None

    @callback
    def _on_state(self, event: Event) -> None:
        new_state = event.data.get("new_state")
        if new_state is None or new_state.state in ("unknown", "unavailable"):
            return
        try:
            watts = float(new_state.state)
        except ValueError:
            return
        ts = new_state.last_updated.timestamp()
        summary = self._detector.sample(ts, watts)
        self._arm_quiet_timer()
        if summary:
            self._hass.async_create_task(self._report(summary))

    def _arm_quiet_timer(self) -> None:
        """Keep one timer aimed at the end of the current quiet spell."""
        if self._unsub_quiet:
            self._unsub_quiet()
            self._unsub_quiet = None
        quiet_since = self._detector.quiet_since
        if quiet_since is None:
            return
        now = datetime.now(timezone.utc).timestamp()
        delay = max(1.0, quiet_since + QUIET_SECONDS + TIMER_MARGIN_SECONDS - now)
        self._unsub_quiet = async_call_later(self._hass, delay, self._on_quiet_timer)

    @callback
    def _on_quiet_timer(self, _now) -> None:
        self._unsub_quiet = None
        summary = self._detector.tick(datetime.now(timezone.utc).timestamp())
        if summary:
            self._hass.async_create_task(self._report(summary))
        else:
            # Still quiet but not long enough (clock drift), or the spell was
            # broken by a sample that re-armed the timer already.
            self._arm_quiet_timer()

    async def _report(self, summary: CycleSummary) -> None:
        payload = {
            "cycle_id": uuid.uuid4().hex,
            "started_at": _iso(summary.started_ts),
            "ended_at": _iso(summary.ended_ts),
            "energy_kwh": summary.energy_kwh,
            "peak_watts": summary.peak_watts,
            "average_watts": summary.average_watts,
            "source": "plug",
            "source_entity": self._entity_id,
            "schema_version": 1,
        }
        _LOGGER.info(
            "cycle finished on %s: %d minutes, %.2f kWh",
            self._entity_id,
            round(summary.duration_seconds / 60),
            summary.energy_kwh,
        )
        # The local event first: it owes nothing to the cloud.
        self._hass.bus.async_fire(
            EVENT_CYCLE_FINISHED,
            {
                "entity_id": self._entity_id,
                "started_at": payload["started_at"],
                "ended_at": payload["ended_at"],
                "duration_seconds": round(summary.duration_seconds),
                "energy_kwh": summary.energy_kwh,
                "peak_watts": summary.peak_watts,
                "average_watts": summary.average_watts,
            },
        )
        self._queue.append(payload)
        del self._queue[:-MAX_QUEUE]
        await self._flush()

    async def _flush(self) -> None:
        remaining: list[dict] = []
        for payload in self._queue:
            try:
                await post_cycle(self._hass, self._api_key, payload)
                _LOGGER.info("cycle summary %s stored", payload["cycle_id"])
            except Exception as err:  # noqa: BLE001 - queued for retry
                _LOGGER.debug("cycle upload failed, will retry: %s", err)
                remaining.append(payload)
        self._queue = remaining
        if self._unsub_retry:
            self._unsub_retry()
            self._unsub_retry = None
        if self._queue:
            self._unsub_retry = async_call_later(
                self._hass, RETRY_SECONDS, self._on_retry_timer
            )

    @callback
    def _on_retry_timer(self, _now) -> None:
        self._unsub_retry = None
        self._hass.async_create_task(self._flush())
