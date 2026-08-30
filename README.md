<p align="center">
  <img src="custom_components/smartgreen_cube/brand/icon.png" width="128" alt="Smart & Green Cube" />
</p>

<h1 align="center">Smart &amp; Green Cube — Home Assistant Integration</h1>

<p align="center">
  Local control of <b>Smart &amp; Green "Cube"</b> lamps (BLE RGBW, Linkio LMP)
  straight from Home Assistant — no cloud, no vendor gateway.
</p>

<p align="center">
  <img alt="HACS Custom" src="https://img.shields.io/badge/HACS-Custom-41BDF5.svg">
  <img alt="Home Assistant 2024.4+" src="https://img.shields.io/badge/Home%20Assistant-2024.4%2B-41BDF5.svg">
  <img alt="License: MIT" src="https://img.shields.io/badge/License-MIT-green.svg">
</p>

<p align="center"><a href="README.de.md">Deutsche Fassung</a></p>

---

This integration speaks **LMP (Linkio Mesh Protocol)**, the proprietary protocol
behind the "Smart & Green – Mesh" app, directly over BLE. It reads the required
mesh keys **once, from the app's encrypted export (`.lap`)** — no fiddling with
hex strings. Control runs through the HA host's Bluetooth adapter or an
**ESPHome Bluetooth proxy** within radio range of the cubes.

> Reverse-engineered for private interoperability with hardware I own.
> Not an official product of Smart & Green / Linkio. All trademarks belong to
> their respective owners.

## Features

- One `light` entity per cube: **on/off, brightness, colour (HS), colour temperature**
- **Acknowledged commands** — the cube confirms every switch; lost commands are
  detected and retried
- **State survives restarts** (stored state instead of "everything off")
- Optional **"All" group entity** — one broadcast reaches every cube through the
  mesh and needs only *one* connection, so it is markedly faster than switching
  each cube in turn
- **Diagnostic sensors** per cube: signal strength, last seen, proxy in use,
  plus the module flags (status LED, key lock, deep sleep) from the imported
  configuration — all read without ever opening a connection
- Transparent use of existing **ESPHome Bluetooth proxies**
- **Auto-discovery** via BLE advertisement (company id `0x04AA`)
- Key import straight from the app's `.lap` export

## Why the first press takes a while

Measured with the integration's own timing: **waiting for an advertisement is
not the problem** — Home Assistant offered a connectable device within 0.0 s in
every observed case. The entire delay sits in *establishing the link* through
the proxy.

Two things make that slow, and both are addressed:

- **Competing connections.** Two cubes commanded shortly after one another used
  to open two connections at once and starve each other on the proxy's few
  slots — one attempt burned 127 s before succeeding in 15 s after the other
  was evicted. Connections are now built one at a time, and a command that
  finds another cube's link already open relays through it instead.
- **Weak signal.** Attempts were logged at −87 and −96 dBm. Near the noise
  floor a link needs more retries and drops more often, so a proxy closer to
  the cube helps.

  Measurement put this in perspective. Through triple glazing at −89 to
  −92 dBm, three cold runs took **2.1 s, 2.9 s and 3.9 s** from command to
  frame sent, each on the first attempt. Weak signal was never what made
  switching slow here; a series of software faults was, and the worst case came
  down from 142 s to under 4 s without the radio path changing at all.

  Be careful reading RSSI here: it is measured on advertisements that *arrived*.
  Through an obstacle the weak ones are lost and only the lucky ones are
  measured, so a bad link can report much the same number as a good one. Treat
  it as a floor on quality, not a measure of it.

What the integration does so it does not get in the way:

- The display switches **immediately** and the command goes out in the
  background — no need to press twice. If the cube does not acknowledge it, the
  display is rolled back afterwards.
- The connection stays open for **two minutes** by default, so follow-up
  commands do not pay for a new connection. Adjustable under *Configure* on the
  integration: longer means the next command is immediate, shorter lets the
  cube's radio sleep sooner. `0` disconnects right after each command.
- LMP is a mesh, so **any** open connection can relay a command to **any** cube.
  If one cube is already connected, commands for the other go through it and
  skip the wait entirely — this is how the vendor app works too.
- Rapid changes (dragging a slider) are coalesced into one send.

### Why setup is slow but commands are fast

A weak link shows a characteristic pattern: the first connection drags, then
every command is instant. That is not a quirk of this integration, it is how
BLE works.

**Establishing a connection has to succeed in one shot.** The proxy must
receive an advertisement *and* its connection request must reach the cube
within 150 µs, on the same channel. Lose either and the whole attempt is void
until the next advertising event. Advertising also uses only three fixed
channels, one of which sits squarely in Wi-Fi channel 6 — there is nowhere to
dodge to.

**Inside a connection it is the opposite.** BLE hops across 37 data channels
and adaptively drops the bad ones, and the link layer retransmits until a
packet gets through. A lost packet costs one connection interval — tens of
milliseconds — not another attempt.

So on a marginal link, setup needs several consecutive successes (probabilities
multiply, which is where minutes come from) while a command needs one eventual
success. Two practical consequences: a longer hold time is worth far more on a
bad link than a good one, and relaying through an existing connection avoids
the fragile part altogether.

## Mind the range

