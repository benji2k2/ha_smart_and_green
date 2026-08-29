"""A simulated cube, standing in for a real lamp over BLE.

It speaks enough of the protocol to exercise the integration's write path: it
accepts GATT writes, decrypts the frame, and answers with a properly encrypted
``STATUS_ACK`` carrying the same command id — exactly what a real cube does when
a command is sent as ``CMD_WITH_ACK`` (verified against the device).

It can also be told to misbehave: stay silent, report an error code, or fail
the write outright.
"""
from __future__ import annotations

CHAR_UUID = "00005002-0000-1000-8000-00805f9b34fb"
_OP_STATUS_ACK = 0x80


class FakeCharacteristic:
    uuid = CHAR_UUID
    properties = ("write", "write-without-response", "notify")


class FakeServices:
    def get_characteristic(self, _uuid):
        return FakeCharacteristic()


class FakeCube:
    """A cube that answers writes with an acknowledgement.

    Args:
        keystream: the 16-byte AES-ECB keystream the integration uses.
        code: acknowledgement code to answer with (0 = success).
        answer: if False, stay silent so a timeout can be exercised.
        write_error: raise this on every write.
        notify_error: raise this when notifications are subscribed.
    """

    def __init__(self, keystream: bytes, code: int = 0, answer: bool = True,
                 write_error: Exception | None = None,
                 notify_error: Exception | None = None) -> None:
        self._keystream = keystream
        self.code = code
        self.answer = answer
        self.write_error = write_error
        self.notify_error = notify_error

        self.is_connected = True
        self.services = FakeServices()
        self.writes: list[bytes] = []
        self.disconnected = False
        self._callback = None

    # -- BleakClient surface used by the integration ----------------------

    async def start_notify(self, _uuid, callback) -> None:
        if self.notify_error is not None:
            raise self.notify_error
        self._callback = callback

    async def write_gatt_char(self, _char, frame: bytes, response: bool = False) -> None:
        if self.write_error is not None:
            raise self.write_error
        self.writes.append(bytes(frame))
        if self.answer and self._callback is not None:
            self._callback(None, bytearray(self._ack_for(frame)))

    async def disconnect(self) -> None:
        self.is_connected = False
        self.disconnected = True

    # -- helpers ----------------------------------------------------------

    def _ack_for(self, frame: bytes) -> bytes:
        """Build the acknowledgement a real cube sends for this frame."""
        cmd_id = frame[1]
        body = [2, _OP_STATUS_ACK, self.code] + [0] * 12
        crc = 0
        for byte in body:
            crc ^= byte
        payload = bytes(a ^ b for a, b in zip([crc] + body, self._keystream))
        # Header as captured from the device: CMD_ACK, private encryption,
        # local frame, then the command id and two zero address bytes.
        return bytes([0x50, cmd_id, 0x00, 0x00]) + payload

    def decoded_writes(self, parse_ack) -> list[int]:
        """Command ids of everything written, for assertions."""
        return [frame[1] for frame in self.writes]
