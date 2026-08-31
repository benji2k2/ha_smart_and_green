"""Light entity behaviour: acknowledgements, routing, restore, background send."""
from __future__ import annotations

import asyncio

import conftest
from fake_cube import FakeCube

sg = conftest.load()
light = sg.light
lmp = sg.lmp
ColorMode = sg.light.ColorMode
HomeAssistantError = sg.light.HomeAssistantError

KEY = bytes(range(16))
NONCE = bytes(range(16, 32))
KEYSTREAM = lmp._keystream(KEY, NONCE)
MAC = "AA:BB:CC:DD:EE:FF"
OTHER_MAC = "11:22:33:44:55:66"


def reset_module_state() -> None:
    for store in (light._CLIENTS, light._ACK_WAITERS, light._ACK_ACTIVE,
                  light._MAC_CACHE, light._LOCKS, light._IDLE_TASKS):
        store.clear()
    # Home Assistant has a single event loop; the test runner gives each test
    # its own, so the module-level lock has to be rebuilt for the new one.
    light._CONNECT_LOCK = asyncio.Lock()


class FakeHass:
    def async_create_task(self, coro):
        return asyncio.get_event_loop().create_task(coro)


class Light(light.SmartGreenCubeLight):
    """The real entity with Home Assistant's plumbing replaced."""

    def __init__(self, lmp_addr: str = "13:40", is_group: bool = False,
                 members: list[str] | None = None) -> None:
        self.hass = FakeHass()
        self._key, self._nonce = KEY, NONCE
        self._lmp = lmp_addr
        self._index, self._class = 0, 19
        self._is_group = is_group
        self._members = members or []
        self._cmd_id = 0
        self._send_task = None
        self._pending = False
        self._before_send = {}
        self._idle_disconnect = 120.0
        self._confirm_wait = 0.0
        self._attr_name = "TestCube"
        self._attr_is_on = False
        self._attr_brightness = 255
        self._attr_color_mode = ColorMode.COLOR_TEMP
        self._attr_color_temp_kelvin = 2700
        self._attr_hs_color = (30.0, 85.0)
        self.state_writes = 0
        self._last_state = None
        self._last_extra = None

    def async_write_ha_state(self) -> None:
        self.state_writes += 1

    async def async_get_last_state(self):
        return self._last_state

    async def async_get_last_extra_data(self):
        return self._last_extra

    async def _acquire_device(self, mac):
        """Home Assistant is stubbed out, so hand back a placeholder at once."""
        return object()


async def write(entity: Light, cube: FakeCube, cmd_id: int = 1,
                expect_ack: bool = True, mac: str = MAC) -> None:
    """Run _write_once against an already-connected fake cube."""
    light._CLIENTS[mac] = cube
    await entity._start_ack_listener(mac, cube)
    frame, _ = entity._build_frame()
    frame = bytes([frame[0], cmd_id]) + frame[2:]
    await entity._write_once(mac, frame, cmd_id, expect_ack)


# --------------------------------------------------------------- acknowledgements

async def test_successful_acknowledgement_is_accepted():
    reset_module_state()
    cube = FakeCube(KEYSTREAM, code=0)
    await write(Light(), cube)
    assert len(cube.writes) == 1


async def test_error_code_is_reported_in_plain_words():
    reset_module_state()
    cube = FakeCube(KEYSTREAM, code=1)
    try:
        await write(Light(), cube)
    except RuntimeError as err:
        assert "NOT_SUPPORTED" in str(err), err
    else:
        raise AssertionError("an error code must not pass as success")


async def test_missing_acknowledgement_fails_so_the_retry_can_run():
    """A silent cube must not look like success — that was the original bug."""
    reset_module_state()
    light.ACK_TIMEOUT = 0.2
    cube = FakeCube(KEYSTREAM, answer=False)
    try:
        await write(Light(), cube)
    except RuntimeError as err:
        assert "acknowledge" in str(err), err
    else:
        raise AssertionError("a lost command must be noticed")
    finally:
        light.ACK_TIMEOUT = 3.0


