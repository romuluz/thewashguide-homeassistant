"""Config flow for The Wash Guide: paste the connect key, done.

The control key is optional and asked for in the same breath, because a PRO
household that has one wants it in from the start, and a household that has
not can add it later with Configure without touching the read key.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError

from . import call_control, fetch_feed
from .const import CONF_API_KEY, CONF_CONTROL_KEY, DOMAIN

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_API_KEY): str,
        vol.Optional(CONF_CONTROL_KEY, default=""): str,
    }
)


async def _check_control_key(hass, key: str) -> str | None:
    """Return an error slug, or None when the control key is good.

    Ping changes nothing, so a wrong key costs the household nothing but a
    message. The three answers worth telling apart: not a key, a read key in
    the control box, and a cloud that is simply not answering.
    """
    try:
        await call_control(hass, key, {"action": "ping"})
    except HomeAssistantError as err:
        text = str(err)
        if "control scope required" in text:
            return "not_control_key"
        if "invalid key" in text or "missing key" in text:
            return "invalid_control_key"
        return "cannot_connect"
    except Exception:  # noqa: BLE001
        return "cannot_connect"
    return None


class WashGuideConfigFlow(ConfigFlow, domain=DOMAIN):
    """Ask for the connect key generated in The Wash Guide app."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            key = user_input[CONF_API_KEY].strip()
            control = (user_input.get(CONF_CONTROL_KEY) or "").strip()
            try:
                feed = await fetch_feed(self.hass, key)
            except ConfigEntryAuthFailed:
                errors["base"] = "invalid_auth"
            except Exception:  # noqa: BLE001 - anything else is "can't reach"
                errors["base"] = "cannot_connect"
            else:
                problem = await _check_control_key(self.hass, control) if control else None
                if problem:
                    errors[CONF_CONTROL_KEY] = problem
                else:
                    household = feed.get("household") or {}
                    title = household.get("name") or "The Wash Guide"
                    return self.async_create_entry(
                        title=title,
                        data={CONF_API_KEY: key, CONF_CONTROL_KEY: control},
                    )
        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return WashGuideOptionsFlow()


class WashGuideOptionsFlow(OptionsFlow):
    """Add, replace or clear the control key after the fact."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        current = self.config_entry.options.get(
            CONF_CONTROL_KEY, self.config_entry.data.get(CONF_CONTROL_KEY, "")
        )
        if user_input is not None:
            control = (user_input.get(CONF_CONTROL_KEY) or "").strip()
            # An empty box is a deliberate revocation: the household keeps
            # reading and stops being able to act.
            problem = await _check_control_key(self.hass, control) if control else None
            if problem:
                errors[CONF_CONTROL_KEY] = problem
            else:
                return self.async_create_entry(data={CONF_CONTROL_KEY: control})
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {vol.Optional(CONF_CONTROL_KEY, default=current): str}
            ),
            errors=errors,
        )
