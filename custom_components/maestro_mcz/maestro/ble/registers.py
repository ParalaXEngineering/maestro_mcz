"""Register map, conversions and dataclass mapping for the BLE transport.

Everything model-specific about the Modbus register image lives here:

* ``REG_TO_STATE`` / ``REG_TO_STATUS`` map a register to a ``(python_attribute,
  converter)`` pair used to populate the :class:`State` / :class:`Status`
  dataclasses (via ``from_mocked_response=True``).
* ``WRITE_MAP`` maps a *logical sensor name* (the ``sensor_name`` used by the
  cloud model and by ``models.py``) to ``(register, encoding, min, max)``.
* :func:`build_state_dict` / :func:`build_status_dict` combine the direct maps
  with the computed fields (state/mode strings, phase reconstruction, flags).

No Home Assistant imports here either — pure data.
"""

from __future__ import annotations

from . import protocol
from .protocol import mode_name, state_name

# ---- capability-scan registers (from firmware oven.h / main.cpp capScan) ----
REG_SET_BOILER = 0x0407
REG_MAXPOT_IDRO = 0x040F
REG_SET_PUFFER = 0x0412
REG_VVEN1_LO = 0x05FD
REG_VVEN1_HI = 0x05FF
REG_TIPO_APP = 0x06A4
REG_ABI_VEN12 = 0x06F4
REG_ABI_VEN34 = 0x06F5
REG_CLIMA = 0x0372
REG_BANCADATI = 0x07C6
SENTINEL16 = 0xFFFF

# Register write addresses (function 0x06).
REG_SETPOINT = protocol.REG["setpoint"]
REG_POWER = protocol.REG["power"]
REG_MODE = protocol.REG["mode"]
REG_ONOFF = protocol.REG["onoff"]
REG_FAN = protocol.REG["fan"]
REG_SILENT = protocol.REG["silent"]
REG_PHASE = protocol.REG["phase"]

# Logical mode keys (lowercase) — these must match both the fan
# ``mode_to_configuration_name_mapping`` keys and the climate preset variants in
# ``models.py`` so that ``State.mode`` resolves against the (synthesised) model.
# Only 0..2 are confirmed on hardware. Codes 3 (Comfort) and 4 (Turbo) are
# assumed by every upstream source (firmware_oven.h, ble-protocol.md, and the
# reference client which literally labels them "Comfort?"/"Turbo?"), so they are
# read back but never offered as a writable target until confirmed on a stove.
MODE_KEYS = {0: "manual", 1: "auto", 2: "overnight", 3: "comfort", 4: "turbo"}
MODE_KEYS_CONFIRMED = {0: "manual", 1: "auto", 2: "overnight"}


# --------------------------------------------------------------------------- #
# converters
def _div10(v: int | None) -> float | None:
    """Convert a raw ÷10 fixed-point register to a float, sentinel-aware."""
    if v is None or v == SENTINEL16:
        return None
    return round(v / 10.0, 1)


def _ident(v: int | None) -> int | None:
    """Return the raw register value unchanged."""
    return v


# reg -> (State attribute, converter)
REG_TO_STATE: dict[int, tuple[str, object]] = {
    0x02BC: ("temp_amb_install", _div10),
    0x02C1: ("temp_scheda", _div10),
    0x02C5: ("temp_fumi", _div10),
    0x02D1: ("vel_real_ventola_fumi", _ident),
    0x0324: ("index_vel_v1", _ident),
    0x03F7: ("set_amb1", _div10),
}

# reg -> (Status attribute, converter)
REG_TO_STATUS: dict[int, tuple[str, object]] = {
    0x02BC: ("temp_amb_install", _div10),
    0x03F7: ("set_amb1", _div10),
    0x03EB: ("set_pot_man", _ident),
    0x0324: ("set_vent_v1", _ident),
}


