"""Diagnose-Sensoren: Signalstärke, letzter Empfang, verwendeter Proxy.

Alle Werte stammen aus den Advertisements der Cubes — es wird dafür keine
Verbindung aufgebaut, die Sensoren kosten also keine Funkzeit und stören die
Steuerung nicht.
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
    """Legt für jedes Modul die Diagnose-Sensoren an."""
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
    """Gemeinsame Basis: findet das Advertisement des zugehörigen Cubes."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, module: dict,
                 key: str) -> None:
        self.hass = hass
        self._lmp = module[MOD_LMP]
        self._attr_unique_id = f"{entry.entry_id}_{self._lmp}_{key}"
        self._attr_device_info = build_device_info(module)

    def _service_info(self):
        """Letztes Advertisement dieses Cubes, oder None."""
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
    """Signalstärke des zuletzt empfangenen Advertisements."""

    _attr_name = "Signalstärke"
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

        # Das Advertisement traegt zusaetzlich den Netzwerkzustand des Cubes.
        # Es enthaelt bewusst KEIN An/Aus — dafuer gibt es keinen Broadcast.
        attrs: dict[str, object] = {}
        md = si.manufacturer_data.get(COMPANY_ID)
        if md:
            decoded = decode_advertisement(bytes(md), self._key, self._nonce)
            if decoded is not None:
                attrs["registriert"] = decoded["registered"]
                attrs["verbunden"] = decoded["connected"]
                attrs.update(decoded["fields"])
            else:
                attrs["hinweis"] = "Advertisement nicht entschlüsselbar"
        self._attrs = attrs


class CubeLastSeenSensor(_CubeDiagnosticSensor):
    """Zeitpunkt des letzten empfangenen Advertisements."""

    _attr_name = "Zuletzt gesehen"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, hass, entry, module) -> None:
        super().__init__(hass, entry, module, "last_seen")

    async def async_update(self) -> None:
        si = self._service_info()
        if si is None:
            return
        # ``si.time`` ist eine monotone Uhr, keine Wanduhr — deshalb ueber den
        # Abstand zu jetzt zurueckrechnen.
        age = max(0.0, time.monotonic() - si.time)
        self._attr_native_value = datetime.now(timezone.utc) - timedelta(seconds=age)


class CubeSourceSensor(_CubeDiagnosticSensor):
    """Über welchen Adapter bzw. Bluetooth-Proxy der Cube empfangen wird."""

    _attr_name = "Bluetooth-Proxy"

    def __init__(self, hass, entry, module) -> None:
        super().__init__(hass, entry, module, "source")

    async def async_update(self) -> None:
        si = self._service_info()
        self._attr_native_value = None if si is None else si.source
