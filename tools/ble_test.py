#!/usr/bin/env python3
"""
Smart & Green / Linkio "Cube" – BLE-Steuerungstest (macOS, bleak).

Baut ein LMP-Farb-/OnOff-Frame exakt wie die App (cmdFactory.build_color_control +
connection.ble.js sendBytesCmd), verschlüsselt es mit den aus internal.config.lap
extrahierten Keys (PRIVATE_KEY-Modus) und schreibt es auf die GATT-Characteristic.

Benutzung:
  ./.venv/bin/python ble_test.py scan                 # nur scannen, nichts senden
  ./.venv/bin/python ble_test.py redtest              # CubeLarge: rot an, 3s, aus
  ./.venv/bin/python ble_test.py redtest --small      # CubeSmall
  ./.venv/bin/python ble_test.py on  --h 285 --s 100 --v 100
  ./.venv/bin/python ble_test.py off
  ./.venv/bin/python ble_test.py redtest --group      # an Gruppe "Alle" (FF:FF)

Erste Ausführung: macOS fragt nach Bluetooth-Zugriff fürs Terminal -> erlauben.
"""
import argparse, asyncio, json, os, sys
from Crypto.Cipher import AES
from bleak import BleakScanner, BleakClient

HERE = os.path.dirname(os.path.abspath(__file__))
CONF = os.path.join(HERE, "conf", "lnk.SandG.conf.txt")

# LINKIO_LMP_SERVICE 41C15000-..., TXRX-Characteristic 5002 (Senden + Notify)
SERVICE_UUID = "41c15000-6def-11e5-bcde-0002a5d5c51b"
CHAR_UUID = "00005002-0000-1000-8000-00805f9b34fb"
COMPANY_ID = 0x04AA                      # Linkio, wie von bleak/CoreBluetooth gemeldet

# LMP-Konstanten (aus core_ble_type.js / core_lmp_opcodes.js)
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
    """Repliziert tinycolor HSVtoRGB: h[0-360], s,v[0-100] -> r,g,b (0-255, gerundet)."""
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
    """processColor() fuer color-white-dimmable, RGBW an, color_mode=COLOR, linear aus."""
    coeff = 1.0
    if intensity_constraint:
        r, g, b = hsv_to_rgb_tinycolor(h, s, 100)
        total = r + g + b
        coeff = 255.0 / total if total else 1.0
    out_v = v * (s / 100.0) * power_constraint * coeff
    white_value = (100 - s) * (v / 100.0) * power_constraint   # color_mode == COLOR -> ohne +0x80
    return {"h": h, "s": s, "v": out_v, "white": white_value}


def build_color_payload(index, onoff, h, s, v):
    """cmdFactory.build_color_control fuer ein RGBW-Device (API>=2)."""
    pc = process_color(h, s, v)
    b = []
    b.append(0)                                  # [0] len-Platzhalter
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
    b[0] = len(b) - 1                            # laenge = index des letzten Bytes
    return b


def build_white_payload(index, onoff, level=100, flag=True):
    """Weiß-Modus: V=0, Weiß-Byte traegt (optional) das 0x80-Flag (WHITE_MODE)."""
    white = int(max(0, min(100, level)))
    if flag:
        white += 0x80
    b = [
        0,
        OP_DEVICE_DATA_SET,
        index & 0xFF,
        (1 if onoff else 0) + (LED_MODE_COLOR << 4),
        0,                                      # V = 0 (Farb-LEDs aus)
        0, 0,                                   # H
        0,                                      # S = 0
        FADE_COLOR_TRANSITION & 0xFF,
        (FADE_COLOR_TRANSITION >> 8) & 0xFF,
        white & 0xFF,                           # Weiss-Byte (+0x80 = WHITE_MODE)
    ]
    b[0] = len(b) - 1
    return b


def build_group_color_payload(class_id, onoff, h, s, v):
    p = build_color_payload(class_id, onoff, h, s, v)
    p[1] = OP_GROUP_DATA_SET                      # 0x56 statt 0x41
    return p


def encrypt_payload(full16, key1, nonce):
    keystream = AES.new(bytes(key1), AES.MODE_ECB).encrypt(bytes(nonce))
    return [full16[i] ^ keystream[i] for i in range(16)]


