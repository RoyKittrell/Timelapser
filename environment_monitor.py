from __future__ import annotations

import argparse
import csv
import fcntl
import json
import math
import os
import struct
import time
from datetime import datetime
from pathlib import Path

I2C_SLAVE = 0x0703
SHT3X_ADDRESS = 0x44
I2C_DEVICE = Path('/dev/i2c-1')
CPU_TEMP_PATH = Path('/sys/class/thermal/thermal_zone0/temp')
DEFAULT_LOG_PATH = Path(__file__).resolve().parent / 'control' / 'environment.csv'
FIELDNAMES = (
    'timestamp',
    'temperature_c',
    'humidity_pct',
    'dew_point_c',
    'dew_point_margin_c',
    'cpu_temperature_c',
)


def _crc8(data: bytes) -> int:
    crc = 0xFF
    for value in data:
        crc ^= value
        for _ in range(8):
            crc = ((crc << 1) ^ 0x31) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def _read_sht3x() -> tuple[float, float]:
    with I2C_DEVICE.open('r+b', buffering=0) as bus:
        fcntl.ioctl(bus.fileno(), I2C_SLAVE, SHT3X_ADDRESS)
        bus.write(bytes((0x24, 0x00)))
        time.sleep(0.02)
        payload = bus.read(6)

    if len(payload) != 6:
        raise RuntimeError(f'SHT3X returned {len(payload)} bytes instead of 6')
    if _crc8(payload[0:2]) != payload[2] or _crc8(payload[3:5]) != payload[5]:
        raise RuntimeError('SHT3X CRC check failed')

    raw_temperature = struct.unpack('>H', payload[0:2])[0]
    raw_humidity = struct.unpack('>H', payload[3:5])[0]
    temperature_c = -45.0 + 175.0 * raw_temperature / 65535.0
    humidity_pct = 100.0 * raw_humidity / 65535.0
    return temperature_c, humidity_pct


def _dew_point_c(temperature_c: float, humidity_pct: float) -> float:
    humidity = min(100.0, max(0.1, humidity_pct))
    a = 17.62
    b = 243.12
    gamma = math.log(humidity / 100.0) + a * temperature_c / (b + temperature_c)
    return b * gamma / (a - gamma)


def _cpu_temperature_c() -> float:
    return float(CPU_TEMP_PATH.read_text(encoding='ascii').strip()) / 1000.0


def read_environment_sample() -> dict[str, object]:
    temperature_c, humidity_pct = _read_sht3x()
    dew_point_c = _dew_point_c(temperature_c, humidity_pct)
    return {
        'timestamp': datetime.now().astimezone().isoformat(timespec='seconds'),
        'temperature_c': round(temperature_c, 2),
        'humidity_pct': round(humidity_pct, 2),
        'dew_point_c': round(dew_point_c, 2),
        'dew_point_margin_c': round(temperature_c - dew_point_c, 2),
        'cpu_temperature_c': round(_cpu_temperature_c(), 2),
    }


def append_environment_sample(
    sample: dict[str, object],
    log_path: Path = DEFAULT_LOG_PATH,
) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open('a+', encoding='utf-8', newline='') as output:
        fcntl.flock(output.fileno(), fcntl.LOCK_EX)
        output.seek(0, os.SEEK_END)
        is_empty = output.tell() == 0
        writer = csv.DictWriter(output, fieldnames=FIELDNAMES)
        if is_empty:
            writer.writeheader()
        writer.writerow({key: sample.get(key, '') for key in FIELDNAMES})
        output.flush()
        os.fsync(output.fileno())


def main() -> int:
    parser = argparse.ArgumentParser(description='Read and log Timelapser enclosure conditions.')
    parser.add_argument('--log', action='store_true', help='Append the reading to the CSV history.')
    parser.add_argument('--output', type=Path, default=DEFAULT_LOG_PATH)
    args = parser.parse_args()

    sample = read_environment_sample()
    if args.log:
        append_environment_sample(sample, args.output)
    print(json.dumps(sample, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
