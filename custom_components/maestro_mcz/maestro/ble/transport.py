"""Bluetooth transport for the MCZ Maestro+ panel, using the Home Assistant BLE API.

Connection is established through Home Assistant's Bluetooth stack
(``bluetooth.async_ble_device_from_address`` + ``bleak_retry_connector``), so it
transparently works through ESPHome BLE proxies. This layer owns the live
register image, serialises reads (a single function ``0x03`` outstanding at a
time), and exposes the small set of operations the controllers need.
"""

from __future__ import annotations

import asyncio
import logging

from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    establish_connection,
)

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant

from . import protocol, registers

_LOGGER = logging.getLogger(__name__)

READ_TIMEOUT = 2.5  # seconds to wait for a function-0x03 reply
CAP_SCAN_TIMEOUT = 3.0

# Poll plan: (base register, count). Covers temps, the state/phase block, the
# setpoint, the mode/power pair and silent. State pushes (##) refresh the rest.
POLL_READS: list[tuple[int, int]] = [
    (0x02BC, 0x33),  # room/board/fumes/active/rpm block (the app's main read)
    (0x0320, 0x22),  # state, phase, live fan, live mode, flags, counters
    (0x03E9, 3),  # mode (0x03E9), _, power (0x03EB)
    (0x03F7, 1),  # setpoint
    (0x03EC, 1),  # silent
]


