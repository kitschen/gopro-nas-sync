"""NetworkManager Wi-Fi controller for the camera access point.

Replaces the SDK's default driver, which parses localized nmcli output (en_US only) and creates a
regular connection that may take over the default route. This one uses terse, locale-independent
nmcli output and a dedicated profile that never becomes the default route, so NAS traffic stays
on Ethernet while the Pi is connected to the camera.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time

from open_gopro.network.wifi import SsidState, WifiController

logger = logging.getLogger(__name__)

PROFILE_NAME = "gopro-sync-camera"


def nmcli(*args: str, check: bool = True, timeout: float = 60) -> str:
    env = {**os.environ, "LC_ALL": "C"}
    proc = subprocess.run(
        ["nmcli", *args], capture_output=True, text=True, timeout=timeout, env=env, check=False
    )
    if check and proc.returncode != 0:
        # Never log the arguments: they can contain the camera Wi-Fi password
        raise RuntimeError(f"nmcli {args[0]} {args[1] if len(args) > 1 else ''} failed: {proc.stderr.strip()}")
    return proc.stdout


def split_terse(line: str) -> list[str]:
    """Split one line of `nmcli -t` output, honouring backslash escapes of ':'."""
    fields, current, escaped = [], [], False
    for ch in line:
        if escaped:
            current.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(ch)
    fields.append("".join(current))
    return fields


class NmcliProfileWifi(WifiController):
    def __init__(self, interface: str | None = None, password: str | None = None) -> None:
        WifiController.__init__(self, interface, password)
        self._ssid: str | None = None
        self.interface = interface  # validates / auto-detects

    async def connect(self, ssid: str, password: str, timeout: float = 15) -> bool:
        return await asyncio.to_thread(self._connect, ssid, password, timeout)

    def _connect(self, ssid: str, password: str, timeout: float) -> bool:
        self._delete_profile()
        deadline = time.monotonic() + max(timeout, 30)
        logger.info("Looking for the camera's Wi-Fi network '%s' on %s...", ssid, self.interface)
        while time.monotonic() < deadline:
            out = nmcli("-t", "-f", "SSID", "device", "wifi", "list", "ifname", self.interface, "--rescan", "yes",
                        check=False)
            if ssid in (split_terse(line)[0] for line in out.splitlines()):
                break
            time.sleep(2)
        else:
            logger.warning("The camera's Wi-Fi network '%s' was not found on %s", ssid, self.interface)
            return False
        logger.info("Found '%s', joining...", ssid)

        nmcli(
            "connection", "add", "type", "wifi", "ifname", self.interface, "con-name", PROFILE_NAME,
            "ssid", ssid, "connection.autoconnect", "no",
            "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password,
            "ipv4.method", "auto", "ipv4.never-default", "yes", "ipv4.ignore-auto-dns", "yes",
            "ipv6.method", "disabled",
        )
        try:
            nmcli("--wait", str(int(max(timeout, 30))), "connection", "up", PROFILE_NAME, timeout=max(timeout, 30) + 15)
        except RuntimeError as e:
            logger.warning("Could not join the camera's Wi-Fi '%s': %s", ssid, e)
            self._delete_profile()
            return False
        self._ssid = ssid
        logger.info("Joined the camera's Wi-Fi '%s'", ssid)
        return True

    async def disconnect(self) -> bool:
        await asyncio.to_thread(self._delete_profile)
        self._ssid = None
        return True

    def _delete_profile(self) -> None:
        names = [split_terse(line)[0] for line in nmcli("-t", "-f", "NAME", "connection", "show", check=False).splitlines()]
        if PROFILE_NAME in names:
            nmcli("connection", "delete", PROFILE_NAME, check=False)

    def current(self) -> tuple[str | None, SsidState]:
        out = nmcli("-t", "-f", "GENERAL.CONNECTION,GENERAL.STATE", "device", "show", self.interface, check=False)
        values = dict(line.split(":", 1) for line in out.splitlines() if ":" in line)
        if values.get("GENERAL.CONNECTION") == PROFILE_NAME and values.get("GENERAL.STATE", "").startswith("100"):
            return self._ssid, SsidState.CONNECTED
        return None, SsidState.DISCONNECTED

    def available_interfaces(self) -> list[str]:
        out = nmcli("-t", "-f", "DEVICE,TYPE", "device", check=False)
        return [f[0] for f in map(split_terse, out.splitlines()) if len(f) > 1 and f[1] == "wifi"]

    def power(self, power: bool) -> bool:
        nmcli("radio", "wifi", "on" if power else "off", check=False)
        return True

    @property
    def is_on(self) -> bool:
        return nmcli("radio", "wifi", check=False).strip() == "enabled"
