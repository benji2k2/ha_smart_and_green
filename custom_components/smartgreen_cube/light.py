"""Light-Plattform für Smart & Green Cube."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from homeassistant.components import bluetooth
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_HS_COLOR,
    ColorMode,
    LightEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
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
from .lmp import build_color_payload, build_frame, temp_to_hs

_LOGGER = logging.getLogger(__name__)

# Verbindung nach dieser Zeit ohne Befehl schließen. Ein Verbindungsaufbau über
# einen ESPHome-Proxy dauert spürbar — deshalb halten wir sie kurz offen, statt
# pro Befehl neu zu verbinden (so macht es auch das funktionierende Testskript).
IDLE_DISCONNECT = 25.0

_LOCKS: dict[str, asyncio.Lock] = {}
_CLIENTS: dict[str, Any] = {}
_IDLE_TASKS: dict[str, asyncio.Task] = {}
# Einmal aufgelöste BLE-Adressen; Cubes werben nach dem Verbinden nicht weiter.
_MAC_CACHE: dict[str, str] = {}


def _lock_for(mac: str) -> asyncio.Lock:
    return _LOCKS.setdefault(mac, asyncio.Lock())


def _adv_name_for(lmp: str) -> str:
    """'13:40' -> 'bulb1340' (Advertising-Name der Cubes)."""
    return "bulb" + lmp.replace(":", "").lower()


def _resolve_mac(hass: HomeAssistant, lmp: str) -> str | None:
    """BLE-Adresse eines Moduls über Adv-Name / Hersteller-Daten finden."""
    if (cached := _MAC_CACHE.get(lmp)) is not None:
        return cached
    want_name = _adv_name_for(lmp)
    for si in bluetooth.async_discovered_service_info(hass, connectable=True):
        match = (si.name or "").lower() == want_name
        if not match:
            md = si.manufacturer_data.get(COMPANY_ID)
            if md and len(md) >= 4:
                match = "%02X:%02X" % (md[3], md[2]) == lmp.upper()
        if match:
            _MAC_CACHE[lmp] = si.address
            _LOGGER.debug("Cube %s -> BLE-Adresse %s", lmp, si.address)
            return si.address
    return None


def _discover_modules(hass: HomeAssistant) -> list[dict]:
    """Modulliste rein aus BLE-Advertisements (Fallback ohne .lap)."""
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


def _schedule_idle_disconnect(mac: str) -> None:
    """Trennt die Verbindung, wenn eine Weile kein Befehl mehr kam."""
    if (old := _IDLE_TASKS.pop(mac, None)) is not None:
        old.cancel()

    async def _close() -> None:
        try:
            await asyncio.sleep(IDLE_DISCONNECT)
        except asyncio.CancelledError:
            return
        client = _CLIENTS.pop(mac, None)
        if client is not None and client.is_connected:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    _IDLE_TASKS[mac] = asyncio.create_task(_close())


async def _drop_client(mac: str) -> None:
    client = _CLIENTS.pop(mac, None)
    if client is not None:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass


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
    """Eine Cube-Leuchte (oder die Gruppe 'Alle').

    Das Gerät kennt HSV plus einen Weiß-Kanal, den die Firmware aus der
    Sättigung ableitet: hohe Sättigung = Farbe, Sättigung 0 = (kaltes) Weiß.
    Warmweiß entsteht über einen warmen Farbton — deshalb Farbtemperatur
    zusätzlich als eigener Modus.
    """

    _attr_has_entity_name = False
    _attr_supported_color_modes = {ColorMode.HS, ColorMode.COLOR_TEMP}
    _attr_min_color_temp_kelvin = 2000
    _attr_max_color_temp_kelvin = 6500
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

        # Optimistischer Startzustand: warmweiß, volle Helligkeit.
        self._attr_is_on = False
        self._attr_brightness = 255
        self._attr_color_mode = ColorMode.COLOR_TEMP
        self._attr_color_temp_kelvin = 2700
        self._attr_hs_color = (30.0, 85.0)

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

    def _target_lmps(self) -> list[str]:
        return self._members if self._is_group else [self._lmp]

    def _build_frame(self) -> bytes:
        """Baut das LMP-Frame aus dem gewünschten Zustand."""
        if self._attr_color_mode == ColorMode.COLOR_TEMP:
            h, s = temp_to_hs(self._attr_color_temp_kelvin or 2700)
        else:
            h, s = self._attr_hs_color or (30.0, 85.0)

        bri = self._attr_brightness if self._attr_brightness is not None else 255
        v = bri / 255.0 * 100.0

        payload = build_color_payload(
            self._index, self._attr_is_on, h, s, v,
            is_group=self._is_group, class_id=self._class,
        )
        return build_frame(self._lmp, payload, self._key, self._nonce,
                           cmd_id=self._next_cmd_id())

    async def _write_once(self, mac: str, frame: bytes) -> None:
        """Schreibt ein Frame; hält die Verbindung für Folgebefehle offen."""
        client = _CLIENTS.get(mac)
        fresh = False
        if client is None or not client.is_connected:
            ble_device = bluetooth.async_ble_device_from_address(
                self.hass, mac, connectable=True
            )
            if ble_device is None:
                raise RuntimeError(f"BLE-Gerät {mac} derzeit nicht verfügbar")
            client = await establish_connection(
                BleakClientWithServiceCache, ble_device, self._attr_name
            )
            _CLIENTS[mac] = client
            fresh = True

        try:
            await client.write_gatt_char(CHAR_UUID, frame, response=False)
        except Exception:  # noqa: BLE001 — manche Proxys wollen "with response"
            await client.write_gatt_char(CHAR_UUID, frame, response=True)

        # Direkt nach einem frischen Verbindungsaufbau verschluckt das Modul
        # den ersten Write gelegentlich — dann einmal nachlegen.
        if fresh:
            await asyncio.sleep(0.12)
            try:
                await client.write_gatt_char(CHAR_UUID, frame, response=False)
            except Exception:  # noqa: BLE001
                pass

        _schedule_idle_disconnect(mac)

    async def _send(self) -> None:
        """Sendet den aktuellen Zustand; probiert alle erreichbaren Module."""
        frame = self._build_frame()
        _LOGGER.debug("%s: sende %s", self._attr_name, frame.hex(" "))

        last_err: Exception | None = None
        for lmp in self._target_lmps():
            mac = _resolve_mac(self.hass, lmp)
            if mac is None:
                _LOGGER.debug("%s: keine BLE-Adresse für %s", self._attr_name, lmp)
                continue
            async with _lock_for(mac):
                for attempt in (1, 2):
                    try:
                        await self._write_once(mac, frame)
                        return
                    except Exception as err:  # noqa: BLE001
                        last_err = err
                        _LOGGER.debug("%s: Versuch %d über %s fehlgeschlagen: %s",
                                      self._attr_name, attempt, mac, err)
                        await _drop_client(mac)
                        if attempt == 1:
                            _MAC_CACHE.pop(lmp, None)
                            if (mac2 := _resolve_mac(self.hass, lmp)) and mac2 != mac:
                                mac = mac2

        if last_err is not None:
            raise HomeAssistantError(
                f"{self._attr_name}: Befehl fehlgeschlagen ({last_err})"
            ) from last_err
        raise HomeAssistantError(
            f"{self._attr_name}: Cube nicht per Bluetooth erreichbar. "
            "Ist ein Bluetooth-Proxy in Reichweite?"
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        if ATTR_BRIGHTNESS in kwargs:
            self._attr_brightness = kwargs[ATTR_BRIGHTNESS]
        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            self._attr_color_temp_kelvin = kwargs[ATTR_COLOR_TEMP_KELVIN]
            self._attr_color_mode = ColorMode.COLOR_TEMP
        if ATTR_HS_COLOR in kwargs:
            self._attr_hs_color = kwargs[ATTR_HS_COLOR]
            self._attr_color_mode = ColorMode.HS
        if not self._attr_brightness:
            self._attr_brightness = 255
        self._attr_is_on = True
        await self._send()
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._attr_is_on = False
        await self._send()
        self.async_write_ha_state()