async def test_closing_a_connection_does_not_kill_the_waiting_send():
    """Dropping a connection must fail the command, not cancel the task.

    Resolving pending waiters by cancelling them raised CancelledError inside
    the waiting send, which asyncio turns into task cancellation: the send
    would die without logging or rolling back.
    """
    reset_module_state()
    light.ACK_TIMEOUT = 5.0
    cube = FakeCube(KEYSTREAM, answer=False)
    entity = Light()
    light._CLIENTS[MAC] = cube
    await entity._start_ack_listener(MAC, cube)

    async def close_soon():
        await asyncio.sleep(0.05)
        light._forget_connection(MAC)

    task = asyncio.get_event_loop().create_task(close_soon())
    try:
        frame, _ = entity._build_frame()
        await entity._write_once(MAC, frame, frame[1], True)
    except RuntimeError as err:
        assert "closed" in str(err), err
    except asyncio.CancelledError:
        raise AssertionError("the send task must not be cancelled")
    else:
        raise AssertionError("a closed connection must fail the command")
    finally:
        await task
        light.ACK_TIMEOUT = 3.0


async def test_group_broadcast_is_repeated_and_never_awaits_an_ack():
    """FF:FF cannot be acknowledged, so the frame is sent more than once."""
    reset_module_state()
    light.UNVERIFIED_REPEAT_GAP = 0.01
    cube = FakeCube(KEYSTREAM, answer=False)
    await write(Light("FF:FF", is_group=True, members=["13:40"]), cube,
                expect_ack=False)
    assert len(cube.writes) == light.UNVERIFIED_REPEATS


async def test_write_falls_back_when_notifications_are_unavailable():
    """A refused subscription must not fail the command.

    The frame still goes out and is confirmed by the acknowledged GATT write;
    only the cube's own acknowledgement is missing, so later commands on this
    connection stay unverified rather than failing.
    """
    reset_module_state()
    cube = FakeCube(KEYSTREAM, notify_error=RuntimeError("no notify"))
    await write(Light(), cube)
    assert cube.writes, "the command must still be delivered"
    assert light._ACK_ACTIVE.get(MAC) is False


# ------------------------------------------------------------------- mesh routing

async def test_route_prefers_an_open_connection_to_any_cube():
    """LMP is a mesh: an open connection to one cube reaches the other.

    Connecting to the target directly costs a full advertising interval
    (~50s), so any live connection is the better route.
    """
    reset_module_state()
    entity = Light("13:40")
    light._MAC_CACHE.update({"13:40": MAC, "41:E0": OTHER_MAC})

    assert entity._routes() == [(MAC, "13:40")], "no connection: go direct"

    light._CLIENTS[OTHER_MAC] = FakeCube(KEYSTREAM)
    assert entity._routes() == [(OTHER_MAC, None), (MAC, "13:40")], \
        "relay through the connected cube first, direct as fallback"


async def test_route_does_not_list_the_same_connection_twice():
    reset_module_state()
    entity = Light("13:40")
    light._MAC_CACHE["13:40"] = MAC
    light._CLIENTS[MAC] = FakeCube(KEYSTREAM)
    assert entity._routes() == [(MAC, None)]


async def test_disconnected_clients_are_not_routes():
    reset_module_state()
    entity = Light("13:40")
    light._MAC_CACHE["13:40"] = MAC
    stale = FakeCube(KEYSTREAM)
    stale.is_connected = False
    light._CLIENTS[OTHER_MAC] = stale
    assert entity._routes() == [(MAC, "13:40")]


async def test_a_busy_connection_is_never_taken_away():
    """Freeing a proxy slot must not break someone else's command in flight."""
    reset_module_state()
    light._CLIENTS[OTHER_MAC] = FakeCube(KEYSTREAM)
    busy = light._lock_for(OTHER_MAC)
    await busy.acquire()
    try:
        await light._release_other_clients(MAC)
        assert OTHER_MAC in light._CLIENTS, "a busy connection must be left alone"
    finally:
        busy.release()

    await light._release_other_clients(MAC)
    assert OTHER_MAC not in light._CLIENTS, "an idle one may be released"


