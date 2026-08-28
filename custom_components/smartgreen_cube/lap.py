"""Entschlüsselung der Smart-&-Green-Konfigurationsdatei (``internal.config.lap``).

Die App (Cordova-Plugin ``linkio-secured-zip`` auf Basis von zip4j) speichert die
Konfiguration als ZIP mit klassischer PKWARE-ZipCrypto-Verschlüsselung. Wichtig:
zip4j schreibt ein abweichendes "Check-Byte", weshalb Pythons ``zipfile`` das
richtige Passwort fälschlich ablehnt. Wir entschlüsseln daher von Hand und
verifizieren über die CRC des entpackten Inhalts statt über das Check-Byte.
"""
from __future__ import annotations

import json
import struct
import zlib
from typing import Any

from .const import DEFAULT_CLASS, MOD_CLASS, MOD_INDEX, MOD_LMP, MOD_NAME

# Standard-CRC32-Tabelle (reflektiertes Polynom 0xEDB88320) für ZipCrypto.
_CRC_TABLE = []
for _i in range(256):
    _c = _i
    for _ in range(8):
        _c = (_c >> 1) ^ (0xEDB88320 if (_c & 1) else 0)
    _CRC_TABLE.append(_c & 0xFFFFFFFF)


class LapError(Exception):
    """Basisfehler beim Verarbeiten der .lap-Datei."""


class LapWrongPassword(LapError):
    """Falsches Passwort (CRC des entpackten Inhalts stimmt nicht)."""


def _zipcrypto_decrypt(enc: bytes, password: bytes) -> bytes:
    k = [0x12345678, 0x23456789, 0x34567890]

    def crc32(ch: int, crc: int) -> int:
        return ((crc >> 8) ^ _CRC_TABLE[(crc ^ ch) & 0xFF]) & 0xFFFFFFFF

    def upd(ch: int) -> None:
        k[0] = crc32(ch, k[0])
        k[1] = (k[1] + (k[0] & 0xFF)) & 0xFFFFFFFF
        k[1] = (k[1] * 134775813 + 1) & 0xFFFFFFFF
        k[2] = crc32((k[1] >> 24) & 0xFF, k[2])

    for c in password:
        upd(c)

    out = bytearray()
    for b in enc:
        t = (k[2] | 2) & 0xFFFF
        c = b ^ (((t * (t ^ 1)) >> 8) & 0xFF)
        out.append(c)
        upd(c)
    return bytes(out)


def decrypt_lap(raw: bytes, password: str) -> list[dict[str, Any]]:
    """Entschlüsselt eine .lap-Datei und gibt die JSON-Konfiguration (Liste) zurück."""
    if raw[:4] != b"PK\x03\x04":
        raise LapError("Keine ZIP-/lap-Datei")
    (
        _sig, _ver, flags, comp, _mt, _md, crc, csize, _usize, nlen, elen,
    ) = struct.unpack("<IHHHHHIIIHH", raw[:30])
    if not (flags & 0x0001):
        raise LapError("Datei ist nicht verschlüsselt")
    off = 30 + nlen + elen
    enc = raw[off:off + csize]

    dec = _zipcrypto_decrypt(enc, password.encode())
    body = dec[12:]  # erste 12 Byte = Verschlüsselungs-Header
    try:
        content = zlib.decompress(body, -15) if comp == 8 else body
    except zlib.error as err:
        raise LapWrongPassword("Falsches Passwort") from err
    if (zlib.crc32(content) & 0xFFFFFFFF) != crc:
        raise LapWrongPassword("Falsches Passwort")

    try:
        return json.loads(content)
    except json.JSONDecodeError as err:  # pragma: no cover
        raise LapError("Konfiguration nicht lesbar") from err


def _as_map(config: list[dict[str, Any]]) -> dict[str, Any]:
    return {e["key"]: e["data"] for e in config if "key" in e}


def extract_keys(config: list[dict[str, Any]]) -> tuple[bytes, bytes, int]:
    """Liefert (key_crypt1, nonce, encryption_mode)."""
    m = _as_map(config)
    key1 = bytes(m["keyCrypt1"])
    nonce = bytes(m["nounceAESCrypt"])
    mode = int(m.get("encryptionMode", 2))
    if len(key1) != 16 or len(nonce) != 16:
        raise LapError("Schlüssel/Nonce haben nicht 16 Byte")
    return key1, nonce, mode


def extract_modules(config: list[dict[str, Any]]) -> tuple[list[dict], dict | None]:
    """Liefert (Module, Gruppe) mit Name, LMP-Adresse, Device-Index und Klasse."""
    m = _as_map(config)
    modules: list[dict] = []
    for key, val in m.items():
        if not key.startswith("mod_") or not isinstance(val, dict):
            continue
        ident = val.get("identification", {})
        lmp = ident.get("lmp_addr")
        if not lmp:
            continue
        index = 0
        cls = DEFAULT_CLASS
        devs = val.get("links", {}).get("devices") or []
        if devs and devs[0] in m:
            dev = m[devs[0]]
            index = dev.get("identification", {}).get("index", 0)
            cls = dev.get("identification", {}).get("type", {}).get("class_id", DEFAULT_CLASS)
        modules.append({
            MOD_NAME: ident.get("name", f"Cube {lmp}"),
            MOD_LMP: lmp,
            MOD_INDEX: index,
            MOD_CLASS: cls,
        })

    group = None
    for key, val in m.items():
        if key.startswith("grp_") and isinstance(val, dict):
            ident = val.get("identification", {})
            group = {
                MOD_NAME: ident.get("name", "Alle"),
                MOD_LMP: ident.get("lmp_addr", "FF:FF"),
                MOD_INDEX: 0,
                MOD_CLASS: ident.get("class", DEFAULT_CLASS),
            }
            break

    modules.sort(key=lambda d: d[MOD_NAME])
    return modules, group
