"""
Hardware peripheral management package for Arduino UNO Q.

Contains modules for interfacing with hardware devices:
- CameraManager (Logitech Brio 105 USB camera / V4L2)
- MicrophoneManager (ALSA audio input / Whisper STT)
- AudioPlayer (ALSA audio playback via 3.5mm jack / Piper TTS)
- LocationRegistry (GPS coordinates & Haversine distance)

These four names are resolved lazily (PEP 562) rather than imported here
eagerly, which breaks an import cycle: config.py imports hw.device_discovery,
importing anything under `hw` runs this initializer, and the three peripheral
modules each read discovery-derived constants (PLAYBACK_DEVICE,
CAMERA_DEVICE_INDEX, MIC_DEVICE, MIC_SAMPLE_RATE) from config at import time —
constants config cannot have defined yet, because it is still blocked on the
discovery call. Eager imports here therefore made discover_all() raise
ImportError every single startup, silently falling back to the hardcoded
device indices.

device_discovery itself imports nothing from config, so with this module lazy
the cycle never forms. `from hw import AudioPlayer` still works as before; it
just loads the submodule on first attribute access.
"""

import importlib

_LAZY_ATTRS = {
    "AudioPlayer": "hw.audio_playback_module",
    "CameraManager": "hw.camera_module",
    "LocationRegistry": "hw.location_module",
    "MicrophoneManager": "hw.microphone_module",
}

__all__ = list(_LAZY_ATTRS)


def __getattr__(name: str):
    """Imports the module owning `name` on first access and caches the result."""
    module_name = _LAZY_ATTRS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(module_name), name)
    globals()[name] = value  # cache so __getattr__ is not consulted again
    return value


def __dir__():
    return sorted(list(globals()) + __all__)
