#!/usr/bin/env python3
"""
Smart & Green / Linkio "Cube" — BLE control test (macOS, bleak).

Builds an LMP colour/on-off frame exactly like the app
(cmdFactory.build_color_control + connection.ble.js sendBytesCmd), encrypts it
with the keys extracted from internal.config.lap (PRIVATE_KEY mode) and writes
it to the GATT characteristic.

Usage:
  ./.venv/bin/python ble_test.py scan                 # scan only, send nothing
  ./.venv/bin/python ble_test.py adv                  # decode advertisements
  ./.venv/bin/python ble_test.py redtest              # CubeLarge: red on, 3s, off
  ./.venv/bin/python ble_test.py redtest --small      # CubeSmall
  ./.venv/bin/python ble_test.py on  --h 285 --s 100 --v 100
  ./.venv/bin/python ble_test.py off
  ./.venv/bin/python ble_test.py redtest --group      # to the "all" group (FF:FF)
  ./.venv/bin/python ble_test.py acktest --small      # acknowledged commands

First run: macOS asks for Bluetooth access for the terminal — allow it.
"""
import argparse, asyncio, hashlib, json, os, sys
from Crypto.Cipher import AES
from bleak import BleakScanner, BleakClient

HERE = os.path.dirname(os.path.abspath(__file__))
CONF = os.path.join(HERE, "conf", "lnk.SandG.conf.txt")

# LINKIO_LMP_SERVICE 41C15000-..., TXRX characteristic 5002 (write + notify)
SERVICE_UUID = "41c15000-6def-11e5-bcde-0002a5d5c51b"
CHAR_UUID = "00005002-0000-1000-8000-00805f9b34fb"
COMPANY_ID = 0x04AA                      # Linkio, as reported by bleak/CoreBluetooth

# LMP constants (from core_ble_type.js / core_lmp_opcodes.js)
MSG_CMD_NO_ACK = 0x00
ENC_NONE, ENC_PUBLIC, ENC_PRIVATE = 0x00, 0x01, 0x02
FRAME_LMP_SHORT = 0x02
OP_DEVICE_DATA_SET = 0x41
OP_GROUP_DATA_SET  = 0x56
LED_MODE_COLOR = 0x01
FADE_COLOR_TRANSITION = 50


def load_conf():
    d = json.load(open(CONF))
    m = {e["key"]: e["data"] for e in d}
    return m


def hexs(b):
    return " ".join("%02X" % x for x in b)


def hsv_to_rgb_tinycolor(h, s, v):
    """Replicates tinycolor HSVtoRGB: h[0-360], s,v[0-100] -> r,g,b (0-255, rounded)."""
    h = (h % 360) / 360.0
    s = s / 100.0
    v = v / 100.0
    i = int(h * 6)
    f = h * 6 - i
    p = v * (1 - s)
    q = v * (1 - f * s)
    t = v * (1 - (1 - f) * s)
    r, g, b = [
        (v, t, p), (q, v, p), (p, v, t),
        (p, q, v), (t, p, v), (v, p, q),
    ][i % 6]
    return round(r * 255), round(g * 255), round(b * 255)


def process_color(h, s, v, power_constraint=1, intensity_constraint=True):
    """processColor() for color-white-dimmable, RGBW on, color_mode=COLOR, linear off."""
    coeff = 1.0
    if intensity_constraint:
        r, g, b = hsv_to_rgb_tinycolor(h, s, 100)
        total = r + g + b
        coeff = 255.0 / total if total else 1.0
    out_v = v * (s / 100.0) * power_constraint * coeff
    white_value = (100 - s) * (v / 100.0) * power_constraint   # color_mode == COLOR -> without +0x80
    return {"h": h, "s": s, "v": out_v, "white": white_value}


def build_color_payload(index, onoff, h, s, v):
    """cmdFactory.build_color_control for an RGBW device (API >= 2)."""
    pc = process_color(h, s, v)
    b = []
    b.append(0)                                  # [0] length placeholder
    b.append(OP_DEVICE_DATA_SET)                 # [1] 0x41
    b.append(index & 0xFF)                       # [2] device index
    b.append((1 if onoff else 0) + (LED_MODE_COLOR << 4))  # [3] onoff | ledmode<<4
    b.append(int(pc["v"]) & 0xFF)                # [4] V
    b.append(int(pc["h"]) & 0xFF)                # [5] H low
    b.append((int(pc["h"]) >> 8) & 0xFF)         # [6] H high
    b.append(int(pc["s"]) & 0xFF)                # [7] S
    b.append(FADE_COLOR_TRANSITION & 0xFF)       # [8] param low
    b.append((FADE_COLOR_TRANSITION >> 8) & 0xFF)  # [9] param high
    b.append(int(pc["white"]) & 0xFF)            # [10] white value (RGBW)
    b[0] = len(b) - 1                            # length = index of the last byte
    return b


