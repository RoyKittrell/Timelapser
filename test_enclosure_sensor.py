import unittest

from enclosure_sensor import crc8, decode_measurement


class EnclosureSensorTests(unittest.TestCase):
    def test_decodes_valid_measurement_and_dew_margin(self):
        temperature_raw = round((30.0 + 45.0) * 65535.0 / 175.0)
        humidity_raw = round(70.0 * 65535.0 / 100.0)
        temperature = temperature_raw.to_bytes(2, "big")
        humidity = humidity_raw.to_bytes(2, "big")
        raw = temperature + bytes([crc8(temperature)]) + humidity + bytes([crc8(humidity)])

        reading = decode_measurement(raw, 0x44)

        self.assertAlmostEqual(reading.temperature_c, 30.0, places=1)
        self.assertAlmostEqual(reading.humidity_percent, 70.0, places=1)
        self.assertAlmostEqual(reading.dew_point_c, 24.0, delta=0.3)
        self.assertAlmostEqual(reading.dew_margin_c, 6.0, delta=0.3)
        self.assertEqual(reading.address, "0x44")

    def test_rejects_bad_crc(self):
        with self.assertRaisesRegex(ValueError, "CRC"):
            decode_measurement(bytes([0x66, 0x66, 0x00, 0x80, 0x00, 0x00]), 0x44)


if __name__ == "__main__":
    unittest.main()
