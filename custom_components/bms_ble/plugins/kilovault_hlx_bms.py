"""Module to support KiloVault HLX iT BMS."""

import asyncio
import codecs
from collections.abc import Callable
from string import hexdigits
from typing import Final

from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.uuids import normalize_uuid_str

from custom_components.bms_ble.const import (
    ATTR_BATTERY_CHARGING,
    ATTR_BATTERY_LEVEL,
    ATTR_CURRENT,
    ATTR_CYCLE_CAP,
    ATTR_CYCLE_CHRG,
    ATTR_CYCLES,
    ATTR_DELTA_VOLTAGE,
    ATTR_POWER,
    ATTR_RUNTIME,
    ATTR_TEMPERATURE,
    ATTR_VOLTAGE,
    KEY_CELL_VOLTAGE,
    KEY_PROBLEM,
)

from .basebms import BaseBMS, BMSsample


class BMS(BaseBMS):
    """KiloVault HLX iT battery class implementation."""

    _HEAD_RSP: Final[bytes] = bytes([0xB0])  # header for responses
    _TAIL_RSP: Final[bytes] = bytes([0x52])  # end for responses
    # TODO: verify numbers
    _CELL_COUNT_LOOKUP = {65: 4, 81: 8, 113: 16}
    _CHECKSUM_LEN: Final[int] = 4
    _FIELDS: Final[list[tuple[str, int, int, bool, Callable[[int], int | float]]]] = [
        (ATTR_VOLTAGE, 1, 8, False, lambda x: float(x / 1000)),
        (ATTR_CURRENT, 9, 8, True, lambda x: float(x / 1000)),
        (ATTR_CYCLE_CHRG, 17, 8, False, lambda x: float(x / 1000)),
        (ATTR_CYCLES, 25, 4, False, lambda x: x),
        (ATTR_BATTERY_LEVEL, 29, 4, False, lambda x: x),
        (ATTR_TEMPERATURE, 33, 4, False, lambda x: round(x * 0.1 - 273.15, 1)),
        (KEY_PROBLEM, 37, 2, False, lambda x: x),
        # TODO: AfeStatus (should be bytes 41 and 42)
    ]

    def __init__(self, ble_device: BLEDevice, reconnect: bool = False) -> None:
        """Initialize BMS."""
        super().__init__(__name__, ble_device, reconnect)
        self._data_final: bytearray = bytearray()

    @staticmethod
    def matcher_dict_list() -> list[dict]:
        """Provide BluetoothMatcher definition."""
        return [
            {
                "local_name": pattern,
                "service_uuid": BMS.uuid_services()[0],
                "connectable": True,
            }
            for pattern in ["7-12V300Ah-CR-*"]
        ]

    @staticmethod
    def device_info() -> dict[str, str]:
        """Return device information for the battery management system."""
        return {"manufacturer": "KiloVault", "model": "HLX"}

    @staticmethod
    def uuid_services() -> list[str]:
        """Return list of 128-bit UUIDs of services required by BMS."""
        return [normalize_uuid_str("ffe0")]  # change service UUID here!

    @staticmethod
    def uuid_rx() -> str:
        """Return 16-bit UUID of characteristic that provides notification/read property."""
        return "ffe4"

    @staticmethod
    def uuid_tx() -> str:
        """Return 16-bit UUID of characteristic that provides write property."""
        raise NotImplementedError

    @staticmethod
    def _calc_values() -> set[str]:
        return {
            ATTR_BATTERY_CHARGING,
            ATTR_CYCLE_CAP,
            ATTR_CYCLE_CHRG,
            ATTR_DELTA_VOLTAGE,
            ATTR_POWER,
            ATTR_RUNTIME,
        }  # calculate further values from BMS provided set ones

    def _notification_handler(
        self, _sender: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        """Handle the RX characteristics notify event (new data arrives)."""

        self._data += data

        # Check for start of frame
        if (start := self._data.find(BMS._HEAD_RSP)) != -1:
            self._data = self._data[start:]

        # TODO: will this ever print "start"?
        self._log.debug(
            "RX BLE data (%s): %s", "start" if data == self._data else "cnt.", data
        )
        self._log.debug(self._data)

        if (end := self._data.find(BMS._TAIL_RSP)) != -1:
            frame = self._data[0:end]
            self._data = self._data[end:]
            self._log.debug(frame)
            self._log.debug(len(frame))

            if not (
                frame.startswith(BMS._HEAD_RSP)
                and len(frame) in BMS._CELL_COUNT_LOOKUP
                and set(frame.decode(errors="replace")[1:]).issubset(hexdigits)
            ):
                self._log.debug("incorrect frame coding: %s", frame)
                return

            if (checksum := BMS._checksum(frame[1 : -BMS._CHECKSUM_LEN])) != int(
                frame[-BMS._CHECKSUM_LEN :], 16
            ):
                self._log.debug(
                    "invalid checksum 0x%X != 0x%X",
                    int(frame[-BMS._CHECKSUM_LEN :], 16),
                    checksum,
                )
                return

            self._data_final = frame
            self._data_event.set()

    @staticmethod
    def _checksum(data: bytearray) -> int:
        return sum(codecs.decode(data, "hex"))

    @staticmethod
    def _cell_voltages(data: bytearray) -> dict[str, float]:
        """Return cell voltages from status message."""
        cell_count = BMS._CELL_COUNT_LOOKUP[len(data)]
        # TODO
        return {
            f"{KEY_CELL_VOLTAGE}{idx}": BMS._conv_int(
                data[45 + idx * 4 : 49 + idx * 4], False
            )
            / 1000
            for idx in range(cell_count)
            if BMS._conv_int(data[45 + idx * 4 : 49 + idx * 4], False)
        }

    @staticmethod
    def _conv_int(data: bytearray, sign: bool) -> int:
        return int.from_bytes(
            codecs.decode(data, "hex"),
            byteorder="little",
            signed=sign,
        )

    async def _async_update(self) -> BMSsample:
        """Update battery status information."""

        await asyncio.wait_for(self._wait_event(), timeout=self.BAT_TIMEOUT)
        return {
            key: func(BMS._conv_int(self._data_final[idx : idx + size], sign))
            for key, idx, size, sign, func in BMS._FIELDS
        } | BMS._cell_voltages(self._data_final)
