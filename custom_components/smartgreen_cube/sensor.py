"""Diagnostic sensors: signal strength, last seen, and the proxy in use.

All values come from the cubes' advertisements — no connection is opened for
them, so the sensors cost no radio time and never get in the way of control.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import time

from homeassistant.components import bluetooth
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, SIGNAL_STRENGTH_DECIBELS_MILLIWATT
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    COMPANY_ID,
    CONF_KEY,
    CONF_MODULES,
    CONF_NONCE,
    MOD_LMP,
)
from .device import build_device_info
from .lmp import decode_advertisement


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    """Create the diagnostic sensors for every module."""
    data = entry.data
    key = bytes.fromhex(data[CONF_KEY])
    nonce = bytes.fromhex(data[CONF_NONCE])

    entities: list[SensorEntity] = []
    for module in data.get(CONF_MODULES, []):
        entities.append(CubeRssiSensor(hass, entry, module, key, nonce))
        entities.append(CubeLastSeenSensor(hass, entry, module))
        entities.append(CubeSourceSensor(hass, entry, module))
    async_add_entities(entities)


class _CubeDiagnosticSensor(SensorEntity):
    """Common base: finds the advertisement belonging to this cube."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, module: dict,
                 key: str) -> None:
        self.hass = hass
        self._lmp = module[MOD_LMP]
        self._attr_unique_id = f"{entry.entry_id}_{self._lmp}_{key}"
        self._attr_device_info = build_device_info(module)

    def _service_info(self):
        """Most recent advertisement of this cube, or None."""
        want_name = "bulb" + self._lmp.replace(":", "").lower()
        for si in bluetooth.async_discovered_service_info(self.hass,
                                                          connectable=True):
            if (si.name or "").lower() == want_name:
                return si
            md = si.manufacturer_data.get(COMPANY_ID)
            if md and len(md) >= 4 and "%02X:%02X" % (md[3], md[2]) == self._lmp:
                return si
        return None


class CubeRssiSensor(_CubeDiagnosticSensor):
    """Signal strength of the most recent advertisement."""

    _attr_translation_key = "rssi"
    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT

    def __init__(self, hass, entry, module, key: bytes, nonce: bytes) -> None:
        super().__init__(hass, entry, module, "rssi")
        self._key = key
        self._nonce = nonce
        self._attrs: dict[str, object] = {}

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return self._attrs

    async def async_update(self) -> None:
        si = self._service_info()
        if si is None:
            self._attr_native_value = None
            self._attr_available = False
            return
        self._attr_available = True
        self._attr_native_value = si.rssi

        # The advertisement also carries the cube's network state. It
        # deliberately does NOT carry on/off — there is no broadcast for that.
        attrs: dict[str, object] = {}
        md = si.manufacturer_data.get(COMPANY_ID)
        if md:
            decoded = decode_advertisement(bytes(md), self._key, self._nonce)
            if decoded is not None:
                attrs["registered"] = decoded["registered"]
                attrs["connected"] = decoded["connected"]
                attrs.update(decoded["fields"])
            else:
                attrs["note"] = "advertisement could not be decrypted"
        self._attrs = attrs


class CubeLastSeenSensor(_CubeDiagnosticSensor):
    """When the last advertisement was received."""

    _attr_translation_key = "last_seen"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, hass, entry, module) -> None:
        super().__init__(hass, entry, module, "last_seen")

    async def async_update(self) -> None:
        si = self._service_info()
        if si is None:
            return
        # ``si.time`` is a monotonic clock, not wall time, so derive the
        # timestamp from how long ago the advertisement arrived.
        age = max(0.0, time.monotonic() - si.time)
        self._attr_native_value = datetime.now(timezone.utc) - timedelta(seconds=age)


class CubeSourceSensor(_CubeDiagnosticSensor):
    """Which adapter or Bluetooth proxy currently receives this cube."""

    _attr_translation_key = "proxy"

    def __init__(self, hass, entry, module) -> None:
        super().__init__(hass, entry, module, "source")

    async def async_update(self) -> None:
        si = self._service_info()
        self._attr_native_value = None if si is None else si.source