# --------------------------------------------------------------- background send

async def test_a_quick_command_is_confirmed_before_the_state_is_shown():
    """Most commands finish in seconds, so wait and show the real outcome."""
    reset_module_state()
    entity = Light()
    entity._confirm_wait = 5.0
    sends = []

    async def quick_send():
        await asyncio.sleep(0.02)
        sends.append(1)

    entity._send = quick_send
    previous = entity._snapshot()
    entity._attr_is_on = True
    await entity._apply(previous)

    assert sends == [1], "the send must have completed before returning"
    assert entity._attr_is_on is True
    assert entity.state_writes == 1


async def test_a_slow_command_stops_blocking_and_shows_the_wish():
    """A cold connect can take twenty seconds; the UI must not look dead."""
    reset_module_state()
    entity = Light()
    entity._confirm_wait = 0.05
    finished = []

    async def slow_send():
        await asyncio.sleep(0.3)
        finished.append(1)

    entity._send = slow_send
    previous = entity._snapshot()
    entity._attr_is_on = True

    started = asyncio.get_event_loop().time()
    await entity._apply(previous)
    elapsed = asyncio.get_event_loop().time() - started

    assert elapsed < 0.25, f"blocked for {elapsed:.2f}s despite the limit"
    assert entity._attr_is_on is True, "show the requested state meanwhile"
    assert finished == [], "the send is still running"
    await entity._send_task
    assert finished == [1], "and it must not have been cancelled"


async def test_confirm_wait_of_zero_is_fully_optimistic():
    """0 restores the old behaviour: show at once, never block."""
    reset_module_state()
    entity = Light()
    entity._confirm_wait = 0.0

    async def slow_send():
        await asyncio.sleep(0.3)

    entity._send = slow_send
    previous = entity._snapshot()
    entity._attr_is_on = True
    started = asyncio.get_event_loop().time()
    await entity._apply(previous)
    assert asyncio.get_event_loop().time() - started < 0.1
    assert entity.state_writes == 1
    await entity._send_task


async def test_rapid_changes_are_coalesced():
    """Dragging a slider must not queue one connection per step."""
    reset_module_state()
    entity = Light()
    sends = []

    async def record():
        await asyncio.sleep(0.02)
        sends.append(entity._attr_brightness)

    entity._send = record
    previous = entity._snapshot()
    for brightness in (50, 100, 150, 200):
        entity._attr_is_on = True
        entity._attr_brightness = brightness
        entity._schedule_send(previous)
    await entity._send_task

    assert len(sends) <= 2, f"{len(sends)} sends for four changes"
    assert sends[-1] == 200, "the last value wins"


async def test_failure_rolls_the_display_back():
    reset_module_state()
    entity = Light()

    async def fail():
        raise HomeAssistantError("cube did not acknowledge the command")

    entity._send = fail
    entity._attr_brightness = 10
    previous = entity._snapshot()
    entity._attr_is_on = True
    entity._attr_brightness = 250
    entity._schedule_send(previous)
    await entity._send_task

    assert entity._attr_is_on is False, "display must not claim success"
    assert entity._attr_brightness == 10, "restore the state before the burst"


async def test_unexpected_errors_also_roll_back():
    reset_module_state()
    entity = Light()

    async def boom():
        raise ValueError("something unforeseen")

    entity._send = boom
    previous = entity._snapshot()
    entity._attr_is_on = True
    entity._schedule_send(previous)
    await entity._send_task
    assert entity._attr_is_on is False


# ------------------------------------------------------------------- restore

class StoredData:
    def __init__(self, data):
        self._data = data

    def as_dict(self):
        return self._data


class LastState:
    def __init__(self, state, attributes):
        self.state = state
        self.attributes = attributes