def build_frame(lmp_addr, payload, key1, nonce, cmd_id=1,
                encryption=ENC_PRIVATE, msg_type=MSG_CMD_NO_ACK):
    """sendBytesCmd() Short-Frame -> 20 Byte fertig fuer den GATT-Write."""
    # Header
    b0 = ((msg_type << 5) & 0xE0) | ((encryption << 3) & 0x18) | (FRAME_LMP_SHORT & 0x07)
    header = [b0, cmd_id & 0xFF]
    # Adresse "41:E0" -> [0xE0, 0x41] (letztes, dann vorletztes Byte)
    parts = lmp_addr.split(":")
    addr = [int(parts[-1], 16), int(parts[-2], 16)]
    # Payload auf 15 Byte auffuellen: erst eine 0, dann 0xFF
    pl = list(payload)
    if len(pl) < 15:
        pl.append(0)
    while len(pl) < 15:
        pl.append(0xFF)
    crc = 0
    for x in pl:
        crc ^= x
    full = [crc] + pl                             # 16 Byte (CRC + 15)
    if encryption in (ENC_PUBLIC, ENC_PRIVATE):
        full = encrypt_payload(full, key1, nonce)
    frame = header + addr + full                  # 4 + 16 = 20
    return bytes(frame)


def parse_adv(v):
    """Manufacturer-Data (ohne Company-ID) laut core_ble_type.js."""
    if len(v) < 4:
        return None
    typ, status = v[0], v[1]
    role = typ & 0x07
    src = "%02X:%02X" % (v[3], v[2])              # als 'high:low' lesbar
    return {"role": role, "connected": bool(status & 0x01),
            "registered": bool(status & 0x80),
            "src_bytes": (v[2], v[3]), "src": src}


async def do_scan(seconds=8.0):
    print(f"Scanne {seconds:.0f}s nach Linkio-Advertisern (Company 0x{COMPANY_ID:04X}) ...")
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
        print("  Nichts gefunden. Cube nah genug? Am iPhone die App schliessen "
              "(haelt evtl. die Verbindung).")
    for addr, (dev, adv, info) in found.items():
        print(f"  {addr}  RSSI {adv.rssi:>4}  name={dev.name!r}")
        if info:
            print(f"      role={info['role']} connected={info['connected']} "
                  f"registered={info['registered']} src≈{info['src']} "
                  f"raw={hexs(adv.manufacturer_data[COMPANY_ID])}")
    return found


async def find_device(target_lmp):
    """Sucht das BLE-Geraet, dessen Adv-Source-Adresse zur LMP-Kurzadresse passt."""
    parts = target_lmp.split(":")
    want = {(int(parts[-1], 16), int(parts[-2], 16)),
            (int(parts[-2], 16), int(parts[-1], 16))}
    # Name-Fallback: CubeSmall wirbt als "Bulb1340" (LMP 13:40 -> "1340")
    want_name = "bulb" + parts[-2].lower() + parts[-1].lower()
    print(f"Suche Modul mit LMP {target_lmp} (Adv-Name ~ {want_name!r}) ...")
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
    # bestes RSSI
    dev, adv = max(match.values(), key=lambda t: t[1].rssi)
    print(f"  gefunden: {dev.address} RSSI {adv.rssi}")
    return dev


async def send_frames(dev, frames):
    def on_notify(_char, data):
        print(f"  <- NOTIFY {hexs(data)}")

    async with BleakClient(dev) as client:
        print(f"  verbunden: {client.address}")
        try:
            await client.start_notify(CHAR_UUID, on_notify)
        except Exception as e:
            print(f"  (keine Notifications: {e})")
        for label, fr, wait in frames:
            print(f"  -> {label}: {hexs(fr)}")
            try:
                await client.write_gatt_char(CHAR_UUID, fr, response=False)
            except Exception as e1:
                print(f"     (write-no-response fehlgeschlagen: {e1}; versuche mit response)")
                await client.write_gatt_char(CHAR_UUID, fr, response=True)
            await asyncio.sleep(wait)
        try:
            await client.stop_notify(CHAR_UUID)
        except Exception:
            pass
    print("  fertig, getrennt.")


async def do_scan_all(seconds=10.0):
    print(f"Roh-Scan {seconds:.0f}s: ALLE BLE-Geraete in Reichweite ...")
    seen = {}

    def cb(device, adv):
        seen[device.address] = (device, adv)

    scanner = BleakScanner(detection_callback=cb)
    await scanner.start()
    await asyncio.sleep(seconds)
    await scanner.stop()

    if not seen:
        print("  Gar nichts empfangen (auch keine Handys/Kopfhoerer) -> BT-Problem.")
        return
    # nach RSSI sortiert, staerkstes zuerst
    for addr, (dev, adv) in sorted(seen.items(), key=lambda kv: -kv[1][1].rssi):
        md = adv.manufacturer_data
        md_str = ", ".join("0x%04X=%s" % (k, hexs(v)) for k, v in md.items()) or "-"
        svcs = ", ".join(adv.service_uuids) or "-"
        name = dev.name or adv.local_name or "?"
        print(f"  RSSI {adv.rssi:>4}  name={name!r}")
        print(f"        mfg: {md_str}")
        print(f"        svc: {svcs}")


