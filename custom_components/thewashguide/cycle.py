"""Cycle detection from a power curve, pure and testable.

A washing machine seen through a metering plug is a simple story: watts rise
when the cycle starts, wander through heating spikes, tumbling and pauses, and
fall to standby when it finishes. The detector reads that story with three
rules:

- The cycle STARTS at the first sample at or above START_WATTS.
- The cycle is CANDIDATE-FINISHED when power falls below QUIET_WATTS; it is
  actually finished only when the quiet holds for QUIET_SECONDS unbroken. Any
  rise above the quiet line cancels the candidate, which is what forgives
  soaks, mid-cycle pauses and anti-crease tumbling. The recorded end is when
  the quiet BEGAN, not when we became sure of it, so the sureness window never
  pads the duration.
- A cycle shorter than MIN_CYCLE_SECONDS is discarded whole: a door light, a
  spin-only blip, someone nudging the dial. The record only ever learns from
  things long enough to have washed something.

Energy is integrated from the curve itself (trapezoid between samples), which
is the honest kWh a metering plug can support; peak and average watts ride
along, cheap to keep and enough to tell a Quick 40 from a Cotton 40 later.

Since 30 August the same integration also fills BUCKETS: watt-hours in
five-minute steps aligned to the wall clock, which are the cycle's shape. Two
things fall out of one small change. Six buckets are exactly a half-hour tariff
slot, so a cycle can be priced against the grid it actually ran on instead of
against a typed average; and the shape of a programme (the heating plateau, the
wash phase, the spin) becomes readable, which is what lets the record eventually
tell a Quick 40 from a Cotton 40 rather than guessing from the temperature.
Alignment to the WALL CLOCK rather than to the cycle's own start is the
load-bearing part: buckets anchored to the start would need an assumption about
the curve inside them, and would price the same wash differently depending on
the minute it began. The design is docs/2026-08-30_THE_SHAPE_AND_THE_PROGRAMME.md.

The same release closes the MERGE: two washes run back to back inside the quiet
window used to be recorded as one impossible cycle with the durations and the
energies summed. Shortening the quiet window was considered and rejected long
ago, because a soak would then split one wash into two plausible halves, and a
wrong split is worse than a merge exactly because it looks reasonable. The
discriminator is that a second wash HEATS AGAIN: a soak resumes at wash-phase
power, a hundred to three hundred watts, while a new wash climbs back to the
heating plateau near two thousand. That gap is wide enough to be a fixed
threshold rather than a learned one. Two COLD washes back to back still merge,
because neither heats and there is nothing to see; the app's own mergeSuspected
remains the backstop for that.

Everything here is deliberately free of Home Assistant imports so it can be
unit-tested with plain Python; cycle_watch.py owns the wiring (state changes
in, timers, upload out).
"""

from __future__ import annotations

from dataclasses import dataclass

# Standby on a modern machine is a watt or three; a running cycle rarely dips
# under ten outside a genuine pause. The quiet line sits below the start line
# on purpose (hysteresis), and QUIET_SECONDS is long enough that a soak with
# the drum still has to stay silent for ten straight minutes to count as done.
START_WATTS = 10.0
QUIET_WATTS = 5.0
QUIET_SECONDS = 600.0
MIN_CYCLE_SECONDS = 600.0

# The shape's resolution. Five minutes is not a compromise between detail and
# size, it is the largest bucket that still divides a half-hour tariff slot
# exactly (six of them) while staying short enough that a heating plateau,
# which runs ten to forty minutes, is several buckets rather than one.
BUCKET_SECONDS = 300.0

# A day of buckets. A domestic cycle cannot approach this and the six-hour stale
# guard upstream would have given up long before, so it is a bound on a
# malfunctioning plug rather than on a wash.
MAX_BUCKETS = 288

# The merge split. A quiet run this long is already too long for tumbling and
# too short for the finish, so it is a candidate boundary; it only becomes one
# if the machine then draws RESTART_WATTS, which only a heater does. Both
# figures sit in the wide gap between wash-phase power (100-300 W) and a heating
# element (1800-2400 W), so neither needs to know anything about the machine.
SPLIT_QUIET_SECONDS = 240.0
RESTART_WATTS = 1200.0

