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

Four bus events for automations: `thewashguide_wash_logged`,
`thewashguide_task_created`, `thewashguide_task_completed` and
`thewashguide_machine_cleaned`.

## Install

1. Copy `custom_components/thewashguide` into your Home Assistant `config/
   custom_components/` folder (or add this repository as a custom repository
   in HACS), then restart Home Assistant.
2. In The Wash Guide app: Settings → Smart home → Generate connect key.
3. In Home Assistant: Settings → Devices & services → Add integration →
   The Wash Guide → paste the key.

The connect key is read-only, scoped to your household's laundry status, and
can be revoked in the app at any time. Data refreshes about once a minute.

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
rewards need an admin or a manager, and juniors cannot create tasks.

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

This integration is in early development alongside the app itself; issues and
automation ideas are very welcome.

## About this repository

This is a published copy. The integration is developed alongside The Wash Guide
app itself, because it has to move in step with the cloud it talks to: an
endpoint and the entities that read it are one change, not two.

So please raise **issues** rather than pull requests. Bug reports, automation
ideas and feature requests are all genuinely welcome, and a fix will land here
in the next release rather than as a merged commit.
