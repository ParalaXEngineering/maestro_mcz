"""Pure protocol layer for the MCZ Maestro+ local BLE link.

Port of the reference client ``mcz_ble_client.py`` (bleak-based) reduced to the
protocol primitives: the AES-128-CBC fixed-IV envelope, the Modbus RTU PDUs, the
CRC16, the frame (de)serialisation and the ``##`` status-broadcast reassembler.

This module has **no** dependency on Home Assistant nor on any BLE stack, so it
can be imported and unit-tested on its own. The secrets below are global for this
firmware generation (see ``reference_ble/ble-protocol.md``); per-device identity is
only the BLE MAC and the serial number (register ``0x0ADC``).
"""

from __future__ import annotations

from collections.abc import Iterator
import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# ---- secrets (global for this firmware generation; verified in the dump) ----
KEY1 = bytes.fromhex("6e296b0bbb1d43f36e47f72e7b6f2e77")
IV0 = bytes.fromhex("da1a557349f25c641b1a368af5b218a7")
TOKEN = bytes.fromhex("31dd34512639377b05a2510de725fc75")  # 16 bytes

assert len(KEY1) == 16 and len(IV0) == 16 and len(TOKEN) == 16  # noqa: S101

# Starting value of the per-message counter (matches the reference client).
COUNTER_START = 0x6A418000


def uuid16(x: int) -> str:
    """Expand a 16-bit Bluetooth UUID to its 128-bit string form."""
    return f"0000{x:04x}-0000-1000-8000-00805f9b34fb"


# ---- BLE UUIDs ----
SVC = uuid16(0xABF0)  # primary service
CH_TX = uuid16(0xABF1)  # write-no-response, commands in
CH_RX = uuid16(0xABF2)  # notify, responses + status pushes out
TARGET_PREFIX = "MCZ_EP"

# ---- register map (from firmware oven.h, verified against a real oven) ----
REG = {
    "setpoint": 0x03F7,
    "power": 0x03EB,
    "mode": 0x03E9,
    "onoff": 0x038A,
    "fan": 0x03FA,
    "silent": 0x03EC,
    "room": 0x02BC,
    "board": 0x02C1,
    "fumes": 0x02C5,
    "active": 0x02C9,
    "fan_comb": 0x02CE,
    "fan_room": 0x02D1,
    "state": 0x0320,
    "phase": 0x0322,
    "alarm": 0x0323,
    "fan_live": 0x0324,
    "mode_live": 0x032E,
    "flags": 0x0332,
    "ignitions": 0x0334,
    "serial": 0x0ADC,
}

MODES = {0: "Manual", 1: "Auto", 2: "Overnight", 3: "Comfort", 4: "Turbo"}
STATES = {
    0x0000: "Off",
    0x0101: "Cleaning",
    0x0201: "Loading",
    0x0301: "Start 1",
    0x0401: "Start 2",
    0x0501: "Stabilization",
    0x0601: "Anti-condensation",
    0x0202: "On",
    0x0103: "Turning off",
}
# Fine-state codes that mean the stove is ramping up (used to derive fase_op).
STATES_STARTING = {0x0101, 0x0201, 0x0301, 0x0401, 0x0501, 0x0601}
STATE_TURNING_OFF = 0x0103

# Base register of the ``##`` status broadcast image.
STATUS_BASE = 0x02BA

# Modbus slave address of the mainboard reached through the panel.
SLAVE = 0x01


def state_name(code: int | None) -> str | None:
    """Return the human label for a fine-state code (``0x0320``)."""
    if code is None:
        return None
    return STATES.get(code, f"0x{code:04X}")


def mode_name(code: int | None) -> str | None:
    """Return the human label for a mode code (``0x03E9`` / ``0x032E``)."""
    if code is None:
        return None
    return MODES.get(code)


