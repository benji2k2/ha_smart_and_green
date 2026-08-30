"""Diagnostic flags for the module settings the app can change.

These come from the imported ``.lap`` configuration, so they cost nothing: no
connection is opened and no radio time is used. The trade-off is that they are
a snapshot from the moment of the export — the cubes do not broadcast these
settings, and reading them live would mean putting extra frames on the wire.

Each entity carries a ``source`` attribute saying where its value came from, so
a stale value can never be mistaken for a live one.
"""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_MODULES, MOD_LMP, MOD_PROPS, PROP_NAMES
from .device import build_device_info

_SOURCE = "imported configuration (snapshot at export time)"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    """Create one flag per module setting that the configuration knows about."""
    entities: list[BinarySensorEntity] = []
    for module in entry.data.get(CONF_MODULES, []):
        props = module.get(MOD_PROPS) or {}
        for key in PROP_NAMES.values():
            if props.get(key) is not None:
                entities.append(CubePropertyFlag(entry, module, key, props[key]))
    async_add_entities(entities)


class CubePropertyFlag(BinarySensorEntity):
    """One module setting, as recorded in the imported configuration."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: ConfigEntry, module: dict, key: str,
                 value: bool) -> None:
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry.entry_id}_{module[MOD_LMP]}_{key}"
        self._attr_is_on = bool(value)
        self._attr_device_info = build_device_info(module)

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        return {"source": _SOURCE}
