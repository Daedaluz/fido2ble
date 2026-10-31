"""Regression tests for issue #34: the uhid device always answers the host."""

import asyncio
import unittest
from unittest import mock

from fido2ble.CMD import CTAPHID_CMD, CTAPBLE_CMD, CTAP_STATUS
from fido2ble.CTAPBLEDevice import DeviceUnavailableError
from fido2ble.CTAPHIDDevice import CTAPHIDDevice
from fido2ble.vendored.dbus_fast import DBusError

from tests.fakes import (
    CTAPHID_BROADCAST_CHANNEL,
    FakeBLE,
    ble_frame,
    hid_report,
    parse_hid,
    patch_uhid,
)

NONCE = b"\x01\x02\x03\x04\x05\x06\x07\x08"


async def settle(seconds=0.0):
    await asyncio.sleep(seconds)


def errors(hid):
    return [
        (ch, payload[0])
        for ch, cmd, payload in map(parse_hid, hid.device.sent)
        if cmd == CTAPHID_CMD.ERROR
    ]


def make_hid(ble, **kwargs):
    with patch_uhid():
        return CTAPHIDDevice(ble, **kwargs)


class InitTests(unittest.IsolatedAsyncioTestCase):
    async def test_init_answers_with_error_when_connect_fails(self):
        ble = FakeBLE(connect_behaviour=DBusError("org.bluez.Error.Failed", "nope"))
        hid = make_hid(ble)
        hid.process_process_hid_message(
            hid_report(CTAPHID_BROADCAST_CHANNEL, CTAPHID_CMD.INIT, NONCE), None
        )
        await settle(0.01)
        self.assertEqual(
            errors(hid), [(CTAPHID_BROADCAST_CHANNEL, CTAP_STATUS.CTAP1_ERR_OTHER)]
        )

    async def test_init_answers_with_error_when_device_gone(self):
        ble = FakeBLE(connect_behaviour=DeviceUnavailableError("gone"))
        hid = make_hid(ble)
        hid.process_process_hid_message(
            hid_report(CTAPHID_BROADCAST_CHANNEL, CTAPHID_CMD.INIT, NONCE), None
        )
        await settle(0.01)
        self.assertEqual(
            errors(hid), [(CTAPHID_BROADCAST_CHANNEL, CTAP_STATUS.CTAP1_ERR_OTHER)]
        )

    async def test_init_answers_with_error_when_connect_hangs(self):
        ble = FakeBLE(connect_behaviour="hang")
        hid = make_hid(ble, connect_timeout=0.05)
        hid.process_process_hid_message(
            hid_report(CTAPHID_BROADCAST_CHANNEL, CTAPHID_CMD.INIT, NONCE), None
        )
        await settle(0.15)
        self.assertEqual(
            errors(hid), [(CTAPHID_BROADCAST_CHANNEL, CTAP_STATUS.CTAP1_ERR_OTHER)]
        )

    async def test_init_on_unknown_channel_answers_invalid_channel(self):
        hid = make_hid(FakeBLE())
        hid.process_process_hid_message(hid_report(0xDEADBEEF, CTAPHID_CMD.INIT, NONCE), None)
        await settle(0.01)
        self.assertEqual(errors(hid), [(0xDEADBEEF, CTAP_STATUS.CTAP1_ERR_INVALID_CHANNEL)])

    async def test_successful_init_allocates_channel(self):
        ble = FakeBLE()
        hid = make_hid(ble)
        hid.process_process_hid_message(
            hid_report(CTAPHID_BROADCAST_CHANNEL, CTAPHID_CMD.INIT, NONCE), None
        )
        await settle(0.01)
        self.assertEqual(len(hid.device.sent), 1)
        channel, cmd, payload = parse_hid(hid.device.sent[0])
        self.assertEqual(channel, CTAPHID_BROADCAST_CHANNEL)
        self.assertEqual(cmd, CTAPHID_CMD.INIT)
        self.assertEqual(payload[:8], NONCE)
        new_channel = int.from_bytes(payload[8:12], "big")
        self.assertIn(new_channel, hid.channels_to_state)
        hid.stop()


