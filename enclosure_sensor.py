#!/usr/bin/env python3
"""Read the enclosure SHT3X sensor through Linux i2c-tools."""

from __future__ import annotations

import math
import re
import subprocess
from dataclasses import asdict, dataclass


I2C_BUS = 1
SHT3X_ADDRESSES = (0x44, 0x45)
BYTE_PATTERN = re.compile(r"0x([0-9a-fA-F]{2})")


@dataclass(frozen=True)
class EnclosureReading:
    temperature_c: float
    humidity_percent: float
    dew_point_c: float
    dew_margin_c: float
    address: str

    def as_dict(self) -> dict[str, float | str]:
        return asdict(self)


def crc8(payload: bytes) -> int:
    crc = 0xFF
    for value in payload:
        crc ^= value
        for _ in range(8):
            crc = ((crc << 1) ^ 0x31) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def decode_measurement(raw: bytes, address: int) -> EnclosureReading:
    if len(raw) != 6:
        raise ValueError(f"Expected 6 SHT3X bytes, received {len(raw)}")
    if crc8(raw[0:2]) != raw[2] or crc8(raw[3:5]) != raw[5]:
        raise ValueError("SHT3X measurement failed CRC validation")

    raw_temperature = int.from_bytes(raw[0:2], "big")
    raw_humidity = int.from_bytes(raw[3:5], "big")
    temperature = -45.0 + 175.0 * raw_temperature / 65535.0
    humidity = max(0.0, min(100.0, 100.0 * raw_humidity / 65535.0))

    # Magnus approximation over the normal enclosure operating range.
    safe_humidity = max(humidity, 0.01)
    gamma = math.log(safe_humidity / 100.0) + 17.62 * temperature / (243.12 + temperature)
    dew_point = 243.12 * gamma / (17.62 - gamma)
    return EnclosureReading(
        temperature_c=temperature,
        humidity_percent=humidity,
        dew_point_c=dew_point,
        dew_margin_c=temperature - dew_point,
        address=f"0x{address:02x}",
    )


def read_sht3x(bus: int = I2C_BUS) -> EnclosureReading:
    errors = []
    for address in SHT3X_ADDRESSES:
        command = [
            "i2ctransfer", "-y", str(bus),
            f"w2@0x{address:02x}", "0x24", "0x00", "r6",
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            errors.append(f"0x{address:02x}: {type(exc).__name__}: {exc}")
            continue
        if result.returncode != 0:
            errors.append(f"0x{address:02x}: {(result.stderr or result.stdout).strip()}")
            continue
        try:
            values = bytes(int(item, 16) for item in BYTE_PATTERN.findall(result.stdout))
            return decode_measurement(values, address)
        except ValueError as exc:
            errors.append(f"0x{address:02x}: {exc}")
    raise RuntimeError("; ".join(errors) or "No SHT3X response")