def build_white_payload(index, onoff, level=100, flag=True):
    """White mode: V=0, white byte optionally carries the 0x80 flag (WHITE_MODE)."""
    white = int(max(0, min(100, level)))
    if flag:
        white += 0x80
    b = [
        0,
        OP_DEVICE_DATA_SET,
        index & 0xFF,
        (1 if onoff else 0) + (LED_MODE_COLOR << 4),
        0,                                      # V = 0 (colour LEDs off)
        0, 0,                                   # H
        0,                                      # S = 0
        FADE_COLOR_TRANSITION & 0xFF,
        (FADE_COLOR_TRANSITION >> 8) & 0xFF,
        white & 0xFF,                           # white byte (+0x80 = WHITE_MODE)
    ]
    b[0] = len(b) - 1
    return b


def build_group_color_payload(class_id, onoff, h, s, v):
    p = build_color_payload(class_id, onoff, h, s, v)
    p[1] = OP_GROUP_DATA_SET                      # 0x56 instead of 0x41
    return p


def encrypt_payload(full16, key1, nonce):
    keystream = AES.new(bytes(key1), AES.MODE_ECB).encrypt(bytes(nonce))
    return [full16[i] ^ keystream[i] for i in range(16)]


FRAME_LOCAL_SHORT = 0x00


def build_frame(lmp_addr, payload, key1, nonce, cmd_id=1,
                encryption=ENC_PRIVATE, msg_type=MSG_CMD_NO_ACK,
                frame_type=FRAME_LMP_SHORT):
    """sendBytesCmd() short frame -> 20 bytes ready for the GATT write.

    frame_type LOCAL (0) corresponds to the app's CONX_MODE_DIRECT: over a
    direct connection two zero bytes take the place of the LMP address. That is
    exactly how the cube replies to us (header 0x50 = msg_type 2 / enc 2 /
    frame 0).
    """
    # header
    b0 = ((msg_type << 5) & 0xE0) | ((encryption << 3) & 0x18) | (frame_type & 0x07)
    header = [b0, cmd_id & 0xFF]
    if frame_type == FRAME_LOCAL_SHORT:
        addr = [0x00, 0x00]
    else:
        # address "41:E0" -> [0xE0, 0x41] (last byte, then second-to-last)
        parts = lmp_addr.split(":")
        addr = [int(parts[-1], 16), int(parts[-2], 16)]
    # pad the payload to 15 bytes: first a 0, then 0xFF
    pl = list(payload)
    if len(pl) < 15:
        pl.append(0)
    while len(pl) < 15:
        pl.append(0xFF)
    crc = 0
    for x in pl:
        crc ^= x
    full = [crc] + pl                             # 16 bytes (CRC + 15)
    if encryption in (ENC_PUBLIC, ENC_PRIVATE):
        full = encrypt_payload(full, key1, nonce)
    frame = header + addr + full                  # 4 + 16 = 20
    return bytes(frame)


def parse_adv(v):
    """Manufacturer data (without company id) per core_ble_type.js."""
    if len(v) < 4:
        return None
    typ, status = v[0], v[1]
    role = typ & 0x07
    src = "%02X:%02X" % (v[3], v[2])              # readable as 'high:low'
    return {"role": role, "connected": bool(status & 0x01),
            "registered": bool(status & 0x80),
            "src_bytes": (v[2], v[3]), "src": src}


async def do_scan(seconds=8.0):
    print(f"Scanning {seconds:.0f}s for Linkio advertisers (company 0x{COMPANY_ID:04X}) ...")
    found = {}

    def cb(device, adv):
        md = adv.manufacturer_data.get(COMPANY_ID)
        if md is None:
            return
        info = parse_adv(md)
        found[device.address] = (device, adv, info)

    scanner = BleakScanner(detection_callback=cb)
    await scanner.start()
    await asyncio.sleep(seconds)
    await scanner.stop()

    if not found:
        print("  Nothing found. Cube close enough? Close the app on the phone "
              "(it may be holding the connection).")
    for addr, (dev, adv, info) in found.items():
        print(f"  {addr}  RSSI {adv.rssi:>4}  name={dev.name!r}")
        if info:
            print(f"      role={info['role']} connected={info['connected']} "
                  f"registered={info['registered']} src≈{info['src']} "
                  f"raw={hexs(adv.manufacturer_data[COMPANY_ID])}")
    return found