class RequestTests(unittest.IsolatedAsyncioTestCase):
    async def _init(self, hid):
        hid.process_process_hid_message(
            hid_report(CTAPHID_BROADCAST_CHANNEL, CTAPHID_CMD.INIT, NONCE), None
        )
        await settle(0.01)
        _, _, payload = parse_hid(hid.device.sent[-1])
        hid.device.sent.clear()
        return int.from_bytes(payload[8:12], "big")

    async def test_cbor_answers_with_error_when_device_unreachable(self):
        ble = FakeBLE(connect_behaviour=DBusError("org.bluez.Error.Failed", "nope"))
        hid = make_hid(ble, connect_attempts=1)
        hid.process_process_hid_message(hid_report(0x42, CTAPHID_CMD.CBOR, b"\x04"), None)
        await settle(0.01)
        self.assertEqual(errors(hid), [(0x42, CTAP_STATUS.CTAP1_ERR_OTHER)])

    async def test_cbor_times_out_when_device_stays_silent(self):
        ble = FakeBLE()
        hid = make_hid(ble, response_timeout=0.05)
        channel = await self._init(hid)
        hid.process_process_hid_message(hid_report(channel, CTAPHID_CMD.CBOR, b"\x04"), None)
        await settle(0.15)
        self.assertEqual(ble.sent[0], (CTAPBLE_CMD.MSG, b"\x04"))
        self.assertEqual(errors(hid), [(channel, CTAP_STATUS.CTAP1_ERR_TIMEOUT)])
        # The pending BLE transaction is cancelled on the device as well.
        self.assertIn((CTAPBLE_CMD.CANCEL, b""), ble.sent)
        hid.stop()

    async def test_keepalives_hold_off_the_timeout(self):
        ble = FakeBLE()
        hid = make_hid(ble, response_timeout=0.08)
        channel = await self._init(hid)
        hid.process_process_hid_message(hid_report(channel, CTAPHID_CMD.CBOR, b"\x04"), None)
        await settle(0.01)
        for _ in range(8):  # 0.24s of keepalives, each well inside the window
            ble.deliver(ble_frame(CTAPBLE_CMD.KEEPALIVE, b"\x02"))
            await settle(0.03)
        ble.deliver(ble_frame(CTAPBLE_CMD.MSG, b"\x00\xa0"))
        await settle(0.12)
        self.assertEqual(errors(hid), [])
        cbor = [p for ch, cmd, p in map(parse_hid, hid.device.sent) if cmd == CTAPHID_CMD.CBOR]
        self.assertEqual(cbor, [b"\x00\xa0"])
        keepalives = [cmd for _, cmd, _ in map(parse_hid, hid.device.sent) if cmd == CTAPHID_CMD.KEEPALIVE]
        self.assertEqual(len(keepalives), 8)
        self.assertIsNone(hid.response_watchdog)
        hid.stop()

    async def test_response_from_device_is_forwarded_and_stops_watchdog(self):
        ble = FakeBLE()
        ble.responses = [ble_frame(CTAPBLE_CMD.MSG, b"\x00\xa1\x01\x81")]
        hid = make_hid(ble, response_timeout=0.05)
        channel = await self._init(hid)
        hid.process_process_hid_message(hid_report(channel, CTAPHID_CMD.CBOR, b"\x04"), None)
        await settle(0.15)
        self.assertEqual(errors(hid), [])
        channel_out, cmd, payload = parse_hid(hid.device.sent[-1])
        self.assertEqual((channel_out, cmd, payload), (channel, CTAPHID_CMD.CBOR, b"\x00\xa1\x01\x81"))
        hid.stop()

    async def test_unsupported_hid_command_is_rejected(self):
        ble = FakeBLE()
        hid = make_hid(ble)
        channel = await self._init(hid)
        hid.process_process_hid_message(hid_report(channel, CTAPHID_CMD.WINK), None)
        await settle(0.01)
        self.assertEqual(errors(hid), [(channel, CTAP_STATUS.CTAP1_ERR_INVALID_COMMAND)])
        hid.stop()


if __name__ == "__main__":
    unittest.main()
