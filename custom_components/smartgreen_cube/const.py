"""Constants for the Smart & Green Cube integration."""

DOMAIN = "smartgreen_cube"

# BLE-GATT (am Gerät verifiziert)
SERVICE_UUID = "41c15000-6def-11e5-bcde-0002a5d5c51b"
CHAR_UUID = "00005002-0000-1000-8000-00805f9b34fb"
COMPANY_ID = 0x04AA  # Linkio, wie von HAs Bluetooth-Stack gemeldet

# Config-Entry-Daten
CONF_KEY = "key_crypt1"
CONF_NONCE = "nonce"
CONF_MODULES = "modules"
CONF_GROUP = "group"

# Modul-Dict-Felder
MOD_NAME = "name"
MOD_LMP = "lmp_addr"
MOD_INDEX = "index"
MOD_CLASS = "class"
MOD_SW = "sw_version"
MOD_HW = "hw_version"
MOD_MODEL = "model"

# LMP-Protokoll
DEFAULT_CLASS = 19  # color-white-dimmable-light
FADE_COLOR_TRANSITION = 50
