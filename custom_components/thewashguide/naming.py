"""How the household introduces itself outside the app."""

from __future__ import annotations

from typing import Any


# The app's own fallback (HouseholdSection.kt displayLabel): a household
# that never chose a moniker is still a House.
DEFAULT_MONIKER = "House"


def household_display(household: dict[str, Any] | None) -> str | None:
    """The moniker-led address, as the app itself says it: "House Townsend",
    never the bare "Townsend". The feed's label is the moniker (House,
    Villa, Casa...) and the name is the household's own; an unchosen
    moniker defaults to House exactly as the app defaults it, and with no
    name at all there is nothing to say and the caller keeps its fallback.
    """
    household = household or {}
    label = (household.get("label") or "").strip()
    name = (household.get("name") or "").strip()
    if name:
        return f"{label or DEFAULT_MONIKER} {name}"
    return label or None