The cubes are BLE devices with a small antenna. Below **−75 dBm** the link
becomes unreliable: the connection often still succeeds but then drops during
service discovery — which looks like a sporadic software fault when it is
really radio range. Five metres through a solid wall is, in practice, **not**
enough.

The **signal strength** sensor shows the current value, and below −75 dBm the
integration warns in the log. The remedy is always a Bluetooth proxy closer to
the cube — Home Assistant automatically picks the one with the best reception.

## What it cannot do

- **No readback of on/off and colour.** The state is neither in the
  advertisement (which carries only network state) nor available on request:
  `DEVICE_DATA_GET` (0x42) is rejected with an undocumented code, regardless of
  addressing, time window, or device versus class id. The app appears to
  receive state as an event (`LMP_EVENT_DEVICE_DATA`, 0x92), and none could be
  provoked in testing. State in Home Assistant is therefore *optimistic*
  (`assumed_state`).

  Commands are **acknowledged** though: the cube confirms with `STATUS_ACK`
  whether it carried one out. A lost command is noticed immediately and
  retried, instead of failing silently.
- **No battery level.** Despite being battery lamps, the firmware does not
  answer: `LMP_COMMAND_MODULE_BATTERY_STATUS_GET` (0x2D) is acknowledged with
  `LMP_ERR_NOT_SUPPORTED`, `0x2C` goes unanswered, and the `MODULE_INFO_GET`
  reply contains no battery field. Checked on device with firmware 2.9.0 /
  API 4.

  The vendor app cannot show it either: it has a listener for a battery event
  that nothing ever emits, and its battery display is commented out in the
  source. LMP is a platform protocol covering battery sensors too, so the
  opcodes exist — this particular device just does not implement them.

### A note on the module flags

Status LED, key lock and deep sleep are shown from the **imported
configuration**, which is a snapshot taken when the export was made. The cubes
do not broadcast these settings, and reading them live would mean putting extra
frames on the wire — so the integration deliberately does not. Each entity
carries a `source` attribute saying so. To read the current values on demand,
use `tools/ble_test.py props`.

Deep sleep is deliberately not exposed as a switch. The app only ever *enables*
it, and a cube in deep sleep wakes solely on a physical button press — a stray
automation would strand the lamp.

## Requirements

- Home Assistant **2024.4+** with the **Bluetooth** integration enabled
- A Bluetooth adapter on the HA host **or** an ESPHome device with
  `bluetooth_proxy` (active connections) within range of the cubes
- Cubes in PRIVATE key mode (the app's default)

## Installation

**Via HACS (custom repository):**

1. HACS → Integrations → ⋮ → *Custom repositories*
2. Add `https://github.com/benji2k2/ha_smart_and_green` as category **Integration**
3. Install "Smart & Green Cube" → restart Home Assistant

**Manually:** copy the `custom_components/smartgreen_cube/` folder into
`<config>/custom_components/` and restart Home Assistant.

## Setup

1. In the **Smart & Green app**, export the configuration (this produces a
   password-protected `.lap` file) and transfer it to the HA machine.
2. In HA: *Settings → Devices & Services → Add integration → "Smart & Green Cube"*.
3. Choose **"Import configuration file (.lap)"**, upload the file and enter the
   **export password**.
4. The integration decrypts the file, reads the keys plus the cube and group
   list, and creates the lights automatically.

No export at hand? **"Enter keys manually"** takes `keyCrypt1` and
`nounceAESCrypt` (16 bytes hex each) directly; the cubes are then found by BLE
scan.

## How it works (short version)

Not Bluetooth SIG Mesh, but **LMP** by Linkio SAS. Control frames are GATT
writes to characteristic `00005002-0000-1000-8000-00805f9b34fb` (service
`41c15000-…`). The 16-byte payload is encrypted as
`payload XOR AES128-ECB(keyCrypt1, nonce)`. The integration icon uses the motif
from the original app.

## Status

Protocol, encryption, frame format and advertisement layout are **verified on a
real device**, as is day-to-day control (on/off, brightness, colour, colour
temperature) through an ESPHome proxy, and the group broadcast to `FF:FF`.
State is *optimistic* — see "What it cannot do". Feedback and issues welcome.

## Security and privacy

The `.lap` file and the extracted key (`keyCrypt1` + nonce) are **secrets of
your installation**. They are never committed to this repository (see
`.gitignore`).

**How Home Assistant stores it:** like every integration secret in HA (Wi-Fi
passwords, tokens, API keys), the key is written to the config entry in
`<config>/.storage/core.config_entries` as **plaintext JSON**. Home Assistant
does **not** encrypt this at rest; its threat model assumes a trusted host.

In practice that means:

- Anyone with filesystem access to the HA host can read the key.
- The key ends up in **backups** — so enable **encrypted backups**.
- The key is never logged and is **redacted in diagnostics downloads**
  (`diagnostics.py`).

For perspective: this is a **local BLE control secret**. It can only be abused
within radio range or through your proxy network, and it is not cloud access.
The same key already exists in the app export, on the phone, and in the cubes.

## Tests

```bash
python3 tests/run.py
```

No Home Assistant install required; see [tests/README.md](tests/README.md).

## Icons

The integration icon lives in `custom_components/smartgreen_cube/brand/`
(`icon.png` 256px, `icon@2x.png` 512px, `logo.png`). The motif is the vendor
logo from the original app.

## Licence

MIT — see [LICENSE](LICENSE).
