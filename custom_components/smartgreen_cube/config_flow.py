"""Config flow for Smart & Green Cube."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    FileSelector,
    FileSelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_CONFIRM_WAIT,
    CONF_GROUP,
    CONF_IDLE_DISCONNECT,
    CONF_KEY,
    CONF_MODULES,
    CONF_NONCE,
    DEFAULT_CONFIRM_WAIT,
    DEFAULT_IDLE_DISCONNECT,
    DOMAIN,
    MAX_CONFIRM_WAIT,
    MAX_IDLE_DISCONNECT,
    MIN_CONFIRM_WAIT,
    MIN_IDLE_DISCONNECT,
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
    """Guides through setup — .lap import or entering keys by hand."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return SmartGreenOptionsFlow()

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """A cube was discovered over BLE -> continue with the normal setup."""
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
        """Upload the configuration file (.lap) and decrypt it with the password."""
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
        """Enter key and nonce as hex by hand (fallback without a file)."""
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
                        CONF_MODULES: [],  # filled in automatically by BLE scan
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


class SmartGreenOptionsFlow(OptionsFlow):
    """Two timing choices that are genuinely the user's to make.

    *Keep connection open* trades latency against battery: a held connection
    makes the next command immediate, but a connected cube keeps its radio
    awake while an idle one only advertises. 0 disconnects straight away.

    *Wait for confirmation* trades responsiveness against honesty. Waiting
    means the state shown is one the cube confirmed; not waiting means it
    appears instantly but may be wrong. Most commands confirm within a few
    seconds, so a short wait covers nearly all of them. 0 never waits.
    """

    async def async_step_init(self, user_input: dict[str, Any] | None = None
                              ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data={
                CONF_IDLE_DISCONNECT: int(user_input[CONF_IDLE_DISCONNECT]),
                CONF_CONFIRM_WAIT: int(user_input[CONF_CONFIRM_WAIT]),
            })

        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required(
                    CONF_IDLE_DISCONNECT,
                    default=options.get(CONF_IDLE_DISCONNECT,
                                        DEFAULT_IDLE_DISCONNECT),
                ): NumberSelector(NumberSelectorConfig(
                    min=MIN_IDLE_DISCONNECT, max=MAX_IDLE_DISCONNECT, step=10,
                    unit_of_measurement="s", mode=NumberSelectorMode.BOX)),
                vol.Required(
                    CONF_CONFIRM_WAIT,
                    default=options.get(CONF_CONFIRM_WAIT,
                                        DEFAULT_CONFIRM_WAIT),
                ): NumberSelector(NumberSelectorConfig(
                    min=MIN_CONFIRM_WAIT, max=MAX_CONFIRM_WAIT, step=1,
                    unit_of_measurement="s", mode=NumberSelectorMode.BOX)),
            }),
        )