# --------------------------------------------------------------------------- #
# write mapping: logical sensor name -> (register, encoding, min, max)
# encodings: "temp_x10", "int", "mode", "fan", "bool", "onoff_toggle"
WRITE_MAP: dict[str, tuple[int, str, float | None, float | None]] = {
    "set_amb1": (REG_SETPOINT, "temp_x10", 5.0, 45.0),
    "set_pot_man": (REG_POWER, "int", 1, 5),
    "mod_funz": (REG_MODE, "mode", 0, 4),
    "set_vent_v1": (REG_FAN, "fan", 0, 6),
    "silent_enabled": (REG_SILENT, "bool", 0, 1),
    "silent": (REG_SILENT, "bool", 0, 1),
}

# Power is deliberately absent from WRITE_MAP.
#
# ``0x038A`` is a toggle, so a caller must know the *requested* state to decide
# whether pressing the button is right. The cloud model declares com_on_off as
# BOOLEAN, and climate.py's boolean branch sends a constant ``True`` for both
# "turn on" and "turn off" (the direction lives in its own guard). Reading an
# absolute state out of that value is impossible, so in hybrid mode power is
# left to the cloud, which owns that press semantic. Only the BLE-only
# controller — whose synthesised model declares com_on_off as INT with explicit
# 0/1 mappings — writes this register locally.
ONOFF_WRITE: tuple[int, str, None, None] = (REG_ONOFF, "onoff_toggle", None, None)