async def test_colour_survives_a_restart_while_switched_off():
    """Regression: red, switched off, restart — the colour came back white.

    Home Assistant strips colour attributes from a light's state while it is
    off, so restoring from the plain state loses them exactly then. The entity
    stores its own copy.
    """
    reset_module_state()
    entity = Light()
    entity._attr_is_on = True
    entity._attr_color_mode = ColorMode.HS
    entity._attr_hs_color = (0.0, 100.0)          # red
    entity._attr_brightness = 200
    stored = entity.extra_restore_state_data.as_dict()

    entity._attr_is_on = False                     # switch off, then "restart"
    stored_off = entity.extra_restore_state_data.as_dict()

    restored = Light()
    restored._last_extra = StoredData(stored_off)
    await restored.async_added_to_hass()

    assert restored._attr_is_on is False
    assert restored._attr_color_mode == ColorMode.HS, "colour mode lost"
    assert restored._attr_hs_color == (0.0, 100.0), "colour lost"
    assert restored._attr_brightness == 200
    assert stored["hs_color"] == [0.0, 100.0]


async def test_restore_falls_back_to_the_plain_state():
    """Entries stored before the extra payload existed still restore."""
    reset_module_state()
    entity = Light()
    entity._last_state = LastState("on", {
        "brightness": 128, "color_temp_kelvin": 2200, "color_mode": "color_temp",
    })
    await entity.async_added_to_hass()
    assert entity._attr_is_on is True
    assert entity._attr_brightness == 128
    assert entity._attr_color_temp_kelvin == 2200


async def test_restore_survives_unusable_stored_values():
    """Corrupt storage must not stop the entity from starting."""
    reset_module_state()
    entity = Light()
    entity._last_extra = StoredData({"is_on": True, "brightness": "nonsense"})
    await entity.async_added_to_hass()
    assert entity._attr_brightness == 255, "fall back to the default"

    other = Light()
    other._last_state = LastState("on", {"hs_color": "broken", "brightness": "x"})
    await other.async_added_to_hass()
    assert other._attr_brightness == 255


async def test_nothing_stored_keeps_the_defaults():
    reset_module_state()
    entity = Light()
    await entity.async_added_to_hass()
    assert entity._attr_is_on is False

    unavailable = Light()
    unavailable._last_state = LastState("unavailable", {})
    unavailable._attr_brightness = 77
    await unavailable.async_added_to_hass()
    assert unavailable._attr_brightness == 77


# ------------------------------------------------------- module property flags

async def test_property_flags_come_from_the_configuration():
    """Zero radio cost: the values are read from the imported .lap file."""
    binary_sensor = sg.binary_sensor
    module = {
        "lmp_addr": "13:40",
        "name": "CubeSmall",
        "properties": {"led_status": True, "key_lock": True, "deep_sleep": False},
    }
    entry = type("Entry", (), {"entry_id": "abc", "data": {"modules": [module]}})()

    created = []
    await binary_sensor.async_setup_entry(None, entry, created.extend)

    assert len(created) == 3
    by_key = {e._attr_translation_key: e for e in created}
    assert by_key["led_status"].is_on is True
    assert by_key["deep_sleep"].is_on is False
    assert "snapshot" in by_key["key_lock"].extra_state_attributes["source"], \
        "the value must be labelled as a snapshot, not passed off as live"


async def test_property_flags_are_skipped_when_unknown():
    """Configurations without these fields must not produce empty entities."""
    binary_sensor = sg.binary_sensor
    module = {"lmp_addr": "13:40", "name": "CubeSmall",
              "properties": {"led_status": None, "key_lock": None,
                             "deep_sleep": None}}
    entry = type("Entry", (), {"entry_id": "abc", "data": {"modules": [module]}})()
    created = []
    await binary_sensor.async_setup_entry(None, entry, created.extend)
    assert created == []

    no_props = {"lmp_addr": "13:40", "name": "CubeSmall"}
    entry2 = type("Entry", (), {"entry_id": "a", "data": {"modules": [no_props]}})()
    created2 = []
    await binary_sensor.async_setup_entry(None, entry2, created2.extend)
    assert created2 == []