# How much unwatched time a resumed cycle may bridge (30 Aug). The detector
# holds its state in memory, so a Home Assistant restart mid-wash is amnesia,
# and the state can now be written down and read back. What it cannot do is
# know what the machine did while nobody was looking, so the gap it will bridge
# is bounded: at the heating plateau five minutes is about 170 Wh, which is a
# sixth of a cycle and the most an estimate should ever be asked to carry.
# Past it the partial wash is abandoned rather than guessed at, and see
# [CycleDetector.restore] for why abandoning is not the same as forgetting.
RESUME_MAX_GAP_SECONDS = 300.0

WS_PER_KWH = 3_600_000.0
WS_PER_WH = 3_600.0


@dataclass(frozen=True)
class CycleSummary:
    """One finished cycle, timestamps in epoch seconds."""

    started_ts: float
    ended_ts: float
    energy_kwh: float
    peak_watts: float
    average_watts: float
    #: Watt-hours drawn in each wall-clock-aligned bucket, in order. The first
    #: and last are partial, because a wash does not begin on a five-minute
    #: boundary; they are integrated from the same curve as the total, so the
    #: series SUMS TO energy_kwh within rounding and nothing is lost at the ends.
    shape_wh: tuple[float, ...] = ()
    #: The wall-clock instant the first bucket opens. Carried rather than
    #: derived from started_ts, so that no reader downstream has to reproduce
    #: this rounding identically, across time zones, forever, in two languages.
    shape_start_ts: float = 0.0
    #: The bucket width, so a later change of resolution is a value rather than
    #: a migration and an old row stays readable.
    shape_bucket_seconds: float = BUCKET_SECONDS

    @property
    def duration_seconds(self) -> float:
        return self.ended_ts - self.started_ts


