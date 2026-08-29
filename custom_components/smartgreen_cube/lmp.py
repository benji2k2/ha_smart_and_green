"""LMP-Protokoll (Linkio Mesh Protocol) — Frame-Bau und Verschlüsselung.

Repliziert 1:1 die Logik der Hersteller-App (``cmdFactory.build_color_control``
und ``connection.ble.js``), am echten Gerät verifiziert.
"""
from __future__ import annotations

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .const import FADE_COLOR_TRANSITION

# LMP-Header-Konstanten (aus core_ble_type.js)
_MSG_CMD_NO_ACK = 0x00
_ENC_PRIVATE = 0x02
_FRAME_LMP_SHORT = 0x02
_OP_DEVICE_DATA_SET = 0x41
_OP_GROUP_DATA_SET = 0x56
_LED_MODE_COLOR = 0x01


def hsv_to_rgb(h: float, s: float, v: float) -> tuple[int, int, int]:
    """tinycolor-kompatible HSV→RGB-Umrechnung. h[0-360], s,v[0-100]."""
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


def temp_to_hs(kelvin: float) -> tuple[float, float]:
    """Farbtemperatur -> (Hue, Sättigung) wie ``TempToHSV`` der App.

    Warmweiß entsteht bei diesen Leuchten über den *Farbweg*: ein warmer
    Ton mit hoher Sättigung. Erst bei Sättigung 0 übernimmt der (kalte)
    Weiß-Kanal.
    """
    import math

    t = max(1000.0, min(40000.0, kelvin)) / 100.0
    if t <= 66:
        re = 255.0
        x = max(t - 2, 1e-6)
        gr = min(255.0, -155.25485562709179 - 0.44596950469579133 * x
                 + 104.49216199393888 * math.log(x))
    else:
        x = max(t - 55, 1e-6)
        re = min(255.0, 351.97690566805693 + 0.114206453784165 * x
                 - 40.25366309332127 * math.log(x))
        x = max(t - 50, 1e-6)
        gr = min(255.0, 325.4494125711974 + 0.07943456536662342 * x
                 - 28.0852963507957 * math.log(x))
    if t >= 66:
        bl = 255.0
    elif t < 19:
        bl = 0.0
    else:
        x = max(t - 10, 1e-6)
        bl = min(255.0, -254.76935184120902 + 0.8274096064007395 * x
                 + 115.67994401066147 * math.log(x))

    r, g, b = max(0.0, re), max(0.0, gr), max(0.0, bl)
    mx, mn = max(r, g, b), min(r, g, b)
    if mx == 0:
        return 0.0, 0.0
    s = (mx - mn) / mx * 100.0
    if mx == mn:
        h = 0.0
    elif mx == r:
        h = (60 * ((g - b) / (mx - mn))) % 360
    elif mx == g:
        h = 60 * ((b - r) / (mx - mn)) + 120
    else:
        h = 60 * ((r - g) / (mx - mn)) + 240
    return h % 360, s


def _process_color(h: float, s: float, v: float) -> dict:
    """processColor() für RGBW-Leuchte (color_mode COLOR, linear aus, rgbw an)."""
    r, g, b = hsv_to_rgb(h, s, 100)
    total = r + g + b
    coeff = 255.0 / total if total else 1.0
    out_v = v * (s / 100.0) * coeff
    white = (100 - s) * (v / 100.0)  # color_mode==COLOR -> ohne +0x80
    return {"h": h, "s": s, "v": out_v, "white": white}


def build_color_payload(index: int, onoff: bool, h: float, s: float, v: float,
                        is_group: bool = False, class_id: int = 19,
                        white: float | None = None,
                        white_mode: bool = False) -> list[int]:
    """RGBW-Payload wie cmdFactory.build_color_control (API>=2).

    ``white`` (0..100) überschreibt den aus der Sättigung abgeleiteten
    Weiß-Wert der App — nötig, wenn Home Assistant den Weiß-Kanal (RGBW)
    explizit vorgibt.

    ``white_mode`` entspricht ``color_mode != COLOR_MODE`` in der App: dort
    wird der Weiß-Wert ab API 2 um ``0x80`` erhöht — dieses Bit schaltet den
    weißen Kanal aktiv. Ohne das Flag ignoriert die Leuchte reines Weiß.
    """
    pc = _process_color(h, s, v)
    if white is not None:
        pc["white"] = max(0.0, min(100.0, white))
    if white_mode:
        pc["white"] = min(100.0, pc["white"]) + 0x80
    op = _OP_GROUP_DATA_SET if is_group else _OP_DEVICE_DATA_SET
    idx = class_id if is_group else index
    b = [
        0,                                              # [0] Länge (unten gesetzt)
        op,                                             # [1] Opcode
        idx & 0xFF,                                     # [2] Device-Index / Class-ID
        (1 if onoff else 0) + (_LED_MODE_COLOR << 4),   # [3] onoff | ledmode<<4
        int(pc["v"]) & 0xFF,                            # [4] V
        int(pc["h"]) & 0xFF,                            # [5] H low
        (int(pc["h"]) >> 8) & 0xFF,                     # [6] H high
        int(pc["s"]) & 0xFF,                            # [7] S
        FADE_COLOR_TRANSITION & 0xFF,                   # [8] param low
        (FADE_COLOR_TRANSITION >> 8) & 0xFF,            # [9] param high
        int(pc["white"]) & 0xFF,                        # [10] white (RGBW)
    ]
    b[0] = len(b) - 1
    return b