async def test_diagnostics_never_open_a_connection():
    """The whole point: diagnostics must cost no battery.

    Only the command path may connect. If a future change makes a sensor
    connect, this fails.
    """
    component = conftest.COMPONENT
    for name in ("sensor.py", "binary_sensor.py"):
        source = (component / name).read_text()
        for forbidden in ("establish_connection", "BleakClient", "write_gatt_char",
                          "start_notify"):
            assert forbidden not in source, f"{name} must not use {forbidden}"


# ------------------------------------------------------- configurable hold time

async def test_hold_time_comes_from_the_entry_options():
    """The connection hold time is the user's latency/battery trade-off."""
    const = sg.const

    class Entry:
        entry_id = "abc"
        def __init__(self, options): self.options = options

    module = {"lmp_addr": "13:40", "name": "CubeSmall", "index": 0, "class": 19}

    default = light.SmartGreenCubeLight(
        FakeHass(), Entry({}), KEY, NONCE, module)
    assert default._idle_disconnect == const.DEFAULT_IDLE_DISCONNECT == 120

    custom = light.SmartGreenCubeLight(
        FakeHass(), Entry({const.CONF_IDLE_DISCONNECT: 15}), KEY, NONCE, module)
    assert custom._idle_disconnect == 15.0

    immediate = light.SmartGreenCubeLight(
        FakeHass(), Entry({const.CONF_IDLE_DISCONNECT: 0}), KEY, NONCE, module)
    assert immediate._idle_disconnect == 0.0, "0 must mean disconnect at once"


async def test_hold_time_is_actually_applied():
    """A short hold really closes the connection; the entity does not ignore it."""
    reset_module_state()
    cube = FakeCube(KEYSTREAM, code=0)
    light._CLIENTS[MAC] = cube
    light._schedule_idle_disconnect(MAC, 0.05)
    await asyncio.sleep(0.15)
    assert cube.disconnected, "the connection should have been closed by now"
    assert MAC not in light._CLIENTS


# --------------------------------------------- competing connection attempts

async def test_second_cube_relays_instead_of_connecting_in_parallel():
    """Regression: two cubes starved each other for over two minutes.

    Field log: CubeLarge spent 127s failing to connect while CubeSmall held the
    proxy's slot; only after evicting CubeSmall did it succeed, in 15s. Since
    LMP is a mesh, the second command should have waited briefly and then gone
    through the first cube's link instead of competing for a second one.
    """
    reset_module_state()
    light._MAC_CACHE.update({"13:40": MAC, "41:E0": OTHER_MAC})

    # CubeLarge holds the connect lock, mimicking a slow connect that
    # eventually succeeds and registers its client.
    entity = Light("13:40")

    async def slow_connect():
        async with light._CONNECT_LOCK:
            await asyncio.sleep(0.05)
            light._CLIENTS[OTHER_MAC] = FakeCube(KEYSTREAM)

    holder = asyncio.get_event_loop().create_task(slow_connect())
    await asyncio.sleep(0)          # let it take the lock

    # CubeSmall now wants its own connection; it must yield to the relay.
    frame, cmd_id = entity._build_frame()
    try:
        await entity._write_once(MAC, frame, cmd_id, expect_ack=False)
    except light._RelayAvailable:
        pass
    else:
        raise AssertionError("should have deferred to the existing connection")
    finally:
        await holder

    assert MAC not in light._CLIENTS, "no second connection may be opened"


async def test_connect_lock_serialises_attempts():
    """Only one connection may be built at a time."""
    reset_module_state()
    order = []

    async def worker(name):
        async with light._CONNECT_LOCK:
            order.append(f"{name}-start")
            await asyncio.sleep(0.02)
            order.append(f"{name}-end")

    await asyncio.gather(worker("a"), worker("b"))
    assert order in (["a-start", "a-end", "b-start", "b-end"],
                     ["b-start", "b-end", "a-start", "a-end"]), order


