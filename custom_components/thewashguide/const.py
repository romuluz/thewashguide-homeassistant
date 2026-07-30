"""Constants for The Wash Guide integration."""

DOMAIN = "thewashguide"

# The cloud endpoints. Both are public by design and serve nothing without a
# valid key from the app: the feed reads, the control endpoint acts.
FEED_URL = "https://roedghepqpkbwswmfgfl.supabase.co/functions/v1/smart-home-feed"
CONTROL_URL = "https://roedghepqpkbwswmfgfl.supabase.co/functions/v1/smart-home-control"

CONF_API_KEY = "api_key"
# The optional PRO control key. Held separately from the read key on purpose:
# the two are different grants, and a household that only ever reads should
# never have a key that could write sitting in its config entry.
CONF_CONTROL_KEY = "control_key"

# The starting poll cadence, matching the free floor. The feed declares the
# floor each key has actually earned in every response (min_poll_seconds: 60
# for a PRO household, 900 free) and the coordinator paces itself by it, so
# this constant only governs the first poll before the cloud has spoken.
DEFAULT_UPDATE_INTERVAL_SECONDS = 900

# The user's own choice of cadence, in minutes, from the options flow. It can
# slow the poll below the plan's floor (a courtesy to batteries and quotas);
# it can never speed it past the floor, because the floor is not ours to give.
# Zero or absent means automatic: poll at whatever floor the feed declares.
CONF_UPDATE_INTERVAL = "update_interval"

EVENT_WASH_LOGGED = "thewashguide_wash_logged"
EVENT_TASK_COMPLETED = "thewashguide_task_completed"
EVENT_TASK_CREATED = "thewashguide_task_created"
EVENT_MACHINE_CLEANED = "thewashguide_machine_cleaned"

# The services the control key unlocks.
SERVICE_CREATE_TASK = "create_task"
SERVICE_CANCEL_TASK = "cancel_task"
SERVICE_REQUEST_MAINTENANCE = "request_maintenance_wash"
SERVICE_LOG_MACHINE_CLEAN = "log_machine_clean"

# A pending task older than this counts as overdue (mirrors the app's first
# nudge rung), independent of the server's nudge state.
OVERDUE_AFTER_HOURS = 24