async def find_device(target_lmp):
    """Find the BLE device whose advertised source address matches the LMP address."""
    parts = target_lmp.split(":")
    want = {(int(parts[-1], 16), int(parts[-2], 16)),
            (int(parts[-2], 16), int(parts[-1], 16))}
    # name fallback: CubeSmall advertises as "Bulb1340" (LMP 13:40 -> "1340")
    want_name = "bulb" + parts[-2].lower() + parts[-1].lower()
    print(f"Looking for module with LMP {target_lmp} (adv name ~ {want_name!r}) ...")
    match = {}

    def cb(device, adv):
        name = (device.name or adv.local_name or "").lower()
        if name == want_name:
            match[device.address] = (device, adv)
            return
        md = adv.manufacturer_data.get(COMPANY_ID)
        if md is None:
            return
        info = parse_adv(md)
        if info and info["src_bytes"] in want:
            match[device.address] = (device, adv)

    scanner = BleakScanner(detection_callback=cb)
    await scanner.start()
    for _ in range(20):
        await asyncio.sleep(0.5)
        if match:
            break
    await scanner.stop()
    if not match:
        return None
    # strongest RSSI
    dev, adv = max(match.values(), key=lambda t: t[1].rssi)
    print(f"  found: {dev.address} RSSI {adv.rssi}")
    return dev


async def send_frames(dev, frames):
    def on_notify(_char, data):
        print(f"  <- NOTIFY {hexs(data)}")

    async with BleakClient(dev) as client:
        print(f"  connected: {client.address}")
        try:
            await client.start_notify(CHAR_UUID, on_notify)
        except Exception as e:
            print(f"  (no notifications: {e})")
        for label, fr, wait in frames:
            print(f"  -> {label}: {hexs(fr)}")
            try:
                await client.write_gatt_char(CHAR_UUID, fr, response=False)
            except Exception as e1:
                print(f"     (write-no-response failed: {e1}; trying with response)")
                await client.write_gatt_char(CHAR_UUID, fr, response=True)
            await asyncio.sleep(wait)
        try:
            await client.stop_notify(CHAR_UUID)
        except Exception:
            pass
    print("  done, disconnected.")


async def do_scan_all(seconds=10.0):
    print(f"Raw scan {seconds:.0f}s: ALL BLE devices in range ...")
    seen = {}

    def cb(device, adv):
        seen[device.address] = (device, adv)

    scanner = BleakScanner(detection_callback=cb)
    await scanner.start()
    await asyncio.sleep(seconds)
    await scanner.stop()

    if not seen:
        print("  Nothing received at all (not even phones/headphones) -> Bluetooth problem.")
        return
    # sorted by RSSI, strongest first
    for addr, (dev, adv) in sorted(seen.items(), key=lambda kv: -kv[1][1].rssi):
        md = adv.manufacturer_data
        md_str = ", ".join("0x%04X=%s" % (k, hexs(v)) for k, v in md.items()) or "-"
        svcs = ", ".join(adv.service_uuids) or "-"
        name = dev.name or adv.local_name or "?"
        print(f"  RSSI {adv.rssi:>4}  name={name!r}")
        print(f"        mfg: {md_str}")
        print(f"        svc: {svcs}")


