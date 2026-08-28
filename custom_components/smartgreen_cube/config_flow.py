"""Config-Flow für Smart & Green Cube."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.selector import (
    FileSelector,
    FileSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_GROUP,
    CONF_KEY,
    CONF_MODULES,
    CONF_NONCE,
    DOMAIN,
)
from .lap import (
    LapError,
    LapWrongPassword,
    decrypt_lap,
    extract_keys,
    extract_modules,
)

_LOGGER = logging.getLogger(__name__)

TITLE = "Smart & Green Cube"


def _parse_hex16(value: str) -> bytes:
    raw = bytes.fromhex(value.replace(":", "").replace(" ", ""))
    if len(raw) != 16:
        raise ValueError("16 Byte erwartet")
    return raw


class SmartGreenConfigFlow(ConfigFlow, domain=DOMAIN):
    """Führt durch die Einrichtung — .lap-Import oder manuelle Schlüsseleingabe."""

    VERSION = 1

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Ein Cube wurde per BLE entdeckt -> zur normalen Einrichtung führen."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return await self.async_step_user()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return self.async_show_menu(
            step_id="user",
            menu_options=["import_lap", "manual"],
        )

    async def async_step_import_lap(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Konfigurationsdatei (.lap) hochladen und mit Passwort entschlüsseln."""
        errors: dict[str, str] = {}
        if user_input is not None:
            file_id = user_input["file"]
            password = user_input["password"]

            def _read() -> bytes:
                with process_uploaded_file(self.hass, file_id) as path:
                    return path.read_bytes()

            try:
                raw = await self.hass.async_add_executor_job(_read)
                config = await self.hass.async_add_executor_job(
                    decrypt_lap, raw, password
                )
                key1, nonce, _mode = extract_keys(config)
                modules, group = extract_modules(config)
            except LapWrongPassword:
                errors["base"] = "wrong_password"
            except (LapError, KeyError, ValueError) as err:
                _LOGGER.warning("lap-Import fehlgeschlagen: %s", err)
                errors["base"] = "invalid_file"
            else:
                if not modules:
                    errors["base"] = "no_modules"
                else:
                    return self.async_create_entry(
                        title=TITLE,
                        data={
                            CONF_KEY: key1.hex(),
                            CONF_NONCE: nonce.hex(),
                            CONF_MODULES: modules,
                            CONF_GROUP: group,
                        },
                    )

        return self.async_show_form(
            step_id="import_lap",
            data_schema=vol.Schema(
                {
                    vol.Required("file"): FileSelector(
                        FileSelectorConfig(accept=".lap,application/octet-stream")
                    ),
                    vol.Required("password"): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Schlüssel und Nonce manuell als Hex eingeben (Fallback ohne Datei)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                key1 = _parse_hex16(user_input[CONF_KEY])
                nonce = _parse_hex16(user_input[CONF_NONCE])
            except ValueError:
                errors["base"] = "invalid_hex"
            else:
                return self.async_create_entry(
                    title=TITLE,
                    data={
                        CONF_KEY: key1.hex(),
                        CONF_NONCE: nonce.hex(),
                        CONF_MODULES: [],  # per BLE-Scan automatisch ergänzt
                        CONF_GROUP: None,
                    },
                )

        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_KEY): str,
                    vol.Required(CONF_NONCE): str,
                }
            ),
            errors=errors,
        )
