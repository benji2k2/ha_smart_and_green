"""Shared device description for the light and sensor entities."""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN, MOD_HW, MOD_LMP, MOD_MODEL, MOD_NAME, MOD_SW


def build_device_info(module: dict) -> DeviceInfo:
    """Build the device entry; versions come from the .lap configuration.

    Firmware and hardware version are already part of the imported
    configuration. They could also be queried from the cube via
    ``MODULE_INFO_GET``, but that costs a connection, and connections are the
    scarce resource with these lamps. If the fields are missing (configuration
    imported by an older version) they are simply left empty.
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