# ---------------------------------------------------------------- Advertising
# Opcodes from core_lmp_opcodes.js, only the ones of interest here.
LMP_NAMES = {
    0x82: "STATUS_NETWORK", 0x84: "STATUS_NETWORK_GTW_LIST",
    0x80: "STATUS_ACK", 0x81: "STATUS_REGISTRATION", 0x83: "STATUS_HEARTBEAT",
    0x91: "STATUS_DEVICE_INFO", 0x92: "EVENT_DEVICE_DATA",
    0x93: "STATUS_DEVICE_DATA", 0x8F: "STATUS_DEVICE_INFO_OLD",
    0xAF: "PARAM_MODULE_REFERENCE", 0xB0: "PARAM_MAC_ADDRESS",
    0xB1: "PARAM_SHORT_ADDRESS", 0xB4: "PARAM_MODULE_TYPE",
    0xB2: "PARAM_MANUFACTURER_NAME", 0xB3: "PARAM_MODEL_NAME",
    0xB5: "PARAM_SW_VERSION", 0xB6: "PARAM_HW_VERSION",
    0xB7: "PARAM_MODULE_NAME", 0xB8: "PARAM_API_VERSION",
    0xB9: "PARAM_DEV_LIST_CHANGEABLE", 0xC0: "PARAM_BATTERY_LEVEL",
    0xC1: "PARAM_ROLE",
    0xC2: "PARAM_RSSI",
}
OP_MODULE_BATTERY_STATUS_GET = 0x2D
OP_MODULES_BATTERY_LEVEL_GET = 0x2C
OP_MODULE_INFO_GET = 0x30
CMD_WITH_ACK = 0x01


def keystream(key1, nonce):
    return AES.new(bytes(key1), AES.MODE_ECB).encrypt(bytes(nonce))


def decode_tlv(body):
    """Split a decrypted payload into (opcode, name, data)."""
    out, i = [], 0
    while i < len(body):
        size = body[i]
        if size == 0:
            break
        typ = body[i + 1]
        out.append((typ, LMP_NAMES.get(typ, "?%#04x" % typ), bytes(body[i + 2:i + 1 + size])))
        i += 1 + size
    return out


def decode_encrypted_payload(enc16, ks):
    """XOR-decrypt, verify the CRC, return TLVs. None on a CRC mismatch."""
    pay = bytes(a ^ b for a, b in zip(enc16, ks))
    crc_rx, body = pay[0], pay[1:16]
    crc = 0
    for x in body:
        crc ^= x
    if crc != crc_rx:
        return None
    return decode_tlv(body)


def decode_adv(md, ks):
    """Decode Linkio manufacturer data (without the company id)."""
    if len(md) < 24:
        return None
    hdr, status = md[0], md[1]
    info = {
        "msg_type": (hdr & 0xE0) >> 5,
        "encryption": (hdr & 0x18) >> 3,
        "role": hdr & 0x07,
        "registered": bool(status & 0x80),
        "connected": bool(status & 0x01),
        "src": "%02X:%02X" % (md[3], md[2]),
        "seq": md[6],
        "tlv": decode_encrypted_payload(md[8:24], ks),
    }
    return info


async def do_adv(ks, seconds=12.0):
    """Listen to the cubes' advertisements and decrypt them (passive only)."""
    print(f"Reading advertisements for {seconds:.0f}s (decrypted) ...")
    seen = {}

    def cb(device, adv):
        md = adv.manufacturer_data.get(COMPANY_ID)
        if md is None:
            return
        info = decode_adv(bytes(md), ks)
        if info:
            seen[device.address] = (device, adv, info)

    scanner = BleakScanner(detection_callback=cb)
    await scanner.start()
    await asyncio.sleep(seconds)
    await scanner.stop()

    if not seen:
        print("  No Linkio advertisements received.")
        return
    for addr, (dev, adv, info) in sorted(seen.items(), key=lambda kv: -kv[1][1].rssi):
        print(f"  {dev.name or '?':10} {addr}  RSSI {adv.rssi:>4}")
        print(f"      src={info['src']} registered={info['registered']} "
              f"connected={info['connected']} seq={info['seq']}")
        if info["tlv"] is None:
            print("      payload: CRC mismatch (wrong key?)")
            continue
        for typ, name, data in info["tlv"]:
            extra = f" -> {int.from_bytes(data, 'little')}" if 0 < len(data) <= 4 else ""
            print(f"      {name:26} {hexs(data)}{extra}")


OP_DEVICE_DATA_GET = 0x42
OP_STATUS_DEVICE_DATA = 0x93


def build_state_query(dest_lmp, device_id, earliest, latest):
    """cmdFactory.build_get_latest_statuses: query a device's state."""
    parts = dest_lmp.split(":")
    b = [0, OP_DEVICE_DATA_GET,
         int(parts[-1], 16), int(parts[-2], 16),   # address: low, high
         device_id & 0xFF]
    for val in (earliest, latest):
        b += [val & 0xFF, (val >> 8) & 0xFF, (val >> 16) & 0xFF, (val >> 24) & 0xFF]
    b[0] = len(b) - 1
    return b


