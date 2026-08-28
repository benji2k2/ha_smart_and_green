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


def _process_color(h: float, s: float, v: float) -> dict:
    """processColor() für RGBW-Leuchte (color_mode COLOR, linear aus, rgbw an)."""
    r, g, b = hsv_to_rgb(h, s, 100)
    total = r + g + b
    coeff = 255.0 / total if total else 1.0
    out_v = v * (s / 100.0) * coeff
    white = (100 - s) * (v / 100.0)  # color_mode==COLOR -> ohne +0x80
    return {"h": h, "s": s, "v": out_v, "white": white}


def build_color_payload(index: int, onoff: bool, h: float, s: float, v: float,
                        is_group: bool = False, class_id: int = 19) -> list[int]:
    """RGBW-Payload wie cmdFactory.build_color_control (API>=2)."""
    pc = _process_color(h, s, v)
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
