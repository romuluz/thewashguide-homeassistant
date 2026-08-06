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

WS_PER_KWH = 3_600_000.0


@dataclass(frozen=True)
class CycleSummary:
    """One finished cycle, timestamps in epoch seconds."""

    started_ts: float
    ended_ts: float
    energy_kwh: float
    peak_watts: float
    average_watts: float

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
    ) -> None:
        self._start_watts = start_watts
        self._quiet_watts = quiet_watts
        self._quiet_seconds = quiet_seconds
        self._min_cycle_seconds = min_cycle_seconds
        self._reset()

    def _reset(self) -> None:
        self._running = False
        self._started_ts = 0.0
        self._quiet_since: float | None = None
        self._last_ts: float | None = None
        self._last_watts = 0.0
        self._energy_ws = 0.0
        self._peak = 0.0

    @property
    def running(self) -> bool:
        return self._running

    @property
    def quiet_since(self) -> float | None:
        """When the current quiet spell began, if one is being timed."""
        return self._quiet_since if self._running else None

    def sample(self, ts: float, watts: float) -> CycleSummary | None:
        """A power reading arrived."""
        if watts < 0 or (self._last_ts is not None and ts < self._last_ts):
            return None  # a glitching plug does not get to edit history

        finished: CycleSummary | None = None
        if not self._running:
            if watts >= self._start_watts:
                self._running = True
                self._started_ts = ts
                self._quiet_since = None
                self._energy_ws = 0.0
                self._peak = watts
        else:
            if self._last_ts is not None and ts > self._last_ts:
                self._energy_ws += (
                    (watts + self._last_watts) / 2.0 * (ts - self._last_ts)
                )
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

    def tick(self, ts: float) -> CycleSummary | None:
        """Time passed with no reading; a quiet plug may just be done."""
        if not self._running or self._quiet_since is None:
            return None
        if ts - self._quiet_since < self._quiet_seconds:
            return None
        # Carry the (quiet) last reading forward so the tail is accounted for.
        if self._last_ts is not None and ts > self._last_ts:
            self._energy_ws += self._last_watts * (ts - self._last_ts)
            self._last_ts = ts
        return self._finish()

    def _finish(self) -> CycleSummary | None:
        ended = self._quiet_since if self._quiet_since is not None else self._last_ts
        started = self._started_ts
        summary: CycleSummary | None = None
        if ended is not None and ended - started >= self._min_cycle_seconds:
            duration = ended - started
            energy_kwh = self._energy_ws / WS_PER_KWH
            summary = CycleSummary(
                started_ts=started,
                ended_ts=ended,
                energy_kwh=round(energy_kwh, 4),
                peak_watts=round(self._peak, 1),
                average_watts=round(self._energy_ws / duration, 1),
            )
        self._reset()
        return summary
