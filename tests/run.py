#!/usr/bin/env python3
"""Run the test suite without pytest.

Home Assistant is not installed here (conftest stubs the handful of names the
component uses), so a plain runner keeps the suite dependency-free apart from
``cryptography``, which the component itself needs.

    python3 tests/run.py
"""
from __future__ import annotations

import asyncio
import inspect
import pathlib
import sys
import traceback

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def main() -> int:
    modules = ["test_protocol", "test_light"]
    passed: list[str] = []
    failed: list[tuple[str, str]] = []

    for name in modules:
        module = __import__(name)
        tests = [(n, f) for n, f in vars(module).items() if n.startswith("test_")]
        print(f"\n{name}  ({len(tests)} tests)")
        for test_name, func in tests:
            try:
                if inspect.iscoroutinefunction(func):
                    asyncio.run(func())
                else:
                    func()
            except Exception:
                failed.append((f"{name}.{test_name}", traceback.format_exc()))
                print(f"  FAIL  {test_name}")
            else:
                passed.append(test_name)
                print(f"  ok    {test_name}")

    print(f"\n{len(passed)} passed, {len(failed)} failed")
    for name, tb in failed:
        print(f"\n--- {name}\n{tb}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