def modbus_crc(data: bytes) -> bytes:
    """Modbus CRC16 (poly ``0xA001``) returned little-endian (lo, hi)."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if (crc & 1) else (crc >> 1)
    return bytes([crc & 0xFF, (crc >> 8) & 0xFF])


def aes_encrypt(plain: bytes) -> bytes:
    """AES-128-CBC encrypt with the fixed IV (never chained)."""
    encryptor = Cipher(algorithms.AES(KEY1), modes.CBC(IV0)).encryptor()
    return encryptor.update(plain) + encryptor.finalize()


def aes_decrypt(cipher: bytes) -> bytes:
    """AES-128-CBC decrypt with the fixed IV (never chained)."""
    decryptor = Cipher(algorithms.AES(KEY1), modes.CBC(IV0)).decryptor()
    return decryptor.update(cipher) + decryptor.finalize()


def build_frame(pdu: bytes, counter: int) -> bytes:
    """Wrap a Modbus PDU into an encrypted frame.

    Layout (plaintext): ``[4B counter LE][16B token][pdu][crc16 LE][PKCS#7]``.
    """
    body = struct.pack("<I", counter & 0xFFFFFFFF) + TOKEN + pdu + modbus_crc(pdu)
    pad = 16 - (len(body) % 16)
    if pad == 0:
        pad = 16
    body += bytes([pad]) * pad
    return aes_encrypt(body)


def modbus_read_pdu(reg: int, count: int) -> bytes:
    """Build a Modbus function ``0x03`` (read holding registers) PDU."""
    return bytes([SLAVE, 0x03, reg >> 8, reg & 0xFF, count >> 8, count & 0xFF])


def modbus_write_pdu(reg: int, val: int) -> bytes:
    """Build a Modbus function ``0x06`` (write single register) PDU."""
    return bytes(
        [SLAVE, 0x06, reg >> 8, reg & 0xFF, (val >> 8) & 0xFF, val & 0xFF]
    )


def parse_frame(cipher: bytes) -> tuple[int | None, bytes | None] | None:
    """Decrypt and validate an AES-Modbus frame.

    Returns ``(counter, pdu)`` on success, ``(None, None)`` on a token mismatch,
    or ``None`` when the buffer is not a valid AES block.
    """
    if len(cipher) % 16 or len(cipher) < 32:
        return None
    plain = aes_decrypt(cipher)
    counter = struct.unpack_from("<I", plain, 0)[0]
    if plain[4:20] != TOKEN:
        return None, None
    pad = plain[-1]
    end = len(plain) - pad if 1 <= pad <= 16 else len(plain)
    return counter, plain[20:end]


def decode_read_response(pdu: bytes, base_reg: int) -> Iterator[tuple[int, int]]:
    """Yield ``(register, value)`` pairs from a function ``0x03`` response.

    A read response is ``01 03 <byteCount> <data..> <crc>`` and does **not**
    carry the base address, so the caller must supply the base of the last read.
    """
    if len(pdu) < 3 or pdu[1] != 0x03:
        return
    nbytes = pdu[2]
    data = pdu[3 : 3 + nbytes]
    for i in range(0, len(data) - 1, 2):
        yield base_reg + i // 2, (data[i] << 8) | data[i + 1]


def decode_write_echo(pdu: bytes) -> tuple[int, int] | None:
    """Return ``(register, value)`` from a function ``0x06`` echo, else ``None``."""
    if len(pdu) < 6 or pdu[1] != 0x06:
        return None
    return (pdu[2] << 8) | pdu[3], (pdu[4] << 8) | pdu[5]


class BroadcastReassembler:
    """Reassembler for the fragmented ``##`` status broadcasts on ``0xABF2``.

    A broadcast is prefixed ``23 23 <type> <frag>`` (fragment ``0x01`` first,
    ``0x02`` last) and, once reassembled, decrypts to a register image starting
    at :data:`STATUS_BASE`. A ``##`` payload length is never a multiple of 16,
    which discriminates it from an AES-Modbus frame.
    """

    def __init__(self) -> None:
        """Initialise an empty reassembler."""
        self._buf = bytearray()
        self._active = False

    @staticmethod
    def is_broadcast(data: bytes) -> bool:
        """Return whether ``data`` is a ``##`` broadcast fragment."""
        return len(data) >= 4 and data[0] == 0x23 and data[1] == 0x23 and bool(
            len(data) % 16
        )

    def feed(self, data: bytes) -> list[tuple[int, int]] | None:
        """Feed one fragment; return decoded ``(reg, val)`` pairs when complete.

        Returns ``None`` while more fragments are expected or on any decode
        failure (token mismatch, misaligned length, unexpected fragment id).
        """
        frag = data[3]
        if frag == 0x01:
            self._buf = bytearray()
            self._active = True
        elif frag != 0x02:
            return None
        if not self._active:
            return None
        self._buf += data[4:]
        if frag != 0x02:
            return None  # wait for the final fragment
        self._active = False
        if len(self._buf) % 16:
            return None
        plain = aes_decrypt(bytes(self._buf))
        if plain[4:20] != TOKEN:
            return None
        pad = plain[-1]
        end = len(plain) - pad if 1 <= pad <= 16 else len(plain)
        payload = plain[20:end]
        return [
            (STATUS_BASE + i // 2, (payload[i] << 8) | payload[i + 1])
            for i in range(0, len(payload) - 1, 2)
        ]
