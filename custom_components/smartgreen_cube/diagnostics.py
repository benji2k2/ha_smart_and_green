"""Diagnose-Daten für Smart & Green Cube — mit Redaction der Geheimnisse."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_KEY, CONF_NONCE

# Diese Felder niemals in Diagnose-Downloads ausgeben.
TO_REDACT = {CONF_KEY, CONF_NONCE}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Liefert Diagnosedaten; Schlüssel und Nonce werden maskiert."""
    return {
        "entry_data": async_redact_data(dict(entry.data), TO_REDACT),
        "entry_options": async_redact_data(dict(entry.options), TO_REDACT),
    }
