"""Light-Plattform für Smart & Green Cube."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from homeassistant.components import bluetooth
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_HS_COLOR,
    ColorMode,
    LightEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CHAR_UUID,
    COMPANY_ID,
    CONF_GROUP,
    CONF_KEY,
    CONF_MODULES,
    CONF_NONCE,
    DEFAULT_CLASS,
    DOMAIN,
    MOD_CLASS,
    MOD_INDEX,
    MOD_LMP,
    MOD_NAME,
)
from .lmp import build_color_payload, build_frame

_LOGGER = logging.getLogger(__name__)

# Ein Lock pro BLE-Adresse — verhindert parallele Verbindungen zum selben Gerät.
_LOCKS: dict[str, asyncio.Lock] = {}


def _lock_for(mac: str) -> asyncio.Lock:
    return _LOCKS.setdefault(mac, asyncio.Lock())


def _lmp_frag(lmp: str) -> str:
    """'13:40' -> '1340' (wie im Advertising-Namen 'Bulb1340')."""
    return lmp.replace(":", "").lower()


def _resolve_mac(hass: HomeAssistant, lmp: str) -> str | None:
    """Findet die BLE-Adresse eines Moduls anhand von Adv-Name / Hersteller-Daten."""
    want_name = "bulb" + _lmp_frag(lmp)
    for si in bluetooth.async_discovered_service_info(hass, connectable=True):
        if (si.name or "").lower() == want_name:
            return si.address
        md = si.manufacturer_data.get(COMPANY_ID)
        if md and len(md) >= 4:
            src = "%02X:%02X" % (md[3], md[2])
            if src.upper() == lmp.upper():
                return si.address
    return None


def _discover_modules(hass: HomeAssistant) -> list[dict]:
    """Baut die Modulliste rein aus BLE-Advertisements (Fallback ohne .lap)."""
    seen: dict[str, dict] = {}
    for si in bluetooth.async_discovered_service_info(hass, connectable=True):
        md = si.manufacturer_data.get(COMPANY_ID)
        name = si.name or ""
        lmp = None
        if md and len(md) >= 4:
            lmp = "%02X:%02X" % (md[3], md[2])
        elif name.lower().startswith("bulb") and len(name) >= 8:
            frag = name[4:8]
            lmp = f"{frag[0:2]}:{frag[2:4]}".upper()
        if not lmp:
            continue
        seen.setdefault(lmp, {
            MOD_NAME: name or f"Cube {lmp}",
            MOD_LMP: lmp,
            MOD_INDEX: 0,
            MOD_CLASS: DEFAULT_CLASS,
        })
    return list(seen.values())


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Legt die Cube-Lampen an."""
    key = bytes.fromhex(entry.data[CONF_KEY])
    nonce = bytes.fromhex(entry.data[CONF_NONCE])
    modules: list[dict] = list(entry.data.get(CONF_MODULES) or [])
    if not modules:
        modules = _discover_modules(hass)

    entities: list[LightEntity] = [
        SmartGreenCubeLight(hass, entry, key, nonce, m) for m in modules
    ]

    group = entry.data.get(CONF_GROUP)
    if group and len(modules) > 1:
        member_lmps = [m[MOD_LMP] for m in modules]
        entities.append(
            SmartGreenCubeLight(hass, entry, key, nonce, group,
                                is_group=True, member_lmps=member_lmps)
        )

    async_add_entities(entities)


class SmartGreenCubeLight(LightEntity):
    """Eine Cube-Leuchte (oder die Gruppe 'Alle')."""

    _attr_has_entity_name = False
    _attr_color_mode = ColorMode.HS
    _attr_supported_color_modes = {ColorMode.HS}
    _attr_assumed_state = True
    _attr_should_poll = False

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        key: bytes,
        nonce: bytes,
        module: dict,
        is_group: bool = False,
        member_lmps: list[str] | None = None,
    ) -> None:
        self.hass = hass
        self._key = key
        self._nonce = nonce
        self._lmp = module[MOD_LMP]
        self._index = module.get(MOD_INDEX, 0)
        self._class = module.get(MOD_CLASS, DEFAULT_CLASS)
        self._is_group = is_group
        self._members = member_lmps or []
        self._cmd_id = 0

        self._attr_name = module.get(MOD_NAME) or f"Cube {self._lmp}"
        suffix = f"group_{self._lmp}" if is_group else self._lmp
        self._attr_unique_id = f"{entry.entry_id}_{suffix}"

        # Optimistischer Startzustand
        self._attr_is_on = False
        self._attr_brightness = 255
        self._attr_hs_color = (0.0, 0.0)

        if not is_group:
            self._attr_device_info = DeviceInfo(
                identifiers={(DOMAIN, self._lmp)},
                name=self._attr_name,
                manufacturer="Smart & Green / Linkio",
                model="Cube RGBW",
            )

    def _next_cmd_id(self) -> int:
        self._cmd_id = (self._cmd_id + 1) % 256
        return self._cmd_id or 1

    async def _gateway_mac(self) -> str | None:
        if self._is_group:
            for lmp in self._members:
                mac = _resolve_mac(self.hass, lmp)
                if mac:
                    return mac
            return None
        return _resolve_mac(self.hass, self._lmp)

    async def _send(self, onoff: bool, h: float, s: float, v: float) -> None:
        mac = await self._gateway_mac()
        if mac is None:
            _LOGGER.warning("Cube %s aktuell nicht per BLE erreichbar", self._lmp)
            raise RuntimeError(f"Cube {self._lmp} nicht erreichbar")

        payload = build_color_payload(
            self._index, onoff, h, s, v,
            is_group=self._is_group, class_id=self._class,
        )
        frame = build_frame(self._lmp, payload, self._key, self._nonce,
                            cmd_id=self._next_cmd_id())

        async with _lock_for(mac):
            ble_device = bluetooth.async_ble_device_from_address(
                self.hass, mac, connectable=True
            )
            if ble_device is None:
                raise RuntimeError(f"BLE-Gerät {mac} nicht verfügbar")
            client = await establish_connection(
                BleakClientWithServiceCache, ble_device, self._attr_name
            )
            try:
                try:
                    await client.write_gatt_char(CHAR_UUID, frame, response=False)
                except Exception:  # noqa: BLE001 - manche Proxys wollen "with response"
                    await client.write_gatt_char(CHAR_UUID, frame, response=True)
            finally:
                await client.disconnect()

    async def async_turn_on(self, **kwargs: Any) -> None:
        if ATTR_BRIGHTNESS in kwargs:
            self._attr_brightness = kwargs[ATTR_BRIGHTNESS]
        if ATTR_HS_COLOR in kwargs:
            self._attr_hs_color = kwargs[ATTR_HS_COLOR]
        h, s = self._attr_hs_color
        v = max(1, round((self._attr_brightness or 255) / 255 * 100))
        await self._send(True, h, s, v)
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        h, s = self._attr_hs_color
        v = max(1, round((self._attr_brightness or 255) / 255 * 100))
        await self._send(False, h, s, v)
        self._attr_is_on = False
        self.async_write_ha_state()
