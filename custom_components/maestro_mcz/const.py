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

# Read-only diagnostic mode (entry.options["read_only"], falling back to
# entry.data["read_only"]). When set, the BLE layer refuses every write.
CONF_READ_ONLY = "read_only"
# Existing entries have no such key: they must keep behaving exactly as before.
DEFAULT_READ_ONLY = False
# A *new* Bluetooth entry, on the other hand, is created read-only: the point of
# the local link has to be proven on reads before it is allowed to press
# anything on a combustion appliance.
DEFAULT_READ_ONLY_NEW_BLE_ENTRY = True

# Diagnostic services (register probing; read-only by construction).
SERVICE_BLE_READ_REGISTERS = "ble_read_registers"
SERVICE_BLE_PROBE_FANS = "ble_probe_fans"
SERVICE_BLE_DUMP = "ble_dump"

ATTR_START = "start"
ATTR_END = "end"
ATTR_COUNT = "count"
ATTR_ENTRY_ID = "entry_id"

# Modbus caps a single function-0x03 answer at 125 registers.
MAX_READ_COUNT = 125
# ble_dump slices its range into small blocks so one silent area does not sink
# the whole sweep, and refuses oversized ranges outright.
DUMP_BLOCK_SIZE = 32
DUMP_MAX_REGISTERS = 512
