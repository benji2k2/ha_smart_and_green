"""Protocol tests: frames, encryption, advertisements, acknowledgements.

The expected byte sequences here are not invented — they were captured from a
real cube during development and are known to make the lamp do the right thing.
Their payloads have been re-encrypted under a test key, so the structure is
genuine while the secret is not anyone's.
"""
from __future__ import annotations

import conftest

sg = conftest.load()
lmp = sg.lmp

# Key/nonce pairs for the tests. Neither belongs to a real installation: the
# captured frames below were recorded from a device, then decrypted and
# re-encrypted under CAPTURE_KEY, so they still exercise the real protocol
# structure — header layout, checksums, TLV parsing — without carrying anyone's
# mesh secret into a public repository.
KEY = bytes(range(16))
NONCE = bytes(range(16, 32))
CAPTURE_KEY = bytes(range(0x10, 0x20))
CAPTURE_NONCE = bytes(range(0x20, 0x30))


def test_frame_layout():
    """Header, address and payload sit where the protocol says they do."""
    payload = lmp.build_color_payload(0, True, 0, 100, 100)
    frame = lmp.build_frame("13:40", payload, KEY, NONCE, cmd_id=7, want_ack=False)

    assert len(frame) == 20, "a short frame is always 20 bytes"
    assert frame[0] == 0x12, "msg_type NO_ACK, private encryption, LMP short frame"
    assert frame[1] == 7, "command id"
    assert frame[2:4] == b"\x40\x13", "address is little-endian: 13:40 -> 40 13"


def test_ack_flag_changes_only_the_header():
    """Requesting an acknowledgement must not disturb payload or checksum."""
    payload = lmp.build_color_payload(0, True, 0, 100, 100)
    without = lmp.build_frame("13:40", payload, KEY, NONCE, cmd_id=1, want_ack=False)
    with_ack = lmp.build_frame("13:40", payload, KEY, NONCE, cmd_id=1, want_ack=True)

    assert with_ack[0] == 0x32 and without[0] == 0x12
    assert with_ack[1:] == without[1:], "only the header byte may differ"


def test_group_frame_targets_the_broadcast_address():
    payload = lmp.build_color_payload(0, True, 0, 100, 100,
                                      is_group=True, class_id=19)
    frame = lmp.build_frame("FF:FF", payload, KEY, NONCE, cmd_id=1)
    assert frame[2:4] == b"\xff\xff"
    assert payload[1] == 0x56, "group commands use GROUP_DATA_SET, not DEVICE_DATA_SET"
    assert payload[2] == 19, "group commands carry the class id, not a device index"


def test_warm_white_is_a_warm_hue_not_the_white_channel():
    """Colour temperature maps onto the colour path, as the app does it.

    The device derives white from saturation, so a low colour temperature has
    to come out as a warm hue at high saturation. 2000K matching the app's
    stored warm-white default is the check that the port is faithful.
    """
    hue, sat = lmp.temp_to_hs(2000)
    assert 25 <= hue <= 35, hue
    assert sat > 85, sat

    cold_hue, cold_sat = lmp.temp_to_hs(6500)
    assert cold_sat < 15, "cold white is nearly unsaturated"


def test_white_is_derived_from_saturation():
    """Full saturation means no white; zero saturation means full white."""
    saturated = lmp.build_color_payload(0, True, 0, 100, 100)
    unsaturated = lmp.build_color_payload(0, True, 0, 0, 100)
    assert saturated[10] == 0
    assert unsaturated[10] == 100


def test_brightness_scales_the_value_byte():
    full = lmp.build_color_payload(0, True, 120, 100, 100)
    half = lmp.build_color_payload(0, True, 120, 100, 50)
    assert half[4] < full[4]
    assert lmp.build_color_payload(0, True, 120, 100, 0)[4] == 0


def test_parse_ack_reads_real_captures():
    """Acknowledgements captured from the device decode to the right codes."""
    key, nonce = CAPTURE_KEY, CAPTURE_NONCE
    captures = {
        "50010000ae1f557e627ed322542e3355c3b86864": (1, 0),
        "50020000ae1f557e627ed322542e3355c3b86864": (2, 0),
        "50010000af1f557f627ed322542e3355c3b86864": (1, 1),
        "50050000ad1f557d627ed322542e3355c3b86864": (5, 3),
    }
    for raw, expected in captures.items():
        assert lmp.parse_ack(bytes.fromhex(raw.replace(" ", "")), key, nonce) == expected

    assert lmp.ACK_ERRORS[0] == "SUCCESS"
    assert lmp.ACK_ERRORS[1] == "NOT_SUPPORTED"
    assert lmp.ACK_ERRORS[3] == "INVALID_PARAMETER"


def test_parse_ack_rejects_non_acknowledgements():
    key, nonce = CAPTURE_KEY, CAPTURE_NONCE
    assert lmp.parse_ack(b"\x00" * 5, key, nonce) is None, "too short"
    assert lmp.parse_ack(b"\x00" * 20, key, nonce) is None, "checksum mismatch"
    # A status frame is not an acknowledgement.
    status = "700000008013447f62122d91c2b6a4de3c47979a"
    assert lmp.parse_ack(bytes.fromhex(status.replace(" ", "")), key, nonce) is None


def test_decode_advertisement_against_real_captures():
    """Advertisements recorded from both cubes decode with a valid checksum."""
    key, nonce = CAPTURE_KEY, CAPTURE_NONCE

    small = lmp.decode_advertisement(
        bytes.fromhex("71b84013ffffd8001e16573e711e00b365d1caaa3e476864"), key, nonce)
    assert small["src"] == "13:40"
    assert small["registered"] is True
    assert small["connected"] is False
    assert "status_network" in small["fields"]

    # Same cube while connected: only the status bit differs.
    connected = lmp.decode_advertisement(
        bytes.fromhex("71b94013ffffe0001e16573e711e00b365d1caaa3e476864"), key, nonce)
    assert connected["connected"] is True

    large = lmp.decode_advertisement(
        bytes.fromhex("71b8e041ffffa3002716579e2337e2e350d1afaa3e476864"), key, nonce)
    assert large["src"] == "41:E0"


def test_decode_advertisement_rejects_bad_input():
    key, nonce = CAPTURE_KEY, CAPTURE_NONCE
    assert lmp.decode_advertisement(b"\x00" * 10, key, nonce) is None, "too short"

    corrupt = bytearray(
        bytes.fromhex("71b84013ffffd8001e16573e711e00b365d1caaa3e476864"))
    corrupt[9] ^= 0xFF
    assert lmp.decode_advertisement(bytes(corrupt), key, nonce) is None, "checksum"
