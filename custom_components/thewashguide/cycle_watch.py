"""The measured machine (WG-16a): wiring the cycle detector into the house.

The watcher subscribes to one power sensor (a metering plug on the machine, or
the machine's own integration), feeds every reading to the pure detector in
cycle.py, and when a cycle finishes it does two things in a fixed order: fires
`thewashguide_cycle_finished` on the local bus (free, works offline), then
posts the small summary to the cloud so the household's record can learn from
it. Individual power readings never leave the house; since 30 August the
summary carries the cycle's SHAPE alongside its totals, which is watt-hours in
five-minute steps, so a wash's phases can be read (and its energy attributed to
the half-hour tariff slots it actually ran in) without the curve itself ever
going anywhere.

A quiet plug stops sending state changes, so the end of a cycle cannot be
detected from samples alone: whenever the detector is timing a quiet spell,
the watcher arms a timer for the moment the spell would count as finished and
lets the detector's tick() make the call.

Uploads are best-effort with patience: a summary that cannot be posted right
now (no internet, the cloud mid-deploy) waits in a small queue and is retried
every fifteen minutes. The detector mints one cycle_id per cycle and the
endpoint upserts on it, so a retry can never double-count a wash.

The start is announced LATE, and deliberately (30 Aug, Nick's ruling). The
detector begins a run at the first reading above the start line, but it throws
away anything shorter than MIN_CYCLE_SECONDS, because a door light or a nudged
dial is not a wash. Announcing at the beginning therefore told the cloud the
drum was turning for things that never were washes, and since only a finishing
summary clears that state, the app could say "the drum has just started" for a
door light until its own six-hour guard gave up. So the announcement waits
until the run has outlasted the minimum and is certain to produce a summary. It
still carries the run's REAL start time, so nothing downstream is misled about
when the wash began; the app simply hears about it ten minutes in.

A restart used to be amnesia (30 Aug). The detector's state and the upload
queue now live in Home Assistant's own storage, so a wash in progress survives
an update and a summary that could not be posted is not destroyed by one. The
detector owns the RULES for resuming (cycle.py's restore, which decides between
finishing, resuming and standing down); this file owns the file.

Monitoring only, always: if the plug behind the sensor can switch, nothing in
this file or anywhere else in the integration will ever touch that switch.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

import aiohttp

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
)
from homeassistant.helpers.storage import Store

from .const import CYCLE_URL, EVENT_CYCLE_FINISHED
from .cycle import MIN_CYCLE_SECONDS, QUIET_SECONDS, CycleDetector, CycleSummary

_LOGGER = logging.getLogger(__name__)

# How long a failed upload waits before trying again, and how many summaries
# the queue will hold. Twenty is a week and a half of heavy laundry; an outage
# longer than that loses the oldest cycles, not the newest.
RETRY_SECONDS = 900
MAX_QUEUE = 20
# The timer fires a touch after the quiet spell completes, so the detector's
# own clock has unambiguously run out.
TIMER_MARGIN_SECONDS = 5

# Saving what the detector knows. Every state TRANSITION is written at once,
# because those are the moments worth not losing; between them a heartbeat
# bounds what a power cut can destroy. Two minutes sits comfortably inside the
# detector's own five-minute resume window, so a crash never leaves a state too
# stale to be taken back. A graceful restart, which is what a Home Assistant
# update actually is, writes on the way down and loses nothing at all.
STORAGE_VERSION = 1
SAVE_INTERVAL_SECONDS = 120.0


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


async def post_cycle(
    hass: HomeAssistant,
    api_key: str,
    payload: dict,
    what: str = "cycle summary",
) -> None:
    """One message up to the cloud; raises so the caller can queue it.

    [what] names the message in the log, because the two kinds fail for very
    different reasons and a person reading their log must not be told a
    summary was rejected when the record is perfectly intact. In particular a
    cloud endpoint older than v0.5.0 refuses every start note with a 400, and
    "cycle summary rejected" would be an alarming way to describe a household
    whose record is arriving exactly as it should.
    """
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
        # A 4xx is the endpoint saying "not with that": a blip the fences
        # caught, a malformed field, or a start note reaching an endpoint too
        # old to know the word. Retrying cannot change its mind, so it is
        # dropped with its reason on record.
        if 400 <= resp.status < 500:
            _LOGGER.warning("%s rejected (%s): %s", what, resp.status, detail)
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
        # Minted when the drum starts rather than when it stops (0.5.0), so the
        # start announcement and the finishing summary name the SAME cycle and
        # the endpoint can clear exactly the run it just ended.
        self._cycle_id: str | None = None
        # Whether the cloud has been told this run is happening. Persisted, so a
        # restart mid-wash neither announces a wash twice nor leaves one silent.
        self._announced = False
        self._unsub_announce = None
        # One file per watched entity, so two machines in one house cannot
        # overwrite each other's half-finished wash.
        safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in entity_id)
        self._store = Store(hass, STORAGE_VERSION, f"thewashguide.cycle.{safe}")
        self._unsub_stop = None
        self._last_save = 0.0

    async def async_start(self) -> None:
        """Take back what we knew, then start watching.

        The order matters: the detector must be restored BEFORE the first state
        change arrives, or the sample that would have resumed a wash starts a
        new one instead, which is the fragment this whole mechanism exists to
        prevent.
        """
        await self._async_restore()
        self._unsub_state = async_track_state_change_event(
            self._hass, [self._entity_id], self._on_state
        )
        # A wash restored mid-quiet needs its timer aimed again, or a cycle that
        # ended during the outage would sit unfinished until the plug next spoke.
        self._arm_quiet_timer()
        self._unsub_stop = self._hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP, self._on_hass_stop
        )
        _LOGGER.info("watching %s for washing-machine cycles", self._entity_id)

    @callback
    def stop(self) -> None:
        for unsub in (
            self._unsub_state, self._unsub_quiet, self._unsub_retry,
            self._unsub_stop, self._unsub_announce,
        ):
            if unsub:
                unsub()
        self._unsub_state = self._unsub_quiet = self._unsub_retry = None
        self._unsub_stop = self._unsub_announce = None

    async def _on_hass_stop(self, _event) -> None:
        """The graceful path, and the common one: an update is a clean stop."""
        await self._async_save(force=True)

    async def _async_restore(self) -> None:
        stored = await self._store.async_load()
        if not isinstance(stored, dict):
            return
        self._queue = [q for q in stored.get("queue", []) if isinstance(q, dict)]
        del self._queue[:-MAX_QUEUE]
        now = datetime.now(timezone.utc).timestamp()
        outcome = self._detector.restore(stored.get("detector"), now)
        if outcome != "fresh":
            # The id is carried back with the wash, so a summary posted after a
            # restart still clears the run its own start announcement opened.
            self._cycle_id = stored.get("cycle_id") or uuid.uuid4().hex
            self._announced = bool(stored.get("announced"))
            _LOGGER.info("cycle state after restart: %s", outcome)
            # A wash that outlasted the minimum while the lights were out has
            # earned its announcement and never got one, so it is made now.
            self._arm_announce_timer(now)
        if self._queue:
            _LOGGER.info("%d cycle summary(s) still waiting to upload", len(self._queue))
            self._hass.async_create_task(self._flush())

    async def _async_save(self, force: bool = False) -> None:
        now = datetime.now(timezone.utc).timestamp()
        if not force and now - self._last_save < SAVE_INTERVAL_SECONDS:
            return
        self._last_save = now
        await self._store.async_save(
            {
                "detector": self._detector.state(),
                "cycle_id": self._cycle_id,
                "announced": self._announced,
                "queue": self._queue,
            }
        )

    @callback
    def _save_soon(self, force: bool = False) -> None:
        self._hass.async_create_task(self._async_save(force=force))

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
        was_running = self._detector.running
        summary = self._detector.sample(ts, watts)
        # A sample can start a cycle or finish one, never both: finishing
        # resets the detector and leaves it stopped. So this transition is
        # unambiguous, and it is the only moment a run begins.
        started = not was_running and self._detector.running
        if started:
            self._cycle_id = uuid.uuid4().hex
            self._announced = False
            self._arm_announce_timer(ts)
        self._arm_quiet_timer()
        if summary:
            self._finish(summary)
        elif was_running and not self._detector.running:
            # A run the detector DISCARDED: too short to have washed anything,
            # so it produced no summary. The state on disk must still stop
            # claiming a wash is in progress. Found on the rig, not in a test.
            self._cycle_id = None
        if was_running and not self._detector.running:
            self._end_announcement_window()
        # A transition is worth writing at once; anything else waits for the
        # heartbeat, so a chatty plug does not write a file every thirty seconds.
        self._save_soon(force=was_running != self._detector.running)

    @callback
    def _arm_announce_timer(self, now: float) -> None:
        """Aim one timer at the moment this run becomes a wash.

        A timer rather than a check on each sample, because a plug that reports
        only on change can sit silent through a steady heating plateau, and the
        announcement should not wait on the machine happening to twitch.
        """
        if self._unsub_announce:
            self._unsub_announce()
            self._unsub_announce = None
        if self._announced or not self._detector.running:
            return
        if self._detector.confirmed_wash(now):
            self._hass.async_create_task(self._announce_start())
            return
        started = self._detector.started_ts
        if started is None:
            return
        delay = max(1.0, started + MIN_CYCLE_SECONDS - now)
        self._unsub_announce = async_call_later(
            self._hass, delay, self._on_announce_timer
        )

    @callback
    def _on_announce_timer(self, _now) -> None:
        self._unsub_announce = None
        now = datetime.now(timezone.utc).timestamp()
        if self._detector.confirmed_wash(now):
            self._hass.async_create_task(self._announce_start())
        elif self._detector.running:
            # Still going but not yet a wash: it fell quiet early and is timing
            # a silence that will end in the bin. Nothing to say, and nothing
            # to wait for either, since that run can no longer become one.
            _LOGGER.debug("run on %s too short to announce", self._entity_id)

    @callback
    def _end_announcement_window(self) -> None:
        self._announced = False
        if self._unsub_announce:
            self._unsub_announce()
            self._unsub_announce = None

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
        was_running = self._detector.running
        summary = self._detector.tick(datetime.now(timezone.utc).timestamp())
        if summary:
            self._finish(summary)
        if was_running and not self._detector.running:
            # The run ended here, WITH a summary or without one: a spell that
            # outlasts the finish window closes the cycle either way, and one
            # too short to be a wash is discarded and produces nothing. The
            # saved state has to stop claiming a wash in progress in both
            # cases, or a restart would restore a wash that is over.
            self._cycle_id = None
            self._end_announcement_window()
            self._save_soon(force=True)
        else:
            # Still quiet but not long enough (clock drift), or the spell was
            # broken by a sample that re-armed the timer already.
            self._arm_quiet_timer()

    async def _announce_start(self) -> None:
        """Tell the cloud the drum is turning, so the app can say so.

        Called once the run has outlasted MIN_CYCLE_SECONDS, never at its first
        reading, so nothing that is not a wash is ever announced. The time it
        carries is still the run's REAL start, so the app is told a wash has
        been going ten minutes rather than that one has just begun.

        Deliberately fire-and-forget. This fact is ephemeral and worth nothing
        late, so it is never queued for retry the way a summary is: a failure
        costs one wash's live line and the summary that follows is untouched,
        which is the half that carries the record. An endpoint too old to know
        the word 'started' answers 400, which post_cycle logs and drops.
        """
        cycle_id = self._cycle_id
        started_ts = self._detector.started_ts
        if not cycle_id or started_ts is None or self._announced:
            return
        self._announced = True
        _LOGGER.info("cycle confirmed on %s, announcing", self._entity_id)
        try:
            await post_cycle(
                self._hass,
                self._api_key,
                {
                    "event": "started",
                    "cycle_id": cycle_id,
                    "started_at": _iso(started_ts),
                    "source_entity": self._entity_id,
                },
                what="cycle start announcement",
            )
        except Exception as err:  # noqa: BLE001 - never let this reach the wash
            _LOGGER.debug("cycle start announcement failed: %s", err)

    @callback
    def _finish(self, summary: CycleSummary) -> None:
        """Claim the finished run's id SYNCHRONOUSLY, then report it.

        _report is a task, so reading the id inside it would let a wash started
        in the meantime hand its own id to the previous wash's summary, which
        would clear the wrong run and mis-key the record. Taking it here, in the
        callback that saw the cycle end, closes that window entirely.
        """
        cycle_id = self._cycle_id or uuid.uuid4().hex
        self._cycle_id = None
        self._hass.async_create_task(self._report(summary, cycle_id))

    async def _report(self, summary: CycleSummary, cycle_id: str) -> None:
        payload = {
            "cycle_id": cycle_id,
            "started_at": _iso(summary.started_ts),
            "ended_at": _iso(summary.ended_ts),
            "energy_kwh": summary.energy_kwh,
            "peak_watts": summary.peak_watts,
            "average_watts": summary.average_watts,
            # The shape (30 Aug): watt-hours in wall-clock-aligned buckets. The
            # start is carried rather than derived from started_at, so nobody
            # downstream has to reproduce this rounding across time zones in
            # two languages, and the width is a field so a later change of
            # resolution is a value rather than a migration.
            "shape_wh": list(summary.shape_wh),
            "shape_start": _iso(summary.shape_start_ts),
            "shape_bucket_seconds": int(summary.shape_bucket_seconds),
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
                # The local half gets the shape too. It never leaves the house
                # on this path, and an automation that wants to draw the curve
                # should not have to ask the cloud for what the plug measured
                # in its own kitchen.
                "shape_wh": list(summary.shape_wh),
                "shape_start": _iso(summary.shape_start_ts),
                "shape_bucket_seconds": int(summary.shape_bucket_seconds),
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
        # The queue holds finished washes that have not reached the cloud yet.
        # Losing one to a restart would lose a real cycle, which is worse than
        # losing a partial one, so it is written down whenever it moves.
        await self._async_save(force=True)
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