def decode_device_data(data):
    """Decode STATUS_DEVICE_DATA for a COLOR_WHITE_DIMMABLE_LIGHT.

    Field order per connectionFactory.js: device id, timestamp (4 bytes),
    status, onoff|ledmode, V, H (2 bytes), S, params (2 bytes), optional white.
    """
    if len(data) < 12:
        return None
    dev_id = data[0]
    ts = int.from_bytes(data[1:5], "little")
    status = data[5]
    onoff = bool(data[6] & 0x0F)
    led_mode = data[6] >> 4
    v = data[7]
    h = data[8] + (data[9] << 8)
    s = data[10]
    params = data[11] + (data[12] << 8) if len(data) > 12 else None
    white = data[13] if len(data) > 13 else None
    return {"device": dev_id, "zeit": ts, "status": status, "an": onoff,
            "ledmode": led_mode, "v": v, "h": h, "s": s,
            "params": params, "weiss": white}


def fmt_tlv_value(data):
    if data and all(32 <= c < 127 for c in data):
        return f'  "{data.decode()}"'
    if 0 < len(data) <= 4:
        return f"  -> {int.from_bytes(data, 'little')}"
    return ""


async def query_module(dev, ks, frames, wait=3.0):
    """Send query frames and decode the notify replies.

    Longer replies arrive as SEVERAL notifies: byte [2] is the index, byte [3]
    the highest index. Each fragment is separately encrypted and CRC-protected,
    but the TLV list spans fragment boundaries — so it may only be parsed after
    reassembly.
    """
    answers = []
    parts = {}

    def flush():
        if not parts:
            return
        stream = b"".join(parts[i] for i in sorted(parts))
        for typ, name, val in decode_tlv(stream):
            print(f"     {name:26} {hexs(val)}{fmt_tlv_value(val)}")
            answers.append((name, bytes(val)))
            if typ == 0x80 and len(val) == 1:
                known = {0: "SUCCESS", 1: "NOT_SUPPORTED", 2: "INVALID_COMMAND",
                         3: "INVALID_PARAMETER", 4: "INVALID_DEVICE",
                         5: "UNREGISTERED", 8: "TIMEOUT", 20: "ITEM_NOT_FOUND"}
                print(f"       -> ACK {val[0]} = "
                      f"{known.get(val[0], 'UNKNOWN')}")
            if typ in (OP_STATUS_DEVICE_DATA, 0x92):
                st = decode_device_data(val)
                if st:
                    print(f"       -> AN={st['an']} ledmode={st['ledmode']} "
                          f"V={st['v']} H={st['h']} S={st['s']} weiss={st['weiss']}")
        parts.clear()

    def on_notify(_char, data):
        print(f"  <- NOTIFY {hexs(data)}")
        if len(data) < 20:
            return
        idx, tot = data[2], data[3]
        pay = bytes(a ^ b for a, b in zip(data[4:20], ks))
        crc_rx, body = pay[0], pay[1:16]
        crc = 0
        for x in body:
            crc ^= x
        if crc != crc_rx:
            print("     (CRC-Fehler / anderes Format)")
            return
        parts[idx] = body
        if idx >= tot:          # letztes Fragment -> auswerten
            flush()

    async with BleakClient(dev) as client:
        print(f"  connected: {client.address}")
        await client.start_notify(CHAR_UUID, on_notify)
        for label, fr in frames:
            print(f"  -> {label}: {hexs(fr)}")
            try:
                await client.write_gatt_char(CHAR_UUID, fr, response=True)
            except Exception:
                await client.write_gatt_char(CHAR_UUID, fr, response=False)
            await asyncio.sleep(wait)
            flush()   # show an incomplete reply anyway
        try:
            await client.stop_notify(CHAR_UUID)
        except Exception:
            pass
    if not answers:
        print("  No usable reply received.")
    return answers


