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
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CHAR_UUID,
    COMPANY_ID,
    CONF_GROUP,
    CONF_KEY,
    CONF_MODULES,
    CONF_NONCE,
    DEFAULT_CLASS,
    MOD_CLASS,
    MOD_INDEX,
    MOD_LMP,
    MOD_NAME,
)
from .device import build_device_info
from .lmp import (
    ACK_ERRORS,
    build_color_payload,
    build_frame,
    parse_ack,
    temp_to_hs,
)

_LOGGER = logging.getLogger(__name__)

# Verbindung nach dieser Zeit ohne Befehl schließen. Ein Verbindungsaufbau über
# einen ESPHome-Proxy dauert spürbar, deshalb halten wir sie offen — im Feldtest
# hat sich das als zuverlässig erwiesen. Folgebefehle (Farb-/Helligkeitsregler)
# laufen so ohne erneuten Verbindungsaufbau.
IDLE_DISCONNECT = 20.0

# Der erste Verbindungsaufbau ist die wacklige Stelle: die Cubes werben nur
# periodisch, und über einen ESPHome-Proxy braucht der Aufbau ein paar Anläufe.
# Sobald die Verbindung steht, laufen Befehle zuverlässig.
DEVICE_WAIT_TRIES = 6      # Versuche, ein verbindbares Gerät zu bekommen
DEVICE_WAIT_DELAY = 1.5    # Sekunden zwischen den Versuchen
CONNECT_ATTEMPTS = 5       # Verbindungsversuche von bleak-retry-connector
SEND_ATTEMPTS = 3          # komplette Anläufe (inkl. Neuverbinden) pro Befehl
RETRY_BACKOFF = 0.4        # Sekunden Pause zwischen den Anläufen

# Ab dieser Signalstärke wird der Link unzuverlässig: die Verbindung kommt oft
# noch zustande, bricht dann aber während der Service-Discovery ab.
WEAK_RSSI = -75

# So lange warten wir auf die Quittung des Cubes. Am Geraet kam sie stets
# innerhalb von Millisekunden; grosszuegig gewaehlt fuer schwache Verbindungen.
ACK_TIMEOUT = 3.0

_LOCKS: dict[str, asyncio.Lock] = {}
# Offene Quittungen: mac -> cmd_id -> Future
_ACK_WAITERS: dict[str, dict[int, asyncio.Future]] = {}
# Auf welchen Verbindungen wir Quittungen tatsaechlich empfangen koennen
_ACK_ACTIVE: dict[str, bool] = {}
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


async def _release_other_clients(keep: str) -> None:
    """Trennt gehaltene Verbindungen zu *anderen* Cubes.

    Ein ESP32-Proxy hat nur wenige Verbindungsslots und muss sich die Funkzeit
    zwischen ihnen teilen. Halten wir Cube A noch 20 s offen und es kommt sofort
    ein Befehl für Cube B, wird dessen Verbindungsaufbau ausgebremst — genau das
    Bild "beim Wechsel dauert es lange, meist beim ersten Mal". Wir geben den
    Funk deshalb frei, bevor wir zum naechsten Cube wechseln.
    """
    for other in [m for m in _CLIENTS if m != keep]:
        if (task := _IDLE_TASKS.pop(other, None)) is not None:
            task.cancel()
        _forget_connection(other)
        client = _CLIENTS.pop(other, None)
        if client is not None and client.is_connected:
            _LOGGER.debug("Gebe Verbindung zu %s frei (Wechsel auf %s)", other, keep)
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass


def _schedule_idle_disconnect(mac: str) -> None:
    """Trennt die Verbindung, wenn eine Weile kein Befehl mehr kam."""
    if (old := _IDLE_TASKS.pop(mac, None)) is not None:
        old.cancel()

    async def _close() -> None:
        try:
            await asyncio.sleep(IDLE_DISCONNECT)
        except asyncio.CancelledError:
            return
        _forget_connection(mac)
        client = _CLIENTS.pop(mac, None)
        if client is not None and client.is_connected:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    _IDLE_TASKS[mac] = asyncio.create_task(_close())