# Nur die Opcodes, die tatsächlich im Advertisement auftauchen.
_LMP_NAMES = {
    0x80: "status_ack", 0x81: "status_registration", 0x82: "status_network",
    0x83: "status_heartbeat", 0x84: "status_network_gateway_list",
    0x8F: "status_device_info", 0x93: "status_device_data",
    0xAF: "module_reference", 0xB0: "mac_address", 0xB1: "short_address",
    0xB4: "module_type", 0xB5: "sw_version", 0xC0: "battery_level",
    0xC2: "rssi",
}


def _keystream(key1: bytes, nonce: bytes) -> bytes:
    enc = Cipher(algorithms.AES(key1), modes.ECB()).encryptor()  # noqa: S305
    return enc.update(bytes(nonce)) + enc.finalize()


def decode_advertisement(md: bytes, key1: bytes, nonce: bytes) -> dict | None:
    """Wertet die Linkio-Manufacturer-Daten eines Advertisements aus.

    ``md`` ist der Teil *hinter* der Company-ID, so wie HAs Bluetooth-Stack ihn
    liefert. Der Payload ist mit demselben Verfahren wie Befehle verschlüsselt
    (XOR mit AES-ECB über den Nonce) und durch eine XOR-Prüfsumme gesichert.
    Rückgabe ``None``, wenn das Paket zu kurz ist oder die Prüfsumme nicht passt.

    Achtung: Die Cubes senden hier ihren *Netzwerk*-Zustand — An/Aus und Farbe
    stehen nicht darin.
    """
    if len(md) < 24:
        return None
    hdr, status = md[0], md[1]
    payload = bytes(a ^ b for a, b in zip(md[8:24], _keystream(key1, nonce)))
    crc_rx, body = payload[0], payload[1:16]
    crc = 0
    for byte in body:
        crc ^= byte
    if crc != crc_rx:
        return None

    fields: dict[str, str] = {}
    i = 0
    while i < len(body):
        size = body[i]
        if size == 0:
            break
        typ = body[i + 1]
        data = body[i + 2:i + 1 + size]
        fields[_LMP_NAMES.get(typ, f"opcode_{typ:#04x}")] = data.hex(" ")
        i += 1 + size

    return {
        "src": "%02X:%02X" % (md[3], md[2]),
        "registered": bool(status & 0x80),
        "connected": bool(status & 0x01),
        "role": hdr & 0x07,
        "fields": fields,
    }


def _encrypt(full16: list[int], key1: bytes, nonce: bytes) -> list[int]:
    enc = Cipher(algorithms.AES(key1), modes.ECB()).encryptor()  # noqa: S305
    keystream = enc.update(bytes(nonce)) + enc.finalize()
    return [full16[i] ^ keystream[i] for i in range(16)]


def build_frame(lmp_addr: str, payload: list[int], key1: bytes, nonce: bytes,
                cmd_id: int = 1) -> bytes:
    """sendBytesCmd() Short-Frame (PRIVATE-Encryption) -> 20-Byte-GATT-Write."""
    b0 = (
        ((_MSG_CMD_NO_ACK << 5) & 0xE0)
        | ((_ENC_PRIVATE << 3) & 0x18)
        | (_FRAME_LMP_SHORT & 0x07)
    )
    parts = lmp_addr.split(":")
    addr = [int(parts[-1], 16), int(parts[-2], 16)]  # letztes, dann vorletztes Byte
    header = [b0, cmd_id & 0xFF]

    pl = list(payload)
    if len(pl) < 15:
        pl.append(0)
    while len(pl) < 15:
        pl.append(0xFF)
    crc = 0
    for x in pl:
        crc ^= x
    full = _encrypt([crc] + pl, key1, nonce)
    return bytes(header + addr + full)
