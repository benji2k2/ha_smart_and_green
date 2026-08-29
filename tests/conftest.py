"""Load the integration without Home Assistant installed.

The component is a handful of modules that import a lot of Home Assistant. A
full HA test harness would be heavy for what is essentially protocol code, so
the few HA names actually used are stubbed here and the modules are executed
under a synthetic ``sg`` package.

Everything below the stubs is the real integration code.
"""
from __future__ import annotations

import enum
import pathlib
import sys
import types

COMPONENT = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "smartgreen_cube"


def _module(name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod
    return mod


class _ColorMode(str, enum.Enum):
    HS = "hs"
    COLOR_TEMP = "color_temp"
    ONOFF = "onoff"


class _HomeAssistantError(Exception):
    """Stand-in for homeassistant.exceptions.HomeAssistantError."""


class _ExtraStoredData:
    """Stand-in for homeassistant.helpers.restore_state.ExtraStoredData."""

    def as_dict(self) -> dict:  # pragma: no cover - overridden by the component
        raise NotImplementedError


class _RestoreEntity:
    async def async_added_to_hass(self) -> None:
        return None

    async def async_get_last_state(self):
        return None

    async def async_get_last_extra_data(self):
        return None


def _install_stubs() -> None:
    _module("bleak_retry_connector",
            BleakClientWithServiceCache=object, establish_connection=None)
    _module("homeassistant")
    _module("homeassistant.components")
    _module("homeassistant.components.bluetooth",
            async_discovered_service_info=lambda *a, **k: [],
            async_last_service_info=lambda *a, **k: None,
            async_ble_device_from_address=lambda *a, **k: None)
    _module("homeassistant.components.light",
            ATTR_BRIGHTNESS="brightness",
            ATTR_COLOR_TEMP_KELVIN="color_temp_kelvin",
            ATTR_HS_COLOR="hs_color",
            ColorMode=_ColorMode,
            LightEntity=type("LightEntity", (), {}))
    _module("homeassistant.components.sensor",
            SensorDeviceClass=types.SimpleNamespace(
                SIGNAL_STRENGTH="signal_strength", TIMESTAMP="timestamp"),
            SensorEntity=type("SensorEntity", (), {}),
            SensorStateClass=types.SimpleNamespace(MEASUREMENT="measurement"))
    _module("homeassistant.config_entries", ConfigEntry=object)
    _module("homeassistant.const",
            STATE_ON="on", STATE_UNAVAILABLE="unavailable",
            EntityCategory=types.SimpleNamespace(DIAGNOSTIC="diagnostic"),
            SIGNAL_STRENGTH_DECIBELS_MILLIWATT="dBm")
    _module("homeassistant.core", HomeAssistant=object)
    _module("homeassistant.exceptions", HomeAssistantError=_HomeAssistantError)
    _module("homeassistant.helpers")
    _module("homeassistant.helpers.device_registry", DeviceInfo=dict)
    _module("homeassistant.helpers.entity_platform", AddEntitiesCallback=object)
    _module("homeassistant.helpers.restore_state",
            ExtraStoredData=_ExtraStoredData, RestoreEntity=_RestoreEntity)


def load() -> types.ModuleType:
    """Import the integration and return the synthetic ``sg`` package."""
    if "sg" in sys.modules:
        return sys.modules["sg"]

    _install_stubs()
    pkg = types.ModuleType("sg")
    pkg.__path__ = [str(COMPONENT)]
    sys.modules["sg"] = pkg

    for name in ("const", "lmp", "device", "lap", "light", "sensor"):
        source = (COMPONENT / f"{name}.py").read_text().replace("from .", "from sg.")
        module = types.ModuleType(f"sg.{name}")
        module.__package__ = "sg"
        sys.modules[f"sg.{name}"] = module
        exec(compile(source, f"{name}.py", "exec"), module.__dict__)
        setattr(pkg, name, module)
    return pkg
