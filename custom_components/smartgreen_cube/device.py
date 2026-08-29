"""Gemeinsame Geräte-Beschreibung für Licht- und Sensor-Entitäten."""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN, MOD_HW, MOD_LMP, MOD_MODEL, MOD_NAME, MOD_SW


def build_device_info(module: dict) -> DeviceInfo:
    """Baut den Geräteeintrag; Versionen stammen aus der .lap-Konfiguration.

    Firmware- und Hardware-Version stehen bereits in der importierten
    Konfiguration. Sie liessen sich auch per ``MODULE_INFO_GET`` vom Cube
    abfragen — das kostet aber eine Verbindung, und die ist bei diesen Leuchten
    das knappe Gut. Fehlen die Felder (Konfiguration einer aelteren Version),
    bleiben sie schlicht leer.
    """
    lmp = module[MOD_LMP]
    return DeviceInfo(
        identifiers={(DOMAIN, lmp)},
        name=module.get(MOD_NAME) or f"Cube {lmp}",
        manufacturer="Smart & Green / Linkio",
        model=module.get(MOD_MODEL) or "Cube RGBW",
        sw_version=module.get(MOD_SW),
        hw_version=module.get(MOD_HW),
    )
