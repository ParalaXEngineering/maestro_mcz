"""Constants for the maestro_mcz integration."""

DOMAIN = "maestro_mcz"
MANUFACTURER = "MCZ"
DEFAULT_POLLING_INTERVAL = 30
MOCKED_FOLDER = "MOCKED_FOLDER"

# Transport selection (stored in entry.data["transport"]).
CONF_TRANSPORT = "transport"
TRANSPORT_CLOUD = "cloud"
TRANSPORT_BLE = "bluetooth"
TRANSPORT_HYBRID = "hybrid"

# BLE device MAC address (stored in entry.data["mac"]).
CONF_MAC = "mac"

# Local advertisement name prefix of the MCZ panel.
BLE_LOCAL_NAME_PREFIX = "MCZ_EP"
