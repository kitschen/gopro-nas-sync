"""Per-model capabilities and quirks, matched on the model name the camera reports."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelInfo:
    family: str
    ble_wake: bool = True  # camera can be woken over BLE from standby
    min_firmware: str | None = None
    pairing_hint: str = "Preferences > Wireless Connections > Connect Device > GoPro Quik"
    notes: str = ""


DEFAULT = ModelInfo(family="Open GoPro camera")

# Ordered: first prefix match wins, so more specific names come first.
MODELS: list[tuple[str, ModelInfo]] = [
    ("HERO13", ModelInfo(family="HERO13")),
    ("HERO12", ModelInfo(family="HERO12")),
    ("HERO11 Black Mini", ModelInfo(
        family="HERO11 Mini",
        pairing_hint="Swipe down > Preferences > Connections > Connect Device > GoPro Quik",
    )),
    ("HERO11", ModelInfo(family="HERO11")),
    ("HERO10", ModelInfo(family="HERO10")),
    ("HERO9", ModelInfo(
        family="HERO9",
        min_firmware="1.60",
        pairing_hint="Swipe down > Connections > Connect Device > GoPro Quik",
        notes="Requires firmware 1.60 or later for Open GoPro support.",
    )),
    ("MAX2", ModelInfo(family="MAX2")),
]

PAIRING_INSTRUCTIONS = (
    "Put the camera into pairing mode. On most models: "
    "Preferences > Wireless Connections > Connect Device > GoPro Quik. "
    "On HERO9 / HERO11 Mini: swipe down > Connections > Connect Device > GoPro Quik."
)


def lookup(model_name: str) -> ModelInfo:
    for prefix, info in MODELS:
        if model_name.startswith(prefix):
            return info
    return DEFAULT