async def test_connect_has_a_time_limit():
    """A doomed attempt must not block for minutes; it blocked 127s in the field."""
    assert light.CONNECT_TIMEOUT <= 60, "a stuck connect has to be cut short"

    reset_module_state()
    entity = Light("13:40")
    light.CONNECT_TIMEOUT = 0.05

    async def never_connects(*args, **kwargs):
        await asyncio.sleep(10)

    original = light.establish_connection
    light.establish_connection = never_connects
    try:
        async with light._CONNECT_LOCK:
            pass
        started = asyncio.get_event_loop().time()
        try:
            await entity._connect(MAC, object(), 0.0)
        except (asyncio.TimeoutError, Exception):
            pass
        elapsed = asyncio.get_event_loop().time() - started
        assert elapsed < 1.0, f"gave up only after {elapsed:.1f}s"
    finally:
        light.establish_connection = original
        light.CONNECT_TIMEOUT = 45.0


async def test_weak_signal_is_not_warned_about_on_every_command():
    """A burst of commands produced a wall of identical warnings in the field."""
    reset_module_state()
    light._WEAK_WARNED.clear()

    warnings = []
    original = light._LOGGER.warning
    light._LOGGER.warning = lambda *a, **k: warnings.append(a)

    class Info:
        rssi = -89
        source = "proxy"

    entity = Light()
    entity.hass = FakeHass()
    bluetooth = sg.light.bluetooth
    original_info = bluetooth.async_last_service_info
    bluetooth.async_last_service_info = lambda *a, **k: Info()
    try:
        for _ in range(5):
            entity._log_link_quality(MAC)
        assert len(warnings) == 1, f"{len(warnings)} warnings for one weak link"

        # A recovered link re-arms the warning.
        Info.rssi = -60
        entity._log_link_quality(MAC)
        Info.rssi = -89
        entity._log_link_quality(MAC)
        assert len(warnings) == 2, "should warn again after the link recovered"
    finally:
        light._LOGGER.warning = original
        bluetooth.async_last_service_info = original_info
        light._WEAK_WARNED.clear()


# ------------------------------------------------- stale cache and slow notify

async def test_slow_notify_does_not_hold_up_the_command():
    """Regression: subscribing hung 16s, then failed, delaying the write.

    Acknowledgements are a nice-to-have. Without them we still send, we just
    cannot confirm — so the subscription must not block the command.
    """
    reset_module_state()
    _notify_limit = light.NOTIFY_TIMEOUT
    light.NOTIFY_TIMEOUT = 0.05

    class SlowNotify(FakeCube):
        async def start_notify(self, uuid, callback):
            await asyncio.sleep(5)

    cube = SlowNotify(KEYSTREAM)
    entity = Light()
    started = asyncio.get_event_loop().time()
    try:
        outcome = await entity._start_ack_listener(MAC, cube)
        elapsed = asyncio.get_event_loop().time() - started
        assert elapsed < 1.0, f"subscription blocked for {elapsed:.1f}s"
        assert outcome == "timeout"
        assert light._ACK_ACTIVE.get(MAC) is False
    finally:
        light.NOTIFY_TIMEOUT = _notify_limit


async def test_stale_cache_errors_are_recognised():
    """These strings are what the device actually returned in the field."""
    real_errors = [
        "Characteristic 00005002-0000-1000-8000-00805f9b34fb was not found!",
        "BluetoothGATTErrorResponse: Insufficient authorization (8)",
        "Bluetooth GATT Error address=FB:3E handle=17 error=133",
    ]
    for text in real_errors:
        assert light._looks_like_stale_cache(Exception(text)), text

    # A plain connection problem is not a cache problem.
    assert not light._looks_like_stale_cache(Exception("timed out"))
    assert not light._looks_like_stale_cache(Exception("no advertisement"))