class CycleDetector:
    """Feed it (timestamp, watts) samples; it hands back finished cycles.

    sample() is called for every power reading; tick() is called when time has
    passed with no reading (a quiet plug stops chattering, so the caller runs
    a timer to ask "still quiet?"). Both return a CycleSummary when a cycle
    just finished, else None.
    """

    def __init__(
        self,
        start_watts: float = START_WATTS,
        quiet_watts: float = QUIET_WATTS,
        quiet_seconds: float = QUIET_SECONDS,
        min_cycle_seconds: float = MIN_CYCLE_SECONDS,
        bucket_seconds: float = BUCKET_SECONDS,
        split_quiet_seconds: float = SPLIT_QUIET_SECONDS,
    ) -> None:
        self._start_watts = start_watts
        self._quiet_watts = quiet_watts
        self._quiet_seconds = quiet_seconds
        self._min_cycle_seconds = min_cycle_seconds
        self._bucket_seconds = bucket_seconds
        self._split_quiet_seconds = split_quiet_seconds
        self._reset_all()

    def _reset(self) -> None:
        self._running = False
        self._started_ts = 0.0
        self._quiet_since: float | None = None
        self._last_ts: float | None = None
        self._last_watts = 0.0
        self._energy_ws = 0.0
        self._peak = 0.0
        # The shape. _shape_start is the wall-clock bucket boundary at or before
        # the cycle's first sample, so bucket i covers
        # [_shape_start + i*width, +width) and the first one is partial.
        self._shape_start = 0.0
        self._buckets: list[float] = []

    def _reset_all(self) -> None:
        self._reset()
        # Not cleared by _reset, because a cycle finishing normally must never
        # suppress the next one. Only restore() sets it, and only a quiet
        # machine clears it.
        self._suppressed = False

    @property
    def running(self) -> bool:
        return self._running

    @property
    def quiet_since(self) -> float | None:
        """When the current quiet spell began, if one is being timed."""
        return self._quiet_since if self._running else None

    @property
    def started_ts(self) -> float | None:
        """When the run in progress began, for a caller timing something off it."""
        return self._started_ts if self._running else None

    def confirmed_wash(self, now: float) -> bool:
        """Has this run already lasted long enough to BE a wash? (30 Aug)

        The same rule [_summarise] applies at the end, asked early: a run this
        long will produce a summary, and a run shorter than this will be thrown
        away. It exists so that nothing outside is obliged to guess which.

        The subtlety is the quiet spell. A run may still be `running` while it
        is timing its own silence, and if that silence began too early the wash
        is already destined for the bin however long the detector goes on
        holding it. So the length is measured to the moment the quiet BEGAN,
        exactly as the summary will measure it, and not to now.
        """
        if not self._running:
            return False
        ended = self._quiet_since if self._quiet_since is not None else now
        return ended - self._started_ts >= self._min_cycle_seconds

    def sample(self, ts: float, watts: float) -> CycleSummary | None:
        """A power reading arrived."""
        if watts < 0 or (self._last_ts is not None and ts < self._last_ts):
            return None  # a glitching plug does not get to edit history

        # Waiting out a wash we lost the middle of. Starting a cycle now would
        # record its tail as a whole wash, which is the exact fragment that
        # drags the learned medians down and the reason this state exists. The
        # machine falling quiet is the all-clear.
        if self._suppressed:
            if watts < self._quiet_watts:
                self._suppressed = False
            self._last_ts = ts
            self._last_watts = watts
            return None

        finished: CycleSummary | None = None
        if not self._running:
            if watts >= self._start_watts:
                self._begin(ts, watts)
        # The machine is drawing again after a real pause, and only a heater
        # climbs this high: this is a SECOND wash whose start would otherwise be
        # swallowed. Tested BEFORE the integration on purpose. The slice from
        # the last quiet reading up to this one spans the pause and the ramp
        # back, and it belongs to neither wash; integrating it first would put a
        # slug of the second wash's heating into the first wash's last buckets,
        # which is the one thing the shape must not get wrong.
        elif (
            self._quiet_since is not None
            and watts >= RESTART_WATTS
            and ts - self._quiet_since >= self._split_quiet_seconds
        ):
            finished = self._split_at(self._quiet_since, ts, watts)
        else:
            if self._last_ts is not None and ts > self._last_ts:
                self._integrate(self._last_ts, ts, self._last_watts, watts)
            self._peak = max(self._peak, watts)
            if watts < self._quiet_watts:
                if self._quiet_since is None:
                    self._quiet_since = ts
                elif ts - self._quiet_since >= self._quiet_seconds:
                    finished = self._finish()
            else:
                self._quiet_since = None

        self._last_ts = ts
        self._last_watts = watts
        return finished

    def _begin(self, ts: float, watts: float) -> None:
        self._running = True
        self._started_ts = ts
        self._quiet_since = None
        self._energy_ws = 0.0
        self._peak = watts
        # Snap back to the wall-clock boundary at or before the first sample.
        self._shape_start = ts - (ts % self._bucket_seconds)
        self._buckets = []

    def _integrate(self, t0: float, t1: float, w0: float, w1: float) -> None:
        """Trapezoid between two samples, into the total AND the buckets.

        The bucket split is done by walking the boundaries between t0 and t1
        rather than by assigning the whole slice to one bucket, because a plug
        that reports every thirty seconds is common but one that reports on
        change alone can leave ten minutes between samples, and dropping such a
        slice into a single bucket would put a heating plateau's worth of energy
        in one place and none either side. Power is taken as linear across the
        slice, which is the same assumption the total has always made.
        """
        self._energy_ws += (w0 + w1) / 2.0 * (t1 - t0)
        if not self._buckets and t0 < self._shape_start:
            return
        span = t1 - t0
        edge = t0
        while edge < t1:
            index = int((edge - self._shape_start) // self._bucket_seconds)
            if index >= MAX_BUCKETS:
                return
            nxt = min(self._shape_start + (index + 1) * self._bucket_seconds, t1)
            # Mean power over just this slice of the ramp.
            a = w0 + (w1 - w0) * ((edge - t0) / span) if span else w0
            b = w0 + (w1 - w0) * ((nxt - t0) / span) if span else w1
            while len(self._buckets) <= index:
                self._buckets.append(0.0)
            self._buckets[index] += (a + b) / 2.0 * (nxt - edge) / WS_PER_WH
            edge = nxt

    def tick(self, ts: float) -> CycleSummary | None:
        """Time passed with no reading; a quiet plug may just be done."""
        if not self._running or self._quiet_since is None:
            return None
        if ts - self._quiet_since < self._quiet_seconds:
            return None
        # Carry the (quiet) last reading forward so the tail is accounted for.
        if self._last_ts is not None and ts > self._last_ts:
            self._integrate(self._last_ts, ts, self._last_watts, self._last_watts)
            self._last_ts = ts
        return self._finish()

    # ---- surviving a restart (30 Aug) ----

    def state(self) -> dict:
        """The detector's whole mind, as plain JSON-safe values.

        Deliberately a dict rather than the dataclass: it is written to disk by
        cycle_watch.py and read back by a build that may be older or newer, so
        it has a version and no types of its own. The pure module owns the
        SHAPE of the state and the rules for resuming it; the watcher owns the
        file, because this module stays free of Home Assistant imports.
        """
        return {
            "version": 1,
            "running": self._running,
            "suppressed": self._suppressed,
            "started_ts": self._started_ts,
            "quiet_since": self._quiet_since,
            "last_ts": self._last_ts,
            "last_watts": self._last_watts,
            "energy_ws": self._energy_ws,
            "peak": self._peak,
            "shape_start": self._shape_start,
            "buckets": list(self._buckets),
        }

    def restore(self, state: dict | None, now: float) -> str:
        """Take a saved mind back, or decide not to. Returns what it did.

        A restart is amnesia, and amnesia has three different endings depending
        on how long the lights were out. They are worth spelling out because
        the wrong one is the bug this exists to fix.

        FINISHED. The saved state was already timing a quiet spell and that
        spell has now outlasted the finish window. The wash ended while nobody
        was watching, and it ended at a moment we know exactly, because quiet
        began before the lights went out. Nothing is missing: the machine was
        silent for the whole gap. So the state comes back whole and the very
        next tick closes the cycle properly. This is a cycle that today is lost
        outright.

        RESUMED. The wash is still going and the gap is small enough that the
        next sample can bridge it the ordinary way, ramping from the last known
        power to the new one, which is exactly what the detector already does
        for any plug that reports on change alone.

        SUPPRESSED. The wash is still going and too much of it is missing. The
        state is dropped, but dropping alone would not be enough: the machine is
        still drawing, so the next sample would START A NEW CYCLE and record the
        tail of one wash as a whole one. That fragment is precisely what drags
        the learned medians down, and it is what happens today. So the detector
        refuses to begin anything until it has seen the machine fall quiet.

        Anything malformed, from a future version, or stamped in the future by a
        clock that has moved, is ignored: a fresh start is always safe.
        """
        if not isinstance(state, dict) or state.get("version") != 1:
            return "fresh"
        try:
            running = bool(state["running"])
            last_ts = state["last_ts"]
            quiet_since = state["quiet_since"]
            buckets = [float(b) for b in state.get("buckets", [])]
        except (KeyError, TypeError, ValueError):
            return "fresh"
        if not running or last_ts is None:
            return "fresh"
        if now < last_ts:
            return "fresh"

        def take() -> None:
            self._running = True
            self._suppressed = False
            self._started_ts = float(state["started_ts"])
            self._quiet_since = quiet_since
            self._last_ts = float(last_ts)
            self._last_watts = float(state["last_watts"])
            self._energy_ws = float(state["energy_ws"])
            self._peak = float(state["peak"])
            self._shape_start = float(state["shape_start"])
            self._buckets = buckets

        if quiet_since is not None and now - quiet_since >= self._quiet_seconds:
            take()
            return "finished"
        if now - last_ts <= RESUME_MAX_GAP_SECONDS:
            take()
            return "resumed"
        self._reset()
        self._suppressed = True
        return "suppressed"

    def _summarise(self, ended: float) -> CycleSummary | None:
        started = self._started_ts
        if ended - started < self._min_cycle_seconds:
            return None
        duration = ended - started
        return CycleSummary(
            started_ts=started,
            ended_ts=ended,
            energy_kwh=round(self._energy_ws / WS_PER_KWH, 4),
            peak_watts=round(self._peak, 1),
            average_watts=round(self._energy_ws / duration, 1),
            shape_wh=tuple(round(b, 2) for b in self._buckets),
            shape_start_ts=self._shape_start,
            shape_bucket_seconds=self._bucket_seconds,
        )

    def _finish(self) -> CycleSummary | None:
        ended = self._quiet_since if self._quiet_since is not None else self._last_ts
        summary = self._summarise(ended) if ended is not None else None
        self._reset()
        return summary

    def _split_at(self, ended: float, ts: float, watts: float) -> CycleSummary | None:
        """Close the wash that just ended and open the one now starting.

        The first wash ends when its quiet BEGAN, exactly as a normal finish
        does, so the pause between two loads belongs to neither. The energy
        drawn during that pause is a few watt-hours of standby and is discarded
        with it, which is the honest place for it to go.

        A first half too short to be a wash is not a wash: the whole run is
        treated as one cycle beginning here, which is what a door left open or a
        dial being nudged before a real wash looks like.
        """
        summary = self._summarise(ended)
        self._reset()
        self._begin(ts, watts)
        return summary