def _forget_connection(mac: str) -> None:
    """Verwirft Quittungs-Zustand einer nicht mehr bestehenden Verbindung."""
    _ACK_ACTIVE.pop(mac, None)
    for waiter in _ACK_WAITERS.pop(mac, {}).values():
        if not waiter.done():
            waiter.cancel()


async def _drop_client(mac: str) -> None:
    _forget_connection(mac)
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
            self._attr_device_info = build_device_info(module)

    def _next_cmd_id(self) -> int:
        self._cmd_id = (self._cmd_id + 1) % 256
        return self._cmd_id or 1

    def _target_lmps(self) -> list[str]:
        return self._members if self._is_group else [self._lmp]

    def _build_frame(self) -> tuple[bytes, int]:
        """Baut das LMP-Frame aus dem gewünschten Zustand; liefert (Frame, cmd_id)."""
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
        # Gruppen-Broadcasts werden nicht quittiert — es gäbe keinen eindeutigen
        # Absender. Für einzelne Cubes lassen wir bestätigen.
        cmd_id = self._next_cmd_id()
        frame = build_frame(self._lmp, payload, self._key, self._nonce,
                            cmd_id=cmd_id, want_ack=not self._is_group)
        return frame, cmd_id

    async def _acquire_device(self, mac: str) -> Any:
        """Wartet geduldig auf ein verbindbares Gerät.

        Die Cubes werben nur periodisch. Direkt nach dem Start (oder wenn eine
        Weile kein Befehl kam) hat Home Assistant oft noch kein frisches
        Advertisement — dann gibt es kurzzeitig keinen verbindbaren Pfad. Statt
        sofort aufzugeben, warten wir ein paar Werbeintervalle ab.
        """
        for attempt in range(DEVICE_WAIT_TRIES):
            device = bluetooth.async_ble_device_from_address(
                self.hass, mac, connectable=True
            )
            if device is not None:
                return device
            if attempt == 0:
                _LOGGER.debug("%s: warte auf Advertisement von %s",
                              self._attr_name, mac)
            await asyncio.sleep(DEVICE_WAIT_DELAY)
        return None

    async def _write_once(self, mac: str, frame: bytes, cmd_id: int,
                          expect_ack: bool) -> None:
        """Schreibt ein Frame und wartet auf die Quittung des Cubes.

        Hält die Verbindung für Folgebefehle offen.
        """
        client = _CLIENTS.get(mac)
        fresh = False
        if client is None or not client.is_connected:
            await _release_other_clients(mac)
            ble_device = await self._acquire_device(mac)
            if ble_device is None:
                raise RuntimeError(
                    f"BLE-Gerät {mac} meldet sich nicht (kein Advertisement)"
                )
            client = await establish_connection(
                BleakClientWithServiceCache, ble_device, self._attr_name,
                max_attempts=CONNECT_ATTEMPTS,
            )
            _CLIENTS[mac] = client
            fresh = True
            await self._start_ack_listener(mac, client)

        waiter: asyncio.Future | None = None
        if expect_ack and _ACK_ACTIVE.get(mac):
            waiter = asyncio.get_running_loop().create_future()
            _ACK_WAITERS.setdefault(mac, {})[cmd_id] = waiter

        # "Write without response" ist fire-and-forget: der Proxy quittiert den
        # Aufruf sofort, auch wenn das Frame den Cube nie erreicht — ein Fehler
        # bleibt dann unsichtbar und wir melden faelschlich Erfolg. Wo die
        # Characteristic quittierte Writes unterstuetzt, nutzen wir die.
        char = client.services.get_characteristic(CHAR_UUID)
        acked = char is not None and "write" in getattr(char, "properties", ())
        target = char if char is not None else CHAR_UUID
        if fresh:
            _LOGGER.debug("%s: Write-Modus %s", self._attr_name,
                          "mit Quittung" if acked else "ohne Quittung")

        try:
            await client.write_gatt_char(target, frame, response=acked)

            # Direkt nach einem frischen Verbindungsaufbau verschluckt das
            # Modul den ersten Write gelegentlich — dann einmal nachlegen.
            # Die Wiederholung traegt dieselbe cmd_id, der Cube quittiert sie
            # also unter derselben Nummer.
            if fresh:
                await asyncio.sleep(0.12)
                try:
                    await client.write_gatt_char(target, frame, response=acked)
                except Exception:  # noqa: BLE001
                    pass

            if waiter is not None:
                try:
                    code = await asyncio.wait_for(waiter, ACK_TIMEOUT)
                except asyncio.TimeoutError as err:
                    raise RuntimeError(
                        "Cube hat den Befehl nicht bestätigt"
                    ) from err
                if code != 0:
                    raise RuntimeError(
                        f"Cube meldet Fehler {ACK_ERRORS.get(code, code)}"
                    )
        finally:
            if waiter is not None:
                _ACK_WAITERS.get(mac, {}).pop(cmd_id, None)

        _schedule_idle_disconnect(mac)

    async def _start_ack_listener(self, mac: str, client: Any) -> None:
        """Abonniert die Notify-Characteristic, um Quittungen zu empfangen.

        Schlaegt das fehl, laeuft alles weiter wie bisher — dann warten wir
        eben nicht auf eine Bestaetigung, statt den Befehl scheitern zu lassen.
        """
        key, nonce = self._key, self._nonce

        def _on_notify(_char: Any, data: bytearray) -> None:
            parsed = parse_ack(bytes(data), key, nonce)
            if parsed is None:
                return
            cmd_id, code = parsed
            waiter = _ACK_WAITERS.get(mac, {}).get(cmd_id)
            if waiter is not None and not waiter.done():
                waiter.set_result(code)

        try:
            await client.start_notify(CHAR_UUID, _on_notify)
            _ACK_ACTIVE[mac] = True
        except Exception as err:  # noqa: BLE001
            _ACK_ACTIVE[mac] = False
            _LOGGER.debug("%s: keine Quittungen verfügbar (%s)",
                          self._attr_name, err)

    def _log_link_quality(self, mac: str) -> None:
        """Protokolliert die Signalstärke und warnt bei schwachem Link.

        Ein Cube am Rand der Reichweite verbindet sich zwar noch, bricht aber
        gern mitten in der Service-Discovery oder beim Schreiben ab. Das sieht
        nach einem sporadischen Softwarefehler aus, ist aber Funkreichweite —
        deshalb steht der Wert im Log, statt dass man ihn suchen muss.
        """
        info = bluetooth.async_last_service_info(self.hass, mac, connectable=True)
        if info is None:
            return
        if info.rssi <= WEAK_RSSI:
            _LOGGER.warning(
                "%s: schwaches Signal (%d dBm über %s). Unter %d dBm brechen "
                "Verbindungen häufig ab — einen Bluetooth-Proxy näher stellen.",
                self._attr_name, info.rssi, info.source, WEAK_RSSI)
        else:
            _LOGGER.debug("%s: Signal %d dBm über %s",
                          self._attr_name, info.rssi, info.source)

    async def _send(self) -> None:
        """Sendet den aktuellen Zustand; probiert alle erreichbaren Module."""
        frame, cmd_id = self._build_frame()
        _LOGGER.debug("%s: sende %s", self._attr_name, frame.hex(" "))

        last_err: Exception | None = None
        for lmp in self._target_lmps():
            mac = _resolve_mac(self.hass, lmp)
            if mac is None:
                _LOGGER.warning("%s: keine BLE-Adresse für %s gefunden",
                                self._attr_name, lmp)
                continue
            self._log_link_quality(mac)
            async with _lock_for(mac):
                for attempt in range(1, SEND_ATTEMPTS + 1):
                    try:
                        await self._write_once(mac, frame, cmd_id,
                                              expect_ack=not self._is_group)
                        _LOGGER.debug("%s: Frame gesendet (Versuch %d, %s)",
                                      self._attr_name, attempt, mac)
                        return
                    except Exception as err:  # noqa: BLE001
                        last_err = err
                        _LOGGER.warning("%s: Versuch %d/%d über %s fehlgeschlagen: %s",
                                        self._attr_name, attempt, SEND_ATTEMPTS,
                                        mac, err)
                        await _drop_client(mac)
                        if attempt == 1:
                            _MAC_CACHE.pop(lmp, None)
                            if (mac2 := _resolve_mac(self.hass, lmp)) and mac2 != mac:
                                mac = mac2
                        if attempt < SEND_ATTEMPTS:
                            await asyncio.sleep(RETRY_BACKOFF)

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
