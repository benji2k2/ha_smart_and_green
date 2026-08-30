"""Constants for the Smart & Green Cube integration."""

DOMAIN = "smartgreen_cube"

# BLE GATT (verified on the device)
SERVICE_UUID = "41c15000-6def-11e5-bcde-0002a5d5c51b"
CHAR_UUID = "00005002-0000-1000-8000-00805f9b34fb"
COMPANY_ID = 0x04AA  # Linkio, as reported by HA's Bluetooth stack

# Config entry data
CONF_KEY = "key_crypt1"
CONF_NONCE = "nonce"
CONF_MODULES = "modules"
CONF_GROUP = "group"

# Module dict fields
MOD_NAME = "name"
MOD_LMP = "lmp_addr"
MOD_INDEX = "index"
MOD_CLASS = "class"
MOD_SW = "sw_version"
MOD_HW = "hw_version"
MOD_MODEL = "model"
MOD_PROPS = "properties"

# Module properties, as the app addresses them (MODULE_PROPERTY_SET/GET).
PROP_LED_STATUS = 0
PROP_KEY_LOCK = 1
PROP_DEEP_SLEEP = 2
PROP_NAMES = {
    PROP_LED_STATUS: "led_status",
    PROP_KEY_LOCK: "key_lock",
    PROP_DEEP_SLEEP: "deep_sleep",
}

# LMP protocol
DEFAULT_CLASS = 19  # color-white-dimmable-light
FADE_COLOR_TRANSITION = 50