async def test_cache_is_cleared_only_when_it_looks_stale():
    reset_module_state()

    class Cube(FakeCube):
        def __init__(self):
            super().__init__(KEYSTREAM)
            self.cleared = False

        async def clear_cache(self):
            self.cleared = True

    stale = Cube()
    light._CLIENTS[MAC] = stale
    await light._drop_client(MAC, clear_cache=True)
    assert stale.cleared and stale.disconnected

    ordinary = Cube()
    light._CLIENTS[MAC] = ordinary
    await light._drop_client(MAC, clear_cache=False)
    assert not ordinary.cleared, "a healthy cache must be kept"
    assert ordinary.disconnected


async def test_subscription_result_is_reported():
    """Three outcomes, because the caller reacts differently to each."""
    reset_module_state()
    entity = Light()

    good = FakeCube(KEYSTREAM)
    assert await entity._start_ack_listener(MAC, good) == "ok"
    assert light._ACK_ACTIVE[MAC] is True

    bad = FakeCube(KEYSTREAM, notify_error=RuntimeError("nope"))
    assert (await entity._start_ack_listener(OTHER_MAC, bad)).startswith("refused")
    assert light._ACK_ACTIVE[OTHER_MAC] is False

    class Slow(FakeCube):
        async def start_notify(self, uuid, callback):
            await asyncio.sleep(5)

    _notify_limit = light.NOTIFY_TIMEOUT
    light.NOTIFY_TIMEOUT = 0.05
    try:
        assert await entity._start_ack_listener("CC", Slow(KEYSTREAM)) == "timeout"
    finally:
        light.NOTIFY_TIMEOUT = _notify_limit


async def test_a_timeout_never_discards_the_gatt_cache():
    """Rediscovery is expensive; only wrong handles justify it."""
    assert not light._looks_like_stale_cache(
        RuntimeError("subscription timed out after 5s"))
    assert not light._looks_like_stale_cache(asyncio.TimeoutError())
    assert light._looks_like_stale_cache(
        RuntimeError("subscription refused: Insufficient authorization (8)"))


async def test_an_unacknowledged_write_is_repeated():
    """A single cube whose subscription failed had no safety net at all.

    An acknowledged GATT write confirms arrival by itself, so a single cube
    needs no repeat. Where the characteristic offers no acknowledged write,
    nothing confirms anything and the frame is sent again.
    """
    reset_module_state()
    light.UNVERIFIED_REPEAT_GAP = 0.01
    _notify_limit = light.NOTIFY_TIMEOUT
    light.NOTIFY_TIMEOUT = 0.05

    class NoAckWrites(FakeCube):
        """A characteristic that only offers write-without-response."""

        def __init__(self, ks):
            super().__init__(ks)

            class Char:
                uuid = light.CHAR_UUID
                properties = ("write-without-response", "notify")

            class Services:
                def get_characteristic(self, _uuid):
                    return Char()

            self.services = Services()

    cube = NoAckWrites(KEYSTREAM)

    async def connect(mac, device, waited):
        light._CLIENTS[mac] = cube
        return cube

    entity = Light()          # a single cube, not a group
    entity._connect = connect
    frame, cmd_id = entity._build_frame()
    try:
        await entity._write_once(MAC, frame, cmd_id, expect_ack=True)
        assert len(cube.writes) == light.UNVERIFIED_REPEATS, \
            f"{len(cube.writes)} writes where nothing confirms arrival"
    finally:
        light.NOTIFY_TIMEOUT = _notify_limit
        light.UNVERIFIED_REPEAT_GAP = 0.2


async def test_a_verified_command_is_sent_once():
    """Verification makes repeats pointless — do not double the traffic."""
    reset_module_state()
    cube = FakeCube(KEYSTREAM, code=0)
    light._CLIENTS[MAC] = cube
    entity = Light()
    await entity._start_ack_listener(MAC, cube)
    frame, cmd_id = entity._build_frame()
    await entity._write_once(MAC, frame, cmd_id, expect_ack=True)
    assert len(cube.writes) == 1, f"{len(cube.writes)} writes despite an ack"