async def dump_gatt(dev):
    async with BleakClient(dev) as client:
        print(f"  connected: {client.address}")
        for svc in client.services:
            print(f"  SERVICE {svc.uuid}")
            for ch in svc.characteristics:
                props = ",".join(ch.properties)
                print(f"      CHAR {ch.uuid}  [{props}]  handle={ch.handle}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action",
                    choices=["scan", "scanall", "adv", "gatt", "battery", "state", "probe", "probe2", "acktest", "props",
                             "redtest", "whitetest", "sweep", "on", "off"])
    ap.add_argument("--gap", action="store_true",
                    help="switch off between steps (instead of a direct transition)")
    ap.add_argument("--hold", type=float, default=3.0,
                    help="seconds per step (default 3)")
    ap.add_argument("--small", action="store_true", help="CubeSmall instead of CubeLarge")
    ap.add_argument("--group", action="store_true", help="to the 'all' group (FF:FF)")
    ap.add_argument("--wait", type=float, default=0,
                    help="seconds to wait for replies (state: default 15)")
    ap.add_argument("--h", type=float, default=285)
    ap.add_argument("--s", type=float, default=100)
    ap.add_argument("--v", type=float, default=100)
    args = ap.parse_args()

    conf = load_conf()
    key1 = conf["keyCrypt1"]
    nonce = conf["nounceAESCrypt"]
    enc_mode = conf.get("encryptionMode", 2)
    # Do NOT print key/nonce: debug output tends to end up in bug reports.
    # A fingerprint is enough to compare two configurations.
    fp = hashlib.sha256(bytes(key1) + bytes(nonce)).hexdigest()[:8]
    print(f"encryptionMode={enc_mode}  key/nonce loaded (fingerprint {fp})")

    modkey = "mod_1" if args.small else "mod_0"
    mod = conf[modkey]
    dev_index = 0                                  # beide Cubes: index 0
    class_id = 19                                  # color-white-dimmable

    if args.action == "scan":
        asyncio.run(do_scan())
        return

    if args.action == "scanall":
        asyncio.run(do_scan_all())
        return

    if args.action == "adv":
        asyncio.run(do_adv(keystream(key1, nonce)))
        return

    if args.action == "props":
        # MODULE_PROPERTY_GET (0x36). The module-level counterpart to the
        # device properties we tried before; MODULE_INFO_GET (0x30) works, so
        # module commands are served. Read-only: nothing is written here.
        target = mod["identification"]["lmp_addr"]
        ks = keystream(key1, nonce)
        OP_MODULE_PROPERTY_GET = 0x36
        names = {0: "LED status indicator", 1: "key lock", 2: "deep sleep"}
        variants = [(f"MODULE_PROPERTY_GET {pid} ({names.get(pid, '?')})",
                     [2, OP_MODULE_PROPERTY_GET, pid]) for pid in range(0, 4)]
        frames = [(label, build_frame(target, payload, key1, nonce, cmd_id=i + 1,
                                      msg_type=CMD_WITH_ACK))
                  for i, (label, payload) in enumerate(variants)]

        async def find_and_props():
            dev = await find_device(target)
            if dev is None:
                print("Module not found.")
                return False
            await query_module(dev, ks, frames, wait=args.wait or 3.0)
            return True

        if not asyncio.run(find_and_props()):
            sys.exit(1)
        return

    if args.action == "acktest":
        target = mod["identification"]["lmp_addr"]
        ks = keystream(key1, nonce)
        # The app controls with CMD_WITH_ACK. Two things to check:
        # 1. does the cube acknowledge the control command (delivery proof)?
        # 2. does it report the change as LMP_EVENT_DEVICE_DATA (0x92)?
        steps = [
            ("RED on  (with ACK)", build_color_payload(dev_index, True, 0, 100, 100)),
            ("OFF     (with ACK)", build_color_payload(dev_index, False, 0, 100, 100)),
        ]
        frames = [(label, build_frame(target, payload, key1, nonce, cmd_id=i + 1,
                                      msg_type=CMD_WITH_ACK))
                  for i, (label, payload) in enumerate(steps)]

        async def find_and_ack():
            dev = await find_device(target)
            if dev is None:
                print("Module not found.")
                return False
            await query_module(dev, ks, frames, wait=args.wait or 5.0)
            return True

        if not asyncio.run(find_and_ack()):
            sys.exit(1)
        return

    if args.action == "probe2":
        target = mod["identification"]["lmp_addr"]
        ks = keystream(key1, nonce)
        OP_DEVICE_INFO_GET = 0x32
        OP_DEVICES_DATA_LIST_GET = 0x4A
        OP_DEVICE_PROPERTY_GET = 0x44
        variants = [
            # MODULE_INFO_GET (0x30) works -> try the device counterpart
            ("DEVICE_INFO_GET with index", [2, OP_DEVICE_INFO_GET, dev_index]),
            ("DEVICE_INFO_GET without index", [1, OP_DEVICE_INFO_GET]),
            ("DEVICES_DATA_LIST_GET", [1, OP_DEVICES_DATA_LIST_GET]),
            ("DEVICES_DATA_LIST_GET with index", [2, OP_DEVICES_DATA_LIST_GET, dev_index]),
        ]
        # property ids beyond 0/1 (those returned INVALID_PARAMETER)
        variants += [(f"PROPERTY_GET property {pid}",
                      [3, OP_DEVICE_PROPERTY_GET, dev_index, pid])
                     for pid in range(2, 8)]

        frames = [(label, build_frame(target, payload, key1, nonce, cmd_id=i + 1,
                                      msg_type=CMD_WITH_ACK,
                                      frame_type=FRAME_LOCAL_SHORT))
                  for i, (label, payload) in enumerate(variants)]

        async def find_and_probe2():
            dev = await find_device(target)
            if dev is None:
                print("Module not found.")
                return False
            await query_module(dev, ks, frames, wait=args.wait or 3.0)
            return True

        if not asyncio.run(find_and_probe2()):
            sys.exit(1)
        return

    if args.action == "probe":
        import time as _time
        target = mod["identification"]["lmp_addr"]
        ks = keystream(key1, nonce)
        now = int(_time.time())
        OP_DEVICE_PROPERTY_GET = 0x44
        variants = [
            ("DATA_GET local, full range",
             build_state_query(target, dev_index, 0, 0xFFFFFFFF), FRAME_LOCAL_SHORT),
            ("DATA_GET local, range 0/0",
             build_state_query(target, dev_index, 0, 0), FRAME_LOCAL_SHORT),
            ("DATA_GET local, last hour",
             build_state_query(target, dev_index, now - 3600, now + 60), FRAME_LOCAL_SHORT),
            ("DATA_GET lmp, class id 19 instead of device",
             build_state_query(target, class_id, 0, 0xFFFFFFFF), FRAME_LMP_SHORT),
            ("PROPERTY_GET local, property 0",
             [3, OP_DEVICE_PROPERTY_GET, dev_index, 0], FRAME_LOCAL_SHORT),
            ("PROPERTY_GET local, property 1",
             [3, OP_DEVICE_PROPERTY_GET, dev_index, 1], FRAME_LOCAL_SHORT),
        ]
        frames = [(label, build_frame(target, payload, key1, nonce, cmd_id=i + 1,
                                      msg_type=CMD_WITH_ACK, frame_type=ft))
                  for i, (label, payload, ft) in enumerate(variants)]

        async def find_and_probe():
            dev = await find_device(target)
            if dev is None:
                print("Module not found.")
                return False
            await query_module(dev, ks, frames, wait=args.wait or 4.0)
            return True

        if not asyncio.run(find_and_probe()):
            sys.exit(1)
        return

    if args.action == "state":
        import time as _time
        target = mod["identification"]["lmp_addr"]
        ks = keystream(key1, nonce)
        now = int(_time.time())
        # Two time windows, since it is unclear which clock the cube keeps.
        queries = [
            ("DEVICE_DATA_GET (full range)", build_state_query(target, dev_index, 0, 0xFFFFFFFF)),
            ("DEVICE_DATA_GET (last 24h)", build_state_query(target, dev_index, now - 86400, now + 3600)),
        ]
        frames = [(label, build_frame(target, payload, key1, nonce, cmd_id=i + 1,
                                      msg_type=CMD_WITH_ACK))
                  for i, (label, payload) in enumerate(queries)]

        async def find_and_state():
            dev = await find_device(target)
            if dev is None:
                print("Module not found.")
                return False
            await query_module(dev, ks, frames, wait=args.wait or 15.0)
            return True

        if not asyncio.run(find_and_state()):
            sys.exit(1)
        return

    if args.action == "battery":
        target = mod["identification"]["lmp_addr"]
        ks = keystream(key1, nonce)
        # Three queries, since it is unclear which one the device answers.
        queries = [
            ("BATTERY_STATUS_GET (0x2D)", [1, OP_MODULE_BATTERY_STATUS_GET]),
            ("BATTERY_LEVEL_GET (0x2C)", [1, OP_MODULES_BATTERY_LEVEL_GET]),
            ("MODULE_INFO_GET (0x30)", [1, OP_MODULE_INFO_GET]),
        ]
        frames = [
            (label, build_frame(target, payload, key1, nonce, cmd_id=i + 1,
                                msg_type=CMD_WITH_ACK))
            for i, (label, payload) in enumerate(queries)
        ]

        async def find_and_query():
            dev = await find_device(target)
            if dev is None:
                print("Module not found.")
                return False
            await query_module(dev, ks, frames)
            return True

        if not asyncio.run(find_and_query()):
            sys.exit(1)
        return

    if args.action == "gatt":
        target = ("13:40" if args.small else "41:E0")

        async def find_and_dump():
            dev = await find_device(target)
            if dev is None:
                print("Module not found.")
                return
            await dump_gatt(dev)

        asyncio.run(find_and_dump())
        return

    # target address + payload
    if args.group:
        target_lmp = "FF:FF"
        def color(onoff, h, s, v):
            return build_group_color_payload(class_id, onoff, h, s, v)
    else:
        target_lmp = mod["identification"]["lmp_addr"]
        def color(onoff, h, s, v):
            return build_color_payload(dev_index, onoff, h, s, v)

    print(f"Target: {modkey} '{mod['identification']['name']}' "
          f"LMP {target_lmp}"
          + ("  (as GROUP)" if args.group else ""))

    frames = []
    if args.action == "sweep":
        # (label, H, S, V) — in the app white goes through the COLOUR path:
        # niedrige Saettigung => hoher Weiss-Kanal (kalt),
        # a warm hue at high saturation => orange LEDs (warm).
        steps = [
            ("Warmweiss ~2200K (h30 s85)", 30, 85, 100),
            ("Neutralweiss  (h30 s40)",    30, 40, 100),
            ("Kaltweiss     (s0)",          0,  0, 100),
            ("ROT",                         0, 100, 100),
            ("GRUEN",                     120, 100, 100),
            ("BLAU",                      240, 100, 100),
            ("Rot 30% gedimmt",             0, 100,  30),
        ]
        cid = 0
        for label, h, s, v in steps:
            cid += 1
            pl = build_color_payload(dev_index, True, h, s, v)
            frames.append((f"{label}  payload={pl[4]},{pl[7]},w={pl[10]}",
                           build_frame(target_lmp, pl, key1, nonce, cmd_id=cid),
                           args.hold))
            if args.gap:
                cid += 1
                frames.append(("  -> off",
                               build_frame(target_lmp,
                                           build_color_payload(dev_index, False, h, s, v),
                                           key1, nonce, cmd_id=cid), 1.2))
        cid += 1
        frames.append(("OFF (end)",
                       build_frame(target_lmp,
                                   build_color_payload(dev_index, False, 0, 0, 100),
                                   key1, nonce, cmd_id=cid), 0.5))
    elif args.action == "whitetest":
        # A: white WITH the 0x80 flag (WHITE_MODE) — the theory
        frames.append(("WHITE with 0x80 flag",
                       build_frame(target_lmp, build_white_payload(dev_index, True, 100, True),
                                   key1, nonce, cmd_id=1), 4.0))
        frames.append(("OFF", build_frame(target_lmp, build_white_payload(dev_index, False, 100, True),
                                          key1, nonce, cmd_id=2), 2.0))
        # B: white WITHOUT the flag — control (previous behaviour)
        frames.append(("WHITE without flag",
                       build_frame(target_lmp, build_white_payload(dev_index, True, 100, False),
                                   key1, nonce, cmd_id=3), 4.0))
        frames.append(("OFF", build_frame(target_lmp, build_white_payload(dev_index, False, 100, False),
                                          key1, nonce, cmd_id=4), 2.0))
    elif args.action == "redtest":
        frames.append(("RED on", build_frame(target_lmp, color(True, 0, 100, 100), key1, nonce, cmd_id=1), 3.0))
        frames.append(("OFF",    build_frame(target_lmp, color(False, 0, 100, 100), key1, nonce, cmd_id=2), 0.5))
    elif args.action == "on":
        frames.append(("ON", build_frame(target_lmp, color(True, args.h, args.s, args.v), key1, nonce, cmd_id=1), 0.5))
    elif args.action == "off":
        frames.append(("OFF", build_frame(target_lmp, color(False, args.h, args.s, args.v), key1, nonce, cmd_id=1), 0.5))

    # find the device and send within ONE event loop (CoreBluetooth requirement)
    lookup_lmp = mod["identification"]["lmp_addr"]

    async def find_and_send():
        dev = await find_device(lookup_lmp)
        if dev is None:
            print("Module not found over BLE. Run 'scan'; close the app on the phone; move closer.")
            return False
        await send_frames(dev, frames)
        return True

    ok = asyncio.run(find_and_send())
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