def decode_onoff(value: object) -> bool | None:
    """Decode a requested power state, or ``None`` when it is not expressible.

    ``0x038A`` is a *toggle*, so the requested absolute state has to be known
    before deciding whether to press the button at all.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "on", "heat"):
            return True
        if lowered in ("0", "false", "off"):
            return False
    return None


# Coarse phase values documented by the firmware: 1 = off/standby, 3 = running.
# Anything else is treated as unknown rather than guessed at, because the only
# power control is a toggle and a wrong guess lights a combustion appliance.
PHASE_OFF = 1
PHASE_ON = 3


def is_known_phase(phase: int | None) -> bool:
    """Return whether ``phase`` is a documented coarse phase value."""
    return phase in (PHASE_OFF, PHASE_ON)


def needs_onoff_toggle(phase: int | None, desired_on: bool) -> bool:
    """Return whether the power button must be pressed to reach ``desired_on``.

    Callers must check :func:`is_known_phase` first: an unknown phase has no
    safe answer here, so it is reported as "no press needed" rather than
    pressing blindly the way the firmware does (its phase is continuously fed
    by status pushes and its caller is a human standing at the stove).
    """
    if not is_known_phase(phase):
        return False
    return desired_on == (phase == PHASE_OFF)


def encode_write(encoding: str, value: object, minv, maxv) -> int:
    """Encode a logical command value into a raw register value.

    Raises ``ValueError`` when the value cannot be represented (the hybrid
    controller uses this to fall back to the cloud path).
    """
    if encoding == "temp_x10":
        raw = int(round(float(value) * 10))
        return max(int(minv * 10), min(int(maxv * 10), raw))
    if encoding == "int":
        return max(int(minv), min(int(maxv), int(value)))
    if encoding == "bool":
        return 1 if value in (True, 1, "1", "true", "True", "on") else 0
    if encoding == "mode":
        if isinstance(value, str):
            lowered = value.strip().lower()
            for code, key in MODE_KEYS.items():
                if key == lowered:
                    return code
            value = int(value)  # may raise ValueError -> caller falls back
        return max(0, min(4, int(value)))
    if encoding == "fan":
        if isinstance(value, str):
            if value.strip().lower() == "auto":
                return 6
            value = int(value)  # may raise ValueError -> caller falls back
        level = int(value)
        if level <= 0:
            return 6  # 0 / off -> auto ventilation (no true off on the room fan)
        return max(1, min(5, level))
    raise ValueError(f"unknown encoding '{encoding}'")


# --------------------------------------------------------------------------- #
# capability scan
# Each entry: (base_register, count) — mirrors capScan() in firmware_main.cpp.
CAP_SCAN_READS: list[tuple[int, int]] = [
    (REG_SET_BOILER, 13),  # 0x0407..0x0413: set_boiler, max_pot_idro, set_puffer
    (REG_ABI_VEN12, 2),  # 0x06F4/0x06F5: fan-enable bytes -> fan count
    (REG_VVEN1_LO, 3),  # 0x05FD..0x05FF: fan-1 speed table -> fan levels
    (REG_TIPO_APP, 1),  # device type
    (REG_CLIMA, 1),  # conditioner block
    (REG_BANCADATI, 9),  # model database id (ASCII)
]


def _count_nonzero_bytes(val: int) -> int:
    """Count the non-zero bytes of a 16-bit value (fan-enable/level packing)."""
    return int((val & 0xFF) != 0) + int((val >> 8) != 0)


def parse_capabilities(regs: dict[int, int]) -> dict[str, object]:
    """Reduce a register image to a capability dict.

    Mirrors the ``ovenApplyReg`` capability branches in ``firmware_main.cpp``.
    """
    caps: dict[str, object] = {
        "hydro": False,
        "boiler": False,
        "puffer": False,
        "clima": False,
        "fan_count": 1,
        "fan_levels": 5,
        "tipo_app": None,
        "banca_dati": "",
    }

    maxpot = regs.get(REG_MAXPOT_IDRO)
    if maxpot is not None:
        caps["hydro"] = maxpot != SENTINEL16 and maxpot > 0
    if REG_SET_BOILER in regs:
        caps["boiler"] = regs[REG_SET_BOILER] != SENTINEL16
    if REG_SET_PUFFER in regs:
        caps["puffer"] = regs[REG_SET_PUFFER] != SENTINEL16
    clima = regs.get(REG_CLIMA)
    if clima is not None:
        caps["clima"] = clima not in (0, SENTINEL16)
    if REG_TIPO_APP in regs:
        caps["tipo_app"] = regs[REG_TIPO_APP]

    fan_count = 0
    for reg in (REG_ABI_VEN12, REG_ABI_VEN34):
        if reg in regs:
            fan_count += _count_nonzero_bytes(regs[reg])
    if fan_count:
        caps["fan_count"] = fan_count

    fan_levels = 0
    for reg in (REG_VVEN1_LO, 0x05FE, REG_VVEN1_HI):
        if reg in regs:
            fan_levels += _count_nonzero_bytes(regs[reg])
    if fan_levels:
        caps["fan_levels"] = max(1, min(5, fan_levels))

    banca = _decode_ascii(regs, REG_BANCADATI + 1, 8)
    if banca:
        caps["banca_dati"] = banca

    return caps


def _decode_ascii(regs: dict[int, int], base: int, count: int) -> str:
    """Decode ``count`` registers from ``base`` as 2 ASCII chars each."""
    out = []
    for i in range(count):
        val = regs.get(base + i)
        if val is None:
            break
        for byte in (val >> 8, val & 0xFF):
            if 32 <= byte < 127:
                out.append(chr(byte))
    return "".join(out).strip()


def decode_serial(regs: dict[int, int], base: int = 0x0ADC, count: int = 8) -> str:
    """Decode the ASCII serial number stored from register ``0x0ADC``."""
    return _decode_ascii(regs, base, count)


# --------------------------------------------------------------------------- #
# dataclass builders
def _flag_bit(regs: dict[int, int], reg: int, bit: int) -> bool | None:
    """Return a single flag bit from a register, or ``None`` when absent."""
    val = regs.get(reg)
    if val is None:
        return None
    return bool((val >> bit) & 1)


def _alarm_code(regs: dict[int, int]) -> int | None:
    """Return the live alarm code (low byte of ``0x0323``)."""
    val = regs.get(protocol.REG["alarm"])
    if val is None:
        return None
    return val & 0xFF


def build_state_dict(regs: dict[int, int], link_ok: bool) -> dict[str, object]:
    """Build a ``{attribute: value}`` dict for the ``State`` dataclass."""
    out: dict[str, object] = {}
    for reg, (attr, conv) in REG_TO_STATE.items():
        if reg in regs:
            out[attr] = conv(regs[reg])

    # state string from the fine-state code (0x0320)
    if protocol.REG["state"] in regs:
        out["state"] = state_name(regs[protocol.REG["state"]])

    # mode string (logical lowercase key) from the live mirror (0x032E)
    mode_raw = regs.get(protocol.REG["mode_live"])
    if mode_raw is None:
        mode_raw = regs.get(protocol.REG["mode"])
    if mode_raw is not None:
        out["mode"] = MODE_KEYS.get(mode_raw, mode_name(mode_raw))

    crono = _flag_bit(regs, protocol.REG["flags"], 6)
    if crono is not None:
        out["crono_enabled"] = crono

    alarm = _alarm_code(regs)
    if alarm is not None:
        out["last_alarm"] = f"A{alarm}" if alarm else None
        out["is_in_error"] = alarm != 0

    out["is_connected"] = link_ok
    return out


def _stato_stufa_and_fase(regs: dict[int, int]) -> tuple[int | None, str | None]:
    """Reconstruct the cloud ``stato_stufa`` / ``fase_op`` pair for the climate entity.

    The climate platform interprets (M2): 0=off, 1=turning-off, 2=standby,
    3=on, and reads ``fase_op`` == "turning-on"/"turning-off" for the ramps.
    """
    phase = regs.get(REG_PHASE)
    state = regs.get(protocol.REG["state"])
    if phase is None:
        return None, None
    if phase == 3:
        fase = "turning-on" if state in protocol.STATES_STARTING else None
        return 3, fase
    if phase == 1:
        if state == protocol.STATE_TURNING_OFF:
            return 1, "turning-off"
        return 0, None
    return phase, None


def build_status_dict(
    regs: dict[int, int],
    link_ok: bool,
    caps: dict[str, object] | None = None,
    identity: dict[str, object] | None = None,
) -> dict[str, object]:
    """Build a ``{attribute: value}`` dict for the ``Status`` dataclass."""
    out: dict[str, object] = {}
    for reg, (attr, conv) in REG_TO_STATUS.items():
        if reg in regs:
            out[attr] = conv(regs[reg])

    stato_stufa, fase_op = _stato_stufa_and_fase(regs)
    if stato_stufa is not None:
        out["stato_stufa"] = stato_stufa
    out["fase_op"] = fase_op

    # live fan level mirror also exposed as set_vent_v1 for the fan entity
    if protocol.REG["fan_live"] in regs:
        out["set_vent_v1"] = regs[protocol.REG["fan_live"]]

    mode_raw = regs.get(protocol.REG["mode_live"])
    if mode_raw is None:
        mode_raw = regs.get(protocol.REG["mode"])
    if mode_raw is not None:
        out["mod_lav_att"] = MODE_KEYS.get(mode_raw, mode_name(mode_raw))

    silent = _flag_bit(regs, protocol.REG["flags"], 5)
    if silent is None and protocol.REG["silent"] in regs:
        silent = bool(regs[protocol.REG["silent"]])
    if silent is not None:
        out["silent"] = silent
        out["silent_enabled"] = silent

    crono = _flag_bit(regs, protocol.REG["flags"], 6)
    if crono is not None:
        out["crono_enabled"] = crono

    power_enabled = None
    if REG_PHASE in regs:
        power_enabled = regs[REG_PHASE] == 3
        out["power_enabled"] = power_enabled

    alarm = _alarm_code(regs)
    if alarm is not None:
        out["is_in_error"] = alarm != 0

    if caps:
        out["nome_banca_dati_sel"] = caps.get("banca_dati") or None

    if identity:
        out["sm_sn"] = identity.get("unique_code")
        out["sm_nome_app"] = identity.get("sm_nome_app")
        out["sm_vs_app"] = identity.get("sm_vs_app")
        out["mc_vs_app"] = identity.get("mc_vs_app")

    out["is_connected"] = link_ok
    return out
