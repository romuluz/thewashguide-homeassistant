# The Wash Guide × Home Assistant

A Home Assistant custom integration for [The Wash Guide](https://thewashguide.app):
laundry, as native entities.

## What you get

One device with: pending wash tasks (the list attached as attributes), the
detergent shield (cupboard percentage, with the per-product breakdown as
attributes for shopping-list automations, and long-term statistics recorded),
the household's last wash and next scheduled wash (timestamps with recipes and
cadence as attributes), an overdue-wash binary sensor that mirrors the app's
nudge ladder, and a wash-personality sensor per household member with tier,
monthly washes and monthly rewards.

Plus the machine the household shares: loads since its last maintenance wash,
when that wash was and who ran it, and two due flags (the maintenance wash,
which falls due on thirty loads or two months, whichever arrives first; and
the pump filter, which is a quarterly job).

Five bus events for automations: `thewashguide_wash_logged`,
`thewashguide_task_created`, `thewashguide_task_completed`,
`thewashguide_machine_cleaned` and `thewashguide_cycle_finished`.

## Install

[![Open your Home Assistant instance and show this repository inside HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=romuluz&repository=thewashguide-homeassistant&category=integration)

1. In HACS, search for **The Wash Guide** and download it (it is in the HACS
   default store; the badge above jumps your own instance straight to it),
   then restart Home Assistant. No HACS? Copy `custom_components/thewashguide`
   into your `config/custom_components/` folder instead.
2. In The Wash Guide app: Settings → Smart home → Generate connect key.
3. In Home Assistant: Settings → Devices & services → Add integration →
   The Wash Guide → paste the key.

The connect key is read-only, scoped to your household's laundry status, and
can be revoked in the app at any time. Data refreshes every 15 minutes, or
every minute for a PRO household: the cloud tells the integration which
cadence your plan allows and it paces itself, so an upgrade takes effect on
the next poll with nothing to set up, and a lapse travels the same wire back
to the 15-minute cadence. If you would rather poll less often than your plan
allows, set an interval in minutes under **Configure**; it can never poll
more often.

## Acting, not just reading (needs a control key)

PRO households can also generate a **control key** in the app, and paste it
into the integration at setup or later with **Configure**. It unlocks four
services:

| Service | What it does |
| --- | --- |
| `thewashguide.create_task` | Post an open wash task to the household board |
| `thewashguide.cancel_task` | Cancel a task still waiting on the board |
| `thewashguide.request_maintenance_wash` | Ask the house to run the machine's maintenance wash |
| `thewashguide.log_machine_clean` | Record that the machine was cleaned, or the filter emptied |

The key's owner is the actor, and the household's own rules travel with it:
rewards need an admin or a manager, and juniors cannot create tasks. The key
is also only as alive as the subscription behind it: if the owner's PRO ends,
the cloud refuses control calls with a plain message (reading carries on
untouched), and resubscribing brings the same key back to life without
regenerating anything.

A wash task cannot be completed from Home Assistant, and a maintenance wash
can. That is deliberate. Completing a wash means a person ran a load of
laundry, spent detergent from the cupboard and added to their wash history,
and an automation cannot honestly claim any of that. An empty drum at 90
degrees produces no laundry, spends no detergent and enters no wash log; the
only thing it produces is the fact that it ran, and a washing machine that has
just finished its cycle knows that fact rather better than the person who will
remember to mention it on Thursday.

So the machine can ask for its own maintenance wash, and log it:

```yaml
automation:
  - alias: "The machine asks for its own maintenance wash"
    trigger:
      - platform: state
        entity_id: binary_sensor.maintenance_wash_due
        to: "on"
    condition: >
      {{ not state_attr('binary_sensor.maintenance_wash_due', 'on_the_board') }}
    action:
      - service: thewashguide.request_maintenance_wash
        data:
          note: "The drum has done thirty loads since the last one."
```

If the maintenance wash is already on the board when you call
`log_machine_clean`, it completes that task rather than logging a second,
parallel truth.

## The measured machine (optional, needs a metering plug)

If the washing machine sits on a smart plug that meters power (or the machine
reports power itself), open **Configure** and pick that power sensor. The
integration then notices cycles from the power curve: watts rise, the cycle is
running; ten straight minutes of silence, it finished. Soaks, pauses and
anti-crease tumbling are forgiven, and a blip too short to have washed
anything is discarded.

When a cycle finishes, two things happen. Locally,
`thewashguide_cycle_finished` fires on the bus with `started_at`, `ended_at`,
`duration_seconds`, `energy_kwh`, `peak_watts` and `average_watts`, ready for
a "machine's done" notification that knows what the cycle actually cost to
run. And a summary of the same six facts is sent to The Wash Guide, where it
joins your household's own record, so the app can start learning what your
machine really does with each kind of wash.

Detection happens entirely in your home and only the summary is uploaded: raw
power readings never leave the house. Monitoring only, always: if the plug can
switch, this integration never touches the switch, and never puts itself in
the machine's power path. One honest caution: a washing machine heats water at
2 to 3 kW, so use a plug properly rated for it, not a lamp-grade one.

This integration is in early development alongside the app itself; issues and
automation ideas are very welcome.

## About this repository

This is a published copy. The integration is developed alongside The Wash Guide
app itself, because it has to move in step with the cloud it talks to: an
endpoint and the entities that read it are one change, not two.

So please raise **issues** rather than pull requests. Bug reports, automation
ideas and feature requests are all genuinely welcome, and a fix will land here
in the next release rather than as a merged commit.
