# Tests

A dependency-light suite: Home Assistant is not installed, so `conftest.py`
stubs the handful of HA names the component uses and loads the real integration
modules underneath. `cryptography` is required — the component itself needs it.

```bash
python3 tests/run.py
```

`fake_cube.py` simulates a cube over BLE. It decrypts what is written to it and
replies with a properly encrypted `STATUS_ACK` carrying the same command id,
which is what a real cube does for a command sent as `CMD_WITH_ACK`. It can also
be told to stay silent, report an error code, or fail the write, so the failure
paths get exercised too.

The byte sequences in `test_protocol.py` are captures from a real cube taken
during development, not invented values — they are known to make the lamp
behave. Testing against them keeps refactors honest.
