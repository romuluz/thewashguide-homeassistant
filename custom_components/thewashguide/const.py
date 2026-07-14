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

UPDATE_INTERVAL_SECONDS = 60

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
