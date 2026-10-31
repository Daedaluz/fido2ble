"""Test doubles for the UHID and BLE sides of the proxy.

They let the HID <-> BLE translation logic run without /dev/uhid, BlueZ or a
real authenticator.
"""

import asyncio
import struct
from unittest import mock

from fido2ble.CTAPBLEDevice import DeviceUnavailableError
from fido2ble.vendored.dbus_fast import DBusError

CTAPHID_BROADCAST_CHANNEL = 0xFFFFFFFF
DEVICE_PATH = "/org/bluez/hci0/dev_D1_9E_0C_99_0D_06"


class FakeUHID:
    """Stands in for uhid.UHIDDevice and records what the host would receive."""

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.sent: list[bytes] = []
        self.destroyed = False
        self.receive_open = None
        self.receive_close = None
        self.receive_output = None

    def send_input(self, data):
        self.sent.append(bytes(data))

    def destroy(self):
        self.destroyed = True

    async def wait_for_start_asyncio(self, delay=0.05):
        return


def patch_uhid():
    return mock.patch("fido2ble.CTAPHIDDevice.uhid.UHIDDevice", FakeUHID)


class FakeBLE:
    """Scriptable stand-in for CTAPBLEDevice.

    ``connect_behaviour`` controls what connect()/reconnect() do:
    "ok" connects, "hang" never returns, an exception instance is raised.
    ``responses`` is a list of raw BLE frames delivered to the HID side after
    each send_ble_message(); an empty list means the device stays silent.
    """

    def __init__(self, device_id=DEVICE_PATH, connect_behaviour="ok"):
        self.device_id = device_id
        self.connect_behaviour = connect_behaviour
        self.available = True
        self.connected = False
        self.timeout = 3000
        self.handler = None
        self.connect_calls = 0
        self.sent: list[tuple[int, bytes]] = []
        self.responses: list[bytes] = []
        self.disconnects = 0

    async def _do_connect(self):
        self.connect_calls += 1
        if not self.available:
            raise DeviceUnavailableError(self.device_id)
        behaviour = self.connect_behaviour
        if behaviour == "hang":
            await asyncio.Event().wait()
        if isinstance(behaviour, BaseException):
            raise behaviour
        self.connected = True

    async def connect(self, handler):
        self.handler = handler
        await self._do_connect()
        return self

    async def reconnect(self):
        await self._do_connect()

    def get_connected_ble(self):
        return self if self.connected else None

    def keep_alive(self):
        self.timeout = 3000

    def mark_unavailable(self):
        self.available = False
        self.connected = False

    async def disconnect(self):
        self.disconnects += 1
        self.connected = False

    async def send_ble_message(self, command, payload):
        if not self.connected:
            raise DBusError("org.bluez.Error.NotConnected", "Not connected")
        self.sent.append((int(command), bytes(payload)))
        for frame in self.responses:
            self.handler(frame)

    def deliver(self, frame: bytes):
        """Push a raw BLE frame to the proxy as if it came via GATT notify."""
        self.handler(frame)


def ble_frame(command: int, payload: bytes = b"") -> bytes:
    return struct.pack(">BH", command, len(payload)) + payload


def hid_report(channel: int, command: int, payload: bytes = b"") -> list[int]:
    """Build one 64-byte CTAPHID initialization packet as uhid delivers it.

    uhid prefixes the report with the report id byte, hence the leading 0.
    """
    packet = struct.pack(">IBH", channel, 0x80 | command, len(payload)) + payload
    packet += b"\0" * (64 - len(packet))
    return [0] + list(packet)


def parse_hid(packet: bytes):
    channel, cmd, length = struct.unpack(">IBH", packet[:7])
    return channel, cmd & 0x7F, packet[7 : 7 + length]
