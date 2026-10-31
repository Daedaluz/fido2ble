"""Regression tests for issue #33: no endless reconnects to a gone device."""

import asyncio
import unittest
from unittest import mock

from fido2ble.CMD import CTAPHID_CMD
from fido2ble.CTAPBLEDevice import CTAPBLEDevice, DeviceUnavailableError
from fido2ble.CTAPHIDDevice import CTAPHIDDevice
from fido2ble.vendored.dbus_fast import DBusError

from tests.fakes import FakeBLE, hid_report, patch_uhid, DEVICE_PATH


class EnsureConnectedTests(unittest.IsolatedAsyncioTestCase):
    async def test_gives_up_after_configured_attempts(self):
        ble = FakeBLE(connect_behaviour=DBusError("org.bluez.Error.Failed", "nope"))
        with patch_uhid(), mock.patch("asyncio.sleep", new=mock.AsyncMock()):
            hid = CTAPHIDDevice(ble, connect_attempts=3)
            self.assertIsNone(await hid.ensure_connected())
        self.assertEqual(ble.connect_calls, 3)

    async def test_stops_immediately_when_device_is_gone(self):
        ble = FakeBLE(connect_behaviour=DeviceUnavailableError(DEVICE_PATH))
        with patch_uhid():
            hid = CTAPHIDDevice(ble, connect_attempts=5)
            self.assertIsNone(await hid.ensure_connected())
        self.assertEqual(ble.connect_calls, 1)

    async def test_returns_device_once_connected(self):
        ble = FakeBLE()
        with patch_uhid():
            hid = CTAPHIDDevice(ble)
            self.assertIs(await hid.ensure_connected(), ble)
        self.assertEqual(ble.connect_calls, 1)


class StopTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_cancels_pending_request_and_destroys_uhid(self):
        ble = FakeBLE(connect_behaviour="hang")
        with patch_uhid():
            hid = CTAPHIDDevice(ble)
            hid.process_process_hid_message(hid_report(0x1234, CTAPHID_CMD.CBOR, b"\x04"), None)
            await asyncio.sleep(0)  # let the request task start and block in connect
            self.assertEqual(len(hid.active_tasks), 1)
            task = hid.active_tasks[0]
            self.assertFalse(task.done())

            hid.stop()
            await asyncio.sleep(0)

            self.assertTrue(task.cancelled())
            self.assertTrue(hid.device.destroyed)
            self.assertFalse(ble.available)
            hid.stop()  # idempotent

    async def test_state_is_per_instance(self):
        with patch_uhid():
            a = CTAPHIDDevice(FakeBLE(device_id="/org/bluez/hci0/dev_AA_AA_AA_AA_AA_AA"))
            b = CTAPHIDDevice(FakeBLE(device_id="/org/bluez/hci0/dev_BB_BB_BB_BB_BB_BB"))
        a.channels_to_state[1] = b"x"
        a.active_tasks.append(object())
        self.assertEqual(b.channels_to_state, {})
        self.assertEqual(b.active_tasks, [])


class _Device1:
    def __init__(self, error=None):
        self.error = error
        self.calls = 0

    async def call_connect(self):
        self.calls += 1
        if self.error:
            raise self.error


class _Proxy:
    def __init__(self, props=None):
        self.props = props or mock.Mock()

    def get_interface(self, name):
        return self.props


def _ble_device(error=None):
    device1 = _Device1(error)
    dev = CTAPBLEDevice(_Proxy(), device1, DEVICE_PATH, True, "/cp", "/cpl", "/status")
    return dev, device1


class CTAPBLEDeviceReconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_method_marks_device_unavailable(self):
        error = DBusError(
            "org.freedesktop.DBus.Error.UnknownMethod",
            'Method "Connect" with signature "" on interface "org.bluez.Device1" doesn\'t exist',
        )
        dev, device1 = _ble_device(error)
        with self.assertRaises(DeviceUnavailableError):
            await dev.reconnect()
        self.assertFalse(dev.available)
        # Further attempts fail fast without touching D-Bus again.
        with self.assertRaises(DeviceUnavailableError):
            await dev.reconnect()
        with self.assertRaises(DeviceUnavailableError):
            await dev.connect(lambda payload: None)
        self.assertEqual(device1.calls, 1)

    async def test_transient_error_propagates_and_keeps_device(self):
        dev, device1 = _ble_device(DBusError("org.bluez.Error.Failed", "le-connection-abort-by-local"))
        with self.assertRaises(DBusError):
            await dev.reconnect()
        self.assertTrue(dev.available)
        self.assertFalse(dev.connected)

    async def test_successful_reconnect_registers_notify_handler_once(self):
        dev, device1 = _ble_device()
        listen = mock.Mock()
        dev.fido_status_notify_listen = listen
        dev.fido_status = mock.Mock(call_start_notify=mock.AsyncMock())
        dev.handler = object()
        await dev.reconnect()
        dev.connected = False  # link dropped without an explicit disconnect()
        await dev.reconnect()
        self.assertTrue(dev.connected)
        self.assertEqual(listen.on_properties_changed.call_count, 1)

    async def test_disconnect_without_prior_connection_is_safe(self):
        dev, _ = _ble_device()
        await dev.disconnect()


if __name__ == "__main__":
    unittest.main()
