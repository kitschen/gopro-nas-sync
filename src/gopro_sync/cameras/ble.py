"""BLE controller with a pairing step that works for an unprivileged service user.

The SDK drives an interactive bluetoothctl and waits for the root prompt ("#"); as a normal user
the prompt ends in ">" and pairing times out. Non-interactive bluetoothctl commands avoid
prompt parsing entirely.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform

import bleak
from open_gopro.network.ble.adapters.bleak_wrapper import BleakWrapperController

logger = logging.getLogger(__name__)

PAIR_TIMEOUT = 30


async def bluetoothctl(*args: str, timeout: float = PAIR_TIMEOUT + 10) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "bluetoothctl", *args,
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, "LC_ALL": "C"},
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, "timeout"
    return proc.returncode or 0, out.decode(errors="replace")


async def disconnect_stale_gopros() -> None:
    """A connection left over from a crashed session stops the camera advertising, so scans fail."""
    if platform.system() != "Linux":
        return
    _, out = await bluetoothctl("devices", "Connected", timeout=10)
    for line in out.splitlines():
        parts = line.split(maxsplit=2)
        if len(parts) == 3 and parts[0] == "Device" and parts[2].startswith("GoPro"):
            logger.info("Disconnecting stale BLE connection to %s", parts[2])
            await bluetoothctl("disconnect", parts[1], timeout=15)


async def is_paired(address: str) -> bool:
    _, out = await bluetoothctl("devices", "Paired", timeout=10)
    return any(line.split()[1:2] == [address] for line in out.splitlines() if line.startswith("Device"))


class NonInteractiveBleController(BleakWrapperController):
    async def pair(self, handle: bleak.BleakClient) -> None:
        if platform.system() != "Linux":
            await super().pair(handle)
            return
        address = handle.address
        if await is_paired(address):
            logger.info("Camera %s is already paired", address)
        else:
            logger.info("Pairing with %s", address)
            _, out = await bluetoothctl(
                "--agent", "NoInputNoOutput", "--timeout", str(PAIR_TIMEOUT), "pair", address
            )
            if "Pairing successful" not in out and "AlreadyExists" not in out and not await is_paired(address):
                raise RuntimeError(f"Bluetooth pairing with {address} failed: {out.strip()[-300:]}")
            logger.info("Paired with %s", address)
        _, out = await bluetoothctl("trust", address, timeout=10)
        if "trust succeeded" not in out:
            logger.warning("Could not mark %s as trusted: %s", address, out.strip()[-200:])