async def dump_gatt(dev):
    async with BleakClient(dev) as client:
        print(f"  verbunden: {client.address}")
        for svc in client.services:
            print(f"  SERVICE {svc.uuid}")
            for ch in svc.characteristics:
                props = ",".join(ch.properties)
                print(f"      CHAR {ch.uuid}  [{props}]  handle={ch.handle}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action",
                    choices=["scan", "scanall", "gatt", "redtest", "whitetest",
                             "sweep", "on", "off"])
    ap.add_argument("--gap", action="store_true",
                    help="zwischen den Schritten ausschalten (statt direktem Wechsel)")
    ap.add_argument("--hold", type=float, default=3.0,
                    help="Sekunden pro Schritt (Standard 3)")
    ap.add_argument("--small", action="store_true", help="CubeSmall statt CubeLarge")
    ap.add_argument("--group", action="store_true", help="an Gruppe 'Alle' (FF:FF)")
    ap.add_argument("--h", type=float, default=285)
    ap.add_argument("--s", type=float, default=100)
    ap.add_argument("--v", type=float, default=100)
    args = ap.parse_args()

    conf = load_conf()
    key1 = conf["keyCrypt1"]
    nonce = conf["nounceAESCrypt"]
    enc_mode = conf.get("encryptionMode", 2)
    print(f"encryptionMode={enc_mode}  key1={hexs(key1)}  nonce={hexs(nonce)}")

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

    if args.action == "gatt":
        target = ("13:40" if args.small else "41:E0")

        async def find_and_dump():
            dev = await find_device(target)
            if dev is None:
                print("Modul nicht gefunden.")
                return
            await dump_gatt(dev)

        asyncio.run(find_and_dump())
        return

    # Ziel-Adresse + Payload
    if args.group:
        target_lmp = "FF:FF"
        def color(onoff, h, s, v):
            return build_group_color_payload(class_id, onoff, h, s, v)
    else:
        target_lmp = mod["identification"]["lmp_addr"]
        def color(onoff, h, s, v):
            return build_color_payload(dev_index, onoff, h, s, v)

    print(f"Ziel: {modkey} '{mod['identification']['name']}' "
          f"LMP {target_lmp}"
          + ("  (als GRUPPE)" if args.group else ""))

    frames = []
    if args.action == "sweep":
        # (Label, H, S, V) — Weiss laeuft in der App ueber den FARBWEG:
        # niedrige Saettigung => hoher Weiss-Kanal (kalt),
        # warmer Ton mit hoher Saettigung => orange LEDs (warm).
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
                frames.append(("  -> aus",
                               build_frame(target_lmp,
                                           build_color_payload(dev_index, False, h, s, v),
                                           key1, nonce, cmd_id=cid), 1.2))
        cid += 1
        frames.append(("AUS (Ende)",
                       build_frame(target_lmp,
                                   build_color_payload(dev_index, False, 0, 0, 100),
                                   key1, nonce, cmd_id=cid), 0.5))
    elif args.action == "whitetest":
        # A: Weiß MIT 0x80-Flag (WHITE_MODE) — Theorie
        frames.append(("WEISS mit 0x80-Flag",
                       build_frame(target_lmp, build_white_payload(dev_index, True, 100, True),
                                   key1, nonce, cmd_id=1), 4.0))
        frames.append(("AUS", build_frame(target_lmp, build_white_payload(dev_index, False, 100, True),
                                          key1, nonce, cmd_id=2), 2.0))
        # B: Weiß OHNE Flag — Gegenprobe (bisheriges Verhalten)
        frames.append(("WEISS ohne Flag",
                       build_frame(target_lmp, build_white_payload(dev_index, True, 100, False),
                                   key1, nonce, cmd_id=3), 4.0))
        frames.append(("AUS", build_frame(target_lmp, build_white_payload(dev_index, False, 100, False),
                                          key1, nonce, cmd_id=4), 2.0))
    elif args.action == "redtest":
        frames.append(("ROT an", build_frame(target_lmp, color(True, 0, 100, 100), key1, nonce, cmd_id=1), 3.0))
        frames.append(("AUS",    build_frame(target_lmp, color(False, 0, 100, 100), key1, nonce, cmd_id=2), 0.5))
    elif args.action == "on":
        frames.append(("AN", build_frame(target_lmp, color(True, args.h, args.s, args.v), key1, nonce, cmd_id=1), 0.5))
    elif args.action == "off":
        frames.append(("AUS", build_frame(target_lmp, color(False, args.h, args.s, args.v), key1, nonce, cmd_id=1), 0.5))

    # Geraet finden + senden in EINER Event-Loop (CoreBluetooth-Anforderung)
    lookup_lmp = mod["identification"]["lmp_addr"]

    async def find_and_send():
        dev = await find_device(lookup_lmp)
        if dev is None:
            print("Modul nicht per BLE gefunden. 'scan' ausfuehren; App am iPhone schliessen; naeher ran.")
            return False
        await send_frames(dev, frames)
        return True

    ok = asyncio.run(find_and_send())
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