async def test_the_subscription_limit_stays_short():
    """It succeeds in under a second or not at all; waiting longer is pure loss.

    Measured successes: 0.41s, 0.48s, 0.51s. Failures ran to whatever limit was
    configured, so every second of limit is time lost on the failure path and
    time not needed on the success path.
    """
    assert light.NOTIFY_TIMEOUT <= 2.0, (
        "a longer limit only delays the reconnect that actually works")


# ----------------------------------------------------- waking a dozing cube

async def test_a_fresh_connection_gets_the_command_before_the_subscription():
    """Subscribing first fails between 20% and 80% of the time, costing ~9s.

    Writing first was observed to deliver immediately where subscribing would
    not have. The command is still verified one layer lower: an acknowledged
    GATT write proves the frame arrived. Only the cube's own acknowledgement
    for this one frame is given up, and the subscription that follows covers
    every command after it.
    """
    reset_module_state()
    order = []

    class Cube(FakeCube):
        async def start_notify(self, uuid, callback):
            order.append("subscribe")
            await super().start_notify(uuid, callback)

        async def write_gatt_char(self, char, frame, response=False):
            order.append("write")
            await super().write_gatt_char(char, frame, response=response)

    cube = Cube(KEYSTREAM)

    async def connect(mac, device, waited):
        light._CLIENTS[mac] = cube
        return cube

    entity = Light()
    entity._connect = connect
    frame, cmd_id = entity._build_frame()

    await entity._write_once(MAC, frame, cmd_id, expect_ack=False)

    assert order[0] == "write", f"the command must go first, got {order}"
    assert "subscribe" in order, "and the subscription must still happen after"


async def test_a_reused_connection_still_verifies():
    """An established connection is already subscribed, so nothing is lost."""
    reset_module_state()
    order = []

    class Cube(FakeCube):
        async def start_notify(self, uuid, callback):
            order.append("subscribe")
            await super().start_notify(uuid, callback)

        async def write_gatt_char(self, char, frame, response=False):
            order.append("write")
            await super().write_gatt_char(char, frame, response=response)

    cube = Cube(KEYSTREAM)

    async def connect(mac, device, waited):
        light._CLIENTS[mac] = cube
        return cube

    entity = Light()
    entity._connect = connect
    frame, cmd_id = entity._build_frame()

    # First command opens the connection: write, then subscribe.
    await entity._write_once(MAC, frame, cmd_id, expect_ack=True)
    assert order[:2] == ["write", "subscribe"], order

    # The next one rides the open connection and is acknowledged by the cube.
    order.clear()
    frame2, cmd_id2 = entity._build_frame()
    await entity._write_once(MAC, frame2, cmd_id2, expect_ack=True)
    assert order == ["write"], f"already subscribed, got {order}"


async def test_a_hanging_write_is_cut_short():
    """One write hung for 16.1s before failing; successful ones take under 1s.

    Measured together with the subscription that follows: 0.32-0.76s. Every
    second of limit beyond that is spent not retrying, and the retry is what
    works.
    """
    assert light.WRITE_TIMEOUT <= 3, "a stuck write must not block the retry"

    reset_module_state()
    light.WRITE_TIMEOUT = 0.05

    class Hangs(FakeCube):
        async def write_gatt_char(self, char, frame, response=False):
            await asyncio.sleep(10)

    cube = Hangs(KEYSTREAM)
    light._CLIENTS[MAC] = cube
    entity = Light()
    frame, cmd_id = entity._build_frame()
    started = asyncio.get_event_loop().time()
    try:
        await entity._write_once(MAC, frame, cmd_id, expect_ack=False)
    except (asyncio.TimeoutError, Exception):
        pass
    elapsed = asyncio.get_event_loop().time() - started
    light.WRITE_TIMEOUT = 2.5
    assert elapsed < 1.0, f"gave up only after {elapsed:.1f}s"