class MczBleTransport:
    """Maintains a BLE link to one MCZ panel and its live register image."""

    def __init__(
        self, hass: HomeAssistant, address: str, name: str | None = None
    ) -> None:
        """Initialise the transport for a given MAC address."""
        self._hass = hass
        self._address = address
        self._name = name or f"MCZ {address}"
        self._client: BleakClientWithServiceCache | None = None
        self._tx: BleakGATTCharacteristic | None = None
        self._counter = protocol.COUNTER_START
        self._regs: dict[int, int] = {}
        self._caps: dict[str, object] = {}
        self._serial: str = ""
        self._reassembler = protocol.BroadcastReassembler()
        self._read_expect: tuple[int, int] | None = None
        self._read_ok = False
        self._read_event = asyncio.Event()
        self._io_lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()

    # -- properties ---------------------------------------------------------
    @property
    def address(self) -> str:
        """Return the target MAC address."""
        return self._address

    @property
    def connected(self) -> bool:
        """Return whether the BLE link is currently up."""
        return self._client is not None and self._client.is_connected

    @property
    def regs(self) -> dict[int, int]:
        """Return a copy of the live register image."""
        return dict(self._regs)

    @property
    def caps(self) -> dict[str, object]:
        """Return the last capability-scan result."""
        return dict(self._caps)

    @property
    def serial(self) -> str:
        """Return the decoded stove serial number (may be empty)."""
        return self._serial

    # -- connection ---------------------------------------------------------
    async def ensure_connected(self) -> bool:
        """Connect if not already connected. Never raises; returns success."""
        if self.connected:
            return True
        async with self._connect_lock:
            if self.connected:
                return True
            try:
                await self._connect()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("BLE connect to %s failed: %s", self._address, err)
                return False
            return self.connected

    async def _connect(self) -> None:
        """Establish the BLE link and subscribe to notifications."""
        device = bluetooth.async_ble_device_from_address(
            self._hass, self._address, connectable=True
        )
        if device is None:
            raise BleConnectionError(
                f"BLE device {self._address} not found (out of range / not advertising)"
            )

        def _on_disconnect(_client: BleakClientWithServiceCache) -> None:
            # Drop the cached image: a value read before the link went down must
            # never be mistaken for a live one after it comes back.
            _LOGGER.debug("BLE link to %s dropped", self._address)
            self._regs.clear()
            self._caps = {}
            self._read_expect = None
            self._reassembler = protocol.BroadcastReassembler()

        client = await establish_connection(
            BleakClientWithServiceCache,
            device,
            self._name,
            disconnected_callback=_on_disconnect,
        )
        service = client.services.get_service(protocol.SVC)
        if service is None:
            await client.disconnect()
            raise BleConnectionError(f"service {protocol.SVC} missing on {self._address}")
        self._tx = service.get_characteristic(protocol.CH_TX)
        if self._tx is None:
            await client.disconnect()
            raise BleConnectionError(f"tx characteristic missing on {self._address}")
        await client.start_notify(protocol.CH_RX, self._on_notify)
        self._client = client
        _LOGGER.debug("BLE link to %s established", self._address)

    async def disconnect(self) -> None:
        """Tear down the BLE link (best effort)."""
        client = self._client
        self._client = None
        self._tx = None
        if client is not None and client.is_connected:
            try:
                await client.stop_notify(protocol.CH_RX)
            except Exception:  # noqa: BLE001
                pass
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    # -- notifications ------------------------------------------------------
    def _on_notify(self, _char: BleakGATTCharacteristic, data: bytearray) -> None:
        """Handle a notification on ``0xABF2`` (status push or Modbus reply)."""
        raw = bytes(data)
        if protocol.BroadcastReassembler.is_broadcast(raw):
            pairs = self._reassembler.feed(raw)
            if pairs:
                for reg, val in pairs:
                    self._regs[reg] = val
            return

        parsed = protocol.parse_frame(raw)
        if not parsed or parsed[0] is None:
            _LOGGER.debug("BLE rx from %s not a valid frame (%dB)", self._address, len(raw))
            return
        _counter, pdu = parsed
        if not pdu:
            return
        if len(pdu) < 2:
            return
        if pdu[1] == 0x03:
            # A function-0x03 reply carries no address, so it can only be decoded
            # against the base of the read it answers. A late reply — one whose
            # read already timed out — must therefore be dropped: crediting it to
            # the *next* read's base would silently corrupt the register image,
            # phase included. Only a reply whose byte count matches the pending
            # read is accepted.
            expected = self._read_expect
            if expected is None:
                _LOGGER.debug("Dropping unsolicited read reply on %s", self._address)
                return
            base, count = expected
            if len(pdu) < 3 or pdu[2] != count * 2:
                _LOGGER.debug(
                    "Dropping mismatched read reply on %s (want %d bytes for 0x%04X,"
                    " got %s)",
                    self._address,
                    count * 2,
                    base,
                    pdu[2] if len(pdu) > 2 else "?",
                )
                return
            self._read_expect = None
            for reg, val in protocol.decode_read_response(pdu, base):
                self._regs[reg] = val
            self._read_ok = True
            self._read_event.set()
        elif pdu[1] == 0x06:
            echo = protocol.decode_write_echo(pdu)
            if echo is not None:
                self._regs[echo[0]] = echo[1]

    # -- io -----------------------------------------------------------------
    async def _send(self, pdu: bytes) -> None:
        """Encrypt and write a PDU to the command characteristic."""
        frame = protocol.build_frame(pdu, self._counter)
        self._counter = (self._counter + 1) & 0xFFFFFFFF
        await self._client.write_gatt_char(self._tx, frame, response=False)

    async def read_regs(
        self, reg: int, count: int, timeout: float = READ_TIMEOUT
    ) -> dict[int, int]:
        """Read ``count`` registers from ``reg`` and return the answered slice.

        Only one function ``0x03`` is ever in flight (serialised by a lock).
        Raises :class:`BleReadTimeout` when the panel does not answer, so a
        caller can tell a fresh value from a stale cached one — deciding whether
        to press the power button on stale data would be unsafe.
        """
        if not self.connected:
            raise BleConnectionError("not connected")
        async with self._io_lock:
            self._read_event.clear()
            self._read_ok = False
            self._read_expect = (reg, count)
            try:
                await self._send(protocol.modbus_read_pdu(reg, count))
                await asyncio.wait_for(self._read_event.wait(), timeout)
            except TimeoutError as err:
                raise BleReadTimeout(
                    f"read 0x{reg:04X} x{count} timed out on {self._address}"
                ) from err
            except BleConnectionError:
                raise
            except Exception as err:  # noqa: BLE001
                raise BleConnectionError(f"read 0x{reg:04X} failed: {err}") from err
            finally:
                self._read_expect = None
        return {r: self._regs[r] for r in range(reg, reg + count) if r in self._regs}

    async def write_reg(self, reg: int, val: int) -> None:
        """Write a single register (function ``0x06``)."""
        if not self.connected:
            raise BleConnectionError("not connected")
        async with self._io_lock:
            await self._send(protocol.modbus_write_pdu(reg, val))
        # optimistic local echo so the next refresh reflects the change quickly
        self._regs[reg] = val

    async def poll_status_block(self) -> dict[int, int]:
        """Run the poll plan, updating the register image. Returns it."""
        for reg, count in POLL_READS:
            if not self.connected:
                break
            try:
                await self.read_regs(reg, count)
            except BleReadTimeout:
                continue  # one unanswered block must not abort the whole cycle
            except BleConnectionError:
                break
        if not self._serial:
            await self._read_serial()
        return self.regs

    async def _read_serial(self) -> None:
        """Read and decode the ASCII serial number (register ``0x0ADC``)."""
        try:
            await self.read_regs(0x0ADC, 8)
        except BleConnectionError:
            return
        serial = registers.decode_serial(self._regs)
        if serial:
            self._serial = serial

    async def cap_scan(self) -> dict[str, object]:
        """Read the capability registers and return the derived capability dict."""
        for reg, count in registers.CAP_SCAN_READS:
            if not self.connected:
                break
            try:
                await self.read_regs(reg, count, timeout=CAP_SCAN_TIMEOUT)
            except BleReadTimeout:
                continue
            except BleConnectionError:
                break
        self._caps = registers.parse_capabilities(self._regs)
        return self.caps


class BleConnectionError(Exception):
    """Raised when the BLE link is unavailable or fails."""


class BleReadTimeout(BleConnectionError):
    """Raised when a register read gets no answer within the timeout."""
