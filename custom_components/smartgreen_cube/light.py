"""Light platform for Smart & Green Cube."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from time import monotonic
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
from homeassistant.const import STATE_ON, STATE_UNAVAILABLE
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import (
    ExtraStoredData,
    RestoreEntity,
)

from .const import (
    CHAR_UUID,
    COMPANY_ID,
    CONF_GROUP,
    CONF_IDLE_DISCONNECT,
    CONF_KEY,
    CONF_MODULES,
    CONF_NONCE,
    DEFAULT_CLASS,
    DEFAULT_IDLE_DISCONNECT,
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

# How long a connection is kept open after a command lives in the entry
# options (DEFAULT_IDLE_DISCONNECT). It is a trade-off: a held connection makes
# the next command immediate, but a connected cube keeps its radio awake, while
# an idle one only advertises. Not unlimited by default, so the vendor app can
# still reach the cubes.

# A proxy cannot start a connection before it has heard an advertisement.
# The cubes advertise about every 11s, but a proxy with a small scan window
# misses most of them, which stretches a cold connect to half a minute. The
# patience below covers that comfortably; sending runs in the background, so
# the wait never blocks the UI.
DEVICE_WAIT_TRIES = 30     # attempts to obtain a connectable device
DEVICE_WAIT_DELAY = 2.0    # seconds between attempts
CONNECT_ATTEMPTS = 5       # connection attempts by bleak-retry-connector
SEND_ATTEMPTS = 3          # full attempts (including reconnect) per command
RETRY_BACKOFF = 0.4        # seconds to wait between attempts

# Below this signal strength the link becomes unreliable: the connection often
# still succeeds, but then drops during service discovery.
WEAK_RSSI = -75

# How long we wait for the cube's acknowledgement. On the device it always
# arrived within milliseconds; generous here for weak connections.
ACK_TIMEOUT = 3.0

# Group broadcasts are never acknowledged, so send them more than once.
GROUP_REPEATS = 3
GROUP_REPEAT_GAP = 0.2

# Only one connection may be built at a time. An ESP32 proxy has few slots and
# one radio; two entities connecting at once starved each other for over two
# minutes in the field. Serialising also means the second command usually finds
# the first one's link already open and relays through it — LMP is a mesh, so
# one connection serves every cube.
_CONNECT_LOCK = asyncio.Lock()

# How long a single connection attempt may take before we give up and retry.
# Without this a doomed attempt blocked for 127s in the field.
CONNECT_TIMEOUT = 45.0

_LOCKS: dict[str, asyncio.Lock] = {}
# Pending acknowledgements: mac -> cmd_id -> future
_ACK_WAITERS: dict[str, dict[int, asyncio.Future]] = {}
# Connections on which we can actually receive acknowledgements
_ACK_ACTIVE: dict[str, bool] = {}
_CLIENTS: dict[str, Any] = {}
_IDLE_TASKS: dict[str, asyncio.Task] = {}
# Resolved BLE addresses; cubes stop advertising once connected.
_MAC_CACHE: dict[str, str] = {}


def _lock_for(mac: str) -> asyncio.Lock:
    return _LOCKS.setdefault(mac, asyncio.Lock())


def _adv_name_for(lmp: str) -> str:
    """'13:40' -> 'bulb1340' (the cubes' advertising name)."""
    return "bulb" + lmp.replace(":", "").lower()


def _resolve_mac(hass: HomeAssistant, lmp: str) -> str | None:
    """Find a module's BLE address via advertising name or manufacturer data."""
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
    """Module list purely from BLE advertisements (fallback without .lap)."""
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


class _RelayAvailable(Exception):
    """Another cube's connection came up while we waited — use that instead."""


def _live_client_other_than(mac: str) -> str | None:
    for other, client in _CLIENTS.items():
        if other != mac and getattr(client, "is_connected", False):
            return other
    return None


async def _release_other_clients(keep: str) -> None:
    """Drop held connections to *other* cubes.

    An ESP32 proxy has only a few connection slots. If they are all taken the
    next connection attempt fails, and this makes room. It is called only
    *after* such a failure: releasing pre-emptively would only hurt when slots
    are free, because every new connection has to catch the cube's next
    advertisement.
    """
    for other in [m for m in _CLIENTS if m != keep]:
        # Never pull a connection out from under a send that is in flight —
        # that would fail someone else's command to free a slot for ours.
        lock = _LOCKS.get(other)
        if lock is not None and lock.locked():
            continue
        if (task := _IDLE_TASKS.pop(other, None)) is not None:
            task.cancel()
        _forget_connection(other)
        client = _CLIENTS.pop(other, None)
        if client is not None and client.is_connected:
            _LOGGER.debug("Releasing connection to %s (switching to %s)", other, keep)
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass


def _schedule_idle_disconnect(mac: str, timeout: float) -> None:
    """Close the connection once no command has arrived for a while."""
    if (old := _IDLE_TASKS.pop(mac, None)) is not None:
        old.cancel()

    async def _close() -> None:
        try:
            await asyncio.sleep(timeout)
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
    """Discard acknowledgement state of a connection that no longer exists.

    Pending waiters are resolved with ``None`` rather than cancelled.
    Cancelling would raise CancelledError inside the waiting ``_send``, which
    asyncio propagates as task cancellation — the send task would then die
    without logging anything or rolling back the optimistic state.
    """
    _ACK_ACTIVE.pop(mac, None)
    for waiter in _ACK_WAITERS.pop(mac, {}).values():
        if not waiter.done():
            waiter.set_result(None)


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
    """Create the cube lights."""
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


@dataclass
class StoredCubeState(ExtraStoredData):
    """The entity's own restore payload.

    Home Assistant strips colour attributes from a light's state while it is
    off, so restoring from the plain state loses the colour of a lamp that was
    switched off before the restart. Storing our own copy keeps it.
    """

    data: dict

    def as_dict(self) -> dict:
        return self.data


class SmartGreenCubeLight(LightEntity, RestoreEntity):
    """A single cube light, or the "all" group.

    The device speaks HSV plus a white channel that the firmware derives from
    saturation: high saturation means colour, saturation 0 means (cold) white.
    Warm white is produced through a warm hue, which is why colour temperature
    exists as a separate mode.
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
        self._send_task: asyncio.Task | None = None
        self._pending = False
        self._before_send: dict = {}
        self._idle_disconnect = float(entry.options.get(
            CONF_IDLE_DISCONNECT, DEFAULT_IDLE_DISCONNECT))

        self._attr_name = module.get(MOD_NAME) or f"Cube {self._lmp}"
        suffix = f"group_{self._lmp}" if is_group else self._lmp
        self._attr_unique_id = f"{entry.entry_id}_{suffix}"

        # Optimistic initial state: warm white at full brightness.
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
        """Cubes that can serve as an entry point for this entity's frame.

        A group broadcast to FF:FF reaches every cube through the mesh, so any
        member will do. Ordering is left to :meth:`_routes`, which puts open
        connections first.
        """
        return self._members if self._is_group else [self._lmp]

    def _build_frame(self) -> tuple[bytes, int]:
        """Build the LMP frame from the desired state; returns (frame, cmd_id)."""
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
        # Group broadcasts are not acknowledged — there would be no single
        # sender to answer. Individual cubes do acknowledge.
        cmd_id = self._next_cmd_id()
        frame = build_frame(self._lmp, payload, self._key, self._nonce,
                            cmd_id=cmd_id, want_ack=not self._is_group)
        return frame, cmd_id

    async def _acquire_device(self, mac: str) -> Any:
        """Wait patiently for a connectable device.

        The cubes advertise only periodically. Right after startup (or after an
        idle spell) Home Assistant often has no fresh advertisement, and then
        there is briefly no connectable path. Rather than giving up at once, we
        wait out an advertising interval.
        """
        for attempt in range(DEVICE_WAIT_TRIES):
            device = bluetooth.async_ble_device_from_address(
                self.hass, mac, connectable=True
            )
            if device is not None:
                return device
            if attempt == 0:
                _LOGGER.debug("%s: waiting for an advertisement from %s",
                              self._attr_name, mac)
            await asyncio.sleep(DEVICE_WAIT_DELAY)
        return None

    async def _connect(self, mac: str, ble_device: Any, waited: float) -> Any:
        """Open a connection, with a time limit and a slot-freeing fallback.

        Must be called while holding ``_CONNECT_LOCK``.
        """
        started = monotonic()
        try:
            client = await asyncio.wait_for(
                establish_connection(
                    BleakClientWithServiceCache, ble_device, self._attr_name,
                    max_attempts=CONNECT_ATTEMPTS,
                ),
                CONNECT_TIMEOUT,
            )
        except (Exception, asyncio.TimeoutError):  # noqa: BLE001
            # Most likely the proxy has no connection slot left. Only now do we
            # release another cube: doing it pre-emptively would drop a healthy
            # link that the mesh could have relayed through.
            if _live_client_other_than(mac) is None:
                raise
            _LOGGER.debug("%s: connection failed after %.1fs, releasing other "
                          "cubes", self._attr_name, monotonic() - started)
            await _release_other_clients(mac)
            client = await asyncio.wait_for(
                establish_connection(
                    BleakClientWithServiceCache, ble_device, self._attr_name,
                    max_attempts=CONNECT_ATTEMPTS,
                ),
                CONNECT_TIMEOUT,
            )

        _CLIENTS[mac] = client
        _LOGGER.debug(
            "%s: connected to %s — %.1fs waiting for a connectable device, "
            "%.1fs establishing the link",
            self._attr_name, mac, waited, monotonic() - started)
        return client

    async def _write_once(self, mac: str, frame: bytes, cmd_id: int,
                          expect_ack: bool) -> None:
        """Write a frame and wait for the cube to acknowledge it.

        Keeps the connection open for follow-up commands.
        """
        client = _CLIENTS.get(mac)
        fresh = False
        if client is None or not client.is_connected:
            # A cold connect is the slow case and it has two very different
            # causes: waiting for HA to offer a connectable device, or the
            # proxy taking its time to establish the link. Time them apart, or
            # the log only shows a long unexplained gap.
            started = monotonic()
            ble_device = await self._acquire_device(mac)
            waited = monotonic() - started
            if ble_device is None:
                raise RuntimeError(
                    f"BLE device {mac} is not responding "
                    f"(no advertisement after {waited:.1f}s)"
                )

            async with _CONNECT_LOCK:
                # Whoever held the lock may have finished in the meantime.
                existing = _CLIENTS.get(mac)
                if existing is not None and existing.is_connected:
                    client = existing
                elif (relay := _live_client_other_than(mac)) is not None:
                    _LOGGER.debug("%s: %s came up while waiting — relaying "
                                  "instead of opening a second connection",
                                  self._attr_name, relay)
                    raise _RelayAvailable
                else:
                    client = await self._connect(mac, ble_device, waited)
                    fresh = True
            if fresh:
                await self._start_ack_listener(mac, client)

        waiter: asyncio.Future | None = None

        if expect_ack and _ACK_ACTIVE.get(mac):
            waiter = asyncio.get_running_loop().create_future()
            _ACK_WAITERS.setdefault(mac, {})[cmd_id] = waiter

        # "Write without response" is fire-and-forget: the proxy acknowledges
        # the call immediately even if the frame never reaches the cube, so a
        # failure stays invisible and we would report false success. Where the
        # characteristic supports acknowledged writes, we use them.
        char = client.services.get_characteristic(CHAR_UUID)
        acked = char is not None and "write" in getattr(char, "properties", ())
        target = char if char is not None else CHAR_UUID
        if fresh:
            _LOGGER.debug("%s: write mode %s", self._attr_name,
                          "acknowledged" if acked else "unacknowledged")

        try:
            await client.write_gatt_char(target, frame, response=acked)

            # Right after a fresh connection the module occasionally swallows
            # the first write, so send it once more. The repeat carries the
            # same cmd_id, so the cube acknowledges it under the same number.
            if fresh:
                await asyncio.sleep(0.12)
                try:
                    await client.write_gatt_char(target, frame, response=acked)
                except Exception:  # noqa: BLE001
                    pass

            # Group broadcasts are not acknowledged (FF:FF has no single
            # sender to answer), so a lost packet goes unnoticed — in the field
            # a first attempt reached only one of two cubes. Hence sending
            # deliberately more than once.
            if not expect_ack:
                for _ in range(GROUP_REPEATS - 1):
                    await asyncio.sleep(GROUP_REPEAT_GAP)
                    try:
                        await client.write_gatt_char(target, frame,
                                                     response=acked)
                    except Exception:  # noqa: BLE001
                        break

            if waiter is not None:
                try:
                    code = await asyncio.wait_for(waiter, ACK_TIMEOUT)
                except asyncio.TimeoutError as err:
                    raise RuntimeError(
                        "cube did not acknowledge the command"
                    ) from err
                if code is None:
                    raise RuntimeError("connection closed before acknowledgement")
                if code != 0:
                    raise RuntimeError(
                        f"cube reports error {ACK_ERRORS.get(code, code)}"
                    )
        finally:
            if waiter is not None:
                _ACK_WAITERS.get(mac, {}).pop(cmd_id, None)

        _schedule_idle_disconnect(mac, self._idle_disconnect)

    async def _start_ack_listener(self, mac: str, client: Any) -> None:
        """Subscribe to the notify characteristic to receive acknowledgements.

        If that fails everything carries on as before: we simply do not wait
        for confirmation, rather than failing the command.
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
            _LOGGER.debug("%s: acknowledgements unavailable (%s)",
                          self._attr_name, err)

    def _log_link_quality(self, mac: str) -> None:
        """Log the signal strength and warn about a weak link.

        A cube at the edge of range still connects, but tends to drop in the
        middle of service discovery or while writing. That looks like a
        sporadic software fault when it is really radio range — so the value
        goes into the log instead of having to be hunted down.
        """
        info = bluetooth.async_last_service_info(self.hass, mac, connectable=True)
        if info is None:
            return
        if info.rssi <= WEAK_RSSI:
            _LOGGER.warning(
                "%s: weak signal (%d dBm via %s). Below %d dBm connections "
                "drop frequently — move a Bluetooth proxy closer.",
                self._attr_name, info.rssi, info.source, WEAK_RSSI)
        else:
            _LOGGER.debug("%s: signal %d dBm via %s",
                          self._attr_name, info.rssi, info.source)

    def _routes(self) -> list[tuple[str, str | None]]:
        """Connections to try, best first, as (mac, lmp_for_cache_reset).

        LMP is a mesh: the frame carries its destination address, so *any*
        connected cube can relay it to the target — that is exactly how the
        vendor app works (it holds one connection and addresses every module
        through it). An open connection is therefore worth far more than a
        matching one, because reaching a cube directly means waiting for the
        proxy to catch one of its advertisements first, which took half a
        minute in the field.

        A relay that does not reach the target simply goes unacknowledged, and
        we fall through to connecting to the cube itself.
        """
        routes: list[tuple[str, str | None]] = [
            (mac, None) for mac, client in _CLIENTS.items()
            if getattr(client, "is_connected", False)
        ]
        for lmp in self._target_lmps():
            mac = _resolve_mac(self.hass, lmp)
            if mac is None:
                # Not necessarily a problem: a cube that has not advertised
                # recently is still reachable through any open connection.
                _LOGGER.debug("%s: no BLE address known for %s",
                              self._attr_name, lmp)
                continue
            if not any(mac == known for known, _ in routes):
                routes.append((mac, lmp))
        return routes

    async def _send(self) -> None:
        """Send the current state over the best available route.

        Two passes: if another cube's connection came up while we waited for
        the connect lock, the routes are recomputed so the frame goes through
        that link instead of opening a second one.
        """
        frame, cmd_id = self._build_frame()
        _LOGGER.debug("%s: sending %s", self._attr_name, frame.hex(" "))

        last_err: Exception | None = None
        for _pass in range(2):
            relay_appeared = False
            for mac, lmp in self._routes():
                self._log_link_quality(mac)
                async with _lock_for(mac):
                    for attempt in range(1, SEND_ATTEMPTS + 1):
                        try:
                            started = monotonic()
                            await self._write_once(
                                mac, frame, cmd_id,
                                expect_ack=not self._is_group)
                            _LOGGER.debug("%s: frame sent (attempt %d, %s) "
                                          "in %.1fs", self._attr_name, attempt,
                                          mac, monotonic() - started)
                            return
                        except _RelayAvailable:
                            relay_appeared = True
                            break
                        except Exception as err:  # noqa: BLE001
                            last_err = err
                            _LOGGER.warning(
                                "%s: attempt %d/%d via %s failed: %s",
                                self._attr_name, attempt, SEND_ATTEMPTS,
                                mac, err)
                            await _drop_client(mac)
                            if attempt == 1 and lmp is not None:
                                _MAC_CACHE.pop(lmp, None)
                                mac2 = _resolve_mac(self.hass, lmp)
                                if mac2 and mac2 != mac:
                                    mac = mac2
                            if attempt < SEND_ATTEMPTS:
                                await asyncio.sleep(RETRY_BACKOFF)
                if relay_appeared:
                    break
            if not relay_appeared:
                break

        if last_err is not None:
            raise HomeAssistantError(
                f"{self._attr_name}: command failed ({last_err})"
            ) from last_err
        raise HomeAssistantError(
            f"{self._attr_name}: cube unreachable over Bluetooth. "
            "Is a Bluetooth proxy within range?"
        )

    async def async_added_to_hass(self) -> None:
        """Restore the last known state.

        The cubes cannot be read back (see README), so after a restart every
        lamp would otherwise show as "off" while actually still lit. Home
        Assistant's stored state is the only source we have.

        It stays an assumption: if someone switched the lamp through the vendor
        app or at the device in the meantime, it can be wrong. Still closer to
        reality than a blanket "off".
        """
        await super().async_added_to_hass()

        # Our own payload first: unlike the plain state it also survives being
        # switched off, which is exactly when the colour would be lost.
        stored = await self.async_get_last_extra_data()
        if stored is not None and self._apply_stored(stored.as_dict()):
            _LOGGER.debug("%s: state restored from stored data (%s)",
                          self._attr_name, self._attr_color_mode)
            return

        last = await self.async_get_last_state()
        if last is None or last.state == STATE_UNAVAILABLE:
            return

        self._attr_is_on = last.state == STATE_ON
        attrs = last.attributes
        # The stored state comes from HA's own storage and may originate from
        # an older version. Unusable values must not stop the entity from
        # starting — the defaults apply instead.
        try:
            if (bri := attrs.get(ATTR_BRIGHTNESS)) is not None:
                self._attr_brightness = int(bri)
            if (kelvin := attrs.get(ATTR_COLOR_TEMP_KELVIN)) is not None:
                self._attr_color_temp_kelvin = int(kelvin)
            if (hs := attrs.get(ATTR_HS_COLOR)) is not None and len(hs) == 2:
                self._attr_hs_color = (float(hs[0]), float(hs[1]))
            mode = attrs.get("color_mode")
            if mode in (ColorMode.HS, ColorMode.COLOR_TEMP):
                self._attr_color_mode = ColorMode(mode)
        except (TypeError, ValueError) as err:
            _LOGGER.debug("%s: stored values unusable (%s)",
                          self._attr_name, err)
        _LOGGER.debug("%s: state restored (%s, %s)",
                      self._attr_name, last.state, self._attr_color_mode)

    async def async_will_remove_from_hass(self) -> None:
        if self._send_task is not None and not self._send_task.done():
            self._send_task.cancel()

    async def async_turn_on(self, **kwargs: Any) -> None:
        previous = self._snapshot()
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
        self._schedule_send(previous)

    async def async_turn_off(self, **kwargs: Any) -> None:
        previous = self._snapshot()
        self._attr_is_on = False
        self._schedule_send(previous)

    # ------------------------------------------------------------------ Senden

    def _snapshot(self) -> dict:
        return {
            "is_on": self._attr_is_on,
            "brightness": self._attr_brightness,
            "color_mode": self._attr_color_mode,
            "color_temp_kelvin": self._attr_color_temp_kelvin,
            "hs_color": self._attr_hs_color,
        }

    @property
    def extra_restore_state_data(self) -> ExtraStoredData:
        """What Home Assistant should hand back after a restart."""
        snap = self._snapshot()
        mode = snap["color_mode"]
        hs = snap["hs_color"]
        return StoredCubeState({
            "is_on": snap["is_on"],
            "brightness": snap["brightness"],
            "color_mode": getattr(mode, "value", mode),
            "color_temp_kelvin": snap["color_temp_kelvin"],
            "hs_color": list(hs) if hs else None,
        })

    def _apply_stored(self, data: dict) -> bool:
        """Apply our own restore payload. Returns False if it is unusable."""
        try:
            self._attr_is_on = bool(data["is_on"])
            if (bri := data.get("brightness")) is not None:
                self._attr_brightness = int(bri)
            if (kelvin := data.get("color_temp_kelvin")) is not None:
                self._attr_color_temp_kelvin = int(kelvin)
            if (hs := data.get("hs_color")) and len(hs) == 2:
                self._attr_hs_color = (float(hs[0]), float(hs[1]))
            if (mode := data.get("color_mode")) in (
                ColorMode.HS.value, ColorMode.COLOR_TEMP.value,
            ):
                self._attr_color_mode = ColorMode(mode)
        except (KeyError, TypeError, ValueError) as err:
            _LOGGER.debug("%s: stored state unusable (%s)", self._attr_name, err)
            return False
        return True

    def _restore(self, snap: dict) -> None:
        self._attr_is_on = snap["is_on"]
        self._attr_brightness = snap["brightness"]
        self._attr_color_mode = snap["color_mode"]
        self._attr_color_temp_kelvin = snap["color_temp_kelvin"]
        self._attr_hs_color = snap["hs_color"]

    def _schedule_send(self, previous: dict) -> None:
        """Show the desired state at once and send in the background.

        A cold connection takes a long time with these lamps: no proxy can
        start one before it has caught an advertisement, and in the field that
        took half a minute. If the service call blocked for that long the UI
        would look as though nothing had happened — and you press again. So the
        display switches immediately; if the cube does not acknowledge the
        command, it is rolled back afterwards.
        """
        if self._send_task is None or self._send_task.done():
            self._before_send = previous
        self._pending = True
        self.async_write_ha_state()
        if self._send_task is None or self._send_task.done():
            self._send_task = self.hass.async_create_task(self._send_loop())

    async def _send_loop(self) -> None:
        """Keep sending until no newer request is outstanding."""
        while self._pending:
            self._pending = False
            try:
                await self._send()
            except Exception as err:  # noqa: BLE001
                if self._pending:
                    continue          # a newer request is already queued
                if isinstance(err, HomeAssistantError):
                    _LOGGER.warning("%s: %s — display rolled back",
                                    self._attr_name, err)
                else:
                    # Unexpected: log with a traceback so it can be fixed, but
                    # still roll back rather than leaving a wrong state behind.
                    _LOGGER.exception("%s: unexpected send failure",
                                      self._attr_name)
                self._restore(self._before_send)
                self.async_write_ha_state()
